#!/usr/bin/env python3
"""
Continue a checked BSRN workflow run by generating QC reports.

This script reads an existing status.json produced by bsrn_download_check.py,
runs the QC graph/report tooling for rows with a DAT file available,
updates status.json, and regenerates the dashboard. Format-check failures are
kept visible in status/dashboard output, but they are not a hard QC blocker.
Metadata ID warnings are also non-blocking so non-curator users can continue
to QC reports and readable data exports.
"""

from __future__ import annotations

import argparse
import csv
import contextlib
import datetime as dt
import io
import os
import sys
from pathlib import Path
import traceback
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
QC_TOOLS_ROOT = PROJECT_ROOT / "tools" / "qc-graphs"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(QC_TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(QC_TOOLS_ROOT))

from scripts.bsrn_download_check import (  # noqa: E402
    JobStatus,
    load_statuses,
    resolve_project_path,
    save_statuses,
    write_run_index,
)
from scripts.bsrn_data_exports import generate_data_exports_for_status, rel_path  # noqa: E402


LR0500_UV_B_MILLIWATT_STATIONS = {
    "BON",
    "BOS",
    "DRA",
    "FPE",
    "GCR",
    "MAN",
    "NAU",
    "PAY",
    "PSU",
    "SXF",
    "TOR",
}


RADIATION_EXPORT_COLUMNS = {
    "SWD": "Short-wave downward radiation [W/m**2]",
    "DIR": "Direct normal radiation [W/m**2]",
    "DIF": "Diffuse short-wave downward radiation [W/m**2]",
    "LWD": "Long-wave downward radiation [W/m**2]",
    "T2": "Air temperature [deg C]",
    "RH": "Relative humidity [%]",
    "P": "Atmospheric pressure [hPa]",
    "SWU": "Short-wave upward radiation [W/m**2]",
    "LWU": "Long-wave upward radiation [W/m**2]",
}


class QcWorkflowError(Exception):
    pass


def write_qc_exception_log(qc_root: Path, status: JobStatus, exc: BaseException) -> None:
    logs_dir = qc_root.parent / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    safe_job = "".join(char if char.isalnum() or char in "._-" else "_" for char in status.job)
    path = logs_dir / f"{safe_job}_qc.traceback.txt"
    path.write_text("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)), encoding="utf-8")


def eligible_for_qc(status: JobStatus) -> bool:
    qc_errors = [error for error in status.errors if error.startswith("QC error:")]
    return bool(status.dat_path and not qc_errors)


def run_qc_for_dat(dat_path: Path, qc_root: Path, include_static_report: bool = False) -> tuple[Path, list[Path], list[str]]:
    if not dat_path.exists():
        raise QcWorkflowError(f"DAT file not found: {dat_path}")

    mpl_config_dir = qc_root / "_matplotlib"
    mpl_config_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(mpl_config_dir))

    qc_root.mkdir(parents=True, exist_ok=True)
    log_path = qc_root / f"{dat_path.stem}_qc_stdout.txt"
    output = io.StringIO()
    warnings: list[str] = []
    outputs: list[Path] = []
    report_path: Path | None = None

    if include_static_report:
        try:
            import bsrn_qc
        except Exception as exc:
            raise QcWorkflowError(f"Could not import tools/qc-graphs/bsrn_qc.py: {exc}") from exc

        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            info = bsrn_qc.process_one_file(dat_path, qc_root)

        log_text = output.getvalue()
        if log_text:
            log_path.write_text(log_text, encoding="utf-8", errors="replace")
            outputs.append(log_path)

        report_name = info.get("report_filename")
        if not report_name:
            raise QcWorkflowError(f"Static QC completed for {dat_path.name} but did not return a report filename")
        report_path = qc_root / report_name
        if not report_path.exists():
            raise QcWorkflowError(f"Static QC report was not created: {report_path}")
        outputs.append(report_path)

    try:
        interactive_path = generate_interactive_report(dat_path, qc_root)
        outputs.append(interactive_path)
        if report_path is None:
            report_path = interactive_path
    except Exception as exc:
        if report_path is None:
            raise QcWorkflowError(f"Interactive QC report failed for {dat_path.name}: {exc}") from exc
        warnings.append(f"QC warning: interactive report skipped for {dat_path.name}: {exc}")

    try:
        outputs.extend(generate_swd_sumsw_plots(dat_path, qc_root))
    except Exception as exc:
        warnings.append(f"QC warning: SWD/SumSW time-of-day plots skipped for {dat_path.name}: {exc}")

    try:
        outputs.extend(generate_logical_record_artifacts(dat_path, qc_root))
    except Exception as exc:
        warnings.append(f"QC warning: optional logical-record extraction skipped for {dat_path.name}: {exc}")

    if report_path is None:
        raise QcWorkflowError(f"QC completed for {dat_path.name} but no QC report was created")
    return report_path, outputs, warnings


def generate_interactive_report(dat_path: Path, qc_root: Path) -> Path:
    import bsrn_qc
    from interactive_report import generate_interactive_report as write_interactive_report

    df, metadata = bsrn_qc.parse_dat_file(dat_path)
    df = bsrn_qc.compute_solar_auxiliary(df, metadata["latitude"], metadata["longitude"])
    lr4000_report = bsrn_qc.check_lr4000(df, metadata)
    df = bsrn_qc.run_qc_checks(df)
    qc_summary = bsrn_qc.summarize_qc_flags(df)
    report_path = qc_root / (
        f"{metadata['station_code']}_{metadata['year']}-{metadata['month']:02d}_QC_report_interactive.html"
    )
    write_interactive_report(df, metadata, qc_summary, report_path, lr4000_report)
    return report_path


def generate_swd_sumsw_plots(dat_path: Path, qc_root: Path) -> list[Path]:
    from bsrn_swd_sumsw_time_of_day import create_plots

    output_dir = qc_root / f"{dat_path.stem}_SWD_SumSW_time_of_day"
    summary = create_plots(dat_path, output_dir=output_dir)
    return [
        Path(summary["daily_panel_grid"]),
        Path(summary["all_days_overlay"]),
    ]


def generate_logical_record_artifacts(dat_path: Path, qc_root: Path) -> list[Path]:
    mpl_config_dir = qc_root / "_matplotlib"
    mpl_config_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(mpl_config_dir))

    import bsrn_qc
    import matplotlib.pyplot as plt

    _, metadata = bsrn_qc.parse_dat_file(dat_path)
    records = extract_optional_logical_records(
        dat_path,
        int(metadata["year"]),
        int(metadata["month"]),
        str(metadata["station_code"]),
        float(metadata["latitude"]),
        float(metadata["longitude"]),
    )
    if not records:
        return []

    output_dir = qc_root / f"{dat_path.stem}_logical_records"
    output_dir.mkdir(parents=True, exist_ok=True)

    outputs: list[Path] = []
    for record_name, rows in records.items():
        if not rows:
            continue
        csv_path = output_dir / f"{dat_path.stem}_{record_name}.csv"
        png_path = output_dir / f"{dat_path.stem}_{record_name}.png"
        write_rows_csv(csv_path, rows)
        plot_logical_record(record_name, rows, png_path, plt)
        outputs.extend([csv_path, png_path])
    return outputs


def generate_data_exports(dat_path: Path, run_root: Path) -> list[Path]:
    """Compatibility wrapper for older callers."""

    status = JobStatus(job=dat_path.stem[:3].upper(), dat_path=str(dat_path))
    outputs, warnings = generate_data_exports_for_status(status, run_root)
    if warnings and not outputs:
        raise QcWorkflowError("; ".join(warnings))
    return outputs


def extract_optional_logical_records(
    dat_path: Path,
    year: int,
    month: int,
    station_code: str,
    latitude: float,
    longitude: float,
) -> dict[str, list[dict[str, object]]]:
    lines = dat_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    blocks: dict[str, list[str]] = {}
    current_record: str | None = None
    for raw_line in lines:
        line = raw_line.strip()
        if line.startswith(("*C", "*U")):
            record_id = line[2:].strip()
            if not record_id.isdigit():
                current_record = None
            else:
                current_record = record_id.zfill(4)
            if current_record is not None:
                blocks.setdefault(current_record, [])
            continue
        if current_record is not None:
            blocks[current_record].append(raw_line)

    extracted: dict[str, list[dict[str, object]]] = {}
    if "0500" in blocks:
        rows = parse_lr0500(blocks["0500"], year, month, station_code)
        if rows:
            extracted["LR0500"] = rows
    if "1000" in blocks:
        rows = parse_lr1000(
            blocks["1000"],
            year,
            month,
            dat_path.stem[:3].upper(),
            latitude,
            longitude,
        )
        if rows:
            extracted["LR1000"] = rows
    if "1200" in blocks:
        rows = parse_lr1200(blocks["1200"], year, month)
        if rows:
            extracted["LR1200"] = rows
    if "3010" in blocks:
        rows = parse_lr3010(blocks["3010"], year, month)
        if rows:
            extracted["LR3010"] = rows
    return extracted


def parse_lr0500(lines: list[str], year: int, month: int, station_code: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    clean_lines = [line.strip() for line in lines if line.strip()]
    uv_b_in_milliwatts = station_code.upper() in LR0500_UV_B_MILLIWATT_STATIONS
    for i in range(0, len(clean_lines) - 1, 2):
        p1 = clean_lines[i].split()
        p2 = clean_lines[i + 1].split()
        if len(p1) < 10 or len(p2) < 12:
            continue
        day, minute = parse_int(p1[0]), parse_int(p1[1])
        if day is None or minute is None:
            continue
        rows.append(
            {
                "Date/Time": make_datetime(year, month, day, minute),
                "date_day": day,
                "time_min": minute,
                "UV-a global [W/m**2]": clean_number(p1[2]),
                "UV-a global, standard deviation [W/m**2]": clean_number(p1[3]),
                "UV-a global, minimum [W/m**2]": clean_number(p1[4]),
                "UV-a global, maximum [W/m**2]": clean_number(p1[5]),
                "UV-b direct [W/m**2]": clean_lr0500_uv_b_number(p1[6], uv_b_in_milliwatts),
                "UV-b direct, standard deviation [W/m**2]": clean_lr0500_uv_b_number(
                    p1[7],
                    uv_b_in_milliwatts,
                ),
                "UV-b direct, minimum [W/m**2]": clean_lr0500_uv_b_number(p1[8], uv_b_in_milliwatts),
                "UV-b direct, maximum [W/m**2]": clean_lr0500_uv_b_number(p1[9], uv_b_in_milliwatts),
                "UV-b global [W/m**2]": clean_lr0500_uv_b_number(p2[0], uv_b_in_milliwatts),
                "UV-b global, standard deviation [W/m**2]": clean_lr0500_uv_b_number(
                    p2[1],
                    uv_b_in_milliwatts,
                ),
                "UV-b global, minimum [W/m**2]": clean_lr0500_uv_b_number(p2[2], uv_b_in_milliwatts),
                "UV-b global, maximum [W/m**2]": clean_lr0500_uv_b_number(p2[3], uv_b_in_milliwatts),
                "UV-b diffuse [W/m**2]": clean_lr0500_uv_b_number(p2[4], uv_b_in_milliwatts),
                "UV-b diffuse, standard deviation [W/m**2]": clean_lr0500_uv_b_number(
                    p2[5],
                    uv_b_in_milliwatts,
                ),
                "UV-b diffuse, minimum [W/m**2]": clean_lr0500_uv_b_number(p2[6], uv_b_in_milliwatts),
                "UV-b diffuse, maximum [W/m**2]": clean_lr0500_uv_b_number(p2[7], uv_b_in_milliwatts),
                "UV upward reflected [W/m**2]": clean_number(p2[8]),
                "UV upward reflected, standard deviation [W/m**2]": clean_number(p2[9]),
                "UV upward reflected, minimum [W/m**2]": clean_number(p2[10]),
                "UV upward reflected, maximum [W/m**2]": clean_number(p2[11]),
            }
        )
    return rows


def clean_lr0500_uv_b_number(value: str, uv_b_in_milliwatts: bool) -> float | None:
    number = clean_number(value)
    if number is None or not uv_b_in_milliwatts:
        return number
    return number / 1000.0


def parse_lr1000(
    lines: list[str],
    year: int,
    month: int,
    station_code: str,
    latitude: float,
    longitude: float,
) -> list[dict[str, object]]:
    clean_lines = [line.rstrip("\n") for line in lines if line.strip()]
    synop_format = lr1000_station_format(station_code)
    if synop_format == 2:
        return parse_lr1000_format2(clean_lines, year, month, station_code, latitude, longitude)
    if synop_format == 6:
        return parse_lr1000_format6(clean_lines, year, month, station_code, latitude, longitude)
    if synop_format in {1, 3, 4, 5}:
        return parse_lr1000_synop_groups(clean_lines, year, month, station_code, latitude, longitude, synop_format)

    rows = []
    for index, line in enumerate(clean_lines, start=1):
        code = line.strip()
        if code:
            rows.append({"line": index, "SYNOP format": synop_format or "unknown", "FM 12-XII Ext. SYNOP code": code})
    return rows


def lr1000_station_format(station_code: str) -> int | None:
    station = station_code.lower()
    if station in {"lin", "sbo"}:
        return 1
    if station in {"abs", "fua", "ish", "mnm", "sap", "syo", "tat", "tor"}:
        return 2
    if station == "tam":
        return 3
    if station == "pay":
        return 4
    if station == "son":
        return 5
    if station in {"gvn", "nya"}:
        return 6
    return None


def parse_lr1000_format2(
    lines: list[str],
    year: int,
    month: int,
    station_code: str,
    latitude: float,
    longitude: float,
) -> list[dict[str, object]]:
    fields = [
        ("Temperature, air [deg C]", 9, 5, lambda line: qmid(line, 9, 5) != "-99.0", "number"),
        ("Pressure, atmospheric [hPa]", 15, 5, lambda line: qmid(line, 17, 3) != "-99", "number"),
        ("Dew/frost point [deg C]", 21, 5, lambda line: qmid(line, 21, 5) != "-99.0", "number"),
        ("Wind direction [deg]", 27, 3, lambda line: qmid(line, 27, 3) != "-99", "number"),
        ("Wind speed [m/sec]", 31, 3, lambda line: qmid(line, 31, 3) != "-99", "number"),
        ("Past weather1 [code]", 35, 1, lambda line: qmid(line, 35, 1) != "/", "text"),
        ("Present weather [code]", 37, 2, lambda line: qmid(line, 37, 2) != "//", "text"),
        ("Total cloud amount [code]", 40, 1, lambda line: qmid(line, 40, 1) != "/", "text"),
        ("Low/middle cloud amount [code]", 42, 1, lambda line: qmid(line, 42, 1) != "/", "text"),
        ("Low cloud [code]", 44, 1, lambda line: qmid(line, 44, 1) != "/", "text"),
        ("Middle cloud [code]", 46, 1, lambda line: qmid(line, 46, 1) != "/", "text"),
        ("High cloud [code]", 48, 1, lambda line: qmid(line, 48, 1) != "/", "text"),
        ("Cloud base height [code]", 50, 1, lambda line: qmid(line, 50, 1) != "/", "text"),
        ("Present blowing snow [code]", 52, 1, lambda line: qmid(line, 52, 1) != "/", "text"),
        ("Past blowing snow [code]", 53, 1, lambda line: qmid(line, 53, 1) != "/", "text"),
        ("Horizontal visibility [code]", 55, 2, lambda line: qmid(line, 55, 2) != "//", "text"),
    ]
    active = [any(test(line) for line in lines) for _, _, _, test, _ in fields]

    rows: list[dict[str, object]] = []
    for line in lines:
        day = parse_int(qmid(line, 1, 2))
        minute = parse_int(qmid(line, 4, 4))
        if day is None or minute is None:
            continue
        row: dict[str, object] = {
            "Station": station_code.upper(),
            "Date/Time": make_datetime(year, month, day, minute),
            "Latitude": latitude,
            "Longitude": longitude,
            "SYNOP format": 2,
        }
        for enabled, (name, start, length, _test, value_type) in zip(active, fields):
            if not enabled:
                continue
            value = qmid(line, start, length).replace("/", "").replace(" ", "")
            if value_type == "number":
                row[name] = clean_synop_number(value)
            else:
                row[name] = value or None
        row["FM 12-XII Ext. SYNOP code"] = line.strip()
        rows.append(row)
    return rows


def parse_lr1000_format6(
    lines: list[str],
    year: int,
    month: int,
    station_code: str,
    latitude: float,
    longitude: float,
) -> list[dict[str, object]]:
    fields = [
        ("Cloud base height [code]", 9, 1, lambda line: qmid(line, 9, 1) != "/", "text"),
        ("Horizontal visibility [code]", 11, 2, lambda line: qmid(line, 11, 2) != "//", "text"),
        ("Wind direction [deg]", 14, 3, lambda line: qmid(line, 14, 3) != "-99", "number"),
        ("Wind speed [m/sec]", 18, 5, lambda line: qmid(line, 18, 5) != "-99.0", "number"),
        ("Temperature, air [deg C]", 24, 5, lambda line: qmid(line, 24, 5) != "-99.0", "number"),
        ("Dew/frost point [deg C]", 30, 5, lambda line: qmid(line, 30, 5) != "-99.0", "number"),
        ("Pressure, atmospheric [hPa]", 36, 6, lambda line: qmid(line, 37, 5) != "-99.0", "number"),
        ("Characteristic of barometric tendency [code]", 43, 1, lambda line: qmid(line, 43, 1) != "/", "text"),
        ("Amount of barometric tendency [hPa]", 45, 5, lambda line: qmid(line, 45, 5) != "-99.0", "number"),
        ("Present weather [code]", 51, 2, lambda line: qmid(line, 51, 2) != "//", "text"),
        ("Past weather1 [code]", 54, 1, lambda line: qmid(line, 54, 1) != "/", "text"),
        ("Past weather2 [code]", 56, 1, lambda line: qmid(line, 56, 1) != "/", "text"),
        ("Low cloud [code]", 58, 1, lambda line: qmid(line, 58, 1) != "/", "text"),
        ("Middle cloud [code]", 60, 1, lambda line: qmid(line, 60, 1) != "/", "text"),
        ("High cloud [code]", 62, 1, lambda line: qmid(line, 62, 1) != "/", "text"),
        ("Total cloud amount", 64, 1, lambda line: qmid(line, 64, 1) != "/", "text"),
        ("Low/middle cloud amount [code]", 66, 1, lambda line: qmid(line, 66, 1) != "/", "text"),
        ("Temperature, air, maximum [deg C]", 68, 5, lambda line: qmid(line, 68, 5) != "-99.0", "number"),
        ("Temperature, air, minimum [deg C]", 74, 5, lambda line: qmid(line, 74, 5) != "-99.0", "number"),
        ("Present blowing snow [code]", 80, 1, lambda line: qmid(line, 80, 1) != "/", "text"),
        ("Past blowing snow [code]", 82, 1, lambda line: qmid(line, 82, 1) != "/", "text"),
        ("Whiteout yes/no [y/n]", 84, 1, lambda line: qmid(line, 84, 1) != "/", "text"),
    ]
    active = [any(test(line) for line in lines) for _, _, _, test, _ in fields]

    rows: list[dict[str, object]] = []
    for line in lines:
        day = parse_int(qmid(line, 1, 2))
        minute = parse_int(qmid(line, 4, 4))
        if day is None or minute is None:
            continue
        row: dict[str, object] = {
            "Station": station_code.upper(),
            "Date/Time": make_datetime(year, month, day, minute),
            "Latitude": latitude,
            "Longitude": longitude,
            "SYNOP format": 6,
        }
        for enabled, (name, start, length, _test, value_type) in zip(active, fields):
            if not enabled:
                continue
            value = qmid(line, start, length).replace("/", "").replace(" ", "")
            if value_type == "number":
                row[name] = clean_synop_number(value)
            else:
                row[name] = value or None
        row["FM 12-XII Ext. SYNOP code"] = line.strip()
        rows.append(row)
    return rows


def parse_lr1000_synop_groups(
    lines: list[str],
    year: int,
    month: int,
    station_code: str,
    latitude: float,
    longitude: float,
    synop_format: int,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        day = parse_int(parts[0][0:2])
        hour = parse_int(parts[0][2:4])
        if day is None or hour is None:
            continue
        row: dict[str, object] = {
            "Station": station_code.upper(),
            "Date/Time": (dt.datetime(year, month, day) + dt.timedelta(hours=hour)).isoformat(timespec="minutes"),
            "Latitude": latitude,
            "Longitude": longitude,
            "SYNOP format": synop_format,
            "IIiii": group_value(parts, 1),
        }

        if synop_format in {1, 5}:
            parse_synop_n_dd_ff(row, group_value(parts, 2))
            parse_synop_temperature(row, "Temperature, air [deg C]", group_value(parts, 3))
            parse_synop_temperature(row, "Dew/frost point [deg C]", group_value(parts, 4))
            parse_synop_pressure(row, "Station Pressure [hPa]", group_value(parts, 5))
            pressure_label = "Geopotential height [m]" if synop_format == 5 else "Pressure, atmospheric [hPa]"
            parse_synop_pressure(row, pressure_label, group_value(parts, 6))
            parse_synop_weather(row, group_value(parts, 7))
            parse_synop_clouds(row, group_value(parts, 8))
            parse_synop_layers(row, parts[10:13])
        elif synop_format in {3, 4}:
            offset = 1 if year < 2008 and synop_format == 3 else 0
            if offset:
                parse_synop_visibility_group(row, group_value(parts, 2))
            parse_synop_n_dd_ff(row, group_value(parts, 2 + offset))
            parse_synop_temperature(row, "Temperature, air [deg C]", group_value(parts, 3 + offset))
            parse_synop_temperature(row, "Dew/frost point [deg C]", group_value(parts, 4 + offset))
            parse_synop_pressure(row, "Station Pressure [hPa]", group_value(parts, 5 + offset))
            pressure_label = "Pressure, atmospheric [hPa]" if synop_format == 4 else "Geopotential height [m]"
            parse_synop_pressure(row, pressure_label, group_value(parts, 6 + offset))
            parse_synop_weather(row, group_value(parts, 7 + offset))
            parse_synop_clouds(row, group_value(parts, 8 + offset))
            layer_start = 10 + offset
            parse_synop_layers(row, parts[layer_start : layer_start + 3])

        row["FM 12-XII Ext. SYNOP code"] = line.strip()
        rows.append(row)
    return shrink_empty_columns(rows)


def group_value(parts: list[str], index: int) -> str:
    return parts[index] if len(parts) > index else ""


def synop_token(value: str) -> str | None:
    value = value.replace("/", "").strip()
    return value or None


def parse_synop_n_dd_ff(row: dict[str, object], group: str) -> None:
    if len(group) < 5:
        return
    row["Total cloud amount [code]"] = clean_synop_number(synop_token(group[0]) or "")
    dd = synop_token(group[1:3])
    row["Wind direction [deg]"] = int(dd) * 10 if dd and dd.isdigit() else None
    row["Wind speed [m/sec]"] = clean_synop_number(synop_token(group[3:5]) or "")


def parse_synop_temperature(row: dict[str, object], label: str, group: str) -> None:
    if len(group) < 5:
        return
    value = synop_token(group[2:5])
    if value is None or not value.isdigit():
        row[label] = None
        return
    number = int(value) / 10.0
    row[label] = -number if group[1:2] == "1" else number


def parse_synop_pressure(row: dict[str, object], label: str, group: str) -> None:
    if len(group) < 5:
        return
    value = synop_token(group[1:5])
    if value is None or not value.isdigit():
        row[label] = None
        return
    pressure = int(value) / 10.0
    row[label] = pressure + 1000.0 if pressure <= 200.0 and "Pressure" in label else pressure


def parse_synop_weather(row: dict[str, object], group: str) -> None:
    if len(group) < 5:
        return
    row["Present weather [code]"] = clean_synop_number(synop_token(group[1:3]) or "")
    row["Past weather1 [code]"] = clean_synop_number(synop_token(group[3:4]) or "")
    row["Past weather2 [code]"] = clean_synop_number(synop_token(group[4:5]) or "")


def parse_synop_clouds(row: dict[str, object], group: str) -> None:
    if len(group) < 5:
        return
    row["Low/middle cloud amount [code]"] = clean_synop_number(synop_token(group[1:2]) or "")
    row["Low cloud [code]"] = clean_synop_number(synop_token(group[2:3]) or "")
    row["Middle cloud [code]"] = clean_synop_number(synop_token(group[3:4]) or "")
    row["High cloud [code]"] = clean_synop_number(synop_token(group[4:5]) or "")


def parse_synop_layers(row: dict[str, object], groups: list[str]) -> None:
    for index, group in enumerate(groups, start=1):
        if len(group) < 5:
            continue
        row[f"Ns {index} [code]"] = clean_synop_number(synop_token(group[1:2]) or "")
        row[f"C {index} [code]"] = clean_synop_number(synop_token(group[2:3]) or "")
        row[f"hshs {index} [code]"] = clean_synop_number(synop_token(group[3:5]) or "")


def parse_synop_visibility_group(row: dict[str, object], group: str) -> None:
    if len(group) < 5:
        return
    row["Cloud base height [code]"] = clean_synop_number(synop_token(group[2:3]) or "")
    row["Horizontal visibility [code]"] = clean_synop_number(synop_token(group[3:5]) or "")


def shrink_empty_columns(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    keep = {key for row in rows for key, value in row.items() if value not in {None, ""}}
    return [{key: value for key, value in row.items() if key in keep} for row in rows]


def qmid(line: str, start: int, length: int) -> str:
    return line[start : start + length].strip()


def clean_synop_number(value: str) -> float | int | None:
    if not value:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    if number <= -90:
        return None
    if number.is_integer():
        return int(number)
    return number


def parse_lr1200(lines: list[str], year: int, month: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        day, minute = parse_int(parts[0]), parse_int(parts[1])
        if day is None or minute is None:
            continue
        rows.append(
            {
                "Date/Time": make_datetime(year, month, day, minute),
                "date_day": day,
                "time_min": minute,
                "Ozone total [DU]": clean_number(parts[2]),
            }
        )
    return rows


def parse_lr3010(lines: list[str], year: int, month: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    clean_lines = [line.strip() for line in lines if line.strip()]
    for i in range(0, len(clean_lines) - 1, 2):
        p1 = clean_lines[i].split()
        p2 = clean_lines[i + 1].split()
        if len(p1) < 10 or len(p2) < 10:
            continue
        day, minute = parse_int(p1[0]), parse_int(p1[1])
        if day is None or minute is None:
            continue
        rows.append(
            {
                "Date/Time": make_datetime(year, month, day, minute),
                "date_day": day,
                "time_min": minute,
                "Short-wave downward (GLOBAL) radiation [W/m**2]": clean_number(p1[2]),
                "Short-wave downward (GLOBAL) radiation, standard deviation [W/m**2]": clean_number(p1[3]),
                "Short-wave downward (GLOBAL) radiation, minimum [W/m**2]": clean_number(p1[4]),
                "Short-wave downward (GLOBAL) radiation, maximum [W/m**2]": clean_number(p1[5]),
                "Short-wave upward (REFLEX) radiation [W/m**2]": clean_number(p1[6]),
                "Short-wave upward (REFLEX) radiation, standard deviation [W/m**2]": clean_number(p1[7]),
                "Short-wave upward (REFLEX) radiation, minimum [W/m**2]": clean_number(p1[8]),
                "Short-wave upward (REFLEX) radiation, maximum [W/m**2]": clean_number(p1[9]),
                "Long-wave downward radiation [W/m**2]": clean_number(p2[0]),
                "Long-wave downward radiation, standard deviation [W/m**2]": clean_number(p2[1]),
                "Long-wave downward radiation, minimum [W/m**2]": clean_number(p2[2]),
                "Long-wave downward radiation, maximum [W/m**2]": clean_number(p2[3]),
                "Long-wave upward radiation [W/m**2]": clean_number(p2[4]),
                "Long-wave upward radiation, standard deviation [W/m**2]": clean_number(p2[5]),
                "Long-wave upward radiation, minimum [W/m**2]": clean_number(p2[6]),
                "Long-wave upward radiation, maximum [W/m**2]": clean_number(p2[7]),
                "Air temperature [deg C]": clean_number(p2[8]),
                "Relative Humidity [%]": clean_number(p2[9]),
            }
        )
    return rows


def parse_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def clean_number(value: str) -> float | None:
    try:
        number = float(value)
    except ValueError:
        return None
    if number in {-999.0, -9999.0, -99.9, -9.9}:
        return None
    return number


def make_datetime(year: int, month: int, day: int, minute: int) -> str:
    return (dt.datetime(year, month, day) + dt.timedelta(minutes=minute)).isoformat(timespec="minutes")


def write_rows_csv(path: Path, rows: list[dict[str, object]]) -> None:
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def plot_logical_record(record_name: str, rows: list[dict[str, object]], output_path: Path, plt) -> None:
    time_values = [row.get("Date/Time") for row in rows]
    has_time = bool(time_values and all(time_values))
    plot_excluded = {
        "Station",
        "Date/Time",
        "Latitude",
        "Longitude",
        "SYNOP format",
        "date_day",
        "time_min",
        "line",
        "FM 12-XII Ext. SYNOP code",
    }
    numeric_cols = [
        key
        for key in rows[0].keys()
        if key not in plot_excluded and any(isinstance(row.get(key), (int, float)) for row in rows)
    ]
    primary_cols = [col for col in numeric_cols if "standard deviation" not in col and "minimum" not in col and "maximum" not in col]

    if has_time and primary_cols:
        fig_height = max(4.0, min(2.0 * len(primary_cols), 18.0))
        fig, axes = plt.subplots(len(primary_cols), 1, figsize=(12, fig_height), sharex=True)
        if len(primary_cols) == 1:
            axes = [axes]
        x_values = [dt.datetime.fromisoformat(str(value)) for value in time_values]
        for ax, col in zip(axes, primary_cols):
            y_values = [row.get(col) for row in rows]
            ax.plot(x_values, y_values, linewidth=0.9, color="#2563eb")
            ax.set_ylabel(short_label(col), rotation=0, ha="right", va="center", labelpad=58)
            ax.grid(True, alpha=0.25)
        axes[-1].set_xlabel("Date/Time")
        axes[0].set_title(f"{record_name} optional logical record")
        fig.autofmt_xdate()
        fig.tight_layout()
        fig.savefig(output_path, dpi=150)
        plt.close(fig)
        return

    fig, ax = plt.subplots(figsize=(12, 5))
    if has_time and primary_cols:
        x_values = [dt.datetime.fromisoformat(str(value)) for value in time_values]
        for col in primary_cols[:8]:
            y_values = [row.get(col) for row in rows]
            ax.plot(x_values, y_values, linewidth=1.0, label=short_label(col))
        ax.set_xlabel("Date/Time")
        ax.set_ylabel("Value")
        ax.legend(loc="best", fontsize="small")
        fig.autofmt_xdate()
    elif numeric_cols:
        for col in numeric_cols[:8]:
            y_values = [row.get(col) for row in rows]
            ax.plot(range(1, len(rows) + 1), y_values, linewidth=1.0, label=short_label(col))
        ax.set_xlabel("Row")
        ax.set_ylabel("Value")
        ax.legend(loc="best", fontsize="small")
    else:
        ax.axis("off")
        sample = "\n".join(str(row.get("FM 12-XII Ext. SYNOP code", ""))[:100] for row in rows[:12])
        ax.text(0.01, 0.98, f"{len(rows)} SYNOP record(s)\n\n{sample}", va="top", ha="left", family="monospace")

    ax.set_title(f"{record_name} optional logical record")
    ax.grid(True, alpha=0.25)
    fig.tight_layout()
    fig.savefig(output_path, dpi=150)
    plt.close(fig)


def short_label(label: str) -> str:
    return label.split("[", 1)[0].strip().replace(" radiation", "")


def rel_paths(paths: Iterable[Path]) -> list[str]:
    result = []
    for path in paths:
        try:
            result.append(str(path.relative_to(PROJECT_ROOT)))
        except ValueError:
            result.append(str(path))
    return result


def run_continue(args: argparse.Namespace) -> int:
    status_path = resolve_project_path(args.status)
    if not status_path.exists():
        raise QcWorkflowError(f"Status JSON not found: {status_path}")

    run_root = status_path.parent
    qc_root = resolve_project_path(args.qc_dir) if args.qc_dir else run_root / "qc_reports"

    statuses = load_statuses(status_path)
    any_attempted = False
    for status in statuses:
        if status.qc_ok and status.qc_report:
            continue
        if not eligible_for_qc(status):
            continue

        any_attempted = True
        status.errors = [error for error in status.errors if not error.startswith("QC error:")]
        status.qc_ok = False
        status.qc_report = None
        status.qc_outputs = []
        status.qc_warnings = []
        status.data_export_dir = None
        status.data_export_outputs = []
        status.data_export_warnings = []
        dat_path = Path(status.dat_path)
        if not dat_path.is_absolute():
            dat_path = PROJECT_ROOT / dat_path
        try:
            report_path, outputs, warnings = run_qc_for_dat(dat_path, qc_root, include_static_report=args.include_static_qc_report)
            data_export_outputs: list[Path] = []
            try:
                data_export_outputs, data_export_warnings = generate_data_exports_for_status(status, run_root)
                status.data_export_warnings = data_export_warnings
            except Exception as exc:
                status.data_export_warnings = [f"Data export warning: skipped for {dat_path.name}: {exc}"]
            status.qc_ok = True
            status.qc_dir = str(qc_root)
            status.qc_report = str(report_path)
            status.qc_outputs = rel_paths(outputs)
            status.qc_warnings = warnings
            status.data_export_dir = rel_path(run_root / "data_exports") if data_export_outputs else None
            status.data_export_outputs = [rel_path(path) for path in data_export_outputs]
        except Exception as exc:
            status.qc_ok = False
            status.qc_dir = str(qc_root)
            status.errors.append(f"QC error: {exc}")
            write_qc_exception_log(qc_root, status, exc)

    if not any_attempted and not any(status.qc_ok for status in statuses):
        raise QcWorkflowError("No status rows are eligible for QC. A DAT file must be available first.")

    save_statuses(status_path, statuses)
    dashboard_path = resolve_project_path(args.dashboard or "dashboard.html")
    write_run_index(
        {
            "root": run_root,
            "qc_reports": qc_root,
        },
        statuses,
        dashboard_path=dashboard_path,
    )
    print(f"QC reports:  {qc_root}")
    print(f"Status JSON: {status_path}")
    print(f"Dashboard:   {dashboard_path}")
    return 1 if any(error.startswith("QC error:") for status in statuses for error in status.errors) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", default=str(PROJECT_ROOT / "output" / "current" / "status.json"))
    parser.add_argument("--qc-dir", help="Output directory for QC reports; defaults to <run>/qc_reports")
    parser.add_argument("--dashboard", help="Central dashboard path; defaults to BSRN/dashboard.html")
    parser.add_argument("--include-static-qc-report", action="store_true", help="Also generate the slower static xxx_QC_report.html report")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run_continue(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
