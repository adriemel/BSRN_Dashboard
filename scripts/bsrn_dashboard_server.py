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
    loading_overlay_css,
    loading_overlay_html,
    loading_overlay_script,
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
            artifacts.append((reference, "Reference import"))
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
        (row.get("batch_reference_import_file") or row.get("reference_import_file"), "Reference import"),
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
            file_links.append({"href": href, "label": f"Metadata LR{record}"})

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
    import_status = "Import files generated" if row.get("import_ok") and row.get("import_outputs") else "Not run"
    files_status = "Available" if file_links else "Not run"
    data_export_status = "Available" if data_export_links else "Warning" if row.get("data_export_warnings") else "Not run"

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
        "import_status": import_status,
        "minute_status": minute_status,
        "minute_detail": minute_detail,
        "files_status": files_status,
        "file_links": file_links,
        "qc_artifacts": qc_links,
        "data_export_status": data_export_status,
        "data_exports": data_export_links,
        "can_export_data": bool(row.get("dat_path")),
        "import_artifacts": import_links,
        "details": compact_details(row, gate_kind, gate_detail),
        "kinds": {
            "overall": status_kind(overall),
            "metadata": status_kind(metadata),
            "format": status_kind(fmt),
            "qc": status_kind(qc),
            "gate": status_kind(gate_label),
            "minutes": status_kind(minute_status),
            "files": status_kind(files_status),
            "exports": status_kind(data_export_status),
            "import": status_kind(import_status),
        },
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


def status_kind(value: str) -> str:
    # Waiting for the curator gets its own colour so it is not confused with a warning.
    if value == "Awaiting curator approval":
        return "pending"
    return status_class(value)


def render_header_batch_actions(statuses: list[dict]) -> str:
    states = [row_state(row)[4] for row in statuses]
    can_continue = "continue" in states
    can_generate = "ready" in states
    actions = (
        ("/export-data", "Export all data", any(row.get("dat_path") for row in statuses), False, "No downloaded DAT files yet."),
        ("/continue-qc", "Continue to QC", can_continue, can_continue, "No files are waiting for QC."),
        (
            "/generate-import-files",
            "Generate import files",
            can_generate,
            can_generate and not can_continue,
            "No files with approved QC are waiting for import files.",
        ),
    )
    forms = []
    for action, label, enabled, primary, reason in actions:
        css = "primary" if primary else "secondary"
        title = "Runs for all files in the batch" if enabled else reason
        disabled = "" if enabled else " disabled"
        forms.append(
            f'<form method="post" action="{action}" title="{html.escape(title, quote=True)}">{csrf_input()}'
            f'<button type="submit" class="{css}"{disabled}>{html.escape(label)}</button></form>'
        )
    return "".join(forms)


LOGO_SVG = (
    '<svg class="logo" viewBox="0 0 64 40" aria-hidden="true" focusable="false">'
    '<path d="M10 31 A22 22 0 0 1 54 31" fill="none" stroke="#E8811A" stroke-width="4" stroke-linecap="round"></path>'
    '<path d="M19 31 A13 13 0 0 1 45 31" fill="none" stroke="#F5B041" stroke-width="4" stroke-linecap="round"></path>'
    '<path d="M2 36 Q32 27 62 36" fill="none" stroke="#4E7389" stroke-width="3" stroke-linecap="round"></path>'
    "</svg>"
)

DASHBOARD_CSS = r"""
    :root {
      color-scheme: light;
      --text: #1F2328;
      --soft: #3D4650;
      --muted: #5C6670;
      --line: #E2E0D9;
      --line-soft: #EEECE6;
      --bg: #F6F5F1;
      --side: #FBFAF7;
      --card: #FFFFFF;
      --accent: #2E5B73;
      --accent-hover: #244A5E;
      --accent-soft: #F3F7F9;
      --accent-line: #B9C6CE;
      --brand: #E8811A;
      --ok-fg: #1A6B45; --ok-bg: #E2F1E8; --ok-bar: #3C9A6B;
      --warning-fg: #875200; --warning-bg: #FDF0D5; --warning-bar: #D99A1E;
      --error-fg: #B42318; --error-bg: #FDE8E6; --error-bar: #C9402F;
      --idle-fg: #59606A; --idle-bg: #EEEEEC; --idle-bar: #DAD8D2;
      --pending-fg: #24506A; --pending-bg: #E4EEF4; --pending-bar: #4E7389;
      --mono: "Cascadia Mono", Consolas, "SFMono-Regular", Menlo, monospace;
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; }
    body { margin: 0; font-family: "Segoe UI", system-ui, -apple-system, Roboto, Arial, sans-serif; font-size: 14px; color: var(--text); background: var(--bg); display: flex; flex-direction: column; }
    a { color: var(--accent); text-decoration: none; }
    a:hover { color: var(--accent-hover); text-decoration: underline; }
    :focus-visible { outline: 3px solid var(--brand); outline-offset: 2px; }
    .mono { font-family: var(--mono); }
    .num { font-variant-numeric: tabular-nums; }
    h1, h2 { margin: 0; }
    p { margin: .25rem 0; color: var(--muted); }

    button, .button { display: inline-flex; align-items: center; justify-content: center; gap: .45rem; min-height: 36px; padding: 0 14px; border-radius: 6px; border: 1px solid var(--accent-line); background: var(--card); color: var(--accent); font: inherit; font-size: 13px; font-weight: 600; white-space: nowrap; cursor: pointer; text-decoration: none; }
    button:hover, .button:hover { background: #F0F4F6; color: var(--accent-hover); text-decoration: none; }
    button.primary, .button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
    button.primary:hover, .button.primary:hover { background: var(--accent-hover); color: #fff; }
    button.danger { color: var(--error-fg); border-color: #E9B7B2; }
    button.danger:hover { background: #FDF3F2; color: var(--error-fg); }
    button.ghost, .button.ghost { background: transparent; border-color: transparent; color: var(--soft); }
    button.ghost:hover, .button.ghost:hover { background: #ECEBE6; }
    button.small, .button.small { min-height: 30px; padding: 0 10px; font-size: 12px; }
    button:disabled, button:disabled:hover { background: #F1F0EC; border-color: var(--line); color: #8A9099; cursor: not-allowed; }
    form { margin: 0; }

    .topbar { flex: 0 0 auto; display: flex; flex-wrap: wrap; align-items: center; gap: 10px 16px; padding: 10px 20px; background: var(--card); border-bottom: 1px solid var(--line); z-index: 10; }
    .brand { display: flex; align-items: center; gap: 12px; }
    .logo { width: 44px; height: 28px; flex: 0 0 auto; }
    .title { font-weight: 700; font-size: 16px; letter-spacing: .01em; white-space: nowrap; }
    .workspace { color: var(--muted); font-size: 12px; }
    .global-links { display: flex; flex-wrap: wrap; gap: 2px; }
    .spacer { flex: 1; }
    .batch-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding-left: 16px; border-left: 1px solid var(--line); }
    .batch-label { font-size: 11px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); }

    .layout { flex: 1 1 auto; min-height: 0; display: grid; grid-template-columns: 290px minmax(0, 1fr); }
    .sidebar { min-height: 0; overflow: auto; background: var(--side); border-right: 1px solid var(--line); padding: 18px 14px; display: flex; flex-direction: column; gap: 12px; }
    .side-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
    .side-label { color: var(--muted); font-size: 11px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; }
    .side-count { font-size: 13px; color: var(--soft); }
    .job-list { display: grid; gap: 8px; }
    .job-button { display: block; width: 100%; min-height: 0; padding: 10px 12px; text-align: left; border: 1px solid var(--line); border-radius: 8px; background: var(--card); color: var(--text); font-weight: 400; }
    .job-button:hover { border-color: var(--accent-line); background: var(--card); color: var(--text); }
    .job-button.active { border-color: var(--accent); box-shadow: inset 3px 0 0 var(--accent); background: var(--accent-soft); }
    .job-top { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
    .job-name { font-family: var(--mono); font-weight: 600; font-size: 14px; }
    .job-marks { display: flex; flex-wrap: wrap; gap: 4px 12px; margin-top: 8px; }
    .mark { font-size: 11px; color: var(--idle-fg); white-space: nowrap; }
    .glyph { font-weight: 700; }
    .g-ok { color: var(--ok-fg); } .g-warning { color: var(--warning-fg); } .g-error { color: var(--error-fg); } .g-idle { color: #8A9099; font-weight: 400; } .g-pending { color: var(--pending-fg); }
    .workspace-menu { margin-top: auto; border-top: 1px solid var(--line); padding-top: 10px; }
    .workspace-menu summary { cursor: pointer; list-style: none; display: flex; align-items: center; gap: 8px; padding: 8px 6px; border-radius: 6px; font-size: 13px; font-weight: 600; color: var(--soft); }
    .workspace-menu summary::-webkit-details-marker { display: none; }
    .workspace-menu summary::after { content: "\25B8"; margin-left: auto; color: var(--muted); transition: transform .15s ease; }
    .workspace-menu[open] summary::after { transform: rotate(90deg); }
    .workspace-menu summary:hover { background: #ECEBE6; }
    .workspace-menu form { display: grid; gap: 8px; padding: 10px 6px 4px; }
    .workspace-menu form + form { border-top: 1px solid var(--line-soft); }
    .checkline { display: flex; align-items: center; gap: .45rem; font-size: 13px; color: var(--text); }
    .checkline input { width: auto; accent-color: var(--accent); }

    .main { min-height: 0; overflow: auto; padding: 22px 28px 48px; display: flex; flex-direction: column; gap: 18px; }
    .alert { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; padding: 12px 16px; border-radius: 8px; background: var(--error-bg); border: 1px solid #F1C0BA; color: var(--error-fg); font-size: 14px; }
    .alert .spacer { flex: 1; }
    .summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; }
    .metric { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px 16px; }
    .metric strong { display: block; font-size: 12px; font-weight: 600; color: var(--muted); }
    .metric span { display: block; margin-top: 4px; font-size: 24px; font-weight: 700; font-variant-numeric: tabular-nums; }
    .metric.warn { background: var(--warning-bg); border-color: #E8C27A; }
    .metric.warn strong, .metric.warn span { color: #6B4100; }

    .subhead { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
    .subhead h1 { font-family: var(--mono); font-size: 26px; font-weight: 600; }
    .badge { display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; line-height: 1.35; white-space: nowrap; }
    .k-ok { background: var(--ok-bg); color: var(--ok-fg); }
    .k-warning { background: var(--warning-bg); color: var(--warning-fg); }
    .k-error { background: var(--error-bg); color: var(--error-fg); }
    .k-idle { background: var(--idle-bg); color: var(--idle-fg); }
    .k-pending { background: var(--pending-bg); color: var(--pending-fg); }

    .stepper { list-style: none; margin: 0; padding: 16px 20px; display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 10px; background: var(--card); border: 1px solid var(--line); border-radius: 10px; }
    .step { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
    .step-bar { height: 4px; border-radius: 2px; background: var(--idle-bar); }
    .step.k-ok, .step.k-warning, .step.k-error, .step.k-idle, .step.k-pending { background: none; }
    .step.k-ok .step-bar { background: var(--ok-bar); }
    .step.k-warning .step-bar { background: var(--warning-bar); }
    .step.k-error .step-bar { background: var(--error-bar); }
    .step.k-pending .step-bar { background: var(--pending-bar); }
    .step-label { font-size: 11px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); }
    .step-value { font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

    .card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; }
    .next-step { padding: 18px 22px; display: flex; flex-wrap: wrap; align-items: center; gap: 14px 24px; }
    .next-text { flex: 1 1 320px; min-width: 0; }
    .eyebrow { font-size: 11px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); }
    .next-title { font-size: 18px; font-weight: 600; margin-top: 4px; }
    .next-detail { font-size: 14px; color: var(--soft); margin-top: 4px; line-height: 1.5; }
    .button-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
    .reject-form { display: flex; flex-wrap: wrap; gap: 8px; }
    .reject-form input { min-width: 220px; }
    input, textarea { width: 100%; min-height: 36px; padding: 8px 10px; border: 1px solid #C9CDD2; border-radius: 6px; font: inherit; color: var(--text); background: #fff; }
    textarea { min-height: 5.4rem; resize: vertical; font-family: var(--mono); font-size: 13px; }

    .details { margin: 0; padding: 0; list-style: none; display: grid; gap: 6px; }
    .detail-line { display: flex; gap: 10px; align-items: baseline; padding: 9px 14px; background: var(--card); border: 1px solid var(--line); border-radius: 8px; font-size: 13px; color: var(--soft); line-height: 1.45; }

    .tabs-card { padding: 0 22px 22px; }
    .tabs { display: flex; flex-wrap: wrap; gap: 0 22px; border-bottom: 1px solid var(--line); margin-bottom: 18px; }
    .tab { min-height: 0; padding: 13px 2px 11px; border: 0; border-bottom: 2px solid transparent; border-radius: 0; background: none; color: var(--muted); font-size: 14px; }
    .tab:hover { background: none; color: var(--text); }
    .tab[aria-selected="true"] { color: var(--text); border-bottom-color: var(--brand); }
    .tab .count { margin-left: 6px; color: var(--muted); font-weight: 400; }
    .panel-head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 12px; }
    .panel-head h2 { font-size: 14px; }
    .link-list { list-style: none; margin: 0 0 18px; padding: 0; border: 1px solid var(--line-soft); border-radius: 8px; overflow: hidden; }
    .link-list li { border-top: 1px solid var(--line-soft); }
    .link-list li:first-child { border-top: 0; }
    .link-list a { display: block; padding: 11px 14px; font-size: 14px; }
    .link-list a:hover { background: var(--accent-soft); }
    .placeholder { padding: 32px; border: 1px dashed #C9CDD2; border-radius: 8px; color: var(--muted); text-align: center; background: var(--card); }
    .plot-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 16px; }
    .plot-card { display: block; min-height: 0; padding: 0; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; background: #fff; text-align: left; color: var(--text); font-weight: 400; }
    .plot-card:hover { border-color: var(--accent); background: #fff; }
    .plot-card img { display: block; width: 100%; aspect-ratio: 16 / 10; object-fit: contain; background: #FBFBFA; border-bottom: 1px solid var(--line-soft); }
    .plot-card span { display: block; padding: 10px 12px; font-size: 13px; font-weight: 600; }
    #lastActionSource > h2 { display: none; }
    #lastActionSource .panel { border: 0; padding: 0; }
    #lastActionSource code { font-family: var(--mono); font-size: 12px; }
    pre { margin: 10px 0 0; white-space: pre-wrap; overflow: auto; max-height: 24rem; padding: 14px 16px; background: #1F2328; color: #E6E8EB; border-radius: 8px; font-family: var(--mono); font-size: 12px; line-height: 1.55; }

    dialog.add-jobs { width: min(540px, calc(100vw - 32px)); padding: 0; border: 1px solid var(--line); border-radius: 12px; box-shadow: 0 16px 48px rgba(31,35,40,.25); color: var(--text); }
    dialog.add-jobs::backdrop { background: rgba(31,35,40,.45); }
    .dialog-body { display: grid; gap: 14px; padding: 22px 24px 24px; }
    .dialog-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .dialog-head h2 { font-size: 18px; }
    .dialog-body label { display: grid; gap: 6px; font-size: 13px; font-weight: 600; color: var(--soft); }
    .hint { font-size: 12px; font-weight: 400; color: var(--muted); }
    .drop-zone { border: 1px dashed var(--accent); border-radius: 8px; padding: 16px; background: var(--accent-soft); color: var(--accent); cursor: pointer; font-size: 13px; font-weight: 600; text-align: center; }
    .drop-zone.active { background: #E2ECF1; }
    .dialog-actions { display: flex; justify-content: flex-end; gap: 8px; }

    .lightbox { position: fixed; inset: 0; display: none; align-items: center; justify-content: center; background: rgba(20,24,28,.84); z-index: 30; padding: 2rem; overflow: auto; }
    .lightbox.open { display: flex; }
    .lightbox img { display: block; width: auto; height: auto; max-width: min(92vw, 1400px); max-height: 84vh; object-fit: contain; background: #fff; border-radius: 6px; }
    .lightbox button { position: absolute; }
    .lightbox .prev { left: 1rem; }
    .lightbox .next { right: 1rem; }
    .lightbox .close { top: 1rem; right: 1rem; }
    .lightbox .caption { position: absolute; bottom: 1rem; left: 0; right: 0; text-align: center; color: #fff; font-weight: 600; }
"""

DASHBOARD_RESPONSIVE_CSS = r"""
    @media (max-width: 980px) {
      body { display: block; height: auto; }
      .layout { grid-template-columns: 1fr; }
      .sidebar, .main { overflow: visible; }
      .sidebar { border-right: 0; border-bottom: 1px solid var(--line); }
      .batch-actions { padding-left: 0; border-left: 0; }
      .stepper { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .main { padding: 18px 16px 40px; }
    }
"""

DASHBOARD_SCRIPT = r"""
    const data = JSON.parse(document.getElementById('dashboard-data').textContent);
    let selected = 0;
    let activeTab = '';
    // Keep navigation state in this tab across action redirects and refreshes.
    const selectionKey = 'bsrn-dashboard-selected-job';
    const tabKey = 'bsrn-dashboard-selected-tab';
    try {
      const savedJob = sessionStorage.getItem(selectionKey);
      selected = Math.max(0, data.rows.findIndex(row => row.job === savedJob));
      activeTab = sessionStorage.getItem(tabKey) || '';
    } catch (_) { /* Storage may be disabled; keep the defaults. */ }
    let plotSet = [];
    let plotIndex = 0;
    const lastActionNode = document.getElementById('lastActionSource');
    const glyphs = {ok: '✓', warning: '▲', error: '✕', idle: '–', pending: '●'};
    function el(tag, attrs = {}, text = '') {
      const node = document.createElement(tag);
      Object.entries(attrs).forEach(([key, value]) => {
        if (key === 'class') node.className = value;
        else if (key === 'href') node.href = value;
        else if (key === 'title') node.title = value;
        else if (key === 'name') node.name = value;
        else if (key === 'value') node.value = value;
        else node.setAttribute(key, value);
      });
      if (text) node.textContent = text;
      return node;
    }
    function badge(value, kind) {
      const k = kind || 'idle';
      return el('span', {class: 'badge k-' + k}, glyphs[k] + ' ' + (value || 'Not run'));
    }
    function renderLinks(target, links) {
      target.replaceChildren();
      links.forEach(item => target.appendChild(el('a', {class: 'button ghost small', href: item.href}, item.label)));
    }
    function renderAlert() {
      const box = document.getElementById('lastActionAlert');
      const action = data.lastAction;
      if (!action || action.returncode === 0) return;
      const alert = el('div', {class: 'alert', role: 'alert'});
      alert.appendChild(el('strong', {}, glyphs.error + ' Last action failed: ' + action.title));
      alert.appendChild(el('span', {}, 'Exit code ' + action.returncode + ', finished ' + action.finished_at + '.'));
      alert.appendChild(el('span', {class: 'spacer'}));
      if (data.rows.length) {
        const show = el('button', {type: 'button', class: 'small'}, 'Show log');
        show.addEventListener('click', () => { setTab('log'); document.getElementById('detailTabs')?.scrollIntoView({block: 'start'}); });
        alert.appendChild(show);
      }
      box.replaceChildren(alert);
    }
    function renderSummary() {
      const box = document.getElementById('summary');
      const items = [
        ['Files', data.summary.files, ''],
        ['Minutes OK', data.summary.minutes_ok, data.summary.minutes_warning ? 'warn' : ''],
        ['Checks OK', data.summary.checks_ok, ''],
        ['QC complete', data.summary.qc_complete, ''],
        ['QC approved', data.summary.qc_approved, ''],
        ['Needs attention', data.summary.needs_attention, data.summary.needs_attention ? 'warn' : '']
      ];
      box.replaceChildren(...items.map(([label, value, kind]) => {
        const card = el('div', {class: 'metric ' + kind});
        card.appendChild(el('strong', {}, label));
        card.appendChild(el('span', {}, String(value)));
        return card;
      }));
    }
    function mark(label, kind, value) {
      const node = el('span', {class: 'mark', title: label + ': ' + (value || 'Not run')}, label + ' ');
      node.appendChild(el('span', {class: 'glyph g-' + kind}, glyphs[kind]));
      return node;
    }
    function renderList() {
      const list = document.getElementById('jobList');
      document.getElementById('jobCount').textContent = data.rows.length === 1 ? '1 station-month' : data.rows.length + ' station-months';
      list.replaceChildren();
      if (!data.rows.length) {
        list.appendChild(el('p', {}, 'No current run yet.'));
        return;
      }
      data.rows.forEach((row, index) => {
        const button = el('button', {class: 'job-button' + (index === selected ? ' active' : ''), type: 'button', 'aria-current': index === selected ? 'true' : 'false'});
        const top = el('span', {class: 'job-top'});
        top.appendChild(el('span', {class: 'job-name'}, row.job));
        top.appendChild(el('span', {class: 'glyph g-' + row.kinds.overall, title: 'Overall: ' + row.overall}, glyphs[row.kinds.overall]));
        button.appendChild(top);
        const marks = el('span', {class: 'job-marks'});
        marks.appendChild(mark('Meta', row.kinds.metadata, row.metadata));
        marks.appendChild(mark('Format', row.kinds.format, row.format));
        marks.appendChild(mark('QC', row.kinds.qc, row.qc));
        marks.appendChild(mark('Approval', row.kinds.gate, row.gate_label));
        button.appendChild(marks);
        button.addEventListener('click', () => {
          selected = index;
          try { sessionStorage.setItem(selectionKey, row.job); } catch (_) {}
          renderList(); renderDetail();
        });
        list.appendChild(button);
      });
    }
    function linkList(items) {
      const list = el('ul', {class: 'link-list'});
      items.forEach(item => {
        const li = el('li');
        li.appendChild(el('a', {href: item.href}, item.label));
        list.appendChild(li);
      });
      return list;
    }
    function postForm(action, label, fields = {}, css = 'primary') {
      const form = el('form', {method: 'post', action});
      form.appendChild(el('input', {type: 'hidden', name: 'csrf_token', value: data.csrfToken}));
      Object.entries(fields).forEach(([name, value]) => form.appendChild(el('input', {type: 'hidden', name, value})));
      form.appendChild(el('button', {type: 'submit', class: css}, label));
      return form;
    }
    function rejectForm(job) {
      const form = el('form', {method: 'post', action: '/reject-qc', class: 'reject-form'});
      form.appendChild(el('input', {type: 'hidden', name: 'csrf_token', value: data.csrfToken}));
      form.appendChild(el('input', {type: 'hidden', name: 'job', value: job}));
      form.appendChild(el('input', {name: 'note', placeholder: 'Reason or follow-up', 'aria-label': 'Reason for rejection'}));
      form.appendChild(el('button', {type: 'submit', class: 'danger'}, 'Reject QC'));
      return form;
    }
    function stepper(row) {
      const steps = [
        ['File', row.dat_href ? 'OK' : 'Not run', row.dat_href ? 'ok' : 'idle'],
        ['Metadata', row.metadata, row.kinds.metadata],
        ['Format', row.format, row.kinds.format],
        ['Minutes', row.minute_status, row.kinds.minutes],
        ['QC', row.qc, row.kinds.qc],
        ['Approval', row.gate_label, row.kinds.gate],
        ['Import', row.import_status === 'Import files generated' ? 'OK' : 'Not run', row.kinds.import]
      ];
      const wrap = el('ol', {class: 'stepper', 'aria-label': 'Pipeline progress'});
      steps.forEach(([label, value, kind]) => {
        const li = el('li', {class: 'step k-' + kind, title: label + ': ' + (value || 'Not run')});
        li.appendChild(el('span', {class: 'step-bar'}));
        li.appendChild(el('span', {class: 'step-label'}, label));
        li.appendChild(el('span', {class: 'step-value g-' + kind}, glyphs[kind] + ' ' + (value || 'Not run')));
        wrap.appendChild(li);
      });
      return wrap;
    }
    function actionCard(row) {
      const node = el('section', {class: 'card next-step', 'aria-label': 'Next step'});
      const text = el('div', {class: 'next-text'});
      text.appendChild(el('div', {class: 'eyebrow'}, 'Next step'));
      const actions = el('div', {class: 'button-row'});
      let title = row.gate_label || 'No action available';
      let detail = row.gate_detail || '';
      if (row.action_kind === 'continue') {
        title = 'Run QC';
        detail = 'Checks are finished. Continue to QC runs QC for all files in the batch that are ready.';
        actions.appendChild(postForm('/continue-qc', 'Continue to QC'));
      } else if (row.action_kind === 'curator') {
        title = row.gate_label === 'Awaiting curator approval' ? 'Review QC and decide' : row.gate_label;
        detail = 'Check the QC plots and reports, then approve or reject this station-month.';
        const plots = el('button', {type: 'button'}, 'View QC plots');
        plots.addEventListener('click', () => setTab('plots'));
        actions.appendChild(plots);
        actions.appendChild(postForm('/approve-qc', 'Approve QC', {job: row.job}));
        actions.appendChild(rejectForm(row.job));
      } else if (row.action_kind === 'ready') {
        title = 'Generate import files';
        detail = 'QC is approved. Generate the PANGAEA import files for this station-month.';
        actions.appendChild(postForm('/generate-import-files', 'Generate import files', {job: row.job}));
      } else if (row.action_kind === 'import_done') {
        title = 'Import files generated';
        detail = 'Import files are available in the Import files tab.';
        row.import_artifacts.forEach(item => actions.appendChild(el('a', {class: 'button', href: item.href}, item.label)));
      } else if (!detail) {
        detail = 'No action available.';
      }
      text.appendChild(el('div', {class: 'next-title'}, title));
      text.appendChild(el('div', {class: 'next-detail'}, detail));
      node.appendChild(text);
      node.appendChild(actions);
      return node;
    }
    function tabsFor(row) {
      const plots = row.qc_artifacts.filter(item => item.kind === 'plot');
      const reports = row.qc_artifacts.filter(item => item.kind !== 'plot');
      return [
        {id: 'plots', label: 'QC plots', count: plots.length, render: () => plotsPanel(plots)},
        {id: 'files', label: 'Files & reports', count: row.file_links.length + reports.length, render: () => filesPanel(row, reports)},
        {id: 'exports', label: 'Data exports', count: row.data_exports.length, render: () => exportsPanel(row)},
        {id: 'import', label: 'Import files', count: row.import_artifacts.length, render: () => importPanel(row)},
        {id: 'log', label: 'Log', count: null, render: () => logPanel()}
      ];
    }
    function panelHead(title, value, kind) {
      const head = el('div', {class: 'panel-head'});
      head.appendChild(el('h2', {}, title));
      if (value !== undefined) head.appendChild(badge(value, kind));
      return head;
    }
    function plotsPanel(plotLinks) {
      const wrap = el('div');
      if (!plotLinks.length) {
        wrap.appendChild(el('div', {class: 'placeholder'}, 'No plots yet - run QC to generate.'));
        return wrap;
      }
      const grid = el('div', {class: 'plot-grid'});
      plotLinks.forEach((item, index) => {
        const fig = el('button', {class: 'plot-card', type: 'button'});
        fig.appendChild(el('img', {src: plotImageSrc(item), alt: item.label, loading: 'lazy'}));
        fig.appendChild(el('span', {}, item.label));
        fig.addEventListener('click', () => openLightbox(plotLinks, index));
        grid.appendChild(fig);
      });
      wrap.appendChild(grid);
      return wrap;
    }
    function filesPanel(row, reports) {
      const wrap = el('div');
      wrap.appendChild(panelHead('Files', row.files_status, row.kinds.files));
      if (row.file_links.length) wrap.appendChild(linkList(row.file_links));
      else wrap.appendChild(el('p', {}, 'No files linked yet.'));
      wrap.appendChild(panelHead('QC reports', row.qc, row.kinds.qc));
      if (reports.length) wrap.appendChild(linkList(reports));
      else wrap.appendChild(el('p', {}, 'No QC artifacts yet.'));
      return wrap;
    }
    function exportsPanel(row) {
      const wrap = el('div');
      const head = panelHead('Data exports', row.data_export_status, row.kinds.exports);
      if (row.can_export_data) head.appendChild(postForm('/export-data', 'Export data', {job: row.job}, row.data_exports.length ? '' : 'primary'));
      wrap.appendChild(head);
      if (row.data_exports.length) wrap.appendChild(linkList(row.data_exports));
      else wrap.appendChild(el('p', {}, 'No data exports yet.'));
      return wrap;
    }
    function importPanel(row) {
      const wrap = el('div');
      wrap.appendChild(panelHead('Import artifacts', row.import_status, row.kinds.import));
      if (row.import_artifacts.length) wrap.appendChild(linkList(row.import_artifacts));
      else wrap.appendChild(el('p', {}, 'No import files generated yet.'));
      return wrap;
    }
    function logPanel() {
      const wrap = el('div', {class: 'log-panel'});
      if (lastActionNode && lastActionNode.childElementCount) {
        lastActionNode.hidden = false;
        wrap.appendChild(lastActionNode);
      } else {
        wrap.appendChild(el('p', {}, 'No dashboard action has run yet.'));
      }
      return wrap;
    }
    function setTab(id) {
      activeTab = id;
      try { sessionStorage.setItem(tabKey, id); } catch (_) {}
      renderDetail();
    }
    function renderDetail() {
      const detail = document.getElementById('detail');
      if (!data.rows.length) {
        const empty = el('div', {class: 'placeholder'});
        empty.appendChild(el('p', {}, 'Start with a job code or local DAT file.'));
        const add = el('button', {type: 'button', class: 'primary'}, 'Add jobs');
        add.addEventListener('click', openAddJobs);
        empty.appendChild(add);
        const nodes = [empty];
        if (lastActionNode && lastActionNode.childElementCount) {
          lastActionNode.hidden = false;
          const log = el('section', {class: 'card tabs-card'});
          log.appendChild(el('div', {class: 'tabs'}, ''));
          log.firstChild.appendChild(el('span', {class: 'tab', 'aria-selected': 'true'}, 'Log'));
          log.appendChild(lastActionNode);
          nodes.push(log);
        }
        const wrap = el('section', {style: 'display: grid; gap: 18px'});
        wrap.replaceChildren(...nodes);
        detail.replaceChildren(wrap);
        return;
      }
      const row = data.rows[selected];
      const section = el('section', {style: 'display: grid; gap: 18px'});
      const head = el('div', {class: 'subhead'});
      head.appendChild(el('h1', {}, row.job));
      head.appendChild(badge(row.overall, row.kinds.overall));
      section.appendChild(head);
      section.appendChild(stepper(row));
      section.appendChild(actionCard(row));
      const lines = [];
      if (row.minute_detail) lines.push(row.minute_detail);
      lines.push(...row.details);
      if (lines.length) {
        const details = el('ul', {class: 'details', 'aria-label': 'Check details'});
        lines.forEach(text => details.appendChild(el('li', {class: 'detail-line'}, text)));
        section.appendChild(details);
      }
      const tabs = tabsFor(row);
      if (!tabs.some(tab => tab.id === activeTab)) activeTab = '';
      if (!activeTab) activeTab = tabs[0].count ? 'plots' : 'files';
      const card = el('section', {class: 'card tabs-card', id: 'detailTabs'});
      const bar = el('div', {class: 'tabs', role: 'tablist', 'aria-label': 'Job outputs'});
      tabs.forEach(tab => {
        const button = el('button', {type: 'button', class: 'tab', role: 'tab', 'aria-selected': tab.id === activeTab ? 'true' : 'false'}, tab.label);
        if (tab.count !== null) button.appendChild(el('span', {class: 'count num'}, String(tab.count)));
        button.addEventListener('click', () => setTab(tab.id));
        bar.appendChild(button);
      });
      card.appendChild(bar);
      const panel = el('div', {role: 'tabpanel'});
      panel.appendChild(tabs.find(tab => tab.id === activeTab).render());
      card.appendChild(panel);
      section.appendChild(card);
      detail.replaceChildren(section);
    }
    const addDialog = document.getElementById('addJobsDialog');
    function openAddJobs() {
      if (addDialog && typeof addDialog.showModal === 'function') addDialog.showModal();
    }
    document.getElementById('openAddJobs').addEventListener('click', openAddJobs);
    addDialog.querySelectorAll('[data-close-dialog]').forEach(button => button.addEventListener('click', () => addDialog.close()));
    // Close the modal on submit so the loading overlay is not hidden behind it.
    addDialog.querySelector('form').addEventListener('submit', () => addDialog.close());
    function appendLocalInputFiles(files) {
      const textarea = document.getElementById('localFileInput');
      if (!textarea || !files || !files.length) return;
      const existing = textarea.value.split(/[\r\n;]+/).map(value => value.trim()).filter(Boolean);
      const seen = new Set(existing.map(value => value.toLowerCase()));
      Array.from(files).forEach(file => {
        if (!file.name.toLowerCase().endsWith('.dat')) return;
        const path = 'input\\' + file.name;
        if (!seen.has(path.toLowerCase())) {
          existing.push(path);
          seen.add(path.toLowerCase());
        }
      });
      textarea.value = existing.join('\n');
    }
    const dropZone = document.getElementById('localDropZone');
    const picker = document.getElementById('localFilePicker');
    if (dropZone && picker) {
      dropZone.addEventListener('click', () => picker.click());
      dropZone.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); picker.click(); }
      });
      picker.addEventListener('change', event => appendLocalInputFiles(event.target.files));
      ['dragenter', 'dragover'].forEach(type => dropZone.addEventListener(type, event => {
        event.preventDefault();
        dropZone.classList.add('active');
      }));
      ['dragleave', 'drop'].forEach(type => dropZone.addEventListener(type, event => {
        event.preventDefault();
        dropZone.classList.remove('active');
      }));
      dropZone.addEventListener('drop', event => appendLocalInputFiles(event.dataTransfer.files));
    }
    function plotImageSrc(item) { const version = item.version || data.lastRefreshed; return version ? item.href + '?v=' + encodeURIComponent(version) : item.href; }
    function openLightbox(items, index) { plotSet = items; plotIndex = index; updateLightbox(); document.getElementById('lightbox').classList.add('open'); }
    function updateLightbox() { const img = document.querySelector('#lightbox img'); if (plotSet.length) { img.src = plotImageSrc(plotSet[plotIndex]); img.alt = plotSet[plotIndex].label; document.querySelector('#lightbox .caption').textContent = plotSet[plotIndex].label; } }
    function lightboxOpen() { return document.getElementById('lightbox').classList.contains('open'); }
    function movePlot(delta) { if (!plotSet.length) return; plotIndex = (plotIndex + delta + plotSet.length) % plotSet.length; updateLightbox(); }
    document.querySelector('#lightbox .close').addEventListener('click', () => document.getElementById('lightbox').classList.remove('open'));
    document.querySelector('#lightbox .prev').addEventListener('click', () => movePlot(-1));
    document.querySelector('#lightbox .next').addEventListener('click', () => movePlot(1));
    document.getElementById('lightbox').addEventListener('click', event => { if (event.target.id === 'lightbox') event.currentTarget.classList.remove('open'); });
    document.addEventListener('keydown', event => { if (!lightboxOpen()) return; if (event.key === 'Escape') document.getElementById('lightbox').classList.remove('open'); if (event.key === 'ArrowLeft') movePlot(-1); if (event.key === 'ArrowRight') movePlot(1); });
    document.getElementById('lastRefreshed').textContent = data.lastRefreshed;
    renderLinks(document.getElementById('globalLinks'), data.globalLinks);
    renderAlert(); renderSummary(); renderList(); renderDetail();
    document.querySelector('.job-button.active')?.scrollIntoView({block: 'nearest'});
"""


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
    last_result = LAST_ACTION or read_last_action_log()
    payload = {
        "rows": rows,
        "summary": summary,
        "globalLinks": global_links,
        "lastRefreshed": now,
        "lastAction": (
            {"title": last_result.title, "finished_at": last_result.finished_at, "returncode": last_result.returncode}
            if last_result is not None
            else None
        ),
        "csrfToken": CSRF_TOKEN,
    }
    data_json = json_for_html_script(payload)
    last_action = render_last_action()
    batch_actions = render_header_batch_actions(statuses)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BSRN Workflow Dashboard</title>
  <style>
    {DASHBOARD_CSS}
    {loading_overlay_css()}
    {DASHBOARD_RESPONSIVE_CSS}
  </style>
</head>
<body>
  <header class="topbar">
    <div class="brand">
      {LOGO_SVG}
      <div>
        <div class="title">BSRN Workflow Dashboard</div>
        <div class="workspace">output/current · refreshed <span id="lastRefreshed" class="num"></span></div>
      </div>
    </div>
    <nav class="global-links" id="globalLinks" aria-label="Batch files"></nav>
    <div class="spacer"></div>
    <div class="batch-actions" role="group" aria-label="Actions for all files">
      <span class="batch-label">All files</span>
      {batch_actions}
    </div>
    <a class="button" href="/" title="Refresh dashboard">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true" focusable="false"><path d="M20 11a8 8 0 1 0-2.3 5.7"></path><path d="M20 4v7h-7"></path></svg>
      Refresh
    </a>
  </header>
  <div class="layout">
    <aside class="sidebar">
      <div class="side-head">
        <div>
          <div class="side-label">Current batch</div>
          <div class="side-count num" id="jobCount"></div>
        </div>
        <button type="button" class="small" id="openAddJobs">+ Add jobs</button>
      </div>
      <div class="job-list" id="jobList"></div>
      <details class="workspace-menu" id="newWorkflowPanel">
        <summary>Workspace actions</summary>
        <form method="post" action="/refresh-reference-ids">
          {csrf_input()}
          <button type="submit">Update reference IDs</button>
        </form>
        <form method="post" action="/new-workflow">
          {csrf_input()}
          <label class="checkline"><input type="checkbox" name="confirm_new" value="1"> Archive current workspace first</label>
          <button type="submit" class="danger">New workflow</button>
        </form>
      </details>
    </aside>
    <main class="main">
      <div id="lastActionAlert"></div>
      <section class="summary" id="summary" aria-label="Batch summary"></section>
      <section id="detail"></section>
    </main>
  </div>
  <div id="lastActionSource" hidden>{last_action}</div>
  <dialog class="add-jobs" id="addJobsDialog" aria-labelledby="addJobsTitle">
    <form method="post" action="/run-download" class="dialog-body">
      {csrf_input()}
      <div class="dialog-head">
        <h2 id="addJobsTitle">Add station-months</h2>
        <button type="button" class="ghost small" data-close-dialog aria-label="Close">Close</button>
      </div>
      <label>Job codes from BSRN FTP<textarea name="job" placeholder="gob0624&#10;cab0425 pay0123"></textarea></label>
      <label>Local DAT files <span class="hint">(fill either job codes or local files, not both)</span><textarea id="localFileInput" name="local_file" placeholder="input\\cab0325.dat&#10;input\\pay0123.dat"></textarea></label>
      <input id="localFilePicker" type="file" accept=".dat" multiple hidden>
      <div id="localDropZone" class="drop-zone" role="button" tabindex="0">Drop or select DAT files from input</div>
      <div class="dialog-actions">
        <button type="button" data-close-dialog>Cancel</button>
        <button type="submit" class="primary">Run download/check</button>
      </div>
    </form>
  </dialog>
  <div class="lightbox" id="lightbox"><button class="close" type="button">Close</button><button class="prev" type="button" aria-label="Previous plot">&lt;</button><img alt=""><button class="next" type="button" aria-label="Next plot">&gt;</button><div class="caption"></div></div>
  {loading_overlay_html()}
  <script id="dashboard-data" type="application/json">{data_json}</script>
  <script>
    {loading_overlay_script()}
    {DASHBOARD_SCRIPT}
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
        job = html.escape(str(row.get("job") or ""), quote=True)
        action_html = (
            f'<form method="post" action="/generate-import-files">{csrf_input()}'
            f'<input type="hidden" name="job" value="{job}">'
            '<button type="submit">Generate import files</button></form>'
        )
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
    reference_links = link(workflow_path(row.get("batch_reference_import_file") or row.get("reference_import_file")), "Reference import")
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
            self.handle_generate_import_files(form)
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

    def handle_generate_import_files(self, form: dict[str, list[str]]) -> None:
        command = [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "bsrn_import_files.py"),
            "--status",
            str(STATUS_PATH),
            "--dashboard",
            str(DASHBOARD_PATH),
        ]
        job = first_value(form, "job")
        if job:
            command.extend(["--job", job])
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
