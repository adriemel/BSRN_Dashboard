#!/usr/bin/env python3
"""Focused regression checks for BSRN import generation behavior."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import asdict
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
TEST_TMP = PROJECT_ROOT / ".import_regression_tmp"


def clean_test_tmp() -> None:
    if TEST_TMP.exists():
        shutil.rmtree(TEST_TMP, ignore_errors=True)
    TEST_TMP.mkdir(parents=True, exist_ok=True)


def iza_metadata():
    from scripts.bsrn_import_files import StationJobMetadata

    return StationJobMetadata(
        station_id=61,
        event_label="IZA",
        station_name="Izana",
        source_id=2965,
        author_id=101370,
        author_name="Carlos J. Torres",
        year=2026,
        month=5,
        pangaea_reference_id=140291,
        latitude=28.3093,
        longitude=-16.4993,
    )


def iza_blocks():
    from scripts.bsrn_download_check import dat_blocks, read_dat_text

    return dat_blocks(read_dat_text(PROJECT_ROOT / "input" / "iza0526.dat").splitlines())


def test_lr0300_net_radiation_keeps_missing_method_id_parameters() -> None:
    from scripts.bsrn_import_files import generate_lr0100_0300

    clean_test_tmp()
    out_dir = TEST_TMP / "imports"
    out_dir.mkdir()
    path = generate_lr0100_0300(
        status=None,
        meta=iza_metadata(),
        blocks=iza_blocks(),
        out_dir=out_dir,
        ids_dir=PROJECT_ROOT / "tools" / "create-importfiles",
    )
    assert path is not None
    text = path.read_text(encoding="utf-8", errors="replace")
    for parameter_id in (55918, 55919, 55920, 55921):
        assert f'"ID": {parameter_id}' in text
    assert '{ "ID": 55918, "PI_ID": 101370, "MethodID": -999' in text
    assert "\t55918\t55919\t55920\t55921\t48820\t2219\t48823" in text


def test_lr1000_fm12_group_rows_generate_dated_import_file() -> None:
    from scripts.bsrn_import_files import generate_lr1000
    from scripts.bsrn_qc_continue import extract_optional_logical_records

    clean_test_tmp()
    out_dir = TEST_TMP / "imports"
    out_dir.mkdir(exist_ok=True)
    meta = iza_metadata()
    optional = extract_optional_logical_records(
        PROJECT_ROOT / "input" / "iza0526.dat",
        meta.year,
        meta.month,
        meta.event_label,
        meta.latitude,
        meta.longitude,
    )
    path = generate_lr1000(meta, optional["LR1000"], out_dir)
    assert path is not None
    text = path.read_text(encoding="utf-8", errors="replace")
    assert '"ID": 50007' in text
    assert "YYGG9 IIiii Nddff" in text
    assert "\n1599\t" in text and "\t50007\n" in text
    assert "2026-05-01T09:00" in text


def test_lr3010_keeps_minus_9_9_temperature_value() -> None:
    from scripts.bsrn_import_files import format_field_value, parse_lr3x30_rows

    blocks = {
        "3010": [
            "1 0 1 0.0 1 1 1 0.0 1 1",
            "1 0.0 1 1 1 0.0 1 1 -9.9 75.0",
        ]
    }
    rows = parse_lr3x30_rows(blocks, iza_metadata(), "3010")
    assert rows
    assert rows[0]["Air temperature [deg C]"] == -9.9
    assert format_field_value(rows[0]["Air temperature [deg C]"], "###0.0") == "-9.9"


def accepted_decision(status):
    from scripts.bsrn_download_check import row_signature

    return {
        "job": status.job,
        "decision": "accepted",
        "accepted": True,
        "rejected": False,
        "decided_at": "2026-06-17T00:00:00",
        "operator_note": "",
        "row_signature": row_signature(status),
        "dat_path": status.dat_path,
        "qc_report": status.qc_report,
        "pangaea_reference_uri": status.pangaea_reference_uri,
        "pangaea_reference_id": status.pangaea_reference_id,
    }


def import_ready_status(job: str, reference_id: int):
    from scripts.bsrn_download_check import JobStatus

    return JobStatus(
        job=job,
        dat_path=f"output/current/dat/{job}.dat",
        reference_import_file=f"output/current/metadata/{job}_refImp.txt",
        pangaea_reference_uri=f"ftp://ftp.bsrn.awi.de/{job[:3].lower()}/{job.lower()}.dat.gz",
        pangaea_reference_id=reference_id,
        metadata_ok=True,
        format_ok=True,
        qc_ok=True,
        qc_report=f"output/current/qc_reports/{job}_QC_report_interactive.html",
    )


def test_import_generation_without_job_filter_processes_all_ready_rows() -> None:
    import scripts.bsrn_import_files as importer
    from scripts.bsrn_download_check import CURATOR_DECISIONS_FILE

    clean_test_tmp()
    run_root = TEST_TMP / "batch_import"
    import_root = run_root / "import_files"
    status_path = run_root / "status.json"
    dashboard_path = run_root / "dashboard.html"
    run_root.mkdir(parents=True, exist_ok=True)

    statuses = [
        import_ready_status("AAA_2026-01", 140001),
        import_ready_status("BBB_2026-01", 140002),
    ]
    status_path.write_text(json.dumps([asdict(status) for status in statuses], indent=2), encoding="utf-8")
    decisions = {"version": 1, "decisions": {status.job: accepted_decision(status) for status in statuses}}
    (run_root / CURATOR_DECISIONS_FILE).write_text(json.dumps(decisions, indent=2), encoding="utf-8")

    generated_jobs: list[str] = []
    original_generate = importer.generate_imports_for_status

    def fake_generate(status, import_root_arg, ids_dir):
        generated_jobs.append(status.job)
        path = import_root_arg / status.job / f"{status.job}_dummy_imp.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("dummy\n", encoding="utf-8")
        return [path], []

    importer.generate_imports_for_status = fake_generate
    try:
        args = argparse.Namespace(
            status=str(status_path),
            ids_dir=str(PROJECT_ROOT / "tools" / "create-importfiles"),
            import_dir=str(import_root),
            dashboard=str(dashboard_path),
            job=None,
        )
        assert importer.run_import_generation(args) == 0
    finally:
        importer.generate_imports_for_status = original_generate

    assert generated_jobs == ["AAA_2026-01", "BBB_2026-01"]
    summary = json.loads((import_root / "import_generation_summary.json").read_text(encoding="utf-8"))
    assert summary["generated_rows"] == 2
    assert summary["selected_jobs"] == []


if __name__ == "__main__":
    test_lr0300_net_radiation_keeps_missing_method_id_parameters()
    test_lr1000_fm12_group_rows_generate_dated_import_file()
    test_lr3010_keeps_minus_9_9_temperature_value()
    test_import_generation_without_job_filter_processes_all_ready_rows()
