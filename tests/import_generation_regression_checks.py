#!/usr/bin/env python3
"""Focused regression checks for BSRN import generation behavior."""

from __future__ import annotations

import shutil
import sys
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


if __name__ == "__main__":
    test_lr0300_net_radiation_keeps_missing_method_id_parameters()
    test_lr1000_fm12_group_rows_generate_dated_import_file()
