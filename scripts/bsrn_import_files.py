#!/usr/bin/env python3
"""Create BSRN PANGAEA import files after QC curator approval."""

from __future__ import annotations

import argparse
import json
import sys
import datetime as dt
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.bsrn_download_check import (  # noqa: E402
    DEFAULT_IDS_DIR,
    JobStatus,
    ToolboxModelAdapter,
    dat_blocks,
    import_gate_status,
    load_parent_id_map,
    load_curator_decisions,
    load_statuses,
    normalize_station_name,
    read_dat_text,
    read_tsv,
    resolve_project_path,
    save_statuses,
    write_run_index,
    workflow_path,
)
from scripts.bsrn_qc_continue import (  # noqa: E402
    extract_optional_logical_records,
    lr1000_station_format,
)
from scripts.bsrn_station_registry import resolve_station_entry  # noqa: E402


DATA_RECORDS_EXCLUDED = {"4000"}
METADATA_RECORDS = {f"{record:04d}" for record in range(1, 10)}
TOOL1_IMPORT_RECORDS = {"0100", "0300", "0500", "1000", "1100", "1200", "1300", "3010", "3030", "3300"}
SUPPORTED_RECORDS = {"0100", "0300", "0500", "1000", "1100", "1200", "1300", "3010", "3030"}
MISSING_SENTINELS = {"", "-9.9", "-99.9", "-999", "-999.0", "-9999", "-9999.0"}


class ImportWorkflowError(Exception):
    pass


@dataclass(frozen=True)
class StationJobMetadata:
    station_id: int
    event_label: str
    station_name: str
    source_id: int
    author_id: int
    author_name: str
    year: int
    month: int
    pangaea_reference_id: int
    latitude: float
    longitude: float

    @property
    def yyyy_mm(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


def write_import_exception_log(import_root: Path, status: JobStatus, exc: BaseException) -> None:
    logs_dir = import_root.parent / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    safe_job = "".join(char if char.isalnum() or char in "._-" else "_" for char in status.job)
    path = logs_dir / f"{safe_job}_import.traceback.txt"
    path.write_text("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)), encoding="utf-8")


def rel_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def is_numeric_reference_id(value: object) -> bool:
    return isinstance(value, int) or (isinstance(value, str) and value.isdigit())


def eligible_for_import(status: JobStatus, decisions: dict) -> tuple[bool, str]:
    gate_kind, gate_label, gate_detail = import_gate_status(status, decisions)
    if gate_kind != "ready":
        detail = f" {gate_detail}" if gate_detail and gate_detail != gate_label else ""
        return False, f"Import generation blocked: {gate_label}.{detail}"
    if not is_numeric_reference_id(status.pangaea_reference_id):
        return False, "Import generation blocked: numeric PANGAEA reference ID is required."
    if not status.dat_path:
        return False, "Import generation blocked: DAT path is missing from status.json."
    return True, gate_label


def load_station_job_metadata(status: JobStatus, ids_dir: Path) -> StationJobMetadata:
    dat_path = workflow_path(status.dat_path)
    if not dat_path.exists():
        raise ImportWorkflowError(f"DAT file not found: {dat_path}")

    blocks = dat_blocks(read_dat_text(dat_path).splitlines())
    lr0001 = blocks.get("0001")
    lr0002 = blocks.get("0002")
    if not lr0001:
        raise ImportWorkflowError(f"{dat_path.name}: LR0001 is missing.")
    if not lr0002 or len(lr0002) < 2:
        raise ImportWorkflowError(f"{dat_path.name}: LR0002 responsible scientist row is missing.")

    first = lr0001[0]
    station_id = int(first[0:3].strip())
    month = int(first[4:6].strip())
    year = int(first[7:11].strip())
    author_name = lr0002[1][0:38].strip()

    model = ToolboxModelAdapter(ids_dir=ids_dir)
    lookup = model.get_bsrn_id_system()
    station_entry = resolve_station_entry(
        station_id,
        event_hint=status.job.split("_", 1)[0].upper() if status.job else dat_path.stem[:3].upper(),
        ids_file=ids_dir / "BSRN_IDs.txt",
    )
    if station_entry is not None:
        event_label = station_entry.event_label
        station_name = normalize_station_name(station_entry.name)
        source_id = station_entry.pangaea_id if station_entry.pangaea_id is not None else -999
    else:
        event_label = lookup.get_data("station", str(station_id), "event")
        station_name = normalize_station_name(lookup.get_data("station", str(station_id), "name"))
        source_id = int(lookup.get_data("station", str(station_id), "pangaea_id") or "-999")
    author_id = int(lookup.get_data("staff", author_name, "pangaea_id") or "-999")
    if not event_label or not station_name or source_id < 0:
        raise ImportWorkflowError(f"{dat_path.name}: station {station_id} is incomplete in BSRN_IDs.txt.")
    if author_id < 0:
        raise ImportWorkflowError(f"{dat_path.name}: responsible scientist {author_name!r} has no PANGAEA staff ID.")
    if int(status.pangaea_reference_id or -1) < 1:
        raise ImportWorkflowError(f"{dat_path.name}: numeric PANGAEA reference ID is missing.")

    latitude, longitude = first_lr0004_lat_lon(blocks)
    read_generated_metadata_outputs(workflow_path(status.metadata_dir), event_label, year, month)
    return StationJobMetadata(
        station_id=station_id,
        event_label=event_label,
        station_name=station_name,
        source_id=source_id,
        author_id=author_id,
        author_name=author_name,
        year=year,
        month=month,
        pangaea_reference_id=int(status.pangaea_reference_id or -1),
        latitude=latitude,
        longitude=longitude,
    )


def read_generated_metadata_outputs(metadata_dir: Path, event_label: str, year: int, month: int) -> None:
    if not metadata_dir.exists():
        raise ImportWorkflowError(f"Metadata directory not found: {metadata_dir}")
    prefix = f"{event_label}_{year:04d}-{month:02d}_"
    generated = sorted(
        path
        for path in metadata_dir.glob(f"{prefix}[0-9][0-9][0-9][0-9].txt")
        if path.stem.rsplit("_", 1)[-1] in METADATA_RECORDS
    )
    if not generated:
        raise ImportWorkflowError(f"Generated metadata outputs were not found for {event_label}_{year:04d}-{month:02d}.")
    for path in generated:
        read_tsv(path)


def detected_data_records(dat_path: Path) -> list[str]:
    records = set(dat_blocks(read_dat_text(dat_path).splitlines()))
    data_records = records - METADATA_RECORDS
    return sorted(record for record in data_records if record in TOOL1_IMPORT_RECORDS or record.isdigit())


def unsupported_record_messages(records: list[str]) -> list[str]:
    messages = []
    for record in records:
        if record in DATA_RECORDS_EXCLUDED:
            messages.append(f"LR{record} is explicitly excluded from this workflow and is not generated as a data import file.")
        elif record not in TOOL1_IMPORT_RECORDS:
            messages.append(f"LR{record} is not a Tool 1 data import converter target for this workflow.")
        elif record not in SUPPORTED_RECORDS:
            messages.append(f"LR{record} data import conversion is not ported in the current workflow.")
    return messages


def output_name(meta: StationJobMetadata, token: str, suffix: str = ".txt") -> str:
    clean_token = token.strip("_")
    return f"{meta.event_label}_{meta.yyyy_mm}_{clean_token}{suffix}"


def parameter_entry(parameter_id: int, pi_id: int, method_id: int, fmt: str = "", comment: str = "") -> dict[str, object]:
    entry: dict[str, object] = {"ID": parameter_id, "PI_ID": pi_id, "MethodID": method_id}
    if fmt:
        entry["Format"] = fmt
    if comment:
        entry["Comment"] = comment
    return entry


def default_geocode_parameters(meta: StationJobMetadata) -> list[dict[str, object]]:
    return [
        parameter_entry(1599, meta.author_id, 43, "yyyy-MM-dd'T'HH:mm"),
        parameter_entry(56349, meta.author_id, 43, "###0"),
    ]


def jsonish(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def format_parameter(entry: dict[str, object]) -> str:
    parts = [f'"ID": {entry["ID"]}', f'"PI_ID": {entry["PI_ID"]}', f'"MethodID": {entry["MethodID"]}']
    if "Format" in entry:
        parts.append(f'"Format": {jsonish(entry["Format"])}')
    if "Comment" in entry:
        parts.append(f'"Comment": {jsonish(entry["Comment"])}')
    return "    { " + ", ".join(parts) + " }"


def title_station(station_name: str) -> str:
    return station_name if station_name.endswith("Station") else f"station {station_name}"


def write_metaheader(
    handle,
    meta: StationJobMetadata,
    title_text: str,
    export_token: str,
    parameters: list[dict[str, object]],
    dataset_comment: str = "",
    parent_id: int | None = None,
) -> None:
    handle.write(f"// METAHEADER - BSRN data import at {datetime.now().strftime('%Y-%m-%dT%H:%M')}\n")
    handle.write("{\n")
    handle.write('  "ReferenceIDs": [\n')
    handle.write(f'    {{ "ID": {meta.pangaea_reference_id}, "RelationTypeID": 13 }} ],\n')
    handle.write(f'  "AuthorIDs": [ {meta.author_id} ],\n')
    handle.write(f'  "SourceID": {meta.source_id},\n')
    handle.write(f'  "Title": {jsonish(f"{title_text} {title_station(meta.station_name)} ({meta.yyyy_mm})")},\n')
    if parent_id is not None:
        handle.write(f'  "ParentID": {parent_id},\n')
    handle.write(f'  "ExportFilename": {jsonish(f"{meta.event_label}_{export_token}_{meta.yyyy_mm}")},\n')
    handle.write(f'  "EventLabel": {jsonish(meta.event_label)},\n')
    handle.write('  "ParameterIDs": [ \n')
    formatted = [format_parameter(parameter) for parameter in parameters]
    for index, line in enumerate(formatted):
        suffix = "," if index < len(formatted) - 1 else " ],"
        handle.write(f"{line}{suffix}\n")
    if dataset_comment:
        handle.write(f'  "DataSetComment": {jsonish(dataset_comment)},\n')
    handle.write('  "ProjectIDs": [ 4094 ],\n')
    handle.write('  "TopologicTypeID": 8,\n')
    handle.write('  "StatusID": 4,\n')
    handle.write('  "CurationLevelID": 30,\n')
    handle.write('  "LicenseID": 107,\n')
    handle.write('  "UserIDs": [ 1144 ],\n')
    handle.write('  "LoginID": 1\n')
    handle.write("}\n")
    handle.write("// METAHEADER END\n")


def geocode_header(import_mode: bool = True) -> str:
    return "1599\t56349" if import_mode else "Date/Time\tHeight above ground [m]"


def geocode_data_row(date_time: str, height_m: int | float = 2) -> str:
    return f"{date_time}\t{format_value(height_m)}"


def format_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return "" if value.strip() in MISSING_SENTINELS else value.strip()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def format_field_value(value: object, fmt: str) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return "" if value.strip() in MISSING_SENTINELS else value.strip()
    if not isinstance(value, (int, float)):
        return str(value)
    if "." in fmt:
        decimals = len(fmt.rsplit(".", 1)[1])
        text = f"{float(value):.{decimals}f}"
        if "0000" in fmt:
            text = text.rstrip("0").rstrip(".")
        return text
    if fmt:
        return str(int(round(float(value))))
    return format_value(value)


def qmid(line: str, start: int, length: int) -> str:
    return line[start : start + length].strip()


def as_float(text: str) -> float | None:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if value in {-999.0, -9999.0, -99.9, -9.9}:
        return None
    return value


def as_int(text: str) -> int | None:
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def make_datetime(year: int, month: int, day: int, minute: int) -> str:
    return (dt.datetime(year, month, day) + dt.timedelta(minutes=minute)).isoformat(timespec="minutes")


def blocks_for_status(status: JobStatus) -> dict[str, list[str]]:
    dat_path = workflow_path(status.dat_path)
    return dat_blocks(read_dat_text(dat_path).splitlines())


def first_lr0004_lat_lon(blocks: dict[str, list[str]]) -> tuple[float, float]:
    lr0004 = blocks.get("0004") or []
    for line in lr0004:
        parts = line.split()
        if len(parts) >= 2:
            try:
                return float(parts[0]) - 90.0, float(parts[1]) - 180.0
            except ValueError:
                continue
    return 0.0, 0.0


def parse_lr0009(blocks: dict[str, list[str]], year: int, month: int) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line in blocks.get("0009") or []:
        parts = line.split()
        parameter_id = method_id = day = hour = minute = None
        if len(parts) >= 5:
            day = as_int(parts[0])
            hour = as_int(parts[1])
            minute = as_int(parts[2])
            parameter_id = as_int(parts[3])
            method_id = as_int(parts[4])
        if parameter_id is None or method_id is None:
            parameter_id = as_int(qmid(line, 10, 9))
            method_id = as_int(qmid(line, 20, 5))
        if parameter_id is None or method_id is None:
            continue
        day = day if day is not None else as_int(qmid(line, 1, 2)) or 1
        hour = hour if hour is not None else as_int(qmid(line, 4, 2)) or 0
        minute = minute if minute is not None else as_int(qmid(line, 7, 2)) or 0
        if day < 1:
            day = 1
        if hour < 0:
            hour = 0
        if minute < 0:
            minute = 0
        records.append(
            {
                "parameter_id": parameter_id,
                "wrmc_id": method_id,
                "datetime": dt.datetime(year, month, day, hour, minute).isoformat(timespec="minutes"),
            }
        )
    return records


class MethodLookup:
    def __init__(self, ids_file: Path) -> None:
        self.methods: dict[tuple[int, int], int] = {}
        self.text_methods: dict[str, int] = {}
        section = ""
        for raw in ids_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line.strip("[]").lower()
                continue
            parts = raw.split("\t")
            if section == "methods" and len(parts) >= 2:
                key = parts[0].strip()
                value = as_int(parts[1].strip())
                if value is None:
                    continue
                pieces = [piece.strip() for piece in key.split(",")]
                if len(pieces) >= 3:
                    station_id = as_int(pieces[0])
                    wrmc_text = pieces[-1].replace("WRMC No.", "").strip()
                    wrmc_id = as_int(wrmc_text)
                    if station_id is not None and wrmc_id is not None:
                        self.methods[(station_id, wrmc_id)] = value
            elif section in {"radiosonde", "expanded", "ozonesonde"} and len(parts) >= 2:
                value = as_int(parts[1].strip())
                if value is not None:
                    self.text_methods[parts[0].strip()] = value

    def method_id(self, station_id: int, wrmc_id: int, default: int = 43) -> int:
        return self.methods.get((station_id, wrmc_id), default)

    def method_text_id(self, text: str, default: int = -999) -> int:
        return self.text_methods.get(text.strip(), default)


def method_for_quantity(meta: StationJobMetadata, lr0009: list[dict[str, object]], lookup: MethodLookup, quantity: int) -> tuple[int, str]:
    matches = [record for record in lr0009 if record["parameter_id"] == quantity]
    if not matches:
        return 43, ""
    first = matches[0]
    last = matches[-1]
    comment = ""
    if len(matches) > 1:
        comment = f"Changed to WRMC No. {last['wrmc_id']} at {last['datetime']}"
    return lookup.method_id(meta.station_id, int(first["wrmc_id"])), comment


def declared_lr0001_parameters(blocks: dict[str, list[str]]) -> set[int]:
    parameters: set[int] = set()
    for line in blocks.get("0001", []):
        for token in line.split():
            value = as_int(token)
            if value is not None:
                parameters.add(value)
    return parameters


def active_fields(rows: list[dict[str, object]], fields: list[tuple[str, int, str, int, str]]) -> list[tuple[str, int, str, int, str]]:
    return [field for field in fields if any(row.get(field[0]) is not None for row in rows)]


def write_import_table(
    path: Path,
    meta: StationJobMetadata,
    title: str,
    token: str,
    fields: list[tuple[str, int, str, int, str]],
    rows: list[dict[str, object]],
    include_height: bool = True,
    dataset_comment: str = "",
    skip_empty_data_rows: bool = False,
    parent_id: int | None = None,
) -> None:
    parameters = [parameter_entry(1599, meta.author_id, 43, "yyyy-MM-dd'T'HH:mm")]
    if include_height:
        parameters.append(parameter_entry(56349, meta.author_id, 43, "###0"))
    for _label, parameter_id, fmt, method_id, comment in fields:
        parameters.append(parameter_entry(parameter_id, meta.author_id, method_id, fmt, comment))
    with path.open("w", encoding="utf-8", newline="") as handle:
        write_metaheader(handle, meta, title, token, parameters, dataset_comment=dataset_comment, parent_id=parent_id)
        header = ["1599"]
        if include_height:
            header.append("56349")
        header.extend(str(field[1]) for field in fields)
        handle.write("\t".join(header) + "\n")
        for row in rows:
            field_values = [format_field_value(row.get(field[0]), field[2]) for field in fields]
            if skip_empty_data_rows and all(value == "" for value in field_values):
                continue
            values = [str(row["Date/Time"])]
            if include_height:
                values.append(format_value(row.get("Height", 2)))
            values.extend(field_values)
            if any(value != "" for value in values[1:]):
                handle.write("\t".join(values) + "\n")


def parse_lr0100_rows(blocks: dict[str, list[str]], meta: StationJobMetadata) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    lines = [line.strip() for line in blocks.get("0100", []) if line.strip()]
    for index in range(0, len(lines) - 1, 2):
        p1 = lines[index].split()
        p2 = lines[index + 1].split()
        if len(p1) < 10 or len(p2) < 11:
            continue
        day = as_int(p1[0])
        minute = as_int(p1[1])
        if day is None or minute is None:
            continue
        rows.append(
            {
                "Date/Time": make_datetime(meta.year, meta.month, day, minute),
                "Height": 2,
                "SWD": as_float(p1[2]),
                "SWD_sd": as_float(p1[3]),
                "SWD_min": as_float(p1[4]),
                "SWD_max": as_float(p1[5]),
                "DIR": as_float(p1[6]),
                "DIR_sd": as_float(p1[7]),
                "DIR_min": as_float(p1[8]),
                "DIR_max": as_float(p1[9]),
                "DIF": as_float(p2[0]),
                "DIF_sd": as_float(p2[1]),
                "DIF_min": as_float(p2[2]),
                "DIF_max": as_float(p2[3]),
                "LWD": as_float(p2[4]),
                "LWD_sd": as_float(p2[5]),
                "LWD_min": as_float(p2[6]),
                "LWD_max": as_float(p2[7]),
                "T2": as_float(p2[8]),
                "RH": as_float(p2[9]),
                "P": as_float(p2[10]),
            }
        )
    return rows


def parse_lr0300_rows(blocks: dict[str, list[str]], meta: StationJobMetadata) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    for line in blocks.get("0300", []):
        parts = line.split()
        if len(parts) < 10:
            continue
        day = as_int(parts[0])
        minute = as_int(parts[1])
        if day is None or minute is None:
            continue
        timestamp = make_datetime(meta.year, meta.month, day, minute)
        rows[timestamp] = {
            "SWU": as_float(parts[2]),
            "SWU_sd": as_float(parts[3]),
            "SWU_min": as_float(parts[4]),
            "SWU_max": as_float(parts[5]),
            "LWU": as_float(parts[6]),
            "LWU_sd": as_float(parts[7]),
            "LWU_min": as_float(parts[8]),
            "LWU_max": as_float(parts[9]),
        }
    return rows


def generate_lr0100_0300(status: JobStatus, meta: StationJobMetadata, blocks: dict[str, list[str]], out_dir: Path, ids_dir: Path) -> Path | None:
    rows = parse_lr0100_rows(blocks, meta)
    if not rows:
        return None
    lr0300_rows = parse_lr0300_rows(blocks, meta)
    for row in rows:
        row.update(lr0300_rows.get(str(row["Date/Time"]), {}))
    lr0009 = parse_lr0009(blocks, meta.year, meta.month)
    lookup = MethodLookup(ids_dir / "BSRN_IDs.txt")
    specs = [
        (2, [("SWD", 31460, "###0"), ("SWD_sd", 55905, "###0.0"), ("SWD_min", 55906, "###0"), ("SWD_max", 55907, "###0")]),
        (3, [("DIR", 45294, "###0"), ("DIR_sd", 55962, "###0.0"), ("DIR_min", 55957, "###0"), ("DIR_max", 55958, "###0")]),
        (4, [("DIF", 45293, "###0"), ("DIF_sd", 55961, "###0.0"), ("DIF_min", 55959, "###0"), ("DIF_max", 55960, "###0")]),
        (5, [("LWD", 45298, "###0"), ("LWD_sd", 55908, "###0.0"), ("LWD_min", 55909, "###0"), ("LWD_max", 55910, "###0")]),
        (131, [("SWU", 55911, "###0"), ("SWU_sd", 55912, "###0.0"), ("SWU_min", 55913, "###0"), ("SWU_max", 55914, "###0")]),
        (132, [("LWU", 45299, "###0"), ("LWU_sd", 55915, "###0.0"), ("LWU_min", 55916, "###0"), ("LWU_max", 55917, "###0")]),
    ]
    fields: list[tuple[str, int, str, int, str]] = []
    for quantity, group in specs:
        method_id, comment = method_for_quantity(meta, lr0009, lookup, quantity)
        fields.extend((label, parameter_id, fmt, method_id, comment) for label, parameter_id, fmt in group)
    fields.extend(
        field
        for quantity, field in [
            (21, ("T2", 48820, "###0.0", 4722, "")),
            (22, ("RH", 2219, "###0.0", 5039, "")),
            (23, ("P", 48823, "###0.0", 359, "")),
        ]
        if quantity in declared_lr0001_parameters(blocks)
    )
    fields = active_fields(rows, fields)
    token = "0100+0300" if "0300" in blocks else "0100"
    path = out_dir / output_name(meta, f"{token}_imp")
    title = "Basic and other measurements of radiation at" if "0300" in blocks else "Basic measurements of radiation at"
    parent_entry = load_parent_id_map().get(meta.event_label.upper())
    parent_id = parent_entry[0] if parent_entry is not None else None
    write_import_table(path, meta, title, "radiation", fields, rows, skip_empty_data_rows=True, parent_id=parent_id)
    return path


def optional_rows(status: JobStatus, meta: StationJobMetadata) -> dict[str, list[dict[str, object]]]:
    return extract_optional_logical_records(
        workflow_path(status.dat_path),
        meta.year,
        meta.month,
        meta.event_label,
        meta.latitude,
        meta.longitude,
    )


def generate_lr0500(meta: StationJobMetadata, rows: list[dict[str, object]], blocks: dict[str, list[str]], out_dir: Path, ids_dir: Path) -> Path | None:
    if not rows:
        return None
    lr0009 = parse_lr0009(blocks, meta.year, meta.month)
    lookup = MethodLookup(ids_dir / "BSRN_IDs.txt")
    uv_b_format = "#0.0000" if meta.event_label in {"BON", "BOS", "DRA", "FPE", "GCR", "MAN", "NAU", "PAY", "PSU", "SXF", "TOR"} else "###0.0"
    specs = [
        (121, "###0.0", [
            ("UV-a global [W/m**2]", 55922), ("UV-a global, standard deviation [W/m**2]", 55923),
            ("UV-a global, minimum [W/m**2]", 55924), ("UV-a global, maximum [W/m**2]", 55925),
        ]),
        (122, uv_b_format, [
            ("UV-b direct [W/m**2]", 55926), ("UV-b direct, standard deviation [W/m**2]", 55927),
            ("UV-b direct, minimum [W/m**2]", 55928), ("UV-b direct, maximum [W/m**2]", 55929),
        ]),
        (123, uv_b_format, [
            ("UV-b global [W/m**2]", 55930), ("UV-b global, standard deviation [W/m**2]", 55931),
            ("UV-b global, minimum [W/m**2]", 55932), ("UV-b global, maximum [W/m**2]", 55933),
        ]),
        (124, uv_b_format, [
            ("UV-b diffuse [W/m**2]", 55934), ("UV-b diffuse, standard deviation [W/m**2]", 55935),
            ("UV-b diffuse, minimum [W/m**2]", 55936), ("UV-b diffuse, maximum [W/m**2]", 55937),
        ]),
        (125, "###0.0", [
            ("UV upward reflected [W/m**2]", 55938), ("UV upward reflected, standard deviation [W/m**2]", 55939),
            ("UV upward reflected, minimum [W/m**2]", 55940), ("UV upward reflected, maximum [W/m**2]", 55941),
        ]),
    ]
    fields: list[tuple[str, int, str, int, str]] = []
    for quantity, fmt, group in specs:
        method_id, comment = method_for_quantity(meta, lr0009, lookup, quantity)
        fields.extend((label, parameter_id, fmt, method_id, comment) for label, parameter_id in group)
    fields = active_fields(rows, fields)
    path = out_dir / output_name(meta, "0500_imp")
    write_import_table(path, meta, "Ultra-violet measurements from", "Ultra-violet", fields, rows, skip_empty_data_rows=True)
    return path


SYNOP_FIELDS: list[tuple[str, int, str, int, str]] = [
    ("Cloud base height [code]", 45259, "#0", 5036, ""),
    ("Horizontal visibility [code]", 45260, "#0", 5037, ""),
    ("Wind direction [deg]", 2221, "###0", 5038, ""),
    ("Wind speed [m/sec]", 18906, "#0.0", 5038, ""),
    ("Temperature, air [deg C]", 4610, "###0.0", 4722, ""),
    ("Dew/frost point [deg C]", 4611, "###0.0", 5039, ""),
    ("Station Pressure [hPa]", 48823, "###0", 359, ""),
    ("Pressure, atmospheric [hPa]", 2224, "###0.0", 359, ""),
    ("Characteristic of barometric tendency [code]", 45311, "#0", 359, ""),
    ("Amount of barometric tendency [hPa]", 45312, "#0", 359, ""),
    ("Present weather [code]", 45261, "#0", 530, ""),
    ("Past weather1 [code]", 45262, "#0", 530, ""),
    ("Past weather2 [code]", 45263, "#0", 530, ""),
    ("Low cloud [code]", 45264, "#0", 530, ""),
    ("Middle cloud [code]", 45265, "#0", 530, ""),
    ("High cloud [code]", 45266, "#0", 530, ""),
    ("Total cloud amount [code]", 45267, "#0", 530, ""),
    ("Total cloud amount", 45267, "#0", 530, ""),
    ("Low/middle cloud amount [code]", 45268, "#0", 530, ""),
    ("Temperature, air, maximum [deg C]", 5151, "###0.0", 4722, ""),
    ("Temperature, air, minimum [deg C]", 5150, "###0.0", 4722, ""),
    ("Present blowing snow [code]", 45307, "#0", 530, ""),
    ("Past blowing snow [code]", 45308, "#0", 530, ""),
    ("Whiteout yes/no [y/n]", 45309, "", 530, ""),
    ("Ns 1 [code]", 57649, "#0", 530, ""), ("C 1 [code]", 57652, "#0", 530, ""), ("hshs 1 [code]", 57655, "#0", 5037, ""),
    ("Ns 2 [code]", 57650, "#0", 530, ""), ("C 2 [code]", 57653, "#0", 530, ""), ("hshs 2 [code]", 57656, "#0", 5037, ""),
    ("Ns 3 [code]", 57651, "#0", 530, ""), ("C 3 [code]", 57654, "#0", 530, ""), ("hshs 3 [code]", 57657, "#0", 5037, ""),
    ("FM 12-XII Ext. SYNOP code", 50007, "", 43, ""),
]


def generate_lr1000(meta: StationJobMetadata, rows: list[dict[str, object]], out_dir: Path) -> Path | None:
    if not rows:
        return None
    fields = active_fields(rows, SYNOP_FIELDS)
    if len(fields) > 1:
        fields = [field for field in fields if field[0] != "FM 12-XII Ext. SYNOP code"]
    if lr1000_station_format(meta.event_label) == 2:
        fields = normalize_lr1000_format2_fields(fields)
    if not fields:
        return None
    # Preserve the Tool 1 sea-level pressure comment for decoded SYNOP formats.
    fields = [
        (
            label,
            pid,
            fmt,
            method,
            "Station pressure reduced to sea level"
            if label == "Pressure, atmospheric [hPa]" and lr1000_station_format(meta.event_label) in {2, 6}
            else comment,
        )
        for label, pid, fmt, method, comment in fields
    ]
    path = out_dir / output_name(meta, "1000_imp")
    write_import_table(path, meta, "Meteorological synoptical observations from", "SYNOP", fields, rows, include_height=False)
    return path


def normalize_lr1000_format2_fields(fields: list[tuple[str, int, str, int, str]]) -> list[tuple[str, int, str, int, str]]:
    """Match Tool 1 ordering and formats for fixed-width SYNOP format 2 stations."""

    order = {
        "Temperature, air [deg C]": 0,
        "Pressure, atmospheric [hPa]": 1,
        "Dew/frost point [deg C]": 2,
        "Wind direction [deg]": 3,
        "Wind speed [m/sec]": 4,
        "Horizontal visibility [code]": 5,
    }
    normalized: list[tuple[str, int, str, int, str]] = []
    for label, pid, fmt, method, comment in fields:
        if label == "Pressure, atmospheric [hPa]":
            fmt = "###0"
        elif label == "Wind speed [m/sec]":
            fmt = "#0"
        normalized.append((label, pid, fmt, method, comment))
    return sorted(normalized, key=lambda field: order.get(field[0], len(order)))


def lr0005_radiosonde_metadata(blocks: dict[str, list[str]], meta: StationJobMetadata, ids_dir: Path) -> tuple[int, str]:
    lines = [line.rstrip("\n") for line in blocks.get("0005", [])]
    if not lines or not lines[0].strip().endswith("Y") or len(lines) < 2:
        return 43, ""
    instrument = lines[1]
    identification = qmid(instrument, 0, 30)
    serial = qmid(instrument, 73, 5)
    if serial:
        identification = f"{identification}, {serial}"
    method_id = MethodLookup(ids_dir / "BSRN_IDs.txt").method_text_id(identification, default=-999)
    location = qmid(instrument, 30, 25)
    distance = as_int(qmid(instrument, 57, 3)) or 0
    remarks = lines[2].strip() if len(lines) >= 3 else ""
    remarks = remarks.replace("no remarks", "").replace("XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX", "").replace("XXX", "").strip()
    pieces = []
    if distance > 0:
        pieces.append(f"Start location: {location}; Distance from radiation site: {distance} km")
    if remarks:
        pieces.append(remarks)
    if meta.event_label in {"E13", "BIL"}:
        pieces.append("The radiosonde measurements from station Southern Great Plains are used for Billings and E13")
    return method_id, "; ".join(pieces)


def parse_lr1100_rows(blocks: dict[str, list[str]], meta: StationJobMetadata) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in blocks.get("1100", []):
        parts = line.split()
        if len(parts) >= 10:
            day = as_int(parts[0])
            minute = as_int(parts[1])
            if day is None or minute is None:
                continue
            row = {
                "Date/Time": make_datetime(meta.year, meta.month, day, minute),
                "Altitude": as_float(parts[4]),
                "Pressure": None if parts[3] == "-999" else as_float(parts[3]),
                "Temperature": lr1100_number(parts[5], "-99.9"),
                "Dew/frost point": lr1100_number(parts[6], "-999.9"),
                "Wind direction": None if parts[7] == "-99" else as_float(parts[7]),
                "Wind speed": None if parts[8] == "-99" else as_float(parts[8]),
                "Ozone": None if parts[9] == "-9.9" else as_float(parts[9]),
            }
        else:
            day = as_int(qmid(line, 1, 2))
            minute = as_int(qmid(line, 4, 4))
            if day is None or minute is None:
                continue
            row = {
                "Date/Time": make_datetime(meta.year, meta.month, day, minute),
                "Altitude": as_float(qmid(line, 21, 5)),
                "Pressure": as_float(qmid(line, 16, 4)) if qmid(line, 16, 4) != "-999" else None,
                "Temperature": lr1100_number(qmid(line, 27, 5), "-99.9"),
                "Dew/frost point": lr1100_number(qmid(line, 33, 6), "-999.9"),
                "Wind direction": as_float(qmid(line, 40, 3)) if qmid(line, 40, 3) != "-99" else None,
                "Wind speed": as_float(qmid(line, 44, 3)) if qmid(line, 44, 3) != "-99" else None,
                "Ozone": as_float(qmid(line, 48, 4)) if qmid(line, 48, 4) != "-9.9" else None,
            }
        if row["Altitude"] is not None and any(row.get(key) is not None for key in row if key not in {"Date/Time", "Altitude"}):
            rows.append(row)
    return rows


def lr1100_number(text: str, missing_value: str) -> float | None:
    if text.strip() == missing_value:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def generate_lr1100(meta: StationJobMetadata, blocks: dict[str, list[str]], out_dir: Path, ids_dir: Path) -> Path | None:
    rows = parse_lr1100_rows(blocks, meta)
    if not rows:
        return None
    method_id, dataset_comment = lr0005_radiosonde_metadata(blocks, meta, ids_dir)
    fields = active_fields(
        rows,
        [
            ("Pressure", 49378, "#####0", method_id, ""),
            ("Temperature", 4610, "##0.0", method_id, ""),
            ("Dew/frost point", 4611, "##0.0", method_id, ""),
            ("Wind direction", 2221, "##0", method_id, ""),
            ("Wind speed", 18906, "##0", method_id, ""),
            ("Ozone", 45289, "##0.0", method_id, ""),
        ],
    )
    if not fields:
        return None
    parameters = [
        parameter_entry(1599, meta.author_id, 43, "yyyy-MM-dd'T'HH:mm"),
        parameter_entry(4607, meta.author_id, 43, "####0"),
        *(parameter_entry(pid, meta.author_id, method, fmt, comment) for _label, pid, fmt, method, comment in fields),
    ]
    path = out_dir / output_name(meta, "1100_imp")
    title_station_name = "Southern Great Plains" if meta.event_label in {"E13", "BIL"} else meta.station_name
    title_meta = StationJobMetadata(**{**meta.__dict__, "station_name": title_station_name})
    with path.open("w", encoding="utf-8", newline="") as handle:
        write_metaheader(handle, title_meta, "Radiosonde measurements from", "radiosonde", parameters, dataset_comment=dataset_comment)
        handle.write("\t".join(["1599", "4607", *(str(field[1]) for field in fields)]) + "\n")
        for row in rows:
            values = [str(row["Date/Time"]), format_value(row.get("Altitude"))]
            values.extend(format_field_value(row.get(field[0]), field[2]) for field in fields)
            handle.write("\t".join(values) + "\n")
    return path


def lr1200_dataset_comment(blocks: dict[str, list[str]]) -> str:
    lr0006 = [line for line in blocks.get("0006", []) if line.strip()]
    if not lr0006 or not lr0006[0].strip().endswith("Y"):
        return ""
    if len(lr0006) < 2:
        return ""
    instrument = lr0006[1]
    location = instrument[30:55].strip()
    distance = instrument[57:60].strip()
    remarks = lr0006[2].strip() if len(lr0006) >= 3 else ""
    remarks = remarks.replace("no remarks", "").replace("XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX", "").replace("XXX", "").strip()
    location = location.replace("CH-7050 Arosa", "Arosa, Switzerland").replace("Tamanrasset", "Tamanrasset, Algeria")
    pieces = []
    if distance and distance != "0":
        pieces.append(f"Location: {location}; Distance from radiation site: {distance} km")
    if remarks:
        pieces.append(remarks)
    return "; ".join(pieces)


def generate_lr1200(meta: StationJobMetadata, rows: list[dict[str, object]], blocks: dict[str, list[str]], out_dir: Path) -> Path | None:
    if not rows:
        return None
    fields = active_fields(rows, [("Ozone total [DU]", 49377, "###0", 43, "")])
    if not fields:
        return None
    path = out_dir / output_name(meta, "1200_imp")
    write_import_table(path, meta, "Ozone measurements from", "Ozone", fields, rows, include_height=False, dataset_comment=lr1200_dataset_comment(blocks))
    return path


def lr0007_expanded_methods(blocks: dict[str, list[str]], ids_dir: Path) -> tuple[int, int, int, int]:
    lookup = MethodLookup(ids_dir / "BSRN_IDs.txt")
    lines = [line.strip() for line in blocks.get("0007", [])]
    if len(lines) < 5:
        return 43, 43, 43, 43
    total_cloud = lookup.method_text_id(lines[1], default=-999) if lines[1] != "XXX" else 43
    cloud_base = lookup.method_text_id(lines[2], default=-999) if lines[2] != "XXX" else 43
    liquid_water = lookup.method_text_id(lines[3], default=43) if lines[3] != "XXX" else 43
    spectral_aod = lookup.method_text_id(lines[4], default=43) if lines[4] != "XXX" else 43
    return total_cloud, cloud_base, liquid_water, spectral_aod


def parse_lr1300_rows(blocks: dict[str, list[str]], meta: StationJobMetadata) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []

    def valid_above(text: str, threshold: float) -> float | None:
        value = as_float(text)
        return value if value is not None and value > threshold else None

    for line in blocks.get("1300", []):
        parts = line.split()
        if len(parts) >= 8:
            day = as_int(parts[0])
            minute = as_int(parts[1])
            if day is None or minute is None:
                continue
            row = {
                "Date/Time": make_datetime(meta.year, meta.month, day, minute),
                "Total cloud amount": valid_above(parts[2], -9),
                "Cloud base height": valid_above(parts[3], -9),
                "Cloud liquid water": valid_above(parts[4], -9.8),
                "AOD wavelength 1": valid_above(parts[5], -9.998),
                "AOD wavelength 2": valid_above(parts[6], -9.998),
                "AOD wavelength 3": valid_above(parts[7], -9.998),
            }
        else:
            day = as_int(qmid(line, 1, 2))
            minute = as_int(qmid(line, 4, 4))
            if day is None or minute is None:
                continue
            total = as_float(qmid(line, 9, 4))
            base = as_float(qmid(line, 14, 5))
            liquid = as_float(qmid(line, 20, 5))
            aod1 = as_float(qmid(line, 28, 6))
            aod2 = as_float(qmid(line, 35, 6))
            aod3 = as_float(qmid(line, 42, 6))
            row = {
                "Date/Time": make_datetime(meta.year, meta.month, day, minute),
                "Total cloud amount": total if total is not None and total > -9 else None,
                "Cloud base height": base if base is not None and base > -9 else None,
                "Cloud liquid water": liquid if liquid is not None and liquid > -9.8 else None,
                "AOD wavelength 1": aod1 if aod1 is not None and aod1 > -9.998 else None,
                "AOD wavelength 2": aod2 if aod2 is not None and aod2 > -9.998 else None,
                "AOD wavelength 3": aod3 if aod3 is not None and aod3 > -9.998 else None,
            }
        if any(value is not None for key, value in row.items() if key != "Date/Time"):
            rows.append(row)
    return rows


def generate_lr1300(meta: StationJobMetadata, blocks: dict[str, list[str]], out_dir: Path, ids_dir: Path) -> Path | None:
    rows = parse_lr1300_rows(blocks, meta)
    if not rows:
        return None
    total_cloud_method, cloud_base_method, liquid_water_method, spectral_aod_method = lr0007_expanded_methods(blocks, ids_dir)
    fields = active_fields(
        rows,
        [
            ("Total cloud amount", 55942, "####0", total_cloud_method, ""),
            ("Cloud base height", 45287, "####0", cloud_base_method, ""),
            ("Cloud liquid water", 55943, "##0.0", liquid_water_method, ""),
            ("AOD wavelength 1", 55944, "##0.000", spectral_aod_method, ""),
            ("AOD wavelength 2", 55945, "##0.000", spectral_aod_method, ""),
            ("AOD wavelength 3", 55946, "##0.000", spectral_aod_method, ""),
        ],
    )
    if not fields:
        return None
    path = out_dir / output_name(meta, "1300_imp")
    write_import_table(
        path,
        meta,
        "Expanded measurements from",
        "expanded",
        fields,
        rows,
        include_height=False,
        dataset_comment="99999: No clouds detected",
    )
    return path


def parse_lr3x30_rows(blocks: dict[str, list[str]], meta: StationJobMetadata, record: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    clean_lines = [line.strip() for line in blocks.get(record, []) if line.strip()]
    for i in range(0, len(clean_lines) - 1, 2):
        p1 = clean_lines[i].split()
        p2 = clean_lines[i + 1].split()
        if len(p1) < 10 or len(p2) < 10:
            continue
        day, minute = as_int(p1[0]), as_int(p1[1])
        if day is None or minute is None:
            continue
        rows.append(
            {
                "Date/Time": make_datetime(meta.year, meta.month, day, minute),
                "Short-wave downward (GLOBAL) radiation [W/m**2]": as_float(p1[2]),
                "Short-wave downward (GLOBAL) radiation, standard deviation [W/m**2]": as_float(p1[3]),
                "Short-wave downward (GLOBAL) radiation, minimum [W/m**2]": as_float(p1[4]),
                "Short-wave downward (GLOBAL) radiation, maximum [W/m**2]": as_float(p1[5]),
                "Short-wave upward (REFLEX) radiation [W/m**2]": as_float(p1[6]),
                "Short-wave upward (REFLEX) radiation, standard deviation [W/m**2]": as_float(p1[7]),
                "Short-wave upward (REFLEX) radiation, minimum [W/m**2]": as_float(p1[8]),
                "Short-wave upward (REFLEX) radiation, maximum [W/m**2]": as_float(p1[9]),
                "Long-wave downward radiation [W/m**2]": as_float(p2[0]),
                "Long-wave downward radiation, standard deviation [W/m**2]": as_float(p2[1]),
                "Long-wave downward radiation, minimum [W/m**2]": as_float(p2[2]),
                "Long-wave downward radiation, maximum [W/m**2]": as_float(p2[3]),
                "Long-wave upward radiation [W/m**2]": as_float(p2[4]),
                "Long-wave upward radiation, standard deviation [W/m**2]": as_float(p2[5]),
                "Long-wave upward radiation, minimum [W/m**2]": as_float(p2[6]),
                "Long-wave upward radiation, maximum [W/m**2]": as_float(p2[7]),
                "Air temperature [deg C]": as_float(p2[8]),
                "Relative Humidity [%]": as_float(p2[9]),
            }
        )
    return rows


def generate_lr3x30(meta: StationJobMetadata, rows: list[dict[str, object]], blocks: dict[str, list[str]], out_dir: Path, ids_dir: Path, height_m: int) -> Path | None:
    if not rows:
        return None
    lr0009 = parse_lr0009(blocks, meta.year, meta.month)
    lookup = MethodLookup(ids_dir / "BSRN_IDs.txt")
    scale = height_m * 100
    specs = [
        (2000000 + scale, [("Short-wave downward (GLOBAL) radiation [W/m**2]", 31460, "###0"), ("Short-wave downward (GLOBAL) radiation, standard deviation [W/m**2]", 55905, "###0.0"), ("Short-wave downward (GLOBAL) radiation, minimum [W/m**2]", 55906, "###0"), ("Short-wave downward (GLOBAL) radiation, maximum [W/m**2]", 55907, "###0")]),
        (131000000 + scale, [("Short-wave upward (REFLEX) radiation [W/m**2]", 55911, "###0"), ("Short-wave upward (REFLEX) radiation, standard deviation [W/m**2]", 55912, "###0.0"), ("Short-wave upward (REFLEX) radiation, minimum [W/m**2]", 55913, "###0"), ("Short-wave upward (REFLEX) radiation, maximum [W/m**2]", 55914, "###0")]),
        (5000000 + scale, [("Long-wave downward radiation [W/m**2]", 45298, "###0"), ("Long-wave downward radiation, standard deviation [W/m**2]", 55908, "###0.0"), ("Long-wave downward radiation, minimum [W/m**2]", 55909, "###0"), ("Long-wave downward radiation, maximum [W/m**2]", 55910, "###0")]),
        (132000000 + scale, [("Long-wave upward radiation [W/m**2]", 45299, "###0"), ("Long-wave upward radiation, standard deviation [W/m**2]", 55915, "###0.0"), ("Long-wave upward radiation, minimum [W/m**2]", 55916, "###0"), ("Long-wave upward radiation, maximum [W/m**2]", 55917, "###0")]),
    ]
    fields: list[tuple[str, int, str, int, str]] = []
    for quantity, group in specs:
        method_id, comment = method_for_quantity(meta, lr0009, lookup, quantity)
        fields.extend((label, parameter_id, fmt, method_id, comment) for label, parameter_id, fmt in group)
    fields.extend([("Air temperature [deg C]", 4610, "###0.0", 4722, ""), ("Relative Humidity [%]", 2219, "###0.0", 5039, "")])
    fields = active_fields(rows, fields)
    for row in rows:
        row["Height"] = height_m
    path = out_dir / output_name(meta, f"3{height_m:03d}_imp")
    write_import_table(path, meta, f"Other measurements at {height_m} m from", f"radiation_{height_m}m", fields, rows, skip_empty_data_rows=True)
    return path


def write_header_preview(path: Path, meta: StationJobMetadata) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write("NOT A PANGAEA DATA IMPORT FILE - diagnostic header preview only.\n")
        handle.write("Complete data import files are generated separately for supported data records.\n\n")
        write_metaheader(
            handle,
            meta,
            "BSRN import generator preview for",
            "header-preview",
            default_geocode_parameters(meta),
        )
        handle.write(geocode_header(import_mode=True) + "\n")
        handle.write(geocode_data_row(f"{meta.yyyy_mm}-01T00:00") + "\n")


def write_unsupported_records(path: Path, records: list[str], warnings: list[str]) -> None:
    lines = [
        "BSRN import generator warnings",
        "",
        "Detected data logical records:",
        *(f"- LR{record}" for record in records),
        "",
        "Unsupported/placeheld records:",
        *(f"- {warning}" for warning in warnings),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_manifest(
    path: Path,
    status: JobStatus,
    meta: StationJobMetadata,
    detected_records: list[str],
    warnings: list[str],
    outputs: list[Path],
    import_files: list[Path],
) -> None:
    manifest = {
        "version": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "job": status.job,
        "dat_path": status.dat_path,
        "metadata_dir": status.metadata_dir,
        "pangaea_reference_id": meta.pangaea_reference_id,
        "station": {
            "station_id": meta.station_id,
            "event_label": meta.event_label,
            "station_name": meta.station_name,
            "source_id": meta.source_id,
            "author_id": meta.author_id,
            "author_name": meta.author_name,
            "year": meta.year,
            "month": meta.month,
        },
        "detected_data_records": [f"LR{record}" for record in detected_records],
        "complete_data_converters_ported": True,
        "ported_data_records": sorted(f"LR{record}" for record in SUPPORTED_RECORDS),
        "generated_complete_import_files": [rel_path(output) for output in import_files],
        "artifacts": [rel_path(output) for output in outputs],
        "warnings": warnings,
    }
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def generate_imports_for_status(status: JobStatus, import_root: Path, ids_dir: Path) -> tuple[list[Path], list[str]]:
    meta = load_station_job_metadata(status, ids_dir)
    dat_path = workflow_path(status.dat_path)
    blocks = blocks_for_status(status)
    records = detected_data_records(dat_path)
    warnings = unsupported_record_messages(records)
    if not records:
        warnings.append("No Tool 1 data logical records were detected for import generation.")

    job_dir = import_root / status.job
    job_dir.mkdir(parents=True, exist_ok=True)
    unsupported_path = job_dir / output_name(meta, "unsupported_records")
    manifest_path = job_dir / output_name(meta, "import_generation_manifest", ".json")

    optional = optional_rows(status, meta)
    import_files: list[Path] = []
    maybe_paths = [
        generate_lr0100_0300(status, meta, blocks, job_dir, ids_dir),
        generate_lr0500(meta, optional.get("LR0500", []), blocks, job_dir, ids_dir),
        generate_lr1000(meta, optional.get("LR1000", []), job_dir),
        generate_lr1100(meta, blocks, job_dir, ids_dir),
        generate_lr1200(meta, optional.get("LR1200", []), blocks, job_dir),
        generate_lr1300(meta, blocks, job_dir, ids_dir),
        generate_lr3x30(meta, parse_lr3x30_rows(blocks, meta, "3010"), blocks, job_dir, ids_dir, 10),
        generate_lr3x30(meta, parse_lr3x30_rows(blocks, meta, "3030"), blocks, job_dir, ids_dir, 30),
    ]
    import_files.extend(path for path in maybe_paths if path is not None)

    for record in sorted(SUPPORTED_RECORDS - {"0300"}):
        if record in records and not any(f"_{record}" in path.name or ("0100" == record and "_0100" in path.name) for path in import_files):
            warnings.append(f"LR{record} was detected but no non-empty import file was generated.")
    if "0300" in records and not any("_0100+0300" in path.name for path in import_files):
        warnings.append("LR0300 was detected but no combined LR0100+LR0300 import file was generated.")

    write_unsupported_records(unsupported_path, records, warnings)
    outputs = [manifest_path, unsupported_path, *import_files]
    write_manifest(manifest_path, status, meta, records, warnings, outputs, import_files)
    return outputs, warnings


def run_import_generation(args: argparse.Namespace) -> int:
    status_path = resolve_project_path(args.status)
    if not status_path.exists():
        raise ImportWorkflowError(f"Status JSON not found: {status_path}")

    ids_dir = resolve_project_path(args.ids_dir or DEFAULT_IDS_DIR)
    run_root = status_path.parent
    import_root = resolve_project_path(args.import_dir) if args.import_dir else run_root / "import_files"
    import_root.mkdir(parents=True, exist_ok=True)

    statuses = load_statuses(status_path)
    decisions = load_curator_decisions(run_root)
    attempted = 0
    generated = 0

    for status in statuses:
        status.import_warnings = []
        status.import_outputs = []
        status.import_ok = False
        status.errors = [error for error in status.errors if not str(error).startswith("Import generation error:")]
        eligible, reason = eligible_for_import(status, decisions)
        if not eligible:
            status.import_warnings.append(reason)
            continue

        attempted += 1
        status.import_dir = rel_path(import_root)
        try:
            outputs, warnings = generate_imports_for_status(status, import_root, ids_dir)
            status.import_outputs = [rel_path(path) for path in outputs]
            status.import_warnings = warnings
            status.import_ok = True
            generated += 1
        except Exception as exc:
            status.import_warnings.append(f"Import generation failed: {exc}")
            status.errors.append(f"Import generation error: {exc}")
            write_import_exception_log(import_root, status, exc)

    summary_path = import_root / "import_generation_summary.json"
    summary = {
        "version": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "status_json": rel_path(status_path),
        "attempted_rows": attempted,
        "generated_rows": generated,
        "blocked_rows": [
            {"job": status.job, "warnings": status.import_warnings}
            for status in statuses
            if not status.import_ok and status.import_warnings
        ],
    }
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    save_statuses(status_path, statuses)
    dashboard_path = resolve_project_path(args.dashboard or "dashboard.html")
    write_run_index({"root": run_root}, statuses, dashboard_path=dashboard_path)
    print(f"Import artifacts: {import_root}")
    print(f"Status JSON:      {status_path}")
    print(f"Dashboard:        {dashboard_path}")
    return 0 if generated else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", default=str(PROJECT_ROOT / "output" / "current" / "status.json"))
    parser.add_argument("--ids-dir", help="Directory containing BSRN_IDs.txt; defaults to tools/create-importfiles")
    parser.add_argument("--import-dir", help="Output directory; defaults to <run>/import_files")
    parser.add_argument("--dashboard", help="Central dashboard path; defaults to BSRN/dashboard.html")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run_import_generation(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
