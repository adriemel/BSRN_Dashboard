"""Run with python -m unittest discover -s tests -p performance_regression_checks.py."""

from __future__ import annotations

import configparser
import os
import re
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import bsrn_data_exports as exports
from scripts import bsrn_download_check as download
from scripts import bsrn_qc_continue as qc


class DownloadTests(unittest.TestCase):
    def test_bounded_overlap_order_duplicates_and_failure(self):
        jobs = [download.Job("TAT", 2026, month) for month in range(1, 6)]
        second_finished = threading.Event()
        both_started = threading.Barrier(2)
        lock = threading.Lock()
        active = maximum = 0
        called = []

        def transfer(job, cfg, directory):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                called.append(job)
            try:
                if job in jobs[:2]:
                    both_started.wait(5)
                if job == jobs[0]:
                    self.assertTrue(second_finished.wait(5), "Downloads did not overlap")
                if job == jobs[1]:
                    second_finished.set()
                    raise OSError("Simulated FTP failure")
                return directory / job.gz_name
            finally:
                with lock:
                    active -= 1

        with patch.object(download, "download_job", side_effect=transfer):
            results = list(download.download_jobs(jobs + [jobs[0]], configparser.ConfigParser(), Path("unused"), 2))
        self.assertEqual([job for job, _, _ in results], jobs)
        self.assertEqual(len(called), len(jobs))
        self.assertEqual(maximum, 2)
        self.assertIsInstance(results[1][2], OSError)
        self.assertTrue(all(error is None for _, _, error in results[2:]))

    def test_serial_mode_and_invalid_limit(self):
        jobs = [download.Job("TAT", 2026, month) for month in (1, 2)]
        with patch.object(download, "download_job", side_effect=[OSError("offline"), Path("ok.gz")]) as transfer:
            results = list(download.download_jobs(jobs, configparser.ConfigParser(), Path("unused"), 1))
        self.assertEqual([call.args[0] for call in transfer.call_args_list], jobs)
        self.assertIsInstance(results[0][2], OSError)
        self.assertEqual(results[1][1], Path("ok.gz"))
        for workers in (0, 5):
            with self.assertRaises(download.WorkflowError):
                list(download.download_jobs(jobs, configparser.ConfigParser(), Path("unused"), workers))


class ExportCacheTests(unittest.TestCase):
    def test_reuse_and_invalidation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dat = root / "tat0426.dat"
            dat.write_bytes(b"original")
            status = download.JobStatus(job="TAT_2026-04", dat_path=str(dat))
            output = root / "data_exports/TAT_2026-04_LR0100_radiation.csv"
            warnings = ["A preserved warning"]

            def generate(*args):
                output.parent.mkdir(exist_ok=True)
                output.write_bytes(dat.read_bytes())
                return [output], warnings

            with patch.object(exports, "_generate_data_exports_for_status", side_effect=generate) as generator:
                self.assertEqual(exports.generate_data_exports_for_status(status, root), ([output], warnings))
                timestamp = output.stat().st_mtime_ns
                self.assertEqual(exports.generate_data_exports_for_status(status, root), ([output], warnings))
                self.assertEqual(generator.call_count, 1)
                self.assertEqual(output.stat().st_mtime_ns, timestamp)

                output.write_bytes(b"tampered")
                exports.generate_data_exports_for_status(status, root)
                self.assertEqual(output.read_bytes(), b"original")
                output.unlink()
                exports.generate_data_exports_for_status(status, root)
                self.assertTrue(output.exists())

                # Same byte length and restored mtime must still invalidate.
                before = dat.stat()
                dat.write_bytes(b"modified")
                os.utime(dat, ns=(before.st_atime_ns, before.st_mtime_ns))
                exports.generate_data_exports_for_status(status, root)
                self.assertEqual(output.read_bytes(), b"modified")
                self.assertEqual(generator.call_count, 4)

                manifest = next((root / "data_exports/.cache").glob("*.json"))
                manifest.write_text("broken JSON")
                exports.generate_data_exports_for_status(status, root)
                self.assertEqual(generator.call_count, 5)

                with patch.object(exports, "_export_cache_key", return_value="new-parser-version"):
                    exports.generate_data_exports_for_status(status, root)
                self.assertEqual(generator.call_count, 6)

                with patch.object(exports.os, "replace", side_effect=OSError("read only")):
                    self.assertEqual(exports.generate_data_exports_for_status(status, root), ([output], warnings))


class ParsedDataTests(unittest.TestCase):
    def test_fixture_artifacts_match_independent_generation(self):
        import bsrn_qc
        dat = PROJECT_ROOT / "input/tat0426.dat"
        status = download.JobStatus(job="TAT_2026-04", dat_path=str(dat))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            qc_root = root / "qc"
            qc_root.mkdir()
            independent = [qc.generate_interactive_report(dat, qc_root)]
            independent.extend(qc.generate_swd_sumsw_plots(dat, qc_root))
            independent.extend(qc.generate_logical_record_artifacts(dat, qc_root))
            csvs, expected_warnings = exports._generate_data_exports_for_status(status, root)
            independent.extend(csvs)

            def contents(path):
                data = path.read_bytes()
                if path.suffix == ".html":
                    data = re.sub(rb'(>Generated</div><div[^>]*>)[^<]*', rb'\1TIMESTAMP', data)
                    data = re.sub(rb'Generated \d{4}-\d{2}-\d{2} \d{2}:\d{2}', b'Generated TIMESTAMP', data)
                return data

            expected = {path: contents(path) for path in independent}
            with patch.object(bsrn_qc, "parse_dat_file", wraps=bsrn_qc.parse_dat_file) as parse:
                _, actual, warnings = qc.run_qc_for_dat(dat, qc_root)
                self.assertEqual(parse.call_count, 1)
            self.assertEqual(warnings, [])
            csvs, export_warnings = exports.generate_data_exports_for_status(status, root)
            actual.extend(csvs)
            self.assertEqual(export_warnings, expected_warnings)
            self.assertEqual(set(actual), set(expected))
            self.assertEqual(len(actual), 9)
            for path in actual:
                self.assertEqual(contents(path), expected[path], str(path))
            with patch.object(exports, "_generate_data_exports_for_status", side_effect=AssertionError("Cache miss")):
                self.assertEqual(exports.generate_data_exports_for_status(status, root), (csvs, expected_warnings))

    def test_consumers_receive_independent_data_and_parse_once(self):
        import pandas as pd
        import bsrn_qc
        frame = pd.DataFrame({"SWD": [1.0]})
        metadata = {"nested": {"value": 1}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dat = root / "tat0426.dat"
            dat.touch()
            report = root / "interactive.html"

            def consume(dat_path, qc_root, *, parsed_data):
                df, meta = parsed_data
                self.assertEqual(df["SWD"].iloc[0], 1.0)
                self.assertEqual(meta["nested"]["value"], 1)
                df.loc[0, "SWD"] = 999
                meta["nested"]["value"] = 999
                return report

            def plots(*args, **kwargs):
                consume(*args, **kwargs)
                return []

            def logical(*args, metadata):
                self.assertEqual(metadata["nested"]["value"], 1)
                return []

            with patch.object(bsrn_qc, "parse_dat_file", return_value=(frame, metadata)) as parse, \
                 patch.object(qc, "generate_interactive_report", side_effect=consume), \
                 patch.object(qc, "generate_swd_sumsw_plots", side_effect=plots), \
                 patch.object(qc, "generate_logical_record_artifacts", side_effect=logical):
                self.assertEqual(qc.run_qc_for_dat(dat, root), (report, [report], []))
                self.assertEqual(parse.call_count, 1)
            self.assertEqual(frame["SWD"].iloc[0], 1.0)
            self.assertEqual(metadata["nested"]["value"], 1)

    def test_static_report_survives_interactive_failure(self):
        import pandas as pd
        import bsrn_qc
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dat = root / "tat0426.dat"
            dat.touch()
            report = root / "static.html"
            report.touch()
            with patch.object(bsrn_qc, "parse_dat_file", return_value=(pd.DataFrame(), {})) as parse, \
                 patch.object(bsrn_qc, "process_one_file", return_value={"report_filename": report.name}), \
                 patch.object(qc, "generate_interactive_report", side_effect=ValueError("report failed")), \
                 patch.object(qc, "generate_swd_sumsw_plots", return_value=[]), \
                 patch.object(qc, "generate_logical_record_artifacts", return_value=[]):
                path, outputs, warnings = qc.run_qc_for_dat(dat, root, include_static_report=True)
            self.assertEqual(path, report)
            self.assertIn(report, outputs)
            self.assertEqual(parse.call_count, 1)
            self.assertEqual(len(warnings), 1)
            self.assertIn("interactive report skipped", warnings[0])


if __name__ == "__main__":
    unittest.main()
