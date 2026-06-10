#!/usr/bin/env python3
"""
Local curator dashboard for the BSRN workflow.

This is a small localhost-only web app around the stable command-line scripts.
It keeps the static dashboard.html snapshot intact, but gives the curator real
buttons for download/check and QC continuation without relying on file:// links
to launch local command files.
"""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, unquote, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.bsrn_download_check import (  # noqa: E402
    DEFAULT_CONFIG,
    DEFAULT_IDS_DIR,
    JobStatus,
    attach_reference_id_status,
    current_decision_state,
    dashboard_minute_completeness,
    import_gate_status,
    is_routine_record_exclusion,
    json_for_html_script,
    load_config,
    load_curator_decisions,
    load_reference_id_cache,
    refresh_reference_ids,
    resolve_project_path,
    row_signature,
    save_curator_decisions,
    status_from_dict,
    write_run_index,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
STATUS_PATH = PROJECT_ROOT / "output" / "current" / "status.json"
DASHBOARD_PATH = PROJECT_ROOT / "dashboard.html"
LAST_ACTION_LOG = PROJECT_ROOT / "output" / "current" / "logs" / "dashboard_server_last_action.txt"
SERVE_ROOT = PROJECT_ROOT / "output" / "current"
CSRF_TOKEN = secrets.token_urlsafe(32)
MAX_FORM_BYTES = 128 * 1024
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


@dataclass
class ActionResult:
    title: str
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    finished_at: str


LAST_ACTION: ActionResult | None = None


def csrf_input() -> str:
    return f'<input type="hidden" name="csrf_token" value="{html.escape(CSRF_TOKEN, quote=True)}">'


def is_loopback_request(handler: BaseHTTPRequestHandler) -> bool:
    client_host = handler.client_address[0]
    if client_host not in {"127.0.0.1", "::1"}:
        return False
    host_header = handler.headers.get("Host", "")
    if host_header.startswith("[::1]"):
        host = "[::1]"
    else:
        host = host_header.split(":", 1)[0].lower()
    return host in LOOPBACK_HOSTS


def redact_text(text: str) -> str:
    replacements = [
        (str(PROJECT_ROOT), "<PROJECT_ROOT>"),
        (str(PROJECT_ROOT).replace("\\", "/"), "<PROJECT_ROOT>"),
        (str(Path.home()), "~"),
        (str(Path.home()).replace("\\", "/"), "~"),
    ]
    for old, new in replacements:
        if old:
            text = text.replace(old, new)
    return text


def load_statuses() -> list[dict]:
    if not STATUS_PATH.exists():
        return []
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [{"job": "status.json", "errors": [f"Could not parse status.json: {exc}"]}]


def workflow_path(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def href_for(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        rel = path.resolve().relative_to(PROJECT_ROOT)
    except ValueError:
        return ""
    try:
        path.resolve().relative_to(SERVE_ROOT.resolve())
    except ValueError:
        return ""
    return "/" + rel.as_posix()


def link(path: Path | None, label: str | None = None, css_class: str = "") -> str:
    href = href_for(path)
    if not href:
        return ""
    class_attr = f' class="{html.escape(css_class, quote=True)}"' if css_class else ""
    text = label or (path.name if path else "")
    return f'<a{class_attr} href="{html.escape(href, quote=True)}">{html.escape(text)}</a>'


def batch_artifacts(statuses: list[dict]) -> list[tuple[Path, str]]:
    artifacts: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for row in statuses:
        reference = workflow_path(row.get("batch_reference_import_file"))
        if reference is not None and reference not in seen:
            seen.add(reference)
            artifacts.append((reference, "Batch reference import"))
        report = workflow_path(row.get("batch_format_report"))
        if report is not None and report not in seen:
            seen.add(report)
            artifacts.append((report, "Batch format-check report"))
    return artifacts


def status_class(value: str) -> str:
    if value.isdigit():
        return "ok"
    return {
        "OK": "ok",
        "Available": "ok",
        "QC approved": "ok",
        "ERROR": "error",
        "FAIL": "warning",
        "Metadata warning": "warning",
        "QC failed": "error",
        "QC rejected": "error",
        "Warning": "warning",
        "FORMAT WARNING": "warning",
        "Awaiting curator approval": "warning",
        "Missing PANGAEA reference ID": "warning",
        "Approval stale": "warning",
        "Import files generated": "ok",
        "Not run": "idle",
        "QC not run": "idle",
        "Metadata not passed": "idle",
        "Checks blocked": "idle",
    }.get(value, "idle")


def badge(value: str) -> str:
    return f'<span class="badge {status_class(value)}">{html.escape(value)}</span>'


def row_state(row: dict) -> tuple[str, str, str, str, str]:
    errors = row.get("errors") or []
    qc_errors = [error for error in errors if str(error).startswith("QC error:")]
    ref_warning = row.get("reference_id_warning")
    metadata_warnings = row.get("metadata_warnings") or []
    decisions = load_curator_decisions(STATUS_PATH.parent)
    gate_kind, gate_label, _gate_detail = import_gate_status(row, decisions)
    metadata = "Warning" if metadata_warnings else "OK" if row.get("metadata_ok") else "ERROR" if errors else "Not run"
    fmt = "OK" if row.get("format_ok") else "FAIL" if row.get("format_report") else "Not run"
    qc = "OK" if row.get("qc_ok") else "ERROR" if qc_errors else "Not run"
    if errors:
        overall = "ERROR"
    elif gate_kind == "ready":
        overall = "Warning" if fmt == "FAIL" else "OK"
    elif metadata_warnings or ref_warning or fmt == "FAIL" or row.get("qc_ok"):
        overall = "Warning"
    else:
        overall = "OK"
    if gate_kind == "ready":
        action = "import_done" if row.get("import_ok") and row.get("import_outputs") else "ready"
    elif row.get("qc_ok") and gate_label in {"Awaiting curator approval", "QC rejected", "Approval stale"}:
        action = "curator"
    elif row.get("qc_ok"):
        action = "qc_links"
    elif row.get("dat_path") and not qc_errors:
        action = "continue"
    else:
        action = "blocked"
    return overall, metadata, fmt, qc, action


def qc_artifacts(row: dict) -> list[tuple[Path, str]]:
    paths = list(row.get("qc_outputs") or [])
    qc_report = row.get("qc_report")
    if qc_report and qc_report not in paths:
        paths.insert(0, qc_report)
    artifacts: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for value in paths:
        path = workflow_path(value)
        if path is None or path in seen or path.suffix.lower() not in {".html", ".png", ".jpg", ".jpeg", ".svg"}:
            continue
        seen.add(path)
        artifacts.append((path, qc_label(path, row)))
    return artifacts


def qc_label(path: Path, row: dict) -> str:
    name = path.name
    if name.endswith("_QC_report_interactive.html"):
        return "Interactive QC report"
    if name.endswith("_QC_report.html"):
        return "Static QC report"
    if row.get("qc_report") and workflow_path(row.get("qc_report")) == path:
        return "QC report"
    if name.endswith("_daily_panels.png"):
        return "SWD/SumSW daily panels"
    if name.endswith("_all_days_overlay.png"):
        return "SWD/SumSW all-days overlay"
    if "_LR" in name and path.suffix.lower() in {".csv", ".tsv"}:
        return f"{path.stem.rsplit('_', 1)[-1]} extraction"
    if "_LR" in name and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".svg"}:
        return f"{path.stem.rsplit('_', 1)[-1]} plot"
    return name


def import_artifacts(row: dict) -> list[tuple[Path, str]]:
    for value in row.get("import_outputs") or []:
        path = workflow_path(value)
        if path is None:
            continue
        return [(path if path.is_dir() else path.parent, "Import folder")]
    import_dir = workflow_path(row.get("import_dir"))
    return [(import_dir, "Import folder")] if import_dir is not None else []


def data_export_artifacts(row: dict) -> list[tuple[Path, str]]:
    export_dir = workflow_path(row.get("data_export_dir"))
    if export_dir is not None:
        return [(export_dir, "Data exports")]
    for value in row.get("data_export_outputs") or []:
        path = workflow_path(value)
        if path is not None:
            return [(path if path.is_dir() else path.parent, "Data exports")]
    return []


def import_label(path: Path) -> str:
    if path.is_dir():
        return "Import files"
    name = path.name
    if name.endswith("_import_generation_manifest.json"):
        return "Import manifest"
    if name.endswith("_header_preview.txt"):
        return "Header preview"
    if name.endswith("_unsupported_records.txt"):
        return "Unsupported records"
    return name


def attention_items(statuses: list[dict]) -> list[str]:
    items: list[str] = []
    decisions = load_curator_decisions(STATUS_PATH.parent)
    for row in statuses:
        for warning in row.get("metadata_warnings") or []:
            items.append(f"{row.get('job', 'Unknown job')}: {warning}")
        for error in row.get("errors") or []:
            items.append(f"{row.get('job', 'Unknown job')}: {error}")
        if row.get("reference_id_warning"):
            items.append(f"{row.get('job', 'Unknown job')}: {row.get('reference_id_warning')}")
        for warning in row.get("qc_warnings") or []:
            items.append(f"{row.get('job', 'Unknown job')}: {warning}")
        for warning in row.get("import_warnings") or []:
            if is_routine_record_exclusion(str(warning)):
                continue
            items.append(f"{row.get('job', 'Unknown job')}: {warning}")
        if row.get("format_report") and not row.get("format_ok"):
            items.append(
                f"{row.get('job', 'Unknown job')}: Format warning; QC and approved import generation may continue."
            )
        gate_kind, gate_label, gate_detail = import_gate_status(row, decisions)
        if row.get("qc_ok") and gate_kind != "ready":
            items.append(f"{row.get('job', 'Unknown job')}: {gate_label}: {gate_detail}")
    return items


def server_dashboard_row(row: dict) -> dict[str, object]:
    overall, metadata, fmt, qc, action = row_state(row)
    decisions = load_curator_decisions(STATUS_PATH.parent)
    gate_kind, gate_label, gate_detail = import_gate_status(row, decisions)
    reference_id = row.get("pangaea_reference_id")
    reference_status = str(reference_id) if reference_id is not None else "Warning" if row.get("reference_id_warning") else "Not run"

    file_links: list[dict[str, str]] = []
    for value, label in (
        (row.get("dat_path"), "DAT file"),
        (row.get("metadata_dir"), "Metadata files"),
        (row.get("reference_import_file"), "Reference import"),
        (row.get("batch_reference_import_file"), "Batch reference import"),
        (row.get("batch_format_report") or row.get("format_report"), "Format report"),
    ):
        path = workflow_path(value)
        href = href_for(path) if path is not None else ""
        if href:
            file_links.append({"href": href, "label": label})
    for record, value in sorted((row.get("batch_metadata_reports") or {}).items()):
        path = workflow_path(value)
        href = href_for(path) if path is not None else ""
        if href:
            file_links.append({"href": href, "label": f"Batch LR{record}"})

    qc_links = []
    for path, label in qc_artifacts(row):
        href = href_for(path)
        if not href:
            continue
        kind = "plot" if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".svg"} else "report"
        item = {"href": href, "label": label, "kind": kind}
        if kind == "plot" and path.exists():
            item["version"] = str(int(path.stat().st_mtime))
        qc_links.append(item)
    data_export_links = [
        {"href": href_for(path), "label": label, "kind": "folder"}
        for path, label in data_export_artifacts(row)
        if href_for(path)
    ]
    import_links = [{"href": href_for(path), "label": label, "kind": "folder"} for path, label in import_artifacts(row) if href_for(path)]
    minute_status, minute_detail = dashboard_minute_completeness(row.get("minute_completeness") or {})

    return {
        "job": str(row.get("job") or ""),
        "overall": overall,
        "metadata": metadata,
        "format": fmt,
        "qc": qc,
        "dat_href": href_for(workflow_path(row.get("dat_path"))) if row.get("dat_path") else "",
        "reference_id": reference_status,
        "gate_label": gate_label,
        "gate_detail": gate_detail if gate_kind == "blocked" else "",
        "action_kind": action,
        "import_status": "Import files generated" if row.get("import_ok") and row.get("import_outputs") else "Not run",
        "minute_status": minute_status,
        "minute_detail": minute_detail,
        "files_status": "Available" if file_links else "Not run",
        "file_links": file_links,
        "qc_artifacts": qc_links,
        "data_export_status": (
            "Available"
            if data_export_links
            else "Warning"
            if row.get("data_export_warnings")
            else "Not run"
        ),
        "data_exports": data_export_links,
        "can_export_data": bool(row.get("dat_path")),
        "import_artifacts": import_links,
        "details": compact_details(row, gate_kind, gate_detail),
    }


def compact_details(row: dict, gate_kind: str, gate_detail: str) -> list[str]:
    items: list[str] = []
    items.extend(str(warning) for warning in row.get("metadata_warnings") or [])
    if row.get("reference_id_warning"):
        items.append(str(row.get("reference_id_warning")))
    if row.get("format_report") and not row.get("format_ok"):
        items.append("Format warning: see report. This does not block QC or approved import generation.")
    items.extend(str(warning) for warning in row.get("qc_warnings") or [])
    items.extend(str(warning) for warning in row.get("data_export_warnings") or [])
    items.extend(
        str(warning)
        for warning in row.get("import_warnings") or []
        if not is_routine_record_exclusion(str(warning))
    )
    items.extend(str(error) for error in row.get("errors") or [])
    if gate_kind == "blocked" and gate_detail:
        items.append(gate_detail)
    if row.get("pangaea_parent_id") is not None and row.get("parent_id_comment"):
        items.append(f"ParentID {row.get('pangaea_parent_id')}: {row.get('parent_id_comment')}")

    result: list[str] = []
    for item in items:
        text = item.strip()
        if text and text not in result:
            result.append(text)
    return result


def dashboard_summary(rows: list[dict[str, object]]) -> dict[str, object]:
    minute_rows = [row for row in rows if row.get("minute_status") != "Not run"]
    minutes_ok = sum(1 for row in minute_rows if row.get("minute_status") == "OK")
    return {
        "files": len(rows),
        "minutes_ok": f"{minutes_ok}/{len(minute_rows)}" if minute_rows else "0/0",
        "minutes_warning": any(row.get("minute_status") == "Warning" for row in minute_rows),
        "checks_ok": sum(1 for row in rows if row.get("overall") == "OK"),
        "qc_complete": sum(1 for row in rows if row.get("qc") == "OK"),
        "qc_approved": sum(1 for row in rows if row.get("gate_label") in {"QC approved", "Import files generated"}),
        "needs_attention": sum(
            1
            for row in rows
            if row.get("overall") in {"ERROR", "Warning"}
            or row.get("action_kind") in {"blocked", "continue", "curator", "ready"}
        ),
    }


def render_header_primary_action(statuses: list[dict]) -> str:
    continuable = [row for row in statuses if row_state(row)[4] == "continue"]
    import_ready = [row for row in statuses if row_state(row)[4] == "ready"]
    buttons = []
    if any(row.get("dat_path") for row in statuses):
        buttons.append(
            f'<form method="post" action="/export-data">{csrf_input()}'
            '<button type="submit" class="primary">Export all data</button></form>'
        )
    if continuable:
        buttons.append(
            f'<form method="post" action="/continue-qc">{csrf_input()}'
            '<button type="submit" class="primary">Continue to QC</button></form>'
        )
        return "".join(buttons)
    if import_ready:
        buttons.append(
            f'<form method="post" action="/generate-import-files">{csrf_input()}'
            '<button type="submit" class="primary">Generate import files</button></form>'
        )
        return "".join(buttons)
    buttons.append('<a class="button secondary" href="#newWorkflowPanel">Start New Workflow</a>')
    return "".join(buttons)


def render_dashboard() -> str:
    statuses = load_statuses()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = [server_dashboard_row(row) for row in statuses]
    summary = dashboard_summary(rows)
    batch_links = [{"href": href_for(path), "label": label} for path, label in batch_artifacts(statuses) if href_for(path)]
    metadata_dir = next((workflow_path(row.get("metadata_dir")) for row in statuses if row.get("metadata_dir")), None)
    global_links = []
    if metadata_dir is not None and href_for(metadata_dir):
        global_links.append({"href": href_for(metadata_dir), "label": "Metadata files"})
    global_links.extend(batch_links)
    payload = {
        "rows": rows,
        "summary": summary,
        "globalLinks": global_links,
        "lastRefreshed": now,
        "csrfToken": CSRF_TOKEN,
    }
    data_json = json_for_html_script(payload)
    last_action = render_last_action()
    primary_action = render_header_primary_action(statuses)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BSRN Workflow Dashboard</title>
  <style>
    :root {{
      color-scheme: light;
      --header: #12343c;
      --text: #12343c;
      --muted: #47636a;
      --line: #acc4d4;
      --bg: #edf4f4;
      --card: #fff;
      --blue: #046c8c;
      --green: #448474;
      --red: #c4573b;
      --amber: #ffbd3d;
      --gray: #acc4d4;
      --active: #e2eded;
    }}
    * {{ box-sizing: border-box; }}
    body {{ font-family: "Segoe UI", "Trebuchet MS", Arial, sans-serif; margin: 0; color: var(--text); background: var(--bg); background-image: radial-gradient(1100px 500px at 85% -10%, rgba(255,189,61,.13), transparent 60%), radial-gradient(900px 600px at -10% 110%, rgba(4,108,140,.10), transparent 55%); background-attachment: fixed; }}
    h1, h2, .title {{ font-family: Georgia, "Times New Roman", serif; letter-spacing: .01em; }}
    a {{ color: var(--blue); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .topbar {{ position: fixed; inset: 0 0 auto 0; min-height: 60px; display: flex; align-items: center; gap: .75rem; padding: .55rem 1rem; background: var(--header); border-bottom: 3px solid #ffbd3d; z-index: 10; }}
    .title {{ font-weight: 700; font-size: 16px; white-space: nowrap; color: #ffbd3d; }}
    .workspace {{ color: #9fb6bc; font-size: 12px; }}
    .spacer {{ flex: 1; }}
    .pill-links {{ display: flex; flex-wrap: wrap; gap: .4rem; min-width: 0; }}
    .pill, button, .button {{ display: inline-flex; align-items: center; justify-content: center; gap: .35rem; border: 1px solid var(--line); border-radius: 999px; padding: .36rem .62rem; background: #fff; font: inherit; font-size: 13px; font-weight: 700; color: var(--blue); cursor: pointer; }}
    button.primary, .button.primary {{ border-radius: 6px; border-color: var(--blue); background: var(--blue); color: #fff; }}
    button.secondary, .button.secondary {{ border-radius: 6px; border-color: var(--blue); color: var(--blue); background: #fff; }}
    .topbar .button, .topbar button {{ white-space: nowrap; flex: 0 0 auto; }}
    .topbar button.primary, .topbar .button.primary {{ background: #ffbd3d; border-color: #ffbd3d; color: #4a3206; }}
    .topbar button.secondary, .topbar .button.secondary {{ background: transparent; border-color: #4f6d75; color: #e8eff1; }}
    .topbar form {{ display: inline-flex; }}
    .layout {{ display: grid; grid-template-columns: 220px minmax(0, 1fr); padding-top: 60px; min-height: 100vh; }}
    .sidebar {{ position: sticky; top: 60px; height: calc(100vh - 60px); overflow: auto; background: #e8eff1; border-right: 1px solid var(--line); padding: 1rem .75rem; }}
    .side-label {{ color: var(--muted); font-size: 11px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; margin: 0 0 .7rem; }}
    .job-list {{ display: grid; gap: .35rem; }}
    .job-button {{ width: 100%; justify-content: flex-start; text-align: left; border: 1px solid var(--line); border-left: 3px solid transparent; border-radius: 6px; padding: .58rem .6rem; background: #fff; color: var(--text); }}
    .job-button.active {{ border-left-color: var(--blue); background: var(--active); }}
    .job-name {{ display: block; font-weight: 700; margin-bottom: .35rem; }}
    .dots {{ display: flex; gap: .25rem; }}
    .dot {{ width: 9px; height: 9px; border-radius: 999px; background: var(--gray); }}
    .main {{ padding: 1.1rem 1.4rem 2rem; overflow: auto; }}
    .summary {{ display: grid; grid-template-columns: repeat(6, minmax(7rem, 1fr)); gap: .75rem; margin-bottom: 1rem; }}
    .metric {{ background: var(--card); border-radius: 8px; border: 1px solid var(--line); border-left: 4px solid var(--blue); box-shadow: 0 2px 8px rgba(0,0,0,.08); padding: .65rem .8rem; }}
    .metric strong {{ display: block; font-size: 12px; color: var(--muted); margin-bottom: .25rem; }}
    .metric span {{ font-weight: 800; font-size: 20px; }}
    .metric.ok {{ border-left-color: var(--green); color: #fff; }}
    .metric.ok strong, .metric.ok span {{ color: #fff; }}
    .metric.warn {{ border-left-color: var(--amber); }}
    .subhead {{ display: flex; flex-wrap: wrap; align-items: center; gap: .6rem; margin: .4rem 0 1rem; }}
    h1 {{ font-size: 24px; margin: 0 .4rem 0 0; }}
    h2 {{ font-size: 15px; margin: 0 0 .55rem; }}
    p {{ margin: .25rem 0; color: var(--muted); }}
    .card, .panel, .notice {{ background: var(--card); border: 1px solid var(--line); border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,.08); padding: .9rem; }}
    .sidebar .panel {{ margin-top: 1rem; box-shadow: none; }}
    .sidebar .panel .side-label {{ color: var(--muted); }}
    .sidebar details summary {{ cursor: pointer; list-style: none; display: flex; align-items: center; justify-content: space-between; gap: .5rem; }}
    .sidebar details summary::-webkit-details-marker {{ display: none; }}
    .sidebar details > summary.side-label::after {{ content: "\\25B8"; font-size: 13px; color: var(--blue); transition: transform .15s ease; }}
    .sidebar details[open] > summary.side-label::after {{ transform: rotate(90deg); }}
    .sidebar .start-panel {{ margin-top: 0; margin-bottom: 1.1rem; }}
    .sidebar .batch-label {{ margin-top: .2rem; }}
    .sidebar .panel form + form {{ margin-top: .8rem; padding-top: .8rem; border-top: 1px solid var(--line); }}
    form {{ display: grid; gap: .6rem; }}
    label {{ display: grid; gap: .25rem; color: var(--muted); font-size: 0.85rem; }}
    .checkline {{ display: flex; align-items: center; gap: 0.45rem; color: var(--text); }}
    .checkline input {{ width: auto; }}
    input, textarea {{ width: 100%; padding: 0.48rem 0.55rem; border: 1px solid var(--line); border-radius: 4px; font: inherit; }}
    textarea {{ min-height: 5.4rem; resize: vertical; }}
    .drop-zone {{ border: 1px dashed var(--blue); border-radius: 6px; padding: .65rem; background: #f7fbfc; color: var(--muted); cursor: pointer; font-size: 12px; text-align: center; }}
    .drop-zone.active {{ background: #e2eded; color: var(--text); }}
    .badge {{ display: inline-flex; align-items: center; border-radius: 999px; padding: .2rem .5rem; font-size: 12px; font-weight: 800; color: #fff; background: var(--gray); }}
    .ok {{ background: var(--green); }}
    .warning, .fail {{ background: var(--amber); color: #4a3206; }}
    .error {{ background: var(--red); }}
    .idle {{ background: var(--gray); }}
    .cards {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .85rem; }}
    .links, .button-row {{ display: flex; flex-wrap: wrap; gap: .45rem; margin-top: .55rem; }}
    .details {{ margin-top: .85rem; display: grid; gap: .4rem; color: #374151; font-size: 13px; }}
    .detail-line {{ padding: .45rem .55rem; background: #f9fafb; border: 1px solid var(--line); border-radius: 6px; }}
    .stepper {{ list-style: none; display: flex; flex-wrap: wrap; gap: 0; margin: .2rem 0 1.1rem; padding: .75rem .9rem; background: var(--card); border: 1px solid var(--line); border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,.08); }}
    .step {{ flex: 1 1 0; min-width: 6.5rem; position: relative; display: grid; justify-items: center; gap: .3rem; padding: .15rem .35rem; text-align: center; }}
    .step::before {{ content: ""; position: absolute; top: 8px; left: -50%; width: 100%; height: 2px; background: var(--line); }}
    .step:first-child::before {{ display: none; }}
    .step-dot {{ width: 18px; height: 18px; border-radius: 999px; background: var(--gray); border: 3px solid #fff; box-shadow: 0 0 0 1px var(--line); position: relative; z-index: 1; }}
    .step.state-ok .step-dot {{ background: var(--green); }}
    .step.state-ok::before {{ background: var(--green); }}
    .step.state-warning .step-dot {{ background: var(--amber); box-shadow: 0 0 0 1px #c9881c; }}
    .step.state-error .step-dot {{ background: var(--red); }}
    .step-label {{ font-size: 11px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); }}
    .step-value {{ font-size: 12px; font-weight: 700; color: var(--text); max-width: 11rem; }}
    .step.state-idle .step-value {{ color: var(--muted); font-weight: 600; }}
    .card.action {{ grid-column: 1 / -1; border-left: 4px solid var(--blue); }}
    .plots {{ margin-top: 1rem; }}
    .plot-grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: .8rem; }}
    .plot-card {{ border: 1px solid var(--line); border-radius: 8px; overflow: hidden; background: #fff; cursor: pointer; padding: 0; display: block; text-align: left; color: var(--text); }}
    .plot-card img {{ display: block; width: 100%; aspect-ratio: 16 / 10; object-fit: contain; background: #f9fafb; }}
    .plot-card span {{ display: block; padding: .5rem .6rem; font-size: 12px; font-weight: 700; color: #374151; }}
    .placeholder {{ border: 1px dashed #9ca3af; border-radius: 8px; padding: 1.2rem; color: var(--muted); background: #fff; }}
    .last-action {{ margin-top: 1rem; }}
    pre {{ white-space: pre-wrap; overflow: auto; max-height: 18rem; padding: .75rem; background: #111827; color: #e5e7eb; border-radius: 6px; }}
    .lightbox {{ position: fixed; inset: 0; display: none; align-items: center; justify-content: center; background: rgba(17,24,39,.82); z-index: 30; padding: 2rem; overflow: auto; }}
    .lightbox.open {{ display: flex; }}
    .lightbox img {{ display: block; width: auto; height: auto; max-width: min(96vw, 1400px); max-height: 86vh; object-fit: contain; background: #fff; }}
    .lightbox button {{ position: absolute; border: 0; background: rgba(255,255,255,.92); color: var(--text); border-radius: 6px; padding: .5rem .7rem; font-weight: 800; cursor: pointer; }}
    .lightbox .prev {{ left: 1rem; }}
    .lightbox .next {{ right: 1rem; }}
    .lightbox .close {{ top: 1rem; right: 1rem; }}
    @media (max-width: 980px) {{
      .topbar {{ position: static; height: auto; flex-wrap: wrap; padding: .75rem; }}
      .topbar .spacer {{ display: none; }}
      .pill-links {{ flex: 1 1 100%; order: 3; }}
      .layout {{ grid-template-columns: 1fr; padding-top: 0; }}
      .sidebar {{ position: static; height: auto; }}
      .summary, .cards, .plot-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header class="topbar">
    <div>
      <div class="title">BSRN Workflow Dashboard</div>
      <div class="workspace">output/current - local dashboard server - refreshed <span id="lastRefreshed"></span></div>
    </div>
    <div class="spacer"></div>
    <nav class="pill-links" id="globalLinks"></nav>
    {primary_action}
    <a class="button secondary" href="/" title="Refresh dashboard">Refresh</a>
  </header>
  <div class="layout">
    <aside class="sidebar">
      <details class="panel start-panel" open>
        <summary class="side-label">1 - Start Download</summary>
        <form method="post" action="/run-download">
          {csrf_input()}
          <label>Job codes<textarea name="job" placeholder="gob0624&#10;cab0425 pay0123"></textarea></label>
          <label>Local DAT files<textarea id="localFileInput" name="local_file" placeholder="input\\cab0325.dat&#10;input\\pay0123.dat"></textarea></label>
          <input id="localFilePicker" type="file" accept=".dat" multiple hidden>
          <div id="localDropZone" class="drop-zone" role="button" tabindex="0">Drop or select DAT files from input</div>
          <button type="submit" class="primary">Run download/check</button>
        </form>
      </details>
      <div class="side-label batch-label">2 - Current batch</div>
      <div class="job-list" id="jobList"></div>
      <details class="panel" id="newWorkflowPanel">
        <summary class="side-label">Utilities</summary>
        <form method="post" action="/refresh-reference-ids">
          {csrf_input()}
          <button type="submit" class="secondary">Update reference IDs</button>
        </form>
        <form method="post" action="/new-workflow">
          {csrf_input()}
          <label class="checkline"><input type="checkbox" name="confirm_new" value="1"> Archive current workspace first</label>
          <button type="submit" class="secondary">New workflow</button>
        </form>
      </details>
    </aside>
    <main class="main">
      <section class="summary" id="summary"></section>
      <section id="detail"></section>
      {last_action}
    </main>
  </div>
  <div class="lightbox" id="lightbox"><button class="close" type="button">Close</button><button class="prev" type="button">&lt;</button><img alt=""><button class="next" type="button">&gt;</button></div>
  <script id="dashboard-data" type="application/json">{data_json}</script>
  <script>
    const data = JSON.parse(document.getElementById('dashboard-data').textContent);
    let selected = 0;
    let plotSet = [];
    let plotIndex = 0;
    const cls = value => {{
      if (!value) return 'idle';
      if (/^\\d+$/.test(String(value))) return 'ok';
      const text = String(value).toLowerCase();
      if (text.includes('error') || text.includes('blocked') || text.includes('rejected') || text.includes('failed')) return 'error';
      if (text.includes('warning') || text.includes('fail') || text.includes('awaiting') || text.includes('stale') || text.includes('missing')) return 'warning';
      if (text.includes('ok') || text.includes('available') || text.includes('approved') || text.includes('generated')) return 'ok';
      return 'idle';
    }};
    function el(tag, attrs = {{}}, text = '') {{
      const node = document.createElement(tag);
      Object.entries(attrs).forEach(([key, value]) => {{
        if (key === 'class') node.className = value;
        else if (key === 'href') node.href = value;
        else if (key === 'title') node.title = value;
        else if (key === 'name') node.name = value;
        else if (key === 'value') node.value = value;
        else node.setAttribute(key, value);
      }});
      if (text) node.textContent = text;
      return node;
    }}
    function badge(value) {{ return el('span', {{class: 'badge ' + cls(value)}}, value || 'Not run'); }}
    function renderLinks(target, links, css = 'pill') {{
      target.replaceChildren();
      links.forEach(item => target.appendChild(el('a', {{class: css, href: item.href}}, item.label)));
    }}
    function renderSummary() {{
      const box = document.getElementById('summary');
      const items = [
        ['Files', data.summary.files, ''],
        ['Minutes OK', data.summary.minutes_ok, data.summary.minutes_warning ? 'warn' : 'ok'],
        ['Checks OK', data.summary.checks_ok, 'ok'],
        ['QC complete', data.summary.qc_complete, 'ok'],
        ['QC approved', data.summary.qc_approved, 'ok'],
        ['Needs attention', data.summary.needs_attention, 'warn']
      ];
      box.replaceChildren(...items.map(([label, value, kind]) => {{
        const card = el('div', {{class: 'metric ' + kind}});
        card.appendChild(el('strong', {{}}, label));
        card.appendChild(el('span', {{}}, String(value)));
        return card;
      }}));
    }}
    function renderList() {{
      const list = document.getElementById('jobList');
      list.replaceChildren();
      if (!data.rows.length) {{
        list.appendChild(el('p', {{}}, 'No current run yet.'));
        return;
      }}
      data.rows.forEach((row, index) => {{
        const button = el('button', {{class: 'job-button' + (index === selected ? ' active' : ''), type: 'button'}});
        const content = el('span');
        content.appendChild(el('span', {{class: 'job-name'}}, row.job));
        const dots = el('span', {{class: 'dots'}});
        [row.metadata, row.format, row.qc].forEach(value => dots.appendChild(el('span', {{class: 'dot ' + cls(value), title: value}})));
        content.appendChild(dots);
        button.appendChild(content);
        button.addEventListener('click', () => {{ selected = index; renderList(); renderDetail(); }});
        list.appendChild(button);
      }});
    }}
    function linkList(items, emptyText) {{
      const wrap = el('div', {{class: 'links'}});
      if (!items.length && emptyText) wrap.appendChild(el('p', {{}}, emptyText));
      items.forEach(item => wrap.appendChild(el('a', {{class: 'pill', href: item.href}}, item.label)));
      return wrap;
    }}
    function postForm(action, label, fields = {{}}, css = 'primary') {{
      const form = el('form', {{method: 'post', action}});
      form.appendChild(el('input', {{type: 'hidden', name: 'csrf_token', value: data.csrfToken}}));
      Object.entries(fields).forEach(([name, value]) => form.appendChild(el('input', {{type: 'hidden', name, value}})));
      form.appendChild(el('button', {{type: 'submit', class: css}}, label));
      return form;
    }}
    function rejectForm(job) {{
      const form = el('form', {{method: 'post', action: '/reject-qc'}});
      form.appendChild(el('input', {{type: 'hidden', name: 'csrf_token', value: data.csrfToken}}));
      form.appendChild(el('input', {{type: 'hidden', name: 'job', value: job}}));
      form.appendChild(el('input', {{name: 'note', placeholder: 'Reason or follow-up'}}));
      form.appendChild(el('button', {{type: 'submit', class: 'secondary'}}, 'Reject QC'));
      return form;
    }}
    function card(title, status, links, emptyText) {{
      const node = el('article', {{class: 'card'}});
      node.appendChild(el('h2', {{}}, title));
      node.appendChild(badge(status));
      node.appendChild(linkList(links, emptyText));
      return node;
    }}
    function dataExportCard(row) {{
      const node = card('Data exports', row.data_export_status, row.data_exports, 'No data exports yet.');
      if (row.can_export_data) {{
        const actions = el('div', {{class: 'button-row'}});
        actions.appendChild(postForm('/export-data', 'Export data', {{job: row.job}}, row.data_exports.length ? 'secondary' : 'primary'));
        node.appendChild(actions);
      }}
      return node;
    }}
    function stepper(row) {{
      const steps = [
        ['File', row.dat_href ? 'OK' : 'Not run'],
        ['Metadata', row.metadata],
        ['Format', row.format],
        ['QC', row.qc],
        ['Approval', row.gate_label],
        ['Import', row.import_status === 'Import files generated' ? 'OK' : 'Not run']
      ];
      const wrap = el('ol', {{class: 'stepper'}});
      steps.forEach(([label, value]) => {{
        const li = el('li', {{class: 'step state-' + cls(value), title: label + ': ' + (value || 'Not run')}});
        li.appendChild(el('span', {{class: 'step-dot'}}));
        li.appendChild(el('span', {{class: 'step-label'}}, label));
        li.appendChild(el('span', {{class: 'step-value'}}, value || 'Not run'));
        wrap.appendChild(li);
      }});
      return wrap;
    }}
    function actionCard(row) {{
      const node = el('article', {{class: 'card action'}});
      node.appendChild(el('h2', {{}}, 'Next step'));
      node.appendChild(badge(row.gate_label));
      const actions = el('div', {{class: 'button-row'}});
      if (row.action_kind === 'continue') actions.appendChild(postForm('/continue-qc', 'Continue to QC'));
      else if (row.action_kind === 'curator') {{
        actions.appendChild(postForm('/approve-qc', 'Approve QC', {{job: row.job}}));
        actions.appendChild(rejectForm(row.job));
      }} else if (row.action_kind === 'ready') actions.appendChild(postForm('/generate-import-files', 'Generate import files'));
      else if (row.action_kind === 'import_done') row.import_artifacts.forEach(item => actions.appendChild(el('a', {{class: 'pill', href: item.href}}, item.label)));
      else actions.appendChild(el('p', {{}}, row.gate_detail || 'No action available.'));
      node.appendChild(actions);
      return node;
    }}
    function renderDetail() {{
      const detail = document.getElementById('detail');
      if (!data.rows.length) {{
        detail.replaceChildren(el('div', {{class: 'placeholder'}}, 'Start with a job code or local DAT file.'));
        return;
      }}
      const row = data.rows[selected];
      const section = el('section');
      const head = el('div', {{class: 'subhead'}});
      head.appendChild(el('h1', {{}}, row.job));
      head.appendChild(badge(row.overall));
      section.appendChild(head);
      section.appendChild(stepper(row));
      const cards = el('div', {{class: 'cards'}});
      cards.appendChild(actionCard(row));
      cards.appendChild(card('Files', row.files_status, row.file_links, 'No files linked yet.'));
      cards.appendChild(card('QC artifacts', row.qc, row.qc_artifacts, 'No QC artifacts yet.'));
      cards.appendChild(dataExportCard(row));
      cards.appendChild(card('Import artifacts', row.import_status, row.import_artifacts, 'No import files generated yet.'));
      cards.appendChild(card('Minute completeness', row.minute_status, [], row.minute_detail || 'Not available.'));
      section.appendChild(cards);
      if (row.details.length) {{
        const details = el('div', {{class: 'details'}});
        row.details.forEach(text => details.appendChild(el('div', {{class: 'detail-line'}}, text)));
        section.appendChild(details);
      }}
      const plots = el('section', {{class: 'plots'}});
      plots.appendChild(el('h2', {{}}, 'QC Plots'));
      const plotLinks = row.qc_artifacts.filter(item => item.kind === 'plot');
      if (!plotLinks.length) {{
        plots.appendChild(el('div', {{class: 'placeholder'}}, 'No plots yet - run QC to generate.'));
      }} else {{
        const grid = el('div', {{class: 'plot-grid'}});
        plotLinks.forEach((item, index) => {{
          const fig = el('button', {{class: 'plot-card', type: 'button'}});
          fig.appendChild(el('img', {{src: plotImageSrc(item), alt: item.label}}));
          fig.appendChild(el('span', {{}}, item.label));
          fig.addEventListener('click', () => openLightbox(plotLinks, index));
          grid.appendChild(fig);
        }});
        plots.appendChild(grid);
      }}
      section.appendChild(plots);
      detail.replaceChildren(section);
    }}
    function appendLocalInputFiles(files) {{
      const textarea = document.getElementById('localFileInput');
      if (!textarea || !files || !files.length) return;
      const existing = textarea.value.split(/[\\r\\n;]+/).map(value => value.trim()).filter(Boolean);
      const seen = new Set(existing.map(value => value.toLowerCase()));
      Array.from(files).forEach(file => {{
        if (!file.name.toLowerCase().endsWith('.dat')) return;
        const path = 'input\\\\' + file.name;
        if (!seen.has(path.toLowerCase())) {{
          existing.push(path);
          seen.add(path.toLowerCase());
        }}
      }});
      textarea.value = existing.join('\\n');
    }}
    const dropZone = document.getElementById('localDropZone');
    const picker = document.getElementById('localFilePicker');
    if (dropZone && picker) {{
      dropZone.addEventListener('click', () => picker.click());
      dropZone.addEventListener('keydown', event => {{
        if (event.key === 'Enter' || event.key === ' ') {{ event.preventDefault(); picker.click(); }}
      }});
      picker.addEventListener('change', event => appendLocalInputFiles(event.target.files));
      ['dragenter', 'dragover'].forEach(type => dropZone.addEventListener(type, event => {{
        event.preventDefault();
        dropZone.classList.add('active');
      }}));
      ['dragleave', 'drop'].forEach(type => dropZone.addEventListener(type, event => {{
        event.preventDefault();
        dropZone.classList.remove('active');
      }}));
      dropZone.addEventListener('drop', event => appendLocalInputFiles(event.dataTransfer.files));
    }}
    function plotImageSrc(item) {{ const version = item.version || data.lastRefreshed; return version ? item.href + '?v=' + encodeURIComponent(version) : item.href; }}
    function openLightbox(items, index) {{ plotSet = items; plotIndex = index; updateLightbox(); document.getElementById('lightbox').classList.add('open'); }}
    function updateLightbox() {{ const img = document.querySelector('#lightbox img'); if (plotSet.length) {{ img.src = plotImageSrc(plotSet[plotIndex]); img.alt = plotSet[plotIndex].label; }} }}
    function movePlot(delta) {{ if (!plotSet.length) return; plotIndex = (plotIndex + delta + plotSet.length) % plotSet.length; updateLightbox(); }}
    document.querySelector('#lightbox .close').addEventListener('click', () => document.getElementById('lightbox').classList.remove('open'));
    document.querySelector('#lightbox .prev').addEventListener('click', () => movePlot(-1));
    document.querySelector('#lightbox .next').addEventListener('click', () => movePlot(1));
    document.getElementById('lightbox').addEventListener('click', event => {{ if (event.target.id === 'lightbox') event.currentTarget.classList.remove('open'); }});
    document.addEventListener('keydown', event => {{ if (event.key === 'Escape') document.getElementById('lightbox').classList.remove('open'); if (event.key === 'ArrowLeft') movePlot(-1); if (event.key === 'ArrowRight') movePlot(1); }});
    document.getElementById('lastRefreshed').textContent = data.lastRefreshed;
    renderLinks(document.getElementById('globalLinks'), data.globalLinks);
    document.querySelectorAll('a[href="#newWorkflowPanel"]').forEach(anchor => anchor.addEventListener('click', () => {{
      const panel = document.getElementById('newWorkflowPanel');
      if (panel) panel.open = true;
    }}));
    renderSummary(); renderList(); renderDetail();
  </script>
</body>
</html>
"""


def render_summary(statuses: list[dict]) -> str:
    total = len(statuses)
    ok = sum(1 for row in statuses if row_state(row)[0] == "OK")
    rows = [server_dashboard_row(row) for row in statuses]
    minute_rows = [row for row in rows if row.get("minute_status") != "Not run"]
    minutes_ok = sum(1 for row in minute_rows if row.get("minute_status") == "OK")
    qc_ok = sum(1 for row in statuses if row.get("qc_ok"))
    decisions = load_curator_decisions(STATUS_PATH.parent)
    approved = sum(1 for row in statuses if import_gate_status(row, decisions)[0] == "ready")
    blocked = sum(
        1
        for row in statuses
        if row_state(row)[4] == "blocked"
        or row.get("reference_id_warning")
        or (row.get("format_report") and not row.get("format_ok"))
    )
    return f"""<div class="summary">
      <div class="metric"><strong>Files</strong><span>{total}</span></div>
      <div class="metric"><strong>Minutes OK</strong><span>{minutes_ok}/{len(minute_rows)}</span></div>
      <div class="metric"><strong>Checks OK</strong><span>{ok}</span></div>
      <div class="metric"><strong>QC complete</strong><span>{qc_ok}</span></div>
      <div class="metric"><strong>QC approved</strong><span>{approved}</span></div>
      <div class="metric"><strong>Needs attention</strong><span>{blocked}</span></div>
    </div>"""


def render_attention(statuses: list[dict]) -> str:
    items = attention_items(statuses)
    if not items:
        return ""
    lis = "".join(f"<li>{html.escape(item)}</li>" for item in items)
    return f'<section class="attention"><strong>Needs attention</strong><ul>{lis}</ul></section>'


def render_batch_links(statuses: list[dict]) -> str:
    artifacts = batch_artifacts(statuses)
    if not artifacts:
        return ""
    links = "".join(link(path, label, "button secondary") for path, label in artifacts)
    return f'<section class="panel"><h2>Batch Files</h2><div class="button-row">{links}</div></section>'


def render_primary_action(statuses: list[dict]) -> str:
    batch_links = "".join(link(path, label, "button secondary") for path, label in batch_artifacts(statuses))
    continuable = [row for row in statuses if row_state(row)[4] == "continue"]
    curator_ready = [row for row in statuses if row_state(row)[4] == "curator"]
    import_ready = [row for row in statuses if row_state(row)[4] == "ready"]
    import_done = [row for row in statuses if row_state(row)[4] == "import_done"]
    completed = [row for row in statuses if row.get("qc_ok")]
    if continuable:
        return f"""<p>DAT files are available. Continue to QC when ready; metadata and format findings remain visible as warnings.</p>
        <div class="button-row">{batch_links}</div>
        <form method="post" action="/continue-qc">{csrf_input()}<button type="submit">Continue to QC</button></form>"""
    if curator_ready:
        buttons = []
        for row in curator_ready:
            job = html.escape(str(row.get("job") or ""), quote=True)
            label = html.escape(str(row.get("job") or "row"))
            buttons.append(
                f'<form method="post" action="/approve-qc">{csrf_input()}<input type="hidden" name="job" value="{job}">'
                f'<button type="submit">Approve QC: {label}</button></form>'
            )
        return '<p>QC has completed. Review the artifacts, then record each curator decision.</p><div class="button-row">' + "".join(buttons) + "</div>"
    if import_ready:
        return f"""<p>QC is approved. Generate import files when ready.</p>
        <form method="post" action="/generate-import-files">{csrf_input()}<button type="submit">Generate import files</button></form>"""
    if import_done:
        artifacts = []
        for row in import_done:
            artifacts.extend(import_artifacts(row))
        links = "".join(link(path, label, "button secondary") for path, label in artifacts[:6])
        return f'<p>Import files have been generated for approved rows.</p><div class="button-row">{links}</div>'
    if completed:
        artifacts = []
        for row in completed:
            artifacts.extend(qc_artifacts(row))
        links = "".join(link(path, label, "button secondary") for path, label in artifacts[:4])
        return f'<p>QC has completed. Open the generated artifacts for curator review.</p><div class="button-row">{links}</div>'
    if statuses:
        return "<p>The current run is waiting for a DAT file or QC output.</p>"
    return "<p>Start with a BSRN job code or a local DAT file.</p>"


def render_row(row: dict) -> str:
    overall, metadata, fmt, qc, action = row_state(row)
    decisions = load_curator_decisions(STATUS_PATH.parent)
    gate_kind, gate_label, gate_detail = import_gate_status(row, decisions)
    artifacts = qc_artifacts(row)
    imports = import_artifacts(row)
    artifact_html = '<div class="artifact-list">' + "".join(link(path, label) for path, label in artifacts) + "</div>" if artifacts else ""
    import_html = '<div class="artifact-list">' + "".join(link(path, label) for path, label in imports) + "</div>" if imports else ""
    reference_id = row.get("pangaea_reference_id")
    reference_status = badge(str(reference_id)) if reference_id is not None else badge("Warning") if row.get("reference_id_warning") else badge("Not run")
    if action == "continue":
        action_html = f'<form method="post" action="/continue-qc">{csrf_input()}<button type="submit">Continue to QC</button></form>'
    elif action == "curator":
        job = html.escape(str(row.get("job") or ""), quote=True)
        action_html = f"""<form method="post" action="/approve-qc">
          {csrf_input()}
          <input type="hidden" name="job" value="{job}">
          <button type="submit">Approve QC</button>
        </form>
        <form method="post" action="/reject-qc">
          {csrf_input()}
          <input type="hidden" name="job" value="{job}">
          <input name="note" placeholder="Reason or follow-up">
          <button type="submit" class="secondary">Reject QC</button>
        </form>"""
    elif action == "ready":
        action_html = f'<form method="post" action="/generate-import-files">{csrf_input()}<button type="submit">Generate import files</button></form>'
    elif action == "import_done":
        action_html = import_html
    elif action == "qc_links":
        action_html = artifact_html
    else:
        action_html = ""
    warnings = [
        *(row.get("metadata_warnings") or []),
        *(row.get("qc_warnings") or []),
        *(warning for warning in (row.get("import_warnings") or []) if not is_routine_record_exclusion(str(warning))),
    ]
    if row.get("format_report") and not row.get("format_ok"):
        warnings.insert(0, "Format warning: see report. This does not block QC or approved import generation.")
    errors = row.get("errors") or []
    reference_warning = [row.get("reference_id_warning")] if row.get("reference_id_warning") else []
    parent_comment = (
        f"ParentID {row.get('pangaea_parent_id')}: {row.get('parent_id_comment')}"
        if row.get("pangaea_parent_id") is not None and row.get("parent_id_comment")
        else ""
    )
    gate_detail_text = gate_detail if gate_kind == "blocked" else ""
    details = "<br>".join(
        html.escape(str(item))
        for item in [*reference_warning, *warnings, *errors, gate_detail_text, parent_comment]
        if item
    )
    reference_links = link(workflow_path(row.get("reference_import_file")), "Reference import")
    batch_reference_link = link(workflow_path(row.get("batch_reference_import_file")), "Batch")
    if reference_links and batch_reference_link:
        reference_links = reference_links + "<br>" + batch_reference_link
    elif batch_reference_link:
        reference_links = batch_reference_link
    batch_format_link = link(workflow_path(row.get("batch_format_report")), "Batch format-check report")
    format_links = batch_format_link or link(workflow_path(row.get("format_report")))
    return (
        "<tr>"
        f"<td>{html.escape(str(row.get('job') or ''))}</td>"
        f"<td>{badge(overall)}</td>"
        f"<td>{badge(metadata)}</td>"
        f"<td>{badge(fmt)}</td>"
        f"<td>{badge(qc)}</td>"
        f"<td>{link(workflow_path(row.get('dat_path')))}</td>"
        f"<td>{link(workflow_path(row.get('metadata_dir')), 'Metadata files')}</td>"
        f"<td>{reference_links}</td>"
        f"<td>{reference_status}</td>"
        f"<td>{format_links}</td>"
        f"<td>{artifact_html}</td>"
        f"<td>{import_html}</td>"
        f"<td>{badge(gate_label)}</td>"
        f"<td>{action_html}</td>"
        f"<td>{details}</td>"
        "</tr>"
    )


def render_last_action() -> str:
    result = LAST_ACTION or read_last_action_log()
    if result is None:
        return ""
    command = " ".join(result.command)
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    output = "\n\n".join(part for part in (stdout, stderr) if part) or "(no output)"
    return f"""<h2>Last Action</h2>
    <div class="panel">
      <p><strong>{html.escape(result.title)}</strong> finished at {html.escape(result.finished_at)} with exit code {result.returncode}.</p>
      <p><code>{html.escape(command)}</code></p>
      <pre>{html.escape(output)}</pre>
    </div>"""


def read_last_action_log() -> ActionResult | None:
    if not LAST_ACTION_LOG.exists():
        return None
    try:
        data = json.loads(LAST_ACTION_LOG.read_text(encoding="utf-8"))
        return ActionResult(**data)
    except Exception:
        return None


def write_last_action_log(result: ActionResult) -> None:
    LAST_ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
    LAST_ACTION_LOG.write_text(json.dumps(result.__dict__, indent=2), encoding="utf-8")


def run_command(title: str, command: list[str]) -> ActionResult:
    global LAST_ACTION
    creationflags = 0
    startupinfo = None
    if sys.platform.startswith("win"):
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0
    proc = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        creationflags=creationflags,
        startupinfo=startupinfo,
    )
    result = ActionResult(
        title=title,
        command=[redact_text(part) for part in command],
        returncode=proc.returncode,
        stdout=redact_text(proc.stdout),
        stderr=redact_text(proc.stderr),
        finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    LAST_ACTION = result
    write_last_action_log(result)
    return result


def regenerate_static_dashboard() -> None:
    statuses = [status_from_dict(row) for row in load_statuses()]
    write_run_index({"root": STATUS_PATH.parent}, statuses, dashboard_path=DASHBOARD_PATH)


def configured_ids_dir() -> Path:
    cfg = load_config(DEFAULT_CONFIG)
    return resolve_project_path(cfg.get("paths", "ids_dir", fallback=str(DEFAULT_IDS_DIR)))


def update_reference_ids_for_current_status() -> ActionResult:
    global LAST_ACTION
    ids_dir = configured_ids_dir()
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    returncode = 0

    try:
        refresh_warning = refresh_reference_ids(ids_dir)
        if refresh_warning:
            stderr_lines.append(f"WARNING: {refresh_warning}")
            returncode = 1
        reference_lookup, cache_warning = load_reference_id_cache(ids_dir)
        if cache_warning:
            stderr_lines.append(f"WARNING: {cache_warning}")
            returncode = 1

        rows = load_statuses()
        if not rows:
            stdout_lines.append(f"Updated {ids_dir / 'BSRN_Reference_IDs.txt'}. No current status rows to recheck.")
        else:
            updated = 0
            still_missing = 0
            for index, row in enumerate(rows):
                status = status_from_dict(row)
                before = status.pangaea_reference_id
                status.pangaea_reference_id = None
                status.pangaea_reference_uri = None
                status.reference_id_warning = None
                attach_reference_id_status(status, reference_lookup, cache_warning)
                if before != status.pangaea_reference_id:
                    updated += 1
                if status.reference_id_warning:
                    still_missing += 1
                rows[index] = asdict(status)
            STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATUS_PATH.write_text(json.dumps(rows, indent=2), encoding="utf-8")
            regenerate_static_dashboard()
            stdout_lines.append(
                f"Updated {ids_dir / 'BSRN_Reference_IDs.txt'} and rechecked {len(rows)} current status row(s). "
                f"Rows with changed reference ID state: {updated}. Still missing reference IDs: {still_missing}."
            )
    except Exception as exc:
        stderr_lines.append(str(exc))
        returncode = 2

    result = ActionResult(
        title="Update reference IDs",
        command=[],
        returncode=returncode,
        stdout="\n".join(stdout_lines),
        stderr="\n".join(stderr_lines),
        finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    write_last_action_log(result)
    LAST_ACTION = result
    return result


def start_new_workflow() -> ActionResult:
    global LAST_ACTION
    output_root = STATUS_PATH.parent.parent
    runs_root = output_root / "runs"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_path = runs_root / f"archived_current_{timestamp}"
    archived_message = "No previous output/current workspace existed."

    if STATUS_PATH.parent.exists():
        runs_root.mkdir(parents=True, exist_ok=True)
        suffix = 1
        while archive_path.exists():
            suffix += 1
            archive_path = runs_root / f"archived_current_{timestamp}_{suffix}"
        archive_current_workspace(STATUS_PATH.parent, archive_path)
        archived_message = f"Previous workspace archived at {archive_path}."

    for directory in ("dat", "downloads_gz", "metadata", "format_reports", "qc_reports", "logs"):
        (STATUS_PATH.parent / directory).mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text("[]\n", encoding="utf-8")
    regenerate_static_dashboard()

    result = ActionResult(
        title="New workflow",
        command=[],
        returncode=0,
        stdout=f"Started a clean output/current workspace. {archived_message}",
        stderr="",
        finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    write_last_action_log(result)
    LAST_ACTION = result
    return result


def archive_current_workspace(current_root: Path, archive_path: Path) -> None:
    """Archive current workspace contents without moving the live server folder."""

    archive_path.mkdir(parents=True, exist_ok=False)
    for child in list(current_root.iterdir()):
        target = archive_path / child.name
        if child.name == "logs":
            copy_locked_artifact(child, target)
            continue
        try:
            shutil.move(str(child), str(target))
        except OSError:
            copy_locked_artifact(child, target)


def copy_locked_artifact(source: Path, target: Path) -> None:
    """Preserve locked files for evidence while leaving the live handle in place."""

    if source.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            copy_locked_artifact(child, target / child.name)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(source, target)
    except OSError as exc:
        note = target.with_name(target.name + ".archive_warning.txt")
        note.write_text(f"Could not archive locked file {source}: {exc}\n", encoding="utf-8")


def record_curator_decision(job: str, decision_value: str, note: str = "") -> ActionResult:
    global LAST_ACTION
    statuses = load_statuses()
    row = next((item for item in statuses if str(item.get("job") or "") == job), None)
    decisions = load_curator_decisions(STATUS_PATH.parent)
    if row is None:
        result = ActionResult(
            title="Curator decision",
            command=[],
            returncode=2,
            stdout="",
            stderr=f"Status row not found for job {job}.",
            finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        write_last_action_log(result)
        LAST_ACTION = result
        return result

    _gate_kind, gate_label, gate_detail = import_gate_status(row, decisions)
    if decision_value == "accepted" and gate_label not in {"Awaiting curator approval", "QC rejected", "Approval stale"}:
        result = ActionResult(
            title="Curator decision",
            command=[],
            returncode=2,
            stdout="",
            stderr=f"{job} cannot be approved: {gate_label}. {gate_detail}",
            finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        write_last_action_log(result)
        LAST_ACTION = result
        return result

    if decision_value == "rejected" and not row.get("qc_ok"):
        result = ActionResult(
            title="Curator decision",
            command=[],
            returncode=2,
            stdout="",
            stderr=f"{job} cannot be rejected before QC completes.",
            finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        write_last_action_log(result)
        LAST_ACTION = result
        return result

    key = str(row.get("job") or "")
    decisions.setdefault("decisions", {})[key] = {
        "job": key,
        "decision": decision_value,
        "accepted": decision_value == "accepted",
        "rejected": decision_value == "rejected",
        "decided_at": datetime.now().isoformat(timespec="seconds"),
        "operator_note": note,
        "row_signature": row_signature(row),
        "dat_path": row.get("dat_path"),
        "qc_report": row.get("qc_report"),
        "pangaea_reference_uri": row.get("pangaea_reference_uri"),
        "pangaea_reference_id": row.get("pangaea_reference_id"),
    }
    save_curator_decisions(STATUS_PATH.parent, decisions)
    regenerate_static_dashboard()
    state, detail = current_decision_state(row, decisions)
    result = ActionResult(
        title="Curator decision",
        command=[],
        returncode=0,
        stdout=f"{job}: recorded {state}. {detail}",
        stderr="",
        finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    write_last_action_log(result)
    LAST_ACTION = result
    return result


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "BSRNDashboard/0.1"

    def do_GET(self) -> None:
        if not is_loopback_request(self):
            self.send_error(HTTPStatus.FORBIDDEN, "Dashboard is only available from localhost")
            return
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/dashboard.html"}:
            self.send_html(render_dashboard())
            return
        self.serve_project_file(parsed.path)

    def do_POST(self) -> None:
        if not is_loopback_request(self):
            self.send_error(HTTPStatus.FORBIDDEN, "Dashboard actions are only available from localhost")
            return
        parsed = urlparse(self.path)
        try:
            form = self.read_form()
        except ValueError as exc:
            self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if first_value(form, "csrf_token") != CSRF_TOKEN:
            self.send_error(HTTPStatus.FORBIDDEN, "Invalid dashboard action token")
            return
        if parsed.path == "/run-download":
            self.handle_run_download(form)
            return
        if parsed.path == "/new-workflow":
            self.handle_new_workflow(form)
            return
        if parsed.path == "/refresh-reference-ids":
            self.handle_refresh_reference_ids()
            return
        if parsed.path == "/continue-qc":
            self.handle_continue_qc()
            return
        if parsed.path == "/export-data":
            self.handle_export_data(form)
            return
        if parsed.path == "/approve-qc":
            self.handle_curator_decision(form, "accepted")
            return
        if parsed.path == "/reject-qc":
            self.handle_curator_decision(form, "rejected")
            return
        if parsed.path == "/generate-import-files":
            self.handle_generate_import_files()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Unknown action")

    def handle_run_download(self, form: dict[str, list[str]]) -> None:
        jobs = split_job_codes(first_value(form, "job"))
        local_files = split_local_files(first_value(form, "local_file"))
        if bool(jobs) == bool(local_files):
            self.redirect_with_error("Enter either one or more job codes or one or more local DAT files, not both.")
            return
        command = [sys.executable, str(PROJECT_ROOT / "scripts" / "bsrn_download_check.py"), "--dashboard", str(DASHBOARD_PATH)]
        if jobs:
            for job in jobs:
                command.extend(["--job", job])
        else:
            for local_file in local_files:
                command.extend(["--local-file", local_file])
        run_command("Download/check", command)
        self.redirect_home()

    def handle_new_workflow(self, form: dict[str, list[str]]) -> None:
        if not first_value(form, "confirm_new"):
            self.redirect_with_error("Confirm New before replacing output/current. The current workspace will be archived first.")
            return
        start_new_workflow()
        self.redirect_home()

    def handle_continue_qc(self) -> None:
        command = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "bsrn_qc_continue.py"),
            "--status",
            str(STATUS_PATH),
            "--dashboard",
            str(DASHBOARD_PATH),
        ]
        run_command("Continue to QC", command)
        self.redirect_home()

    def handle_export_data(self, form: dict[str, list[str]]) -> None:
        command = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "bsrn_data_exports.py"),
            "--status",
            str(STATUS_PATH),
            "--dashboard",
            str(DASHBOARD_PATH),
        ]
        job = first_value(form, "job")
        if job:
            command.extend(["--job", job])
        run_command("Export data", command)
        self.redirect_home()

    def handle_refresh_reference_ids(self) -> None:
        update_reference_ids_for_current_status()
        self.redirect_home()

    def handle_generate_import_files(self) -> None:
        command = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "bsrn_import_files.py"),
            "--status",
            str(STATUS_PATH),
            "--dashboard",
            str(DASHBOARD_PATH),
        ]
        run_command("Generate import files", command)
        self.redirect_home()

    def handle_curator_decision(self, form: dict[str, list[str]], decision_value: str) -> None:
        job = first_value(form, "job")
        note = first_value(form, "note")
        if not job:
            self.redirect_with_error("Missing status row job for curator decision.")
            return
        record_curator_decision(job, decision_value, note)
        self.redirect_home()

    def read_form(self) -> dict[str, list[str]]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid Content-Length") from exc
        if length > MAX_FORM_BYTES:
            raise ValueError("Form body is too large")
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        return parse_qs(body, keep_blank_values=True)

    def redirect_home(self) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.end_headers()

    def redirect_with_error(self, message: str) -> None:
        global LAST_ACTION
        LAST_ACTION = ActionResult(
            title="Input error",
            command=[],
            returncode=2,
            stdout="",
            stderr=message,
            finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.redirect_home()

    def send_html(self, content: str) -> None:
        data = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'self'; form-action 'self'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def serve_project_file(self, raw_path: str) -> None:
        rel = unquote(raw_path).lstrip("/")
        target = (PROJECT_ROOT / rel).resolve()
        try:
            target.relative_to(SERVE_ROOT.resolve())
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN, "Path is outside output/current")
            return
        if not target.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        if target.is_dir():
            self.send_html(render_directory_listing(target))
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        # Artifacts (plots, reports) are regenerated in place; force revalidation so
        # the browser never shows a stale thumbnail even without the ?v= cache buster.
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(target.stat().st_size))
        self.end_headers()
        with target.open("rb") as handle:
            shutil.copyfileobj(handle, self.wfile)

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")


def first_value(form: dict[str, list[str]], key: str) -> str:
    values = form.get(key) or [""]
    return values[0].strip()


def split_job_codes(value: str) -> list[str]:
    codes = [part.strip() for part in re.split(r"[\s,;]+", value) if part.strip()]
    return list(dict.fromkeys(codes))


def split_local_files(value: str) -> list[str]:
    paths = [part.strip().strip('"') for part in re.split(r"[\r\n;]+", value) if part.strip()]
    return list(dict.fromkeys(paths))


def render_directory_listing(directory: Path) -> str:
    try:
        rel = directory.relative_to(PROJECT_ROOT)
    except ValueError:
        rel = Path(".")
    entries = []
    if directory != SERVE_ROOT.resolve():
        parent = href_for(directory.parent)
        if parent:
            entries.append(f'<li><a href="{html.escape(parent, quote=True)}">..</a></li>')
    for child in sorted(directory.iterdir(), key=lambda path: (path.is_file(), path.name.lower())):
        suffix = "/" if child.is_dir() else ""
        entries.append(f'<li>{link(child, child.name + suffix)}</li>')
    items = "\n".join(entries) or "<li>No files</li>"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(rel.as_posix())}</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 2rem; color: #17212b; }}
    a {{ color: #005ea8; }}
    li {{ margin: 0.25rem 0; }}
  </style>
</head>
<body>
  <h1>{html.escape(rel.as_posix())}</h1>
  <p><a href="/">Back to dashboard</a></p>
  <ul>{items}</ul>
</body>
</html>
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", default=DEFAULT_PORT, type=int)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"BSRN dashboard server: http://{args.host}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard server.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
