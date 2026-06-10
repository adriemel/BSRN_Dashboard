#!/usr/bin/env python3
"""Station metadata helpers backed by the local BSRN_IDs.txt cache."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BSRN_IDS = PROJECT_ROOT / "tools" / "create-importfiles" / "BSRN_IDs.txt"


@dataclass(frozen=True)
class StationEntry:
    station_id: int
    event_label: str
    name: str
    pangaea_id: int | None


def load_station_entries(ids_file: Path = DEFAULT_BSRN_IDS) -> dict[str, StationEntry]:
    """Load station rows from BSRN_IDs.txt keyed by event label."""

    if not ids_file.exists():
        return {}

    entries: dict[str, StationEntry] = {}
    in_station_section = False
    header_seen = False
    for raw_line in ids_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            in_station_section = line.lower() == "[station]"
            header_seen = False
            continue
        if not in_station_section:
            continue
        if not header_seen:
            header_seen = True
            continue

        parts = raw_line.split("\t")
        if len(parts) < 3:
            continue
        station_id_text = parts[0].strip()
        event_label = parts[1].strip().upper()
        name = parts[2].strip()
        pangaea_id_text = parts[3].strip() if len(parts) > 3 else ""
        if not station_id_text.isdigit() or not event_label:
            continue
        entries[event_label] = StationEntry(
            station_id=int(station_id_text),
            event_label=event_label,
            name=name,
            pangaea_id=int(pangaea_id_text) if pangaea_id_text.isdigit() else None,
        )
    return entries


def load_station_codes(ids_file: Path = DEFAULT_BSRN_IDS) -> list[str]:
    return sorted(load_station_entries(ids_file))


def load_station_names(ids_file: Path = DEFAULT_BSRN_IDS) -> dict[str, str]:
    return {code: entry.name for code, entry in load_station_entries(ids_file).items()}


def load_station_entries_by_id(ids_file: Path = DEFAULT_BSRN_IDS) -> dict[int, list[StationEntry]]:
    entries_by_id: dict[int, list[StationEntry]] = {}
    for entry in load_station_entries(ids_file).values():
        entries_by_id.setdefault(entry.station_id, []).append(entry)
    return entries_by_id


def resolve_station_entry(
    station_id: int,
    event_hint: str | None = None,
    ids_file: Path = DEFAULT_BSRN_IDS,
) -> StationEntry | None:
    matches = load_station_entries_by_id(ids_file).get(station_id, [])
    if not matches:
        return None
    if event_hint:
        event_hint = event_hint.upper()
        hinted = [entry for entry in matches if entry.event_label == event_hint]
        if hinted:
            return hinted[0]
        return None
    return matches[0]
