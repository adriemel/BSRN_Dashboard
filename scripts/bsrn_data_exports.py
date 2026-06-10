#!/usr/bin/env python3
"""Create user-readable BSRN data CSV exports without requiring QC."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.bsrn_download_check import (  # noqa: E402
    JobStatus,
    dat_blocks,
    load_statuses,
    read_dat_text,
    resolve_project_path,
    save_statuses,
    workflow_path,
    write_run_index,
)


class DataExportError(Exception):
    pass


EXPORT_RECORDS = {"0100", "0300", "0500", "1000", "1100", "1200", "1300", "3010", "3030"}

LR0100_HEADERS = {
    "Date/Time": "Date/Time",
    "Height": "Height above ground [m]",
    "SWD": "Short-wave downward radiation [W/m**2]",
    "SWD_sd": "Short-wave downward radiation, standard deviation [W/m**2]",
    "SWD_min": "Short-wave downward radiation, minimum [W/m**2]",
    "SWD_max": "Short-wave downward radiation, maximum [W/m**2]",
    "DIR": "Direct normal radiation [W/m**2]",
    "DIR_sd": "Direct normal radiation, standard deviation [W/m**2]",
    "DIR_min": "Direct normal radiation, minimum [W/m**2]",
    "DIR_max": "Direct normal radiation, maximum [W/m**2]",
    "DIF": "Diffuse short-wave downward radiation [W/m**2]",
    "DIF_sd": "Diffuse short-wave downward radiation, standard deviation [W/m**2]",
    "DIF_min": "Diffuse short-wave downward radiation, minimum [W/m**2]",
    "DIF_max": "Diffuse short-wave downward radiation, maximum [W/m**2]",
    "LWD": "Long-wave downward radiation [W/m**2]",
    "LWD_sd": "Long-wave downward radiation, standard deviation [W/m**2]",
    "LWD_min": "Long-wave downward radiation, minimum [W/m**2]",
    "LWD_max": "Long-wave downward radiation, maximum [W/m**2]",
    "T2": "Air temperature [deg C]",
    "RH": "Relative humidity [%]",
    "P": "Atmospheric pressure [hPa]",
}

LR0300_HEADERS = {
    "Date/Time": "Date/Time",
    "SWU": "Short-wave upward radiation [W/m**2]",
    "SWU_sd": "Short-wave upward radiation, standard deviation [W/m**2]",
    "SWU_min": "Short-wave upward radiation, minimum [W/m**2]",
    "SWU_max": "Short-wave upward radiation, maximum [W/m**2]",
    "LWU": "Long-wave upward radiation [W/m**2]",
    "LWU_sd": "Long-wave upward radiation, standard deviation [W/m**2]",
    "LWU_min": "Long-wave upward radiation, minimum [W/m**2]",
    "LWU_max": "Long-wave upward radiation, maximum [W/m**2]",
}

LR1100_HEADERS = {
    "Date/Time": "Date/Time",
    "Altitude": "Altitude [m]",
    "Pressure": "Pressure [hPa]",
    "Temperature": "Temperature [deg C]",
    "Dew/frost point": "Dew/frost point [deg C]",
    "Wind direction": "Wind direction [deg]",
    "Wind speed": "Wind speed [m/sec]",
    "Ozone": "Ozone [mPa]",
}

LR1300_HEADERS = {
    "Date/Time": "Date/Time",
    "Total cloud amount": "Total cloud amount",
    "Cloud base height": "Cloud base height [m]",
    "Cloud liquid water": "Cloud liquid water",
    "AOD wavelength 1": "Aerosol optical depth, wavelength 1",
    "AOD wavelength 2": "Aerosol optical depth, wavelength 2",
    "AOD wavelength 3": "Aerosol optical depth, wavelength 3",
}


def rel_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def read_blocks(dat_path: Path) -> dict[str, list[str]]:
    return dat_blocks(read_dat_text(dat_path).splitlines())


def export_metadata_from_blocks(status: JobStatus, blocks: dict[str, list[str]], dat_path: Path):
    from scripts.bsrn_import_files import StationJobMetadata, first_lr0004_lat_lon

    lr0001 = blocks.get("0001") or []
    station = status.job.split("_", 1)[0].upper() if status.job else dat_path.stem[:3].upper()
    station_id = 0
    year = month = 0
    if lr0001:
        first = lr0001[0]
        try:
            station_id = int(first[0:3].strip())
            month = int(first[4:6].strip())
            year = int(first[7:11].strip())
        except ValueError:
            pass
    if not year or not month:
        raise DataExportError(f"{dat_path.name}: LR0001 station month/year is missing or invalid.")
    latitude, longitude = first_lr0004_lat_lon(blocks)
    return StationJobMetadata(
        station_id=station_id,
        event_label=station,
        station_name=station,
        source_id=-999,
        author_id=-999,
        author_name="",
        year=year,
        month=month,
        pangaea_reference_id=-999,
        latitude=latitude,
        longitude=longitude,
    )


def rename_rows(rows: list[dict[str, object]], headers: dict[str, str]) -> list[dict[str, object]]:
    renamed = []
    for row in rows:
        renamed.append({target: row.get(source) for source, target in headers.items() if source in row})
    return shrink_empty_columns(renamed)


def shrink_empty_columns(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    if not rows:
        return rows
    keep = {
        key
        for row in rows
        for key, value in row.items()
        if key == "Date/Time" or value not in {None, ""}
    }
    return [{key: value for key, value in row.items() if key in keep} for row in rows]


def write_rows_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_record_export(
    export_dir: Path,
    station: str,
    yyyy_mm: str,
    record: str,
    label: str,
    rows: list[dict[str, object]],
) -> Path | None:
    rows = shrink_empty_columns(rows)
    if not rows:
        return None
    path = export_dir / f"{station}_{yyyy_mm}_{record}_{label}.csv"
    write_rows_csv(path, rows)
    return path


def generate_data_exports_for_status(status: JobStatus, run_root: Path) -> tuple[list[Path], list[str]]:
    """Generate separate readable CSVs for supported data logical records."""

    if not status.dat_path:
        return [], [f"Data export warning: {status.job} has no DAT file yet."]
    dat_path = workflow_path(status.dat_path)
    if not dat_path.exists():
        return [], [f"Data export warning: DAT file not found: {dat_path}"]

    from scripts.bsrn_import_files import (  # noqa: WPS433
        parse_lr0100_rows,
        parse_lr0300_rows,
        parse_lr1100_rows,
        parse_lr1300_rows,
        parse_lr3x30_rows,
    )
    from scripts.bsrn_qc_continue import extract_optional_logical_records  # noqa: WPS433

    blocks = read_blocks(dat_path)
    meta = export_metadata_from_blocks(status, blocks, dat_path)
    export_dir = run_root / "data_exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    warnings: list[str] = []
    station = meta.event_label.upper()
    yyyy_mm = meta.yyyy_mm

    lr0100 = rename_rows(parse_lr0100_rows(blocks, meta), LR0100_HEADERS)
    path = write_record_export(export_dir, station, yyyy_mm, "LR0100", "radiation", lr0100)
    if path is not None:
        outputs.append(path)

    lr0300 = [
        {"Date/Time": timestamp, **row}
        for timestamp, row in sorted(parse_lr0300_rows(blocks, meta).items())
    ]
    path = write_record_export(export_dir, station, yyyy_mm, "LR0300", "upward_radiation", rename_rows(lr0300, LR0300_HEADERS))
    if path is not None:
        outputs.append(path)

    optional_rows = extract_optional_logical_records(
        dat_path,
        meta.year,
        meta.month,
        meta.event_label,
        meta.latitude,
        meta.longitude,
    )
    for record_name, label in (("LR0500", "ultraviolet"), ("LR1000", "synop"), ("LR1200", "ozone")):
        rows = optional_rows.get(record_name, [])
        path = write_record_export(export_dir, station, yyyy_mm, record_name, label, rows)
        if path is not None:
            outputs.append(path)

    path = write_record_export(export_dir, station, yyyy_mm, "LR1100", "radiosonde", rename_rows(parse_lr1100_rows(blocks, meta), LR1100_HEADERS))
    if path is not None:
        outputs.append(path)

    path = write_record_export(export_dir, station, yyyy_mm, "LR1300", "expanded", rename_rows(parse_lr1300_rows(blocks, meta), LR1300_HEADERS))
    if path is not None:
        outputs.append(path)

    for record, height in (("3010", 10), ("3030", 30)):
        rows = parse_lr3x30_rows(blocks, meta, record)
        for row in rows:
            row["Height above ground [m]"] = height
        path = write_record_export(export_dir, station, yyyy_mm, f"LR{record}", f"radiation_{height}m", rows)
        if path is not None:
            outputs.append(path)

    detected_data_records = sorted(
        record
        for record in blocks
        if record.isdigit() and 100 <= int(record) <= 3030 and record not in EXPORT_RECORDS
    )
    for record in detected_data_records:
        warnings.append(f"Data export warning: LR{record} is not yet available as a readable CSV export.")
    if not outputs:
        warnings.append(f"Data export warning: no supported data logical records were exported for {dat_path.name}.")
    return outputs, warnings


def update_status_exports(
    status: JobStatus,
    run_root: Path,
    outputs: list[Path],
    warnings: list[str],
) -> None:
    existing_warnings = [
        warning
        for warning in getattr(status, "data_export_warnings", [])
        if not str(warning).startswith("Data export warning:")
    ]
    status.data_export_warnings = existing_warnings + warnings
    if outputs:
        export_dir = run_root / "data_exports"
        status.data_export_dir = rel_path(export_dir)
        status.data_export_outputs = [rel_path(path) for path in outputs]


def selected_for_export(status: JobStatus, requested_jobs: set[str]) -> bool:
    if not requested_jobs:
        return bool(status.dat_path)
    return status.job in requested_jobs


def run_export(args: argparse.Namespace) -> int:
    status_path = resolve_project_path(args.status)
    if not status_path.exists():
        raise DataExportError(f"Status JSON not found: {status_path}")
    run_root = status_path.parent
    statuses = load_statuses(status_path)
    requested_jobs = set(args.job or [])
    attempted = 0
    for status in statuses:
        if not selected_for_export(status, requested_jobs):
            continue
        attempted += 1
        outputs, warnings = generate_data_exports_for_status(status, run_root)
        update_status_exports(status, run_root, outputs, warnings)
    if requested_jobs and attempted == 0:
        raise DataExportError(f"No matching status rows for: {', '.join(sorted(requested_jobs))}")
    if attempted == 0:
        raise DataExportError("No current status rows have DAT files available for export.")
    save_statuses(status_path, statuses)
    dashboard_path = resolve_project_path(args.dashboard or "dashboard.html")
    write_run_index({"root": run_root}, statuses, dashboard_path=dashboard_path)
    print(f"Data exports: {run_root / 'data_exports'}")
    print(f"Status JSON:  {status_path}")
    print(f"Dashboard:    {dashboard_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", default=str(PROJECT_ROOT / "output" / "current" / "status.json"))
    parser.add_argument("--dashboard", help="Central dashboard path; defaults to BSRN/dashboard.html")
    parser.add_argument("--job", action="append", help="Export only the matching status row job label, e.g. CAB_2025-04")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run_export(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
