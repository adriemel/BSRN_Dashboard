"""Regression checks for consolidated-only metadata artifacts."""

from pathlib import Path
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from scripts import bsrn_download_check as workflow
from scripts.bsrn_import_files import ImportWorkflowError, read_generated_metadata_outputs


class MetadataOutputTests(unittest.TestCase):
    def test_local_workflow_retains_ten_files_and_import_prerequisite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = workflow.build_parser().parse_args([
                "--config", str(PROJECT_ROOT / "config/bsrn_workflow.ini.example"),
                "--local-file", str(PROJECT_ROOT / "input/tat0426.dat"),
                "--output-root", str(root), "--dashboard", str(root / "dashboard.html"),
            ])
            self.assertEqual(workflow.run_workflow(args), 0)
            statuses = workflow.load_statuses(root / "current/status.json")
            self.assertEqual(len(statuses), 1)
            status = statuses[0]
            metadata = root / "current/metadata"
            self.assertEqual(len(list(metadata.iterdir())), 10)
            self.assertEqual(Path(status.reference_import_file).name, "reference_import.txt")
            read_generated_metadata_outputs(metadata, "TAT", 2026, 4)
            self.assertTrue(status.metadata_ok)
            self.assertTrue(status.format_ok)

    def prepare(self, root, jobs):
        statuses = []
        for job in jobs:
            year, month = workflow.parse_status_year_month(job)
            dat_name = workflow.Job(job.split('_')[0], year, month).dat_name
            reference = root / f"{job}_refImp.txt"
            workflow.write_tsv(reference, [["Title", "URI"], [job, f"ftp://ftp.bsrn.awi.de/{dat_name[:3]}/{dat_name}.gz"]])
            for value in range(1, 10):
                # Include a header-only optional record and embedded punctuation.
                rows = [["File name", "Value"]]
                if value != 5:
                    rows.append([dat_name, f"{job}: record {value}; unchanged"])
                workflow.write_tsv(root / f"{job}_{value:04d}.txt", rows)
            status = workflow.JobStatus(job=job, dat_path=str(root / dat_name), metadata_dir=str(root), reference_import_file=str(reference))
            workflow.attach_reference_id_status(status, {}, None)
            statuses.append(status)
        return statuses

    def test_single_and_multiple_jobs_keep_only_consolidated_files(self):
        for jobs in (["TAT_2026-04"], ["TAT_2026-04", "GOB_2024-06"]):
            with self.subTest(jobs=jobs), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                statuses = self.prepare(root, jobs)
                reference = workflow.write_batch_reference_import(statuses, root)
                reports = workflow.write_batch_metadata_reports(statuses, root)
                expected = {path.name: path.read_bytes() for path in [reference, *reports.values()]}
                workflow.attach_batch_artifacts(statuses, reference, None, reports)
                workflow.remove_individual_metadata_files(statuses, root, reports, reference)
                self.assertEqual(set(path.name for path in root.iterdir()), {"reference_import.txt", *(f"metadata_{value:04d}.txt" for value in range(1, 10))})
                for name, content in expected.items():
                    self.assertEqual((root / name).read_bytes(), content)
                table = workflow.read_tsv(reports["0001"])
                self.assertEqual(table[0], ["Job", "Source file", "File name", "Value"])
                self.assertEqual([row[0] for row in table[1:]], jobs)
                self.assertEqual(len(workflow.read_tsv(reports["0005"])), 1)
                lookup = {status.pangaea_reference_uri: index + 100 for index, status in enumerate(statuses)}
                for index, status in enumerate(statuses):
                    self.assertEqual(status.reference_import_file, str(reference))
                    # Match the server's refresh behavior; no saved URI to rely on.
                    status.pangaea_reference_uri = None
                    status.pangaea_reference_id = None
                    workflow.attach_reference_id_status(status, lookup, None)
                    self.assertEqual(status.pangaea_reference_id, index + 100)
                    year, month = workflow.parse_status_year_month(status.job)
                    read_generated_metadata_outputs(root, status.job.split('_')[0], year, month)
                with self.assertRaises(ImportWorkflowError):
                    read_generated_metadata_outputs(root, "PAY", 2026, 4)
                with self.assertRaises(workflow.WorkflowError):
                    workflow.reference_uri_from_import_file(reference, "PAY_2026-04")

    def test_missing_consolidated_files_preserve_sources_and_unrelated_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            statuses = self.prepare(root, ["TAT_2026-04"])
            unrelated = root / "notes.txt"
            unrelated.write_text("Keep me")
            workflow.remove_individual_metadata_files(statuses, root, {"0001": root / "missing.txt"}, root / "missing-reference.txt")
            self.assertTrue((root / "TAT_2026-04_0001.txt").exists())
            self.assertTrue((root / "TAT_2026-04_refImp.txt").exists())
            self.assertEqual(unrelated.read_text(), "Keep me")
            # Archived runs with the previous layout remain usable.
            read_generated_metadata_outputs(root, "TAT", 2026, 4)


if __name__ == "__main__":
    unittest.main()
