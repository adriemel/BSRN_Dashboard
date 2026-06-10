#!/usr/bin/env python3
"""
Download, unpack, metadata-check, and format-check BSRN station-to-archive files.

This is a non-GUI entry point for agentic workflows. It deliberately reuses the
existing BSRN Toolbox ingestor/converter for LR0001-LR0009 metadata exports.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import configparser
import ftplib
import gzip
import hashlib
import html
import json
import os
import re
import ssl
import shutil
import subprocess
import sys
import traceback
import types
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

try:
    from scripts.bsrn_station_registry import load_station_codes, resolve_station_entry
except ModuleNotFoundError:
    from bsrn_station_registry import load_station_codes, resolve_station_entry


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLBOX_ROOT = PROJECT_ROOT / "tools" / "download-extract" / "BSRN_Toolbox_py"
FORMAT_CHECK_SOURCE = PROJECT_ROOT / "tools" / "format-check" / "f_check_V3_4.c"
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "bsrn_workflow.ini"
DEFAULT_IDS_DIR = PROJECT_ROOT / "tools" / "create-importfiles"
BSRN_IDS_URL = "https://store.pangaea.de/config/bsrn/BSRN_IDs.txt"
BSRN_REFERENCE_IDS_URL = "https://www.pangaea.de/ddi?request=bsrn/BSRNReferences&format=textfile&charset=UTF-8"
BSRN_REFERENCE_IDS_FILE = "BSRN_Reference_IDs.txt"
CURATOR_DECISIONS_FILE = "curator_decisions.json"
PARENT_IDS_FILE = PROJECT_ROOT / "metadata" / "ParentIDs.txt"
MISSING_ID_VALUES = {"", "-999", "-9999"}


STATIONS = load_station_codes(DEFAULT_IDS_DIR / "BSRN_IDs.txt")


@dataclass(frozen=True)
class Job:
    station: str
    year: int
    month: int

    @property
    def base(self) -> str:
        return f"{self.station.lower()}{self.month:02d}{self.year % 100:02d}"

    @property
    def gz_name(self) -> str:
        return f"{self.base}.dat.gz"

    @property
    def dat_name(self) -> str:
        return f"{self.base}.dat"

    @property
    def label(self) -> str:
        return f"{self.station.upper()}_{self.year}-{self.month:02d}"


@dataclass
class JobStatus:
    job: str
    source: str | None = None
    gz_path: str | None = None
    dat_path: str | None = None
    metadata_dir: str | None = None
    reference_import_file: str | None = None
    batch_reference_import_file: str | None = None
    batch_metadata_reports: dict[str, str] = field(default_factory=dict)
    pangaea_reference_uri: str | None = None
    pangaea_reference_id: int | None = None
    reference_id_warning: str | None = None
    pangaea_parent_id: int | None = None
    parent_id_comment: str | None = None
    format_report: str | None = None
    batch_format_report: str | None = None
    minute_completeness: dict[str, object] = field(default_factory=dict)
    qc_report: str | None = None
    qc_dir: str | None = None
    qc_ok: bool = False
    qc_outputs: list[str] = field(default_factory=list)
    qc_warnings: list[str] = field(default_factory=list)
    data_export_dir: str | None = None
    data_export_outputs: list[str] = field(default_factory=list)
    data_export_warnings: list[str] = field(default_factory=list)
    metadata_warnings: list[str] = field(default_factory=list)
    import_dir: str | None = None
    import_outputs: list[str] = field(default_factory=list)
    import_warnings: list[str] = field(default_factory=list)
    import_ok: bool = False
    downloaded: bool = False
    decompressed: bool = False
    metadata_ok: bool = False
    format_ok: bool = False
    errors: list[str] = field(default_factory=list)


class WorkflowError(Exception):
    pass


def status_from_dict(row: dict) -> JobStatus:
    fields = JobStatus.__dataclass_fields__
    values = {name: row.get(name) for name in fields}
    for list_field in (
        "qc_outputs",
        "qc_warnings",
        "data_export_outputs",
        "data_export_warnings",
        "metadata_warnings",
        "import_outputs",
        "import_warnings",
        "errors",
    ):
        if values.get(list_field) is None:
            values[list_field] = []
    for dict_field in ("batch_metadata_reports", "minute_completeness"):
        if values.get(dict_field) is None:
            values[dict_field] = {}
    return JobStatus(**values)


def load_statuses(status_path: Path) -> list[JobStatus]:
    rows = json.loads(status_path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise WorkflowError(f"Status JSON must contain a list: {status_path}")
    return [status_from_dict(row) for row in rows]


def save_statuses(status_path: Path, statuses: list[JobStatus]) -> None:
    status_path.write_text(json.dumps([asdict(status) for status in statuses], indent=2), encoding="utf-8")


def resolve_project_path(value: str | Path | None, default: str | Path | None = None) -> Path:
    path = Path(value if value is not None else default if default is not None else "")
    return path if path.is_absolute() else PROJECT_ROOT / path


def json_for_html_script(value: object) -> str:
    return json.dumps(value, ensure_ascii=True).replace("</", "<\\/")


def write_exception_log(logs_dir: Path, name: str, exc: BaseException) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._") or "workflow_error"
    path = logs_dir / f"{safe_name}.traceback.txt"
    path.write_text("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)), encoding="utf-8")
    return path


class ToolboxModelAdapter:
    """Small non-GUI model surface needed by Ingestor, Converter, and BsrnIdSystem."""

    def __init__(self, ids_dir: Path, verbose: bool = False, debug: bool = False):
        if str(TOOLBOX_ROOT) not in sys.path:
            sys.path.insert(0, str(TOOLBOX_ROOT))
        ensure_optional_requests_stub()
        from logic.bsrn_id_system import BsrnIdSystem
        from logic.helper import SmartPrinter

        self._verbose = verbose
        self._debug = debug
        self._ids_dir = ids_dir
        self._printer = SmartPrinter(verbose=verbose, debug=debug)
        self._bsrn_ids = BsrnIdSystem(self)

    def get_smart_printer(self):
        return self._printer

    def get_user_lookup_dir(self):
        return self._ids_dir

    def get_bsrn_id_system(self):
        return self._bsrn_ids

    def is_verbose(self):
        return self._verbose

    def is_debug(self):
        return self._debug

    def get_observer(self):
        return None

    def get_metadata_recs_int(self):
        return list(range(1, 10))

    def get_data_recs_int(self):
        return [100, 300, 400, 500, 1000, 1100, 1200, 1300, 1500, 3010, 3030, 4000]

    def get_data_recs_str(self):
        return [f"{record:04d}" for record in self.get_data_recs_int()]

    def get_stations_lower(self):
        return [station.lower() for station in self.get_stations()]

    def get_stations(self):
        stations = load_station_codes(self._ids_dir / "BSRN_IDs.txt")
        return stations or STATIONS

    def get_stations_num(self):
        return len(self.get_stations())

    def get_months_int(self):
        return list(range(1, 13))

    def get_years(self):
        return list(range(1992, 2031))

    def get_year_lowest_short(self):
        return 92


def ensure_optional_requests_stub() -> None:
    try:
        import requests  # noqa: F401
    except ModuleNotFoundError:
        requests_stub = types.ModuleType("requests")

        def _get(*_args, **_kwargs):
            raise RuntimeError("requests is not installed and BSRN_IDs.txt is not available locally")

        requests_stub.get = _get
        sys.modules["requests"] = requests_stub


def load_config(path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if path.exists():
        cfg.read(path, encoding="utf-8")
    return cfg


def refresh_bsrn_ids(ids_dir: Path, url: str = BSRN_IDS_URL) -> str | None:
    """Refresh BSRN_IDs.txt so reruns can pick up curator-added PANGAEA IDs."""

    ids_dir.mkdir(parents=True, exist_ok=True)
    target = ids_dir / "BSRN_IDs.txt"
    curl = shutil.which("curl")
    if curl:
        command = [curl, "-fsSL", "--retry", "2"]
        if os.name == "nt":
            command.append("--ssl-no-revoke")
        command.extend(["-o", str(target), url])
        proc = subprocess.run(command, text=True, capture_output=True)
        if proc.returncode == 0:
            return None
        if target.exists():
            return f"Could not refresh BSRN_IDs.txt from {url}; using existing local copy: {proc.stderr.strip() or proc.stdout.strip()}"
        raise WorkflowError(f"Could not download BSRN_IDs.txt from {url}: {proc.stderr.strip() or proc.stdout.strip()}")
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            content = response.read()
        target.write_bytes(content)
        return None
    except (OSError, urllib.error.URLError) as exc:
        if target.exists():
            return f"Could not refresh BSRN_IDs.txt from {url}; using existing local copy: {exc}"
        raise WorkflowError(f"Could not download BSRN_IDs.txt from {url}: {exc}") from exc


def refresh_reference_ids(ids_dir: Path, url: str = BSRN_REFERENCE_IDS_URL) -> str | None:
    """Refresh the cached PANGAEA BSRN reference-ID list on explicit request."""

    ids_dir.mkdir(parents=True, exist_ok=True)
    target = ids_dir / BSRN_REFERENCE_IDS_FILE
    curl = shutil.which("curl")
    if curl:
        command = [curl, "-fsSL", "--retry", "2"]
        if os.name == "nt":
            command.append("--ssl-no-revoke")
        command.extend(["-o", str(target), url])
        proc = subprocess.run(command, text=True, capture_output=True)
        if proc.returncode == 0:
            return None
        if target.exists():
            return (
                f"Could not refresh {BSRN_REFERENCE_IDS_FILE} from {url}; "
                f"using existing local copy: {proc.stderr.strip() or proc.stdout.strip()}"
            )
        raise WorkflowError(f"Could not download {BSRN_REFERENCE_IDS_FILE} from {url}: {proc.stderr.strip() or proc.stdout.strip()}")
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            content = response.read()
        target.write_bytes(content)
        return None
    except (OSError, urllib.error.URLError) as exc:
        if target.exists():
            return f"Could not refresh {BSRN_REFERENCE_IDS_FILE} from {url}; using existing local copy: {exc}"
        raise WorkflowError(f"Could not download {BSRN_REFERENCE_IDS_FILE} from {url}: {exc}") from exc


def load_reference_id_cache(ids_dir: Path) -> tuple[dict[str, int], str | None]:
    """Load cached PANGAEA BSRN reference IDs keyed by station-to-archive FTP URI."""

    cache_path = ids_dir / BSRN_REFERENCE_IDS_FILE
    if not cache_path.exists() or cache_path.stat().st_size == 0:
        return {}, f"{BSRN_REFERENCE_IDS_FILE} is missing or empty; run again with --refresh-reference-ids after importing the reference in PANGAEA."

    lookup: dict[str, int] = {}
    rows = read_tsv(cache_path)
    for row in rows:
        parsed = parse_reference_id_row(row)
        if parsed is None:
            continue
        uri, reference_id = parsed
        lookup[uri] = reference_id
    if not lookup:
        return {}, f"{BSRN_REFERENCE_IDS_FILE} did not contain parseable numeric PANGAEA reference IDs."
    return lookup, None


def load_parent_id_map(path: Path = PARENT_IDS_FILE) -> dict[str, tuple[int, str]]:
    """Load station-wide PANGAEA collection ParentID mappings from metadata/ParentIDs.txt."""

    if not path.exists() or path.stat().st_size == 0:
        return {}
    rows = read_tsv(path)
    if not rows:
        return {}

    header = [cell.strip().lower() for cell in rows[0]]
    acronym_idx = header.index("acronym") if "acronym" in header else 0
    parent_idx = header.index("parentid") if "parentid" in header else 1
    comment_idx = header.index("comment") if "comment" in header else 2

    mapping: dict[str, tuple[int, str]] = {}
    for row in rows[1:]:
        if len(row) <= max(acronym_idx, parent_idx):
            continue
        acronym = row[acronym_idx].strip().upper()
        parent_id_text = row[parent_idx].strip()
        if not acronym or not re.fullmatch(r"\d+", parent_id_text):
            continue
        comment = row[comment_idx].strip() if len(row) > comment_idx else ""
        mapping[acronym] = (int(parent_id_text), comment)
    return mapping


def attach_parent_id_status(status: JobStatus, parent_ids: dict[str, tuple[int, str]]) -> None:
    station = status.job.split("_", 1)[0].upper()
    parent = parent_ids.get(station)
    if parent is None:
        return
    status.pangaea_parent_id, status.parent_id_comment = parent


def parse_reference_id_row(row: list[str]) -> tuple[str, int] | None:
    uri = next((value.strip() for value in row if value.strip().startswith("ftp://ftp.bsrn.awi.de/")), "")
    if not uri:
        return None
    for value in row:
        text = value.strip()
        if re.fullmatch(r"\d+", text):
            return uri, int(text)
    return None


def reference_uri_from_import_file(path: Path) -> str:
    rows = read_tsv(path)
    if len(rows) < 2:
        raise WorkflowError(f"Cannot resolve PANGAEA reference ID: {path.name} has no reference row")
    header = rows[0]
    if "URI" not in header:
        raise WorkflowError(f"Cannot resolve PANGAEA reference ID: {path.name} has no URI column")
    uri_idx = header.index("URI")
    uri = rows[1][uri_idx].strip() if len(rows[1]) > uri_idx else ""
    if not uri:
        raise WorkflowError(f"Cannot resolve PANGAEA reference ID: {path.name} has a blank URI")
    return uri


def attach_reference_id_status(status: JobStatus, reference_lookup: dict[str, int], cache_warning: str | None) -> None:
    if not status.reference_import_file:
        return
    uri = reference_uri_from_import_file(workflow_path(status.reference_import_file))
    status.pangaea_reference_uri = uri
    reference_id = reference_lookup.get(uri)
    if reference_id is not None:
        status.pangaea_reference_id = reference_id
        return
    if cache_warning:
        status.reference_id_warning = f"PANGAEA reference ID unavailable for {uri}. {cache_warning}"
    else:
        status.reference_id_warning = (
            f"PANGAEA reference ID missing for {uri}. Import the generated reference import file into PANGAEA, "
            "then rerun download/check with --refresh-reference-ids."
        )


def parse_job_code(code: str) -> Job:
    name = Path(code).name.lower()
    if name.endswith(".dat.gz"):
        name = name[:-7]
    elif name.endswith(".dat"):
        name = name[:-4]
    if len(name) != 7:
        raise argparse.ArgumentTypeError(f"Invalid BSRN job/file code: {code}")
    station = name[:3].upper()
    try:
        month = int(name[3:5])
        yy = int(name[5:7])
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid BSRN job/file code: {code}") from exc
    year = 1900 + yy if yy >= 92 else 2000 + yy
    return Job(station=station, year=year, month=month)


def validate_job(job: Job, station_codes: Iterable[str] | None = None) -> Job:
    valid_stations = set(station_codes or STATIONS)
    if job.station.upper() not in valid_stations:
        raise argparse.ArgumentTypeError(f"Unknown station acronym: {job.station}")
    if not 1 <= job.month <= 12:
        raise argparse.ArgumentTypeError(f"Month must be 1-12: {job.month}")
    if not 1992 <= job.year <= 2030:
        raise argparse.ArgumentTypeError(f"Year must be 1992-2030: {job.year}")
    return Job(station=job.station.upper(), year=job.year, month=job.month)


def expand_jobs(args: argparse.Namespace) -> list[Job]:
    jobs: list[Job] = []
    ids_dir = resolve_project_path(args.ids_dir or DEFAULT_IDS_DIR)
    station_codes = load_station_codes(ids_dir / "BSRN_IDs.txt") or STATIONS
    for code in args.job or []:
        jobs.append(validate_job(parse_job_code(code), station_codes))
    if args.jobs_file:
        with Path(args.jobs_file).open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith("#"):
                    jobs.append(validate_job(parse_job_code(line), station_codes))
    if args.station or args.year or args.month:
        if not (args.station and args.year and args.month):
            raise WorkflowError("--station, --year, and --month must be used together")
        for station in args.station:
            for year in args.year:
                for month in args.month:
                    jobs.append(validate_job(Job(station=station, year=year, month=month), station_codes))
    deduped = list({job.base: job for job in jobs}.values())
    if not deduped and not args.local_file:
        raise WorkflowError("No jobs requested. Use --job, --jobs-file, or --station/--year/--month.")
    return sorted(deduped, key=lambda job: (job.station, job.year, job.month))


def create_run_dirs(output_root: Path, run_id: str | None, archive: bool = False) -> dict[str, Path]:
    if run_id is not None:
        root = output_root / "runs" / run_id
    elif archive:
        root = output_root / "runs" / datetime.now().strftime("%Y%m%d_%H%M%S")
    else:
        root = output_root / "current"
        if root.exists():
            shutil.rmtree(root)
    dirs = {
        "root": root,
        "downloads_gz": root / "downloads_gz",
        "dat": root / "dat",
        "metadata": root / "metadata",
        "format_reports": root / "format_reports",
        "qc_reports": root / "qc_reports",
        "logs": root / "logs",
    }
    root.mkdir(parents=True, exist_ok=True)
    return dirs


def download_job(job: Job, cfg: configparser.ConfigParser, download_dir: Path) -> Path:
    download_dir.mkdir(parents=True, exist_ok=True)
    host = cfg.get("ftp", "host", fallback="ftp.bsrn.awi.de")
    user = os.environ.get("BSRN_FTP_USER") or cfg.get("ftp", "user", fallback="anonymous")
    password = os.environ.get("BSRN_FTP_PASSWORD") or cfg.get("ftp", "password", fallback="anonymous@")
    timeout = cfg.getint("ftp", "timeout_seconds", fallback=60)
    protocol = cfg.get("ftp", "protocol", fallback="ftps").strip().lower()
    remote_dir = cfg.get("ftp", "remote_root", fallback="/").rstrip("/")
    remote_path = f"{remote_dir}/{job.station.lower()}/{job.gz_name}" if remote_dir else f"/{job.station.lower()}/{job.gz_name}"
    local_path = download_dir / job.gz_name
    if protocol in {"ftps", "ftp_tls", "ftpes"}:
        curl = shutil.which("curl")
        if curl:
            url = f"ftp://{host}{remote_path}"
            command = [
                curl,
                "--fail",
                "--show-error",
                "--location",
                "--ssl-reqd",
                "--connect-timeout",
                str(timeout),
                "--max-time",
                str(timeout),
                "--config",
                "-",
                "--output",
                str(local_path),
                url,
            ]
            if os.name == "nt":
                command.insert(4, "--ssl-no-revoke")
            # Pass credentials via a stdin config instead of the command line, so
            # they are not visible in the process list / Task Manager command line.
            credential_blob = f'{user}:{password}'.replace("\\", "\\\\").replace('"', '\\"')
            proc = subprocess.run(
                command,
                text=True,
                capture_output=True,
                input=f'user = "{credential_blob}"\n',
            )
            if proc.returncode != 0:
                if local_path.exists():
                    local_path.unlink()
                raise WorkflowError(f"FTPS download failed for {job.gz_name}: {proc.stderr.strip() or proc.stdout.strip()}")
            return local_path
        ftp_class = ftplib.FTP_TLS
        ftp_kwargs = {"context": ssl.create_default_context()}
    elif protocol == "ftp":
        ftp_class = ftplib.FTP
        ftp_kwargs = {}
    else:
        raise WorkflowError(f"Unsupported FTP protocol {protocol!r}; use 'ftps' or 'ftp'.")
    with ftp_class(host, timeout=timeout, **ftp_kwargs) as ftp:
        ftp.login(user=user, passwd=password)
        if isinstance(ftp, ftplib.FTP_TLS):
            ftp.prot_p()
        with local_path.open("wb") as handle:
            ftp.retrbinary(f"RETR {remote_path}", handle.write)
    return local_path


def decompress_gzip(gz_path: Path, dat_dir: Path) -> Path:
    dat_dir.mkdir(parents=True, exist_ok=True)
    dat_path = dat_dir / gz_path.name.removesuffix(".gz")
    with gzip.open(gz_path, "rb") as src, dat_path.open("wb") as dst:
        shutil.copyfileobj(src, dst)
    return dat_path


def copy_local_dat(path: Path, dat_dir: Path) -> Path:
    dat_dir.mkdir(parents=True, exist_ok=True)
    target = dat_dir / path.name.lower()
    shutil.copy2(path, target)
    return target


def extract_metadata(dat_path: Path, metadata_root: Path, ids_dir: Path, verbose: bool = False) -> tuple[Path, Path]:
    if str(TOOLBOX_ROOT) not in sys.path:
        sys.path.insert(0, str(TOOLBOX_ROOT))
    from logic.converter import Converter
    from logic.ingestor import Ingestor

    model = ToolboxModelAdapter(ids_dir=ids_dir, verbose=verbose)
    ingestor = Ingestor(model)
    b_err_tec, s_err_tec, _i_err, _i_wrn, _i_inf, _unknown, _report, _overview, odic_data = ingestor.ingest(str(dat_path))
    if b_err_tec:
        raise WorkflowError(f"Metadata ingest failed for {dat_path.name}: {s_err_tec}")

    metadata_dir = metadata_root
    metadata_dir.mkdir(parents=True, exist_ok=True)
    converter = Converter(model)
    fake_export_file = metadata_dir / dat_path.name
    ok, _files, _lines, errors = converter.convert(
        odic_data,
        str(fake_export_file),
        lst_recs=[f"{record:04d}" for record in range(1, 10)],
        b_multi=False,
    )
    if not ok:
        raise WorkflowError(f"Metadata export failed for {dat_path.name}: {errors}")
    repair_lr0002_export(dat_path, metadata_dir, model)
    normalize_metadata_exports(metadata_dir)
    reference_import_file = create_reference_import_file(dat_path, metadata_dir, model)
    return metadata_dir, reference_import_file


def create_reference_import_file(dat_path: Path, metadata_dir: Path, model: ToolboxModelAdapter) -> Path:
    """Create the Tool 1-style BSRN station-to-archive reference import table."""

    lines = read_dat_text(dat_path).splitlines()
    blocks = dat_blocks(lines)
    lr0001 = blocks.get("0001")
    lr0002 = blocks.get("0002")
    if not lr0001:
        raise WorkflowError(f"Cannot create reference import file for {dat_path.name}: LR0001 is missing")
    if not lr0002 or len(lr0002) < 2:
        raise WorkflowError(f"Cannot create reference import file for {dat_path.name}: LR0002 station scientist is missing")

    first = lr0001[0]
    station_id = first[0:3].strip()
    month_text = first[4:6].strip().zfill(2)
    year_text = first[7:11].strip()
    if not station_id or not month_text.strip("0") or not year_text:
        raise WorkflowError(f"Cannot create reference import file for {dat_path.name}: LR0001 station/date fields are incomplete")

    lookup = model.get_bsrn_id_system()
    station_entry = resolve_station_entry(
        int(station_id),
        event_hint=dat_path.stem[:3].upper(),
        ids_file=Path(model.get_user_lookup_dir()) / "BSRN_IDs.txt",
    )
    if station_entry is None or not station_entry.name:
        raise WorkflowError(f"Cannot create reference import file for {dat_path.name}: station {station_id} is missing from BSRN_IDs.txt")
    event_label = station_entry.event_label
    station_name = normalize_station_name(station_entry.name)

    expected_name = f"{event_label.lower()}{month_text}{year_text[-2:]}.dat"
    if dat_path.name.lower() != expected_name:
        raise WorkflowError(
            f"Cannot create reference import file for {dat_path.name}: expected filename {expected_name} from LR0001/BSRN_IDs.txt"
        )

    station_scientist = lr0002[1][0:38].strip()
    pi_id = lookup.get_data("staff", station_scientist, "pangaea_id") or "-999"
    yyyy_mm = f"{int(year_text):04d}-{int(month_text):02d}"
    title_station = station_name if station_name.endswith("Station") else f"station {station_name}"
    uri = f"ftp://ftp.bsrn.awi.de/{event_label.lower()}/{event_label.lower()}{month_text}{year_text[-2:]}.dat.gz"

    output_path = metadata_dir / f"{event_label}_{yyyy_mm}_refImp.txt"
    rows = [
        ["Author(s)", "Year", "Title", "URI", "PublicationStatus", "PublicationType"],
        [
            pi_id,
            str(datetime.now().year),
            f"BSRN Station-to-archive file for {title_station} ({yyyy_mm})",
            uri,
            "published",
            "dataset",
        ],
    ]
    write_tsv(output_path, rows)
    return output_path


def normalize_station_name(station_name: str) -> str:
    return station_name.replace("Ny-&Aring%lesund", "Ny-Alesund").replace("S&atilde%o", "Sao")


def repair_lr0002_export(dat_path: Path, metadata_dir: Path, model: ToolboxModelAdapter) -> None:
    """Rebuild LR0002 from fixed-width source lines when the toolbox parser shifts blank fax fields."""

    lines = read_dat_text(dat_path).splitlines()
    blocks = dat_blocks(lines)
    lr0001 = blocks.get("0001")
    lr0002 = blocks.get("0002")
    if not lr0001 or len(lr0001) < 1 or not lr0002 or len(lr0002) < 8:
        return

    first = lr0001[0]
    station_id = first[0:3].strip()
    month = first[4:6].strip().zfill(2)
    year = first[7:11].strip()
    lookup = model.get_bsrn_id_system()
    station_entry = resolve_station_entry(
        int(station_id),
        event_hint=dat_path.stem[:3].upper(),
        ids_file=Path(model.get_user_lookup_dir()) / "BSRN_IDs.txt",
    )
    event_label = station_entry.event_label if station_entry else lookup.get_data("station", station_id, "event")
    station_name = station_entry.name if station_entry else lookup.get_data("station", station_id, "name")
    yyyy_mm = f"{year}-{month}"

    scientist = parse_lr0002_person(lr0002[1], lr0002[2], lr0002[3])
    deputy = parse_lr0002_person(lr0002[5], lr0002[6], lr0002[7])
    scientist_id = lookup.get_data("staff", scientist["name"], "pangaea_id") if scientist["name"] else ""
    deputy_id = lookup.get_data("staff", deputy["name"], "pangaea_id") if deputy["name"] else ""

    header = [
        "File name",
        "Station ID",
        "Event label",
        "Station",
        "YYYY-MM",
        "Position",
        "Scientist",
        "Telephon",
        "Fax",
        "TCP/IP",
        "e-mail",
        "Address",
        "PANGAEA staff ID",
    ]
    rows = [
        header,
        [
            dat_path.name,
            station_id,
            event_label,
            station_name,
            yyyy_mm,
            "1",
            scientist["name"],
            scientist["phone"],
            scientist["fax"],
            scientist["tcpip"],
            scientist["email"],
            scientist["address"],
            scientist_id,
        ],
        [
            dat_path.name,
            station_id,
            event_label,
            station_name,
            yyyy_mm,
            "2",
            deputy["name"],
            deputy["phone"],
            deputy["fax"],
            deputy["tcpip"],
            deputy["email"],
            deputy["address"],
            deputy_id,
        ],
    ]

    output_path = metadata_dir / f"{event_label}_{year}-{month}_0002.txt"
    write_tsv(output_path, rows)


# Small cache so the several independent passes over the same DAT file within one
# run (reference import, LR0002 repair, minute completeness, format check) do not
# re-read and re-decode the file each time. Keyed on (path, mtime_ns, size) so a
# changed file is always re-read. Strings are immutable, so sharing is safe.
_DAT_TEXT_CACHE: dict[str, tuple[int, int, str]] = {}
_DAT_TEXT_CACHE_MAX = 4


def read_dat_text(dat_path: Path) -> str:
    key = str(dat_path)
    try:
        stat = dat_path.stat()
    except OSError:
        stat = None
    if stat is not None:
        cached = _DAT_TEXT_CACHE.get(key)
        if cached is not None and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            return cached[2]
    content = dat_path.read_bytes()
    text = None
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = content.decode("latin-1", errors="replace")
    if stat is not None:
        if len(_DAT_TEXT_CACHE) >= _DAT_TEXT_CACHE_MAX:
            _DAT_TEXT_CACHE.pop(next(iter(_DAT_TEXT_CACHE)))
        _DAT_TEXT_CACHE[key] = (stat.st_mtime_ns, stat.st_size, text)
    return text


def dat_blocks(lines: list[str]) -> dict[str, list[str]]:
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(("*C", "*U")) and len(stripped) >= 6 and stripped[2:6].isdigit():
            current = stripped[2:6]
            blocks.setdefault(current, [])
            continue
        if current is not None:
            blocks[current].append(line)
    return blocks


def parse_lr0002_person(name_line: str, contact_line: str, address_line: str) -> dict[str, str]:
    return {
        "name": name_line[0:38].strip(),
        "phone": name_line[39:59].strip(),
        "fax": name_line[60:80].strip(),
        "tcpip": contact_line[0:15].strip(),
        "email": contact_line[16:66].strip(),
        "address": address_line[0:80].strip(),
    }


def normalize_metadata_exports(metadata_dir: Path) -> None:
    """Make toolbox metadata exports match the compact curator-check layout."""

    base_columns = ["File name", "Station ID", "Event label", "Station", "YYYY-MM"]
    wanted_headers = {
        "0004": base_columns + ["Surface type", "Topography type"],
        "0005": base_columns
        + [
            "Manufacturer",
            "Location",
            "Distance from radiation site [km]",
            "Time of 1st launch [hh]",
            "Time of 2nd launch [hh]",
            "Time of 3rd launch [hh]",
            "Time of 4th launch [hh]",
            "Identification of radiosonde",
            "Remarks",
            "PANGAEA method",
            "PANGAEA method ID",
        ],
        "0006": base_columns
        + [
            "Manufacturer",
            "Location",
            "Distance from radiation site [km]",
            "Identification number of ozone instrument",
            "Remarks",
            "PANGAEA Method ID",
        ],
        "0007": base_columns
        + [
            "Date/Time when change occured",
            "Method est. cloud amount (digital proc.)",
            "Method est. cloud height (with instrument)",
            "Method est. cloud liquid water cont.",
            "Method est. cloud aerosol vertical distr.",
            "Method est. water vapor pressure v. d.",
            "SYNOP flags",
        ],
    }

    for path in metadata_dir.glob("*_0001.txt"):
        rows = read_tsv(path)
        if len(rows) >= 2:
            header, row = rows[0], rows[1]
            params = [val for val in row[len(base_columns) :] if val and val != "-1"]
            new_header = base_columns + ["Parameter"] + [""] * max(0, len(params) - 1)
            write_tsv(path, [new_header, row[: len(base_columns)] + params])

    for path in metadata_dir.glob("*_0003.txt"):
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) > 2 and lines[2] != "Message:":
            lines.insert(2, "Message:")
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    for rec in ("0005", "0006"):
        if not any(metadata_dir.glob(f"*_{rec}.txt")):
            prefix = next(metadata_dir.glob("*_0001.txt"), None)
            if prefix is not None:
                out = metadata_dir / prefix.name.replace("_0001.txt", f"_{rec}.txt")
                write_tsv(out, [wanted_headers[rec]])

    for path in metadata_dir.glob("*_0004.txt"):
        rows = read_tsv(path)
        if len(rows) >= 2:
            write_tsv(path, project_rows(rows, wanted_headers["0004"], {"Topography type": ["Topography type", "Typography type"]}))

    for path in metadata_dir.glob("*_0007.txt"):
        rows = read_tsv(path)
        if len(rows) >= 2:
            projected = project_rows(
                rows,
                wanted_headers["0007"],
                {
                    "Method est. cloud height (with instrument)": [
                        "Method est. cloud height (with instrument)",
                        "Method est. cloud base height (with instrument)",
                    ],
                    "Method est. cloud liquid water cont.": [
                        "Method est. cloud liquid water cont.",
                        "Method est. cloud liquid water content",
                    ],
                    "Method est. cloud aerosol vertical distr.": [
                        "Method est. cloud aerosol vertical distr.",
                        "Method est. cloud aerosol vertical distribution",
                    ],
                    "Method est. water vapor pressure v. d.": [
                        "Method est. water vapor pressure v. d.",
                        "Method est. water vapour press. v.d.",
                    ],
                },
            )
            write_tsv(path, projected)

    for path in metadata_dir.glob("*_0009.txt"):
        rows = read_tsv(path)
        if len(rows) >= 2:
            header = rows[0]
            date_idx = header.index("Date/Time") if "Date/Time" in header else None
            ym_idx = header.index("YYYY-MM") if "YYYY-MM" in header else None
            if date_idx is not None and ym_idx is not None:
                for row in rows[1:]:
                    if len(row) > max(date_idx, ym_idx) and row[date_idx].endswith("-T:"):
                        row[date_idx] = f"{row[ym_idx]}-01T00:00"
                write_tsv(path, rows)


def read_tsv(path: Path) -> list[list[str]]:
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        return list(csv.reader(handle, delimiter="\t"))


def write_tsv(path: Path, rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerows(rows)


def write_batch_reference_import(statuses: list[JobStatus], metadata_dir: Path) -> Path | None:
    """Combine per-file reference imports into one Tool 1-style batch table."""

    header: list[str] | None = None
    rows: list[list[str]] = []
    seen_keys: set[str] = set()
    for status in statuses:
        if not status.reference_import_file:
            continue
        path = workflow_path(status.reference_import_file)
        if not path.exists():
            continue
        table = read_tsv(path)
        if len(table) < 2:
            continue
        file_header = table[0]
        if header is None:
            header = file_header
        uri_idx = file_header.index("URI") if "URI" in file_header else None
        for row in table[1:]:
            uri = row[uri_idx].strip() if uri_idx is not None and len(row) > uri_idx else ""
            key = uri or status.job
            if key in seen_keys:
                continue
            seen_keys.add(key)
            rows.append(row)

    if header is None or not rows:
        return None
    output_path = metadata_dir / "reference_import_batch.txt"
    write_tsv(output_path, [header, *rows])
    return output_path


def write_batch_format_report(statuses: list[JobStatus], reports_dir: Path) -> Path | None:
    """Stack per-file format-check reports into one curator-facing batch report."""

    sections: list[str] = []
    seen_keys: set[str] = set()
    for status in statuses:
        if not status.format_report:
            continue
        path = workflow_path(status.format_report)
        if not path.exists():
            continue
        key = f"{status.job}|{path.resolve()}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        content = path.read_text(encoding="utf-8", errors="replace").rstrip()
        sections.append(
            "\n".join(
                [
                    "=" * 78,
                    f"Job: {status.job}",
                    f"File: {Path(status.dat_path).name if status.dat_path else path.stem}",
                    f"Report: {path.name}",
                    "=" * 78,
                    content,
                ]
            )
        )

    if not sections:
        return None
    output_path = reports_dir / "format_check_batch_report.txt"
    output_path.write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    return output_path


def write_batch_metadata_reports(statuses: list[JobStatus], metadata_dir: Path) -> dict[str, Path]:
    """Combine per-job LR0001-LR0009 metadata check files into batch tables."""

    reports: dict[str, Path] = {}
    for record in (f"{value:04d}" for value in range(1, 10)):
        header: list[str] | None = None
        rows: list[list[str]] = []
        seen_files: set[Path] = set()
        for status in statuses:
            if not status.metadata_dir:
                continue
            source_dir = workflow_path(status.metadata_dir)
            path = source_dir / f"{status.job}_{record}.txt"
            if not path.exists() or path in seen_files:
                continue
            seen_files.add(path)
            table = read_tsv(path)
            if not table:
                continue
            file_header = table[0]
            if header is None:
                header = ["Job", "Source file", *file_header]
            source_name = Path(status.dat_path).name if status.dat_path else str(status.source or "")
            for row in table[1:]:
                rows.append([status.job, source_name, *row])

        if header is None:
            continue
        output_path = metadata_dir / f"metadata_batch_{record}.txt"
        write_tsv(output_path, [header, *rows])
        reports[record] = output_path
    return reports


def attach_batch_artifacts(
    statuses: list[JobStatus],
    batch_reference_import: Path | None,
    batch_format_report: Path | None,
    batch_metadata_reports: dict[str, Path] | None = None,
) -> None:
    reference_text = str(batch_reference_import) if batch_reference_import is not None else None
    format_text = str(batch_format_report) if batch_format_report is not None else None
    metadata_text = {record: str(path) for record, path in (batch_metadata_reports or {}).items()}
    for status in statuses:
        status.batch_reference_import_file = reference_text
        status.batch_format_report = format_text
        status.batch_metadata_reports = metadata_text


def attach_minute_completeness(status: JobStatus) -> None:
    """Store non-blocking LR0100/LR0300 monthly minute coverage details."""

    if not status.dat_path:
        status.minute_completeness = {}
        return
    try:
        year, month = parse_status_year_month(status.job)
        dat_path = workflow_path(status.dat_path)
        blocks = dat_blocks(read_dat_text(dat_path).splitlines())
        expected = calendar.monthrange(year, month)[1] * 24 * 60
        records: dict[str, object] = {
            "LR0100": minute_record_completeness(blocks.get("0100") or [], year, month, expected, paired_lines=True),
        }
        if "0300" in blocks:
            records["LR0300"] = minute_record_completeness(blocks.get("0300") or [], year, month, expected, paired_lines=False)
        else:
            records["LR0300"] = {
                "status": "not_available",
                "expected": expected,
                "complete": 0,
                "missing": expected,
                "duplicate": 0,
                "invalid": 0,
                "present": False,
            }
        status.minute_completeness = {
            "year": year,
            "month": month,
            "expected_minutes": expected,
            "records": records,
        }
    except Exception as exc:
        status.minute_completeness = {
            "status": "error",
            "error": str(exc),
        }


def parse_status_year_month(job_label: str) -> tuple[int, int]:
    match = re.fullmatch(r"[A-Z0-9]{3}_(\d{4})-(\d{2})", job_label)
    if not match:
        raise WorkflowError(f"Cannot parse station month/year from status job label: {job_label}")
    return int(match.group(1)), int(match.group(2))


def minute_record_completeness(
    lines: list[str],
    year: int,
    month: int,
    expected: int,
    paired_lines: bool,
) -> dict[str, object]:
    minutes, invalid = minute_offsets_from_record(lines, year, month, paired_lines=paired_lines)
    unique = set(minutes)
    duplicate = max(0, len(minutes) - len(unique))
    missing = max(0, expected - len(unique))
    status = "ok" if missing == 0 and duplicate == 0 and invalid == 0 else "warning"
    return {
        "status": status,
        "expected": expected,
        "complete": len(unique),
        "missing": missing,
        "duplicate": duplicate,
        "invalid": invalid,
        "present": bool(lines),
    }


def parse_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def minute_offsets_from_record(lines: list[str], year: int, month: int, paired_lines: bool) -> tuple[list[int], int]:
    days_in_month = calendar.monthrange(year, month)[1]
    clean_lines = [line.strip() for line in lines if line.strip()]
    step = 2 if paired_lines else 1
    offsets: list[int] = []
    invalid = 0
    for index in range(0, len(clean_lines), step):
        parts = clean_lines[index].split()
        if len(parts) < 2:
            invalid += 1
            continue
        day = parse_int(parts[0])
        minute = parse_int(parts[1])
        if day is None or minute is None or day < 1 or day > days_in_month or minute < 0 or minute > 1439:
            invalid += 1
            continue
        offsets.append((day - 1) * 1440 + minute)
    if paired_lines and len(clean_lines) % 2:
        invalid += 1
    return offsets, invalid


def validate_metadata_ids(metadata_dir: Path, job_label: str | None = None) -> list[str]:
    issues: list[str] = []
    prefix = f"{job_label}_" if job_label else "*_"
    for path in sorted(metadata_dir.glob(f"{prefix}0002.txt")):
        issues.extend(validate_lr0002_staff_ids(path))
    for path in sorted(metadata_dir.glob(f"{prefix}0008.txt")):
        issues.extend(validate_required_id_column(path, "PANGAEA method ID", "PANGAEA method"))
    for path in sorted(metadata_dir.glob(f"{prefix}0009.txt")):
        issues.extend(validate_required_id_column(path, "PANGAEA method ID", "WRMC ID of instrument"))
    return issues


def validate_lr0002_staff_ids(path: Path) -> list[str]:
    """LR0002 deputies do not receive PANGAEA staff IDs; require IDs only for position 1."""

    rows = read_tsv(path)
    if not rows:
        return []
    header = rows[0]
    if "PANGAEA staff ID" not in header:
        return []
    id_idx = header.index("PANGAEA staff ID")
    scientist_idx = header.index("Scientist") if "Scientist" in header else None
    position_idx = header.index("Position") if "Position" in header else None
    issues: list[str] = []
    for row_num, row in enumerate(rows[1:], start=2):
        scientist = row[scientist_idx].strip() if scientist_idx is not None and len(row) > scientist_idx else ""
        if scientist in {"", "XXX"}:
            continue
        position = row[position_idx].strip() if position_idx is not None and len(row) > position_idx else ""
        if position and position != "1":
            continue
        value = row[id_idx].strip() if len(row) > id_idx else ""
        if value in MISSING_ID_VALUES:
            detail = f" for {scientist}" if scientist else ""
            issues.append(f"{path.name} row {row_num}: missing PANGAEA staff ID{detail}; refresh BSRN_IDs.txt after adding the ID in PANGAEA and rerun.")
    return issues


def validate_required_id_column(
    path: Path,
    id_column: str,
    label_column: str,
    ignored_names: set[str] | None = None,
) -> list[str]:
    rows = read_tsv(path)
    if not rows:
        return []
    header = rows[0]
    if id_column not in header:
        return []
    ignored_names = ignored_names or set()
    id_idx = header.index(id_column)
    label_idx = header.index(label_column) if label_column in header else None
    issues: list[str] = []
    for row_num, row in enumerate(rows[1:], start=2):
        value = row[id_idx].strip() if len(row) > id_idx else ""
        label = row[label_idx].strip() if label_idx is not None and len(row) > label_idx else ""
        if label in ignored_names:
            continue
        if value in MISSING_ID_VALUES:
            detail = f" for {label}" if label else ""
            issues.append(f"{path.name} row {row_num}: missing {id_column}{detail}; refresh BSRN_IDs.txt after adding the ID in PANGAEA and rerun.")
    return issues


def project_rows(rows: list[list[str]], wanted_header: list[str], aliases: dict[str, list[str]] | None = None) -> list[list[str]]:
    aliases = aliases or {}
    source_header = rows[0]
    projected = [wanted_header]
    for row in rows[1:]:
        values = dict(zip(source_header, row))
        out = []
        for col in wanted_header:
            names = aliases.get(col, [col])
            out.append(next((values[name] for name in names if name in values), ""))
        projected.append(out)
    return projected


def find_c_compiler() -> str | None:
    for compiler in ("gcc", "cc", "clang"):
        path = shutil.which(compiler)
        if path:
            return path
    for path in (
        Path(r"C:\msys64\ucrt64\bin\gcc.exe"),
        Path(r"C:\msys64\mingw64\bin\gcc.exe"),
        Path(r"C:\msys64\clang64\bin\clang.exe"),
    ):
        if path.exists():
            return str(path)
    return None


def checker_executable_path(build_dir: Path) -> Path:
    return build_dir / ("f_check.exe" if os.name == "nt" else "f_check")


def compile_checker(build_dir: Path, force: bool = False) -> Path:
    build_dir.mkdir(parents=True, exist_ok=True)
    exe = checker_executable_path(build_dir)
    if exe.exists() and not force:
        return exe
    compiler = find_c_compiler()
    if compiler is None:
        raise WorkflowError("No C compiler found for f_check_V3_4.c. Install gcc/cc/clang or provide --checker-exe.")
    env = os.environ.copy()
    compiler_dir = str(Path(compiler).parent)
    env["PATH"] = compiler_dir + os.pathsep + env.get("PATH", "")
    commands = [
        [compiler, "-O2", "-std=c89", "-w", "-o", str(exe), str(FORMAT_CHECK_SOURCE)],
        [compiler, "-O2", "-w", "-o", str(exe), str(FORMAT_CHECK_SOURCE)],
    ]
    last = None
    for command in commands:
        last = subprocess.run(command, text=True, capture_output=True, env=env)
        if last.returncode == 0 and exe.exists():
            return exe
    raise WorkflowError(f"Could not compile format checker: {last.stderr if last else 'unknown error'}")


def configured_checker_executable(args: argparse.Namespace, cfg: configparser.ConfigParser) -> Path | None:
    value = args.checker_exe or cfg.get("tools", "format_checker_exe", fallback="").strip()
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.exists():
        source = "--checker-exe" if args.checker_exe else "config [tools] format_checker_exe"
        raise WorkflowError(f"Configured format checker from {source} does not exist: {path}")
    if path.is_dir():
        source = "--checker-exe" if args.checker_exe else "config [tools] format_checker_exe"
        raise WorkflowError(f"Configured format checker from {source} is a directory, not an executable: {path}")
    return path


def can_compile_checker(args: argparse.Namespace, cfg: configparser.ConfigParser) -> bool:
    return bool(args.force_compile or cfg.getboolean("tools", "allow_compile_format_checker", fallback=False))


def run_python_format_check(dat_path: Path, reports_dir: Path) -> tuple[bool, Path, str]:
    """Python-native port of the legacy BSRN f_check gate used by normal runs."""

    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"{dat_path.stem}.rep"
    report_lines = [
        "",
        "**********",
        f"File name: {dat_path.name}",
        "**********",
        "*Python-native BSRN format checker",
    ]
    raw = dat_path.read_bytes()

    line_length_errors, missing_final_newline = check_format_line_lengths(raw)
    if missing_final_newline:
        report_lines.append("*ERROR: Missing 'new line' at the end of file.")
        report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
        return False, report_path, "\n".join(report_lines)
    if line_length_errors:
        report_lines.extend(format_line_length_errors(line_length_errors))
        report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
        return False, report_path, "\n".join(report_lines)
    report_lines.append("*Check for line length.......... OK")

    text = read_dat_text(dat_path)
    lines = text.splitlines()
    illegal_errors = check_format_illegal_characters(lines)
    if illegal_errors:
        report_lines.extend(format_illegal_character_errors(illegal_errors))
        report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
        return False, report_path, "\n".join(report_lines)
    report_lines.append("*Check for illegal characters... OK")

    format_errors = check_format_record_lines(lines)
    if format_errors:
        report_lines.extend(format_record_line_errors(format_errors))
        report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
        return False, report_path, "\n".join(report_lines)
    report_lines.append("*Check for line format.......... OK")

    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    return True, report_path, "\n".join(report_lines)


def check_format_line_lengths(raw: bytes) -> tuple[list[tuple[int, str]], bool]:
    if raw and not raw.endswith((b"\n", b"\r")):
        return [], True
    errors: list[tuple[int, str]] = []
    for line_no, line in enumerate(raw.splitlines(keepends=True), start=1):
        content = line[:-2] if line.endswith(b"\r\n") else line[:-1] if line.endswith((b"\n", b"\r")) else line
        if len(content) > 80:
            preview = content[:20].decode("latin-1", errors="replace")
            errors.append((line_no, preview))
            if len(errors) >= 5:
                break
    return errors, False


def format_line_length_errors(errors: list[tuple[int, str]]) -> list[str]:
    lines = [
        "*ERROR: A line longer than 80 characters or missing line separator.",
        "",
        "        For help the number and the first 20 characters of the line",
        "        causing the problem are printed below.",
        "        -------------------------------------------",
        "        | line number:  |  first 20 characters:   |",
        "        -------------------------------------------",
    ]
    for line_no, preview in errors:
        lines.append(f"        |   {line_no:6d}      |  {preview:.20s}   |")
        lines.append("        -------------------------------------------")
    if len(errors) >= 5:
        lines.append("        NOTE: Only first 5 wrong lines are printed.")
    return lines


# Precompiled "first illegal character" scanners. These encode exactly the same
# allowed-character rules as the original per-character Python loop, but let the
# regex engine (C speed) find the first violation per line.
_ILLEGAL_NUMERIC_RE = re.compile(r"[^ +\-.0-9]")  # records > 99 except 1000
_ILLEGAL_PRINTABLE_RE = re.compile(r"[^\x20-\x7e]")  # printable ASCII
_ILLEGAL_PRINTABLE_TAB_RE = re.compile(r"[^\x20-\x7e\t]")  # record 3 also allows tab


def check_format_illegal_characters(lines: list[str]) -> list[tuple[int, int, int, str]]:
    errors: list[tuple[int, int, int, str]] = []
    rec_num = 0
    for line_no, line in enumerate(lines, start=1):
        if line.startswith("*"):
            match = re.match(r"^\*[CU](\d{4})", line.strip())
            if match:
                rec_num = int(match.group(1))
                continue
            errors.append((rec_num, line_no, 1, line[:1] or " "))
            break
        if rec_num == 0:
            errors.append((rec_num, line_no, 1, line[:1] or " "))
            break
        if rec_num > 99 and rec_num != 1000:
            scanner = _ILLEGAL_NUMERIC_RE
        elif rec_num == 3:
            scanner = _ILLEGAL_PRINTABLE_TAB_RE
        else:
            scanner = _ILLEGAL_PRINTABLE_RE
        hit = scanner.search(line)
        if hit is not None:
            errors.append((rec_num, line_no, hit.start() + 1, hit.group(0)))
        if len(errors) >= 5:
            break
    return errors


def format_illegal_character_errors(errors: list[tuple[int, int, int, str]]) -> list[str]:
    lines = [
        "*ERROR: An illegal character occurred.",
        "",
        "        Allowed ASCII characters:",
        "           - in logical records 1000 and less than 100: printable ASCII characters",
        "             and tabulator in logical record 3.",
        "           - in all other logical records: space, '+', '-', '.', and digits.",
        "",
        "        --------------------------------------------------------------------",
        "        |  log. record:  | line number:  |  position:  |  wrong character  |",
        "        --------------------------------------------------------------------",
    ]
    for rec_num, line_no, pos, char in errors:
        printable = char if " " <= char <= "~" else " "
        lines.append(f"        |      {rec_num:4d}      |   {line_no:6d}      |      {pos:2d}     |   {printable}   |  {ord(char):02X} (hex) |")
        lines.append("        --------------------------------------------------------------------")
    if len(errors) >= 5:
        lines.append("        NOTE: Only first 5 lines with illegal characters are printed.")
    return lines


def check_format_record_lines(lines: list[str]) -> list[str]:
    errors: list[str] = []
    rec_num = 0
    index_in_record = 0
    prev_day = prev_min = 0
    prev_seq = prev_height = 0
    band4 = False

    for line_no, line in enumerate(lines, start=1):
        if line.startswith("*"):
            match = re.match(r"^\*[CU](\d{4})$", line.strip())
            if not match:
                errors.append(f"Line {line_no}: incorrect record marker.")
                break
            rec_num = int(match.group(1))
            index_in_record = 0
            prev_day = prev_min = 0
            prev_seq = prev_height = 0
            band4 = False
            continue
        if rec_num in {3, 1000}:
            continue
        if line == "":
            errors.append(f"Log. record {rec_num}, line {line_no}: empty line.")
            break

        values = line.split()
        record_error = validate_format_line_tokens(rec_num, index_in_record, values, band4)
        if record_error:
            errors.append(f"Log. record {rec_num}, line {line_no}: {record_error}")
            break

        day_min = data_record_day_min(rec_num, index_in_record, values)
        if day_min is not None:
            day, minute = day_min
            if rec_num == 1100:
                seq = int(values[2])
                height = int(values[4])
                if (prev_seq >= seq or prev_height >= height) and (prev_day, prev_min) == (day, minute):
                    errors.append(f"Log. record {rec_num}, line {line_no}: wrong seq. no. or height; both have to increase within one time.")
                    break
                if prev_day > day or (prev_day == day and prev_min > minute):
                    errors.append(f"Log. record {rec_num}, line {line_no}: time of measurement cannot decrease.")
                    break
                prev_seq, prev_height = seq, height
            elif prev_day > day or (prev_day == day and prev_min >= minute):
                errors.append(f"Log. record {rec_num}, line {line_no}: time of measurement has to increase.")
                break
            prev_day, prev_min = day, minute

        index_in_record, band4 = next_format_line_index(rec_num, index_in_record, values, band4)

    return errors[:5]


def validate_format_line_tokens(rec_num: int, index: int, values: list[str], band4: bool) -> str | None:
    expected = expected_format_token_count(rec_num, index, values, band4)
    if expected is None:
        return "incorrect record number."
    if expected == -1:
        return None
    if len(values) != expected:
        return f"expected {expected} fields, found {len(values)}."
    numeric_records = {1, 9, 100, 200, 300, 400, 500, 1100, 1200, 1300, 1500, 3010, 3025, 3030, 3300, 4000, 4010, 4030}
    if rec_num in numeric_records:
        for value in values:
            if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", value):
                return f"numeric field expected, found {value!r}."
    return None


def expected_format_token_count(rec_num: int, index: int, values: list[str], band4: bool) -> int | None:
    if rec_num == 1:
        return 4 if index == 0 else 8
    if rec_num == 2:
        return [3, -1, -1, -1][index % 4]
    if rec_num == 4:
        return [3, 2, -1, -1, -1, 4, 3, 22][index % 8]
    if rec_num == 5:
        return [4, -1, -1][index % 3]
    if rec_num == 6:
        return [4, -1, -1][index % 3]
    if rec_num == 7:
        return 3 if index == 0 else 6 if index == 6 else -1
    if rec_num == 8:
        if index == 0:
            return 4
        if index == 1:
            return -1
        if index == 2:
            return -1
        if index == 3:
            return 12 if len(values) > 10 else 10
        if index == 4:
            return -1
        if index in {5, 6, 7} or (band4 and index == 8):
            return -1
        if index in {8, 9}:
            return -1
        return None
    if rec_num == 9:
        return 6
    if rec_num == 100:
        return [10, 11][index % 2]
    if rec_num in {200, 300}:
        return 14
    if rec_num == 400:
        return [14, 12, 12][index % 3]
    if rec_num == 500:
        return [10, 12][index % 2]
    if rec_num == 1100:
        return 10
    if rec_num == 1200:
        return 3
    if rec_num == 1300:
        return 8
    if rec_num == 1500:
        return 8
    if rec_num in {3010, 3025, 3030, 3300}:
        return [10, 10][index % 2]
    if rec_num in {4000, 4010, 4030}:
        return 12
    return None


def data_record_day_min(rec_num: int, index: int, values: list[str]) -> tuple[int, int] | None:
    if rec_num in {100, 400, 500, 3010, 3025, 3030, 3300} and index % (2 if rec_num != 400 else 3) == 0:
        return int(values[0]), int(values[1])
    if rec_num in {200, 300, 1100, 1200, 1300, 1500, 4000, 4010, 4030}:
        return int(values[0]), int(values[1])
    return None


def next_format_line_index(rec_num: int, index: int, values: list[str], band4: bool) -> tuple[int, bool]:
    if rec_num in {1, 9, 200, 300, 1100, 1200, 1300, 1500, 4000, 4010, 4030}:
        return (1 if rec_num == 1 else 0), band4
    if rec_num in {2, 5, 6, 100, 400, 500, 3010, 3025, 3030, 3300}:
        cycle = {2: 4, 5: 3, 6: 3, 100: 2, 400: 3, 500: 2, 3010: 2, 3025: 2, 3030: 2, 3300: 2}[rec_num]
        return (index + 1) % cycle, band4
    if rec_num == 4:
        return (7 if index >= 7 else index + 1), band4
    if rec_num == 7:
        return 0 if index == 6 else index + 1, band4
    if rec_num == 8:
        if index == 3:
            return 4, len(values) > 10
        if index == 7 and band4:
            return 8, False
        return (index + 1) % 10, band4
    return index + 1, band4


def format_record_line_errors(errors: list[str]) -> list[str]:
    lines = [
        "*ERROR: Incorrect format.",
        "",
        "        For each logical record the line number from the beginning of",
        "        the file and the first wrong-format message are printed below.",
        "",
    ]
    lines.extend(f"        +{error}" for error in errors)
    return lines


def run_format_check(dat_path: Path, reports_dir: Path, checker_exe: Path) -> tuple[bool, Path, str]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    work_dat = reports_dir / dat_path.name
    shutil.copy2(dat_path, work_dat)
    env = os.environ.copy()
    checker_dir = str(Path(checker_exe).parent)
    msys_dir = str(Path(r"C:\msys64\ucrt64\bin"))
    env["PATH"] = checker_dir + os.pathsep + msys_dir + os.pathsep + env.get("PATH", "")
    proc = subprocess.run(
        [str(Path(checker_exe).resolve()), work_dat.name],
        text=True,
        capture_output=True,
        cwd=reports_dir,
        env=env,
    )
    generated_report = work_dat.with_suffix(".rep")
    final_report = reports_dir / f"{dat_path.stem}.rep"
    if generated_report.exists() and generated_report != final_report:
        generated_report.replace(final_report)
    elif generated_report.exists():
        final_report = generated_report
    output = (proc.stdout or "") + (proc.stderr or "")
    if output:
        (reports_dir / f"{dat_path.stem}.stdout.txt").write_text(output, encoding="utf-8", errors="replace")
    try:
        work_dat.unlink()
    except OSError:
        pass
    return proc.returncode == 0, final_report, output


def decisions_path_for_run(run_root: Path) -> Path:
    return run_root / CURATOR_DECISIONS_FILE


def status_to_decision_row(status: JobStatus | dict) -> dict:
    if isinstance(status, JobStatus):
        return asdict(status)
    return dict(status)


def decision_key(status: JobStatus | dict) -> str:
    row = status_to_decision_row(status)
    return str(row.get("job") or "")


def row_signature(status: JobStatus | dict) -> str:
    row = status_to_decision_row(status)
    signature_fields = {
        "job": row.get("job"),
        "source": row.get("source"),
        "dat_path": normalized_path_signature(row.get("dat_path")),
        "reference_import_file": normalized_path_signature(row.get("reference_import_file")),
        "pangaea_reference_uri": row.get("pangaea_reference_uri"),
        "pangaea_reference_id": row.get("pangaea_reference_id"),
        "metadata_ok": bool(row.get("metadata_ok")),
        "format_ok": bool(row.get("format_ok")),
        "qc_ok": bool(row.get("qc_ok")),
        "qc_report": normalized_path_signature(row.get("qc_report")),
        "qc_outputs": sorted(normalized_path_signature(value) for value in (row.get("qc_outputs") or [])),
    }
    encoded = json.dumps(signature_fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalized_path_signature(value: str | Path | None) -> str | None:
    if not value:
        return None
    path = workflow_path(value)
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def load_curator_decisions(run_root: Path) -> dict:
    path = decisions_path_for_run(run_root)
    if not path.exists():
        return {"version": 1, "decisions": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"version": 1, "decisions": {}}
    if not isinstance(data, dict):
        return {"version": 1, "decisions": {}}
    data.setdefault("version", 1)
    decisions = data.get("decisions")
    if not isinstance(decisions, dict):
        data["decisions"] = {}
    return data


def save_curator_decisions(run_root: Path, data: dict) -> Path:
    path = decisions_path_for_run(run_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data["version"] = 1
    data.setdefault("decisions", {})
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def decision_for_status(status: JobStatus | dict, decisions: dict) -> dict | None:
    key = decision_key(status)
    decision = (decisions.get("decisions") or {}).get(key)
    return decision if isinstance(decision, dict) else None


def current_decision_state(status: JobStatus | dict, decisions: dict) -> tuple[str, str]:
    decision = decision_for_status(status, decisions)
    if not decision:
        return "awaiting", "Awaiting curator approval"
    if decision.get("row_signature") != row_signature(status):
        return "stale", "Stored curator decision no longer matches this status row; review QC again."
    if decision.get("decision") == "accepted":
        return "accepted", "QC approved; ready for import generation."
    if decision.get("decision") == "rejected":
        note = decision.get("operator_note") or "QC rejected by curator."
        return "rejected", str(note)
    return "awaiting", "Awaiting curator approval"


def import_gate_status(status: JobStatus | dict, decisions: dict) -> tuple[str, str, str]:
    row = status_to_decision_row(status)
    errors = [str(error) for error in (row.get("errors") or [])]
    qc_errors = [error for error in errors if error.startswith("QC error:")]
    if qc_errors:
        return "blocked", "QC failed", qc_errors[0]
    if not row.get("qc_ok"):
        return "blocked", "QC not run", "Run QC before curator approval."
    if row.get("pangaea_reference_id") is None:
        detail = row.get("reference_id_warning") or "A numeric PANGAEA reference ID is required before import-file generation."
        return "blocked", "Missing PANGAEA reference ID", str(detail)
    state, detail = current_decision_state(status, decisions)
    if state == "accepted":
        import_outputs = row.get("import_outputs") or []
        if row.get("import_ok") and import_outputs:
            return "ready", "Import files generated", "Import files are available for the currently ported converters."
        metadata_warnings = row.get("metadata_warnings") or []
        if metadata_warnings:
            detail = f"{detail} Metadata warnings remain visible for curator review."
        if not row.get("format_ok"):
            detail = f"{detail} Format check did not pass; curator approval is being used to continue."
        return "ready", "QC approved", detail
    if state == "rejected":
        return "blocked", "QC rejected", detail
    if state == "stale":
        return "blocked", "Approval stale", detail
    return "blocked", "Awaiting curator approval", detail


def batch_metadata_artifacts(statuses: list[JobStatus]) -> list[tuple[str, Path]]:
    artifacts: list[tuple[str, Path]] = []
    seen: set[tuple[str, Path]] = set()
    for status in statuses:
        for record, value in sorted((status.batch_metadata_reports or {}).items()):
            path = workflow_path(value)
            key = (record, path)
            if key in seen:
                continue
            seen.add(key)
            artifacts.append((record, path))
    return artifacts


def write_run_index(run_dirs: dict[str, Path], statuses: list[JobStatus], dashboard_path: Path | None = None) -> None:
    index_path = run_dirs["root"] / "index.html"
    dashboard_path = dashboard_path or (PROJECT_ROOT / "dashboard.html")
    continue_script = write_continue_script(run_dirs["root"], dashboard_path)
    export_script = write_export_script(run_dirs["root"], dashboard_path)
    curator_decisions = load_curator_decisions(run_dirs["root"])
    batch_reference = next((workflow_path(status.batch_reference_import_file) for status in statuses if status.batch_reference_import_file), None)
    batch_format = next((workflow_path(status.batch_format_report) for status in statuses if status.batch_format_report), None)

    def build_html(base_path: Path) -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = [static_dashboard_row(base_path, status, curator_decisions, continue_script) for status in statuses]
        summary = dashboard_summary_rows(rows)
        global_links = []
        metadata_dir = next((workflow_path(status.metadata_dir) for status in statuses if status.metadata_dir), None)
        if metadata_dir is not None:
            global_links.append({"href": rel_href(base_path, metadata_dir), "label": "Metadata files"})
        if batch_reference is not None:
            global_links.append({"href": rel_href(base_path, batch_reference), "label": "Reference import"})
        if batch_format is not None:
            global_links.append({"href": rel_href(base_path, batch_format), "label": "Format report"})
        header_actions = []
        if any(status.dat_path for status in statuses):
            header_actions.append(
                {
                    "href": rel_href(base_path, export_script),
                    "label": "Export all data",
                    "detail": "Static snapshot command wrapper; the local dashboard server runs exports directly.",
                }
            )

        primary_action = {
            "kind": "idle",
            "label": "Start New Workflow",
            "detail": "Open the local dashboard server for workflow actions.",
            "href": "",
        }
        continuable = [row for row in rows if row["action_kind"] == "continue"]
        import_ready = [row for row in rows if row["action_kind"] == "ready"]
        if continuable:
            primary_action = {
                "kind": "primary",
                "label": "Continue to QC",
                "detail": "Static snapshot link; the local dashboard server runs QC directly.",
                "href": rel_href(base_path, continue_script),
            }
        elif import_ready:
            primary_action = {
                "kind": "primary",
                "label": "Generate import files",
                "detail": "Open the local dashboard server to generate import files.",
                "href": "",
            }

        payload = {
            "rows": rows,
            "summary": summary,
            "globalLinks": global_links,
            "headerActions": header_actions,
            "primaryAction": primary_action,
            "lastRefreshed": now,
            "staticSnapshot": True,
        }
        data_json = json_for_html_script(payload)
        return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BSRN Workflow Dashboard</title>
  <style>
    :root { color-scheme: light; --header:#12343c; --blue:#046c8c; --text:#12343c; --muted:#47636a; --line:#acc4d4; --bg:#edf4f4; --card:#fff; --green:#448474; --amber:#ffbd3d; --red:#c4573b; --gray:#acc4d4; --active:#e2eded; }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: "Segoe UI", "Trebuchet MS", Arial, sans-serif; color: var(--text); background: var(--bg); background-image: radial-gradient(1100px 500px at 85% -10%, rgba(255,189,61,.13), transparent 60%), radial-gradient(900px 600px at -10% 110%, rgba(4,108,140,.10), transparent 55%); background-attachment: fixed; }
    h1, h2, .title { font-family: Georgia, "Times New Roman", serif; letter-spacing: .01em; }
    a { color: var(--blue); text-decoration: none; }
    a:hover { text-decoration: underline; }
    .topbar { position: fixed; inset: 0 0 auto 0; min-height: 60px; display: flex; align-items: center; gap: .75rem; padding: .55rem 1rem; background: var(--header); border-bottom: 3px solid #ffbd3d; z-index: 10; }
    .title { font-weight: 700; font-size: 16px; white-space: nowrap; color: #ffbd3d; }
    .workspace { color: #9fb6bc; font-size: 12px; }
    .spacer { flex: 1; }
    .pill-links { display: flex; flex-wrap: wrap; gap: .4rem; min-width: 0; }
    .pill, .button { display: inline-flex; align-items: center; gap: .35rem; border: 1px solid var(--line); border-radius: 999px; padding: .36rem .62rem; background: #fff; font-size: 13px; font-weight: 700; color: var(--blue); }
    .button { border-radius: 6px; border-color: var(--blue); background: var(--blue); color: #fff; }
    .button.secondary { background: #fff; color: var(--blue); }
    .topbar .button, .topbar .refresh { white-space: nowrap; flex: 0 0 auto; }
    .topbar .button { background: #ffbd3d; border-color: #ffbd3d; color: #4a3206; }
    .topbar .button.secondary { background: transparent; border-color: #4f6d75; color: #e8eff1; }
    .refresh { min-height: 34px; display: inline-grid; place-items: center; border: 1px solid #4f6d75; border-radius: 6px; padding: .36rem .62rem; background: transparent; color: #e8eff1; font-size: 13px; font-weight: 700; }
    .layout { display: grid; grid-template-columns: 220px minmax(0, 1fr); padding-top: 60px; min-height: 100vh; }
    .sidebar { position: sticky; top: 60px; height: calc(100vh - 60px); overflow: auto; background: #e8eff1; border-right: 1px solid var(--line); padding: 1rem .75rem; }
    .side-label { color: var(--muted); font-size: 11px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; margin: 0 0 .7rem; }
    .job-list { display: grid; gap: .35rem; }
    .job-button { width: 100%; text-align: left; border: 1px solid var(--line); border-left: 3px solid transparent; border-radius: 6px; padding: .58rem .6rem; background: #fff; cursor: pointer; }
    .job-button.active { border-left-color: var(--blue); background: var(--active); }
    .job-name { display: block; font-weight: 700; margin-bottom: .35rem; }
    .dots { display: flex; gap: .25rem; }
    .dot { width: 9px; height: 9px; border-radius: 999px; background: var(--gray); }
    .main { padding: 1.1rem 1.4rem 2rem; overflow: auto; }
    .summary { display: grid; grid-template-columns: repeat(6, minmax(7rem, 1fr)); gap: .75rem; margin-bottom: 1rem; }
    .metric { background: var(--card); border-radius: 8px; border: 1px solid var(--line); border-left: 4px solid var(--blue); box-shadow: 0 2px 8px rgba(0,0,0,.08); padding: .65rem .8rem; }
    .metric strong { display: block; font-size: 12px; color: var(--muted); margin-bottom: .25rem; }
    .metric span { font-weight: 800; font-size: 20px; }
    .metric.ok { border-left-color: var(--green); color: #fff; }
    .metric.ok strong, .metric.ok span { color: #fff; }
    .metric.warn { border-left-color: var(--amber); }
    .subhead { display: flex; flex-wrap: wrap; align-items: center; gap: .6rem; margin: .4rem 0 1rem; }
    h1 { font-size: 24px; margin: 0 .4rem 0 0; }
    h2 { font-size: 15px; margin: 0 0 .55rem; }
    p { margin: .25rem 0; color: var(--muted); }
    .badge { display: inline-flex; align-items: center; border-radius: 999px; padding: .2rem .5rem; font-size: 12px; font-weight: 800; color: #fff; background: var(--gray); }
    .ok { background: var(--green); }
    .warning, .fail { background: var(--amber); color: #4a3206; }
    .error { background: var(--red); }
    .idle { background: var(--gray); }
    .cards { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .85rem; }
    .card, .notice, .last-action { background: var(--card); border: 1px solid var(--line); border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,.08); padding: .9rem; }
    .links { display: flex; flex-wrap: wrap; gap: .45rem; margin-top: .55rem; }
    .details { margin-top: .85rem; display: grid; gap: .4rem; color: #374151; font-size: 13px; }
    .detail-line { padding: .45rem .55rem; background: #f9fafb; border: 1px solid var(--line); border-radius: 6px; }
    .stepper { list-style: none; display: flex; flex-wrap: wrap; gap: 0; margin: .2rem 0 1.1rem; padding: .75rem .9rem; background: var(--card); border: 1px solid var(--line); border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,.08); }
    .step { flex: 1 1 0; min-width: 6.5rem; position: relative; display: grid; justify-items: center; gap: .3rem; padding: .15rem .35rem; text-align: center; }
    .step::before { content: ""; position: absolute; top: 8px; left: -50%; width: 100%; height: 2px; background: var(--line); }
    .step:first-child::before { display: none; }
    .step-dot { width: 18px; height: 18px; border-radius: 999px; background: var(--gray); border: 3px solid #fff; box-shadow: 0 0 0 1px var(--line); position: relative; z-index: 1; }
    .step.state-ok .step-dot { background: var(--green); }
    .step.state-ok::before { background: var(--green); }
    .step.state-warning .step-dot { background: var(--amber); box-shadow: 0 0 0 1px #c9881c; }
    .step.state-error .step-dot { background: var(--red); }
    .step-label { font-size: 11px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); }
    .step-value { font-size: 12px; font-weight: 700; color: var(--text); max-width: 11rem; }
    .step.state-idle .step-value { color: var(--muted); font-weight: 600; }
    .card.action { grid-column: 1 / -1; border-left: 4px solid var(--blue); }
    .plots { margin-top: 1rem; }
    .plot-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: .8rem; }
    .plot-card { border: 1px solid var(--line); border-radius: 8px; overflow: hidden; background: #fff; cursor: pointer; }
    .plot-card img { display: block; width: 100%; aspect-ratio: 16 / 10; object-fit: contain; background: #f9fafb; }
    .plot-card span { display: block; padding: .5rem .6rem; font-size: 12px; font-weight: 700; color: #374151; }
    .placeholder { border: 1px dashed #9ca3af; border-radius: 8px; padding: 1.2rem; color: var(--muted); background: #fff; }
    .lightbox { position: fixed; inset: 0; display: none; align-items: center; justify-content: center; background: rgba(17,24,39,.82); z-index: 30; padding: 2rem; overflow: auto; }
    .lightbox.open { display: flex; }
    .lightbox img { display: block; width: auto; height: auto; max-width: min(96vw, 1400px); max-height: 86vh; object-fit: contain; background: #fff; }
    .lightbox button { position: absolute; border: 0; background: rgba(255,255,255,.92); color: var(--text); border-radius: 6px; padding: .5rem .7rem; font-weight: 800; cursor: pointer; }
    .lightbox .prev { left: 1rem; }
    .lightbox .next { right: 1rem; }
    .lightbox .close { top: 1rem; right: 1rem; }
    @media (max-width: 980px) { .topbar { position: static; height: auto; flex-wrap: wrap; padding: .75rem; } .topbar .spacer { display: none; } .pill-links { flex: 1 1 100%; order: 3; } .layout { grid-template-columns: 1fr; padding-top: 0; } .sidebar { position: static; height: auto; } .summary, .cards, .plot-grid { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <header class="topbar">
    <div>
      <div class="title">BSRN Workflow Dashboard</div>
      <div class="workspace">output/current - static snapshot - refreshed <span id="lastRefreshed"></span></div>
    </div>
    <div class="spacer"></div>
    <nav class="pill-links" id="globalLinks"></nav>
    <nav class="pill-links" id="headerActions"></nav>
    <a id="primaryAction" class="button secondary" href="http://127.0.0.1:8765/">Start New Workflow</a>
    <a class="refresh" href="" title="Refresh snapshot">Refresh</a>
  </header>
  <div class="layout">
    <aside class="sidebar">
      <div class="side-label">Current batch</div>
      <div class="job-list" id="jobList"></div>
    </aside>
    <main class="main">
      <section class="summary" id="summary"></section>
      <section id="detail"></section>
    </main>
  </div>
  <div class="lightbox" id="lightbox"><button class="close" type="button">Close</button><button class="prev" type="button">&lt;</button><img alt=""><button class="next" type="button">&gt;</button></div>
  <script id="dashboard-data" type="application/json">""" + data_json + """</script>
  <script>
    const data = JSON.parse(document.getElementById('dashboard-data').textContent);
    let selected = 0;
    let plotSet = [];
    let plotIndex = 0;
    const cls = value => {
      if (!value) return 'idle';
      if (/^\\d+$/.test(String(value))) return 'ok';
      const text = String(value).toLowerCase();
      if (text.includes('error') || text.includes('blocked') || text.includes('rejected') || text.includes('failed')) return 'error';
      if (text.includes('warning') || text.includes('fail') || text.includes('awaiting') || text.includes('stale') || text.includes('missing')) return 'warning';
      if (text.includes('ok') || text.includes('available') || text.includes('approved') || text.includes('generated')) return 'ok';
      return 'idle';
    };
    function el(tag, attrs = {}, text = '') {
      const node = document.createElement(tag);
      Object.entries(attrs).forEach(([key, value]) => {
        if (key === 'class') node.className = value;
        else if (key === 'href') node.href = value;
        else if (key === 'title') node.title = value;
        else node.setAttribute(key, value);
      });
      if (text) node.textContent = text;
      return node;
    }
    function badge(value) { return el('span', {class: 'badge ' + cls(value)}, value || 'Not run'); }
    function renderLinks(target, links, css = 'pill') {
      target.replaceChildren();
      links.forEach(item => target.appendChild(el('a', {class: css, href: item.href}, item.label)));
    }
    function renderSummary() {
      const box = document.getElementById('summary');
      const items = [
        ['Files', data.summary.files, ''],
        ['Minutes OK', data.summary.minutes_ok, data.summary.minutes_warning ? 'warn' : 'ok'],
        ['Checks OK', data.summary.checks_ok, 'ok'],
        ['QC complete', data.summary.qc_complete, 'ok'],
        ['QC approved', data.summary.qc_approved, 'ok'],
        ['Needs attention', data.summary.needs_attention, 'warn']
      ];
      box.replaceChildren(...items.map(([label, value, kind]) => {
        const card = el('div', {class: 'metric ' + kind});
        card.appendChild(el('strong', {}, label));
        card.appendChild(el('span', {}, String(value)));
        return card;
      }));
    }
    function renderList() {
      const list = document.getElementById('jobList');
      list.replaceChildren();
      if (!data.rows.length) {
        list.appendChild(el('p', {}, 'No current run yet.'));
        return;
      }
      data.rows.forEach((row, index) => {
        const button = el('button', {class: 'job-button' + (index === selected ? ' active' : ''), type: 'button'});
        button.appendChild(el('span', {class: 'job-name'}, row.job));
        const dots = el('span', {class: 'dots'});
        [row.metadata, row.format, row.qc].forEach(value => dots.appendChild(el('span', {class: 'dot ' + cls(value), title: value})));
        button.appendChild(dots);
        button.addEventListener('click', () => { selected = index; renderList(); renderDetail(); });
        list.appendChild(button);
      });
    }
    function linkList(items, emptyText) {
      const wrap = el('div', {class: 'links'});
      if (!items.length && emptyText) wrap.appendChild(el('p', {}, emptyText));
      items.forEach(item => wrap.appendChild(el('a', {class: 'pill', href: item.href}, item.label)));
      return wrap;
    }
    function card(title, status, links, emptyText) {
      const node = el('article', {class: 'card'});
      node.appendChild(el('h2', {}, title));
      node.appendChild(badge(status));
      node.appendChild(linkList(links, emptyText));
      return node;
    }
    function stepper(row) {
      const steps = [
        ['File', row.dat_href ? 'OK' : 'Not run'],
        ['Metadata', row.metadata],
        ['Format', row.format],
        ['QC', row.qc],
        ['Approval', row.gate_label],
        ['Import', row.import_status === 'Import files generated' ? 'OK' : 'Not run']
      ];
      const wrap = el('ol', {class: 'stepper'});
      steps.forEach(([label, value]) => {
        const li = el('li', {class: 'step state-' + cls(value), title: label + ': ' + (value || 'Not run')});
        li.appendChild(el('span', {class: 'step-dot'}));
        li.appendChild(el('span', {class: 'step-label'}, label));
        li.appendChild(el('span', {class: 'step-value'}, value || 'Not run'));
        wrap.appendChild(li);
      });
      return wrap;
    }
    function renderDetail() {
      const detail = document.getElementById('detail');
      if (!data.rows.length) {
        detail.replaceChildren(el('div', {class: 'placeholder'}, 'Start with a job code or local DAT file.'));
        return;
      }
      const row = data.rows[selected];
      const section = el('section');
      const head = el('div', {class: 'subhead'});
      head.appendChild(el('h1', {}, row.job));
      head.appendChild(badge(row.overall));
      section.appendChild(head);
      section.appendChild(stepper(row));
      const cards = el('div', {class: 'cards'});
      const gateCard = card('Next step', row.gate_label, row.action_links, row.gate_detail || 'No action available.');
      gateCard.className = 'card action';
      cards.appendChild(gateCard);
      cards.appendChild(card('Files', row.files_status, row.file_links, 'No files linked yet.'));
      cards.appendChild(card('QC artifacts', row.qc, row.qc_artifacts, 'No QC artifacts yet.'));
      cards.appendChild(card('Data exports', row.data_export_status, row.data_exports, 'No data exports yet.'));
      cards.appendChild(card('Import artifacts', row.import_status, row.import_artifacts, 'No import files generated yet.'));
      cards.appendChild(card('Minute completeness', row.minute_status, [], row.minute_detail || 'Not available.'));
      section.appendChild(cards);
      if (row.details.length) {
        const details = el('div', {class: 'details'});
        row.details.forEach(text => details.appendChild(el('div', {class: 'detail-line'}, text)));
        section.appendChild(details);
      }
      const plots = el('section', {class: 'plots'});
      plots.appendChild(el('h2', {}, 'QC Plots'));
      const plotLinks = row.qc_artifacts.filter(item => item.kind === 'plot');
      if (!plotLinks.length) {
        plots.appendChild(el('div', {class: 'placeholder'}, 'No plots yet - run QC to generate.'));
      } else {
        const grid = el('div', {class: 'plot-grid'});
        plotLinks.forEach((item, index) => {
          const fig = el('button', {class: 'plot-card', type: 'button'});
          const img = el('img', {src: plotImageSrc(item), alt: item.label});
          fig.appendChild(img);
          fig.appendChild(el('span', {}, item.label));
          fig.addEventListener('click', () => openLightbox(plotLinks, index));
          grid.appendChild(fig);
        });
        plots.appendChild(grid);
      }
      section.appendChild(plots);
      detail.replaceChildren(section);
    }
    function plotImageSrc(item) { const version = item.version || data.lastRefreshed; return version ? item.href + '?v=' + encodeURIComponent(version) : item.href; }
    function openLightbox(items, index) { plotSet = items; plotIndex = index; updateLightbox(); document.getElementById('lightbox').classList.add('open'); }
    function updateLightbox() { const img = document.querySelector('#lightbox img'); if (plotSet.length) { img.src = plotImageSrc(plotSet[plotIndex]); img.alt = plotSet[plotIndex].label; } }
    function movePlot(delta) { if (!plotSet.length) return; plotIndex = (plotIndex + delta + plotSet.length) % plotSet.length; updateLightbox(); }
    document.querySelector('#lightbox .close').addEventListener('click', () => document.getElementById('lightbox').classList.remove('open'));
    document.querySelector('#lightbox .prev').addEventListener('click', () => movePlot(-1));
    document.querySelector('#lightbox .next').addEventListener('click', () => movePlot(1));
    document.getElementById('lightbox').addEventListener('click', event => { if (event.target.id === 'lightbox') event.currentTarget.classList.remove('open'); });
    document.addEventListener('keydown', event => { if (event.key === 'Escape') document.getElementById('lightbox').classList.remove('open'); if (event.key === 'ArrowLeft') movePlot(-1); if (event.key === 'ArrowRight') movePlot(1); });
    document.getElementById('lastRefreshed').textContent = data.lastRefreshed;
    renderLinks(document.getElementById('globalLinks'), data.globalLinks);
    renderLinks(document.getElementById('headerActions'), data.headerActions || [], 'button');
    const primary = document.getElementById('primaryAction');
    primary.textContent = data.primaryAction.label;
    primary.title = data.primaryAction.detail;
    if (data.primaryAction.href) primary.href = data.primaryAction.href;
    renderSummary(); renderList(); renderDetail();
  </script>
</body>
</html>
"""
    index_path.write_text(build_html(index_path), encoding="utf-8")
    dashboard_path.parent.mkdir(parents=True, exist_ok=True)
    dashboard_path.write_text(build_html(dashboard_path), encoding="utf-8")


def static_dashboard_row(base_path: Path, status: JobStatus, curator_decisions: dict, continue_script: Path) -> dict[str, object]:
    qc_errors = [error for error in status.errors if error.startswith("QC error:")]
    gate_kind, gate_label, gate_detail = import_gate_status(status, curator_decisions)
    metadata = "Warning" if status.metadata_warnings else "OK" if status.metadata_ok else "ERROR" if status.errors else "Not run"
    fmt = "OK" if status.format_ok else "FAIL" if status.format_report else "Not run"
    qc = "OK" if status.qc_ok else "ERROR" if qc_errors else "Not run"
    if status.errors:
        overall = "ERROR"
    elif gate_kind == "ready":
        overall = "Warning" if status.format_report and not status.format_ok else "OK"
    elif status.metadata_warnings or status.reference_id_warning or (status.format_report and not status.format_ok) or status.qc_ok:
        overall = "Warning"
    else:
        overall = "OK"
    if gate_kind == "ready":
        action_kind = "import_done" if status.import_ok and status.import_outputs else "ready"
    elif status.qc_ok and gate_label in {"Awaiting curator approval", "QC rejected", "Approval stale"}:
        action_kind = "curator"
    elif status.qc_ok:
        action_kind = "qc_links"
    elif status.dat_path and not qc_errors:
        action_kind = "continue"
    else:
        action_kind = "blocked"

    reference_id_status = (
        str(status.pangaea_reference_id)
        if status.pangaea_reference_id is not None
        else "Warning"
        if status.reference_id_warning
        else "Not run"
    )
    file_links: list[dict[str, str]] = []
    for value, label in (
        (status.dat_path, "DAT file"),
        (status.metadata_dir, "Metadata files"),
        (status.reference_import_file, "Reference import"),
        (status.batch_reference_import_file, "Batch reference import"),
        (status.batch_format_report or status.format_report, "Format report"),
    ):
        if value:
            file_links.append({"href": rel_href(base_path, workflow_path(value)), "label": label})
    for record, value in sorted((status.batch_metadata_reports or {}).items()):
        file_links.append({"href": rel_href(base_path, workflow_path(value)), "label": f"Batch LR{record}"})

    minute_status, minute_detail = dashboard_minute_completeness(status.minute_completeness)

    action_links: list[dict[str, str]] = []
    if action_kind == "continue":
        action_links.append({"href": rel_href(base_path, continue_script), "label": "Continue to QC"})
    elif action_kind in {"qc_links", "curator"}:
        action_links.extend(qc_artifacts_for_dashboard(base_path, status))
    elif action_kind in {"ready", "import_done"}:
        action_links.extend(import_artifacts_for_dashboard(base_path, status))

    return {
        "job": status.job,
        "overall": overall,
        "metadata": metadata,
        "format": fmt,
        "qc": qc,
        "dat_href": rel_href(base_path, workflow_path(status.dat_path)) if status.dat_path else "",
        "reference_id": reference_id_status,
        "gate_label": gate_label,
        "gate_detail": gate_detail if gate_kind == "blocked" else "",
        "action_kind": action_kind,
        "import_status": "Import files generated" if status.import_ok and status.import_outputs else "Not run",
        "minute_status": minute_status,
        "minute_detail": minute_detail,
        "files_status": "Available" if file_links else "Not run",
        "file_links": file_links,
        "qc_artifacts": qc_artifacts_for_dashboard(base_path, status),
        "data_export_status": (
            "Available"
            if data_export_artifacts_for_dashboard(base_path, status)
            else "Warning"
            if status.data_export_warnings
            else "Not run"
        ),
        "data_exports": data_export_artifacts_for_dashboard(base_path, status),
        "import_artifacts": import_artifacts_for_dashboard(base_path, status),
        "action_links": action_links,
        "details": dashboard_details(status, gate_kind, gate_detail),
    }


def dashboard_summary_rows(rows: list[dict[str, object]]) -> dict[str, object]:
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


def dashboard_minute_completeness(details: dict[str, object] | None) -> tuple[str, str]:
    if not details:
        return "Not run", ""
    if details.get("status") == "error":
        return "Warning", f"Could not calculate minute completeness: {details.get('error')}"
    records = details.get("records")
    if not isinstance(records, dict):
        return "Not run", ""

    parts: list[str] = []
    has_warning = False
    for record in ("LR0100", "LR0300"):
        record_details = records.get(record)
        if not isinstance(record_details, dict):
            continue
        if record_details.get("status") == "not_available":
            if record == "LR0300":
                parts.append("LR0300 not available")
            continue
        if record_details.get("status") != "ok":
            has_warning = True
        parts.append(
            (
                f"{record}: {record_details.get('complete', 0)}/{record_details.get('expected', 0)} complete; "
                f"missing {record_details.get('missing', 0)}, duplicate {record_details.get('duplicate', 0)}, "
                f"invalid {record_details.get('invalid', 0)}"
            )
        )
    return ("Warning" if has_warning else "OK"), "; ".join(parts)


def dashboard_details(status: JobStatus, gate_kind: str, gate_detail: str) -> list[str]:
    items: list[str] = []
    items.extend(status.metadata_warnings)
    if status.reference_id_warning:
        items.append(status.reference_id_warning)
    if status.format_report and not status.format_ok:
        items.append("Format warning: see report. This does not block QC or approved import generation.")
    items.extend(status.qc_warnings)
    items.extend(status.data_export_warnings)
    items.extend(warning for warning in status.import_warnings if not is_routine_record_exclusion(warning))
    items.extend(status.errors)
    if gate_kind == "blocked" and gate_detail:
        items.append(gate_detail)
    if status.pangaea_parent_id is not None and status.parent_id_comment:
        items.append(f"ParentID {status.pangaea_parent_id}: {status.parent_id_comment}")
    result: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def is_routine_record_exclusion(message: str) -> bool:
    text = str(message)
    lower = text.lower()
    is_lr40xx = re.search(r"\bLR40\d{2}\b", text, flags=re.IGNORECASE) is not None
    return is_lr40xx and (
        "explicitly excluded" in lower
        or "not generated as a data import file" in lower
        or "not a tool 1 data import converter target" in lower
    )


def write_continue_script(run_root: Path, dashboard_path: Path) -> Path:
    """Write a Windows command wrapper for the next workflow step."""

    script_path = run_root / "continue_qc.cmd"
    python_exe = Path(sys.executable)
    qc_script = PROJECT_ROOT / "scripts" / "bsrn_qc_continue.py"
    status_json = run_root / "status.json"
    command = (
        f'"{python_exe}" "{qc_script}" '
        f'--status "{status_json}" '
        f'--dashboard "{dashboard_path}"'
    )
    script_path.write_text(
        "@echo off\r\n"
        f"{command}\r\n"
        "set EXITCODE=%ERRORLEVEL%\r\n"
        "echo.\r\n"
        "echo QC step finished with exit code %EXITCODE%. This window will close automatically.\r\n"
        "timeout /t 1 /nobreak >nul\r\n"
        "exit /b %EXITCODE%\r\n",
        encoding="utf-8",
    )
    return script_path


def write_export_script(run_root: Path, dashboard_path: Path) -> Path:
    """Write a Windows command wrapper for user-readable data exports."""

    script_path = run_root / "export_data.cmd"
    python_exe = Path(sys.executable)
    export_script = PROJECT_ROOT / "scripts" / "bsrn_data_exports.py"
    status_json = run_root / "status.json"
    command = (
        f'"{python_exe}" "{export_script}" '
        f'--status "{status_json}" '
        f'--dashboard "{dashboard_path}"'
    )
    script_path.write_text(
        "@echo off\r\n"
        f"{command}\r\n"
        "set EXITCODE=%ERRORLEVEL%\r\n"
        "echo.\r\n"
        "echo Data export step finished with exit code %EXITCODE%. This window will close automatically.\r\n"
        "timeout /t 1 /nobreak >nul\r\n"
        "exit /b %EXITCODE%\r\n",
        encoding="utf-8",
    )
    return script_path


def rel_link(from_file: Path, target: Path) -> str:
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    rel = os.path.relpath(target, start=from_file.parent).replace("\\", "/")
    return f'<a href="{html.escape(rel, quote=True)}">{html.escape(target.name)}</a>'


def rel_href(from_file: Path, target: Path) -> str:
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    return os.path.relpath(target, start=from_file.parent).replace("\\", "/")


def workflow_path(value: str | Path | None) -> Path:
    path = Path(value or "")
    return path if path.is_absolute() else PROJECT_ROOT / path


def qc_artifact_links(base_path: Path, status: JobStatus) -> str:
    paths = list(status.qc_outputs)
    if status.qc_report and status.qc_report not in paths:
        paths.insert(0, status.qc_report)

    important = []
    seen: set[Path] = set()
    for value in paths:
        path = workflow_path(value)
        if path in seen or not is_curator_qc_artifact(path):
            continue
        seen.add(path)
        important.append(path)

    return "<br>".join(rel_link_with_label(base_path, path, qc_artifact_label(path, status)) for path in important)


def qc_artifacts_for_dashboard(base_path: Path, status: JobStatus) -> list[dict[str, str]]:
    paths = list(status.qc_outputs)
    if status.qc_report and status.qc_report not in paths:
        paths.insert(0, status.qc_report)

    artifacts: list[dict[str, str]] = []
    seen: set[Path] = set()
    for value in paths:
        path = workflow_path(value)
        if path in seen or not is_curator_qc_artifact(path):
            continue
        seen.add(path)
        kind = "plot" if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".svg"} else "report"
        artifact = {"href": rel_href(base_path, path), "label": qc_artifact_label(path, status), "kind": kind}
        if kind == "plot" and path.exists():
            artifact["version"] = str(int(path.stat().st_mtime))
        artifacts.append(artifact)
    return artifacts


def data_export_artifacts_for_dashboard(base_path: Path, status: JobStatus) -> list[dict[str, str]]:
    folder: Path | None = None
    if status.data_export_dir:
        folder = workflow_path(status.data_export_dir)
    elif status.data_export_outputs:
        first_output = workflow_path(status.data_export_outputs[0])
        folder = first_output if first_output.is_dir() else first_output.parent
    if folder is None:
        return []
    return [{"href": rel_href(base_path, folder), "label": "Data exports", "kind": "folder"}]


def is_curator_qc_artifact(path: Path) -> bool:
    suffix = path.suffix.lower()
    return suffix in {".html", ".png", ".jpg", ".jpeg", ".svg"}


def qc_artifact_label(path: Path, status: JobStatus) -> str:
    name = path.name
    if name.endswith("_QC_report_interactive.html"):
        return "Interactive QC report"
    if name.endswith("_QC_report.html"):
        return "Static QC report"
    if status.qc_report and workflow_path(status.qc_report) == path:
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


def import_artifact_links(base_path: Path, status: JobStatus) -> str:
    artifacts = import_artifacts_for_dashboard(base_path, status)
    return "<br>".join(
        f'<a href="{html.escape(item["href"], quote=True)}">{html.escape(item["label"])}</a>' for item in artifacts
    )


def import_artifacts_for_dashboard(base_path: Path, status: JobStatus) -> list[dict[str, str]]:
    folder = import_folder_for_status(status)
    if folder is None:
        return []
    return [{"href": rel_href(base_path, folder), "label": "Import folder", "kind": "folder"}]


def import_folder_for_status(status: JobStatus) -> Path | None:
    for value in status.import_outputs:
        path = workflow_path(value)
        if path.is_dir():
            return path
        if path.name:
            return path.parent
    if status.import_dir:
        return workflow_path(status.import_dir)
    return None


def import_artifact_label(path: Path) -> str:
    if path.is_dir():
        return "Import files"
    if path.name.endswith("_import_generation_manifest.json"):
        return "Import manifest"
    if path.name.endswith("_header_preview.txt"):
        return "Header preview"
    if path.name.endswith("_unsupported_records.txt"):
        return "Unsupported records"
    return path.name


def rel_link_with_label(from_file: Path, target: Path, label: str) -> str:
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    rel = os.path.relpath(target, start=from_file.parent).replace("\\", "/")
    return f'<a href="{html.escape(rel, quote=True)}">{html.escape(label)}</a>'


def run_workflow(args: argparse.Namespace) -> int:
    cfg = load_config(resolve_project_path(args.config))
    output_root = resolve_project_path(args.output_root or cfg.get("paths", "output_root", fallback="output"))
    ids_dir = resolve_project_path(args.ids_dir or cfg.get("paths", "ids_dir", fallback=str(DEFAULT_IDS_DIR)))
    if args.refresh_bsrn_ids:
        id_refresh_warning = refresh_bsrn_ids(ids_dir)
        if id_refresh_warning:
            print(f"WARNING: {id_refresh_warning}", file=sys.stderr)
    if args.refresh_reference_ids:
        reference_refresh_warning = refresh_reference_ids(ids_dir)
        if reference_refresh_warning:
            print(f"WARNING: {reference_refresh_warning}", file=sys.stderr)
    reference_lookup, reference_cache_warning = load_reference_id_cache(ids_dir)
    parent_ids = load_parent_id_map()
    run_dirs = create_run_dirs(output_root, args.run_id, archive=args.archive_run)
    statuses: list[JobStatus] = []

    checker_error = None
    checker_exe = None
    use_python_checker = not args.skip_format_check
    try:
        checker_exe = None if args.skip_format_check else configured_checker_executable(args, cfg)
        if not args.skip_format_check and checker_exe is not None:
            use_python_checker = False
        elif not args.skip_format_check and can_compile_checker(args, cfg):
            checker_exe = compile_checker(run_dirs["root"], force=args.force_compile)
            use_python_checker = False
    except WorkflowError as exc:
        checker_exe = None
        use_python_checker = False
        checker_error = str(exc)
        run_dirs["logs"].mkdir(parents=True, exist_ok=True)
        (run_dirs["logs"] / "format_checker_setup_error.txt").write_text(checker_error + "\n", encoding="utf-8")

    local_files = [Path(path) for path in args.local_file or []]
    station_codes = load_station_codes(ids_dir / "BSRN_IDs.txt") or STATIONS
    for local_file in local_files:
        job = validate_job(parse_job_code(local_file.name), station_codes)
        status = JobStatus(job=job.label, source=str(local_file))
        statuses.append(status)
        try:
            dat_path = copy_local_dat(local_file, run_dirs["dat"])
            status.dat_path = str(dat_path)
            attach_minute_completeness(status)
            metadata_dir, reference_import_file = extract_metadata(dat_path, run_dirs["metadata"], ids_dir, verbose=args.verbose)
            status.metadata_dir = str(metadata_dir)
            status.reference_import_file = str(reference_import_file)
            attach_reference_id_status(status, reference_lookup, reference_cache_warning)
            attach_parent_id_status(status, parent_ids)
            metadata_issues = validate_metadata_ids(metadata_dir, status.job)
            if metadata_issues:
                status.metadata_warnings.extend(f"Metadata warning: {issue}" for issue in metadata_issues)
            status.metadata_ok = True
            if checker_error:
                status.errors.append(f"Format check not run: {checker_error}")
            elif not args.skip_format_check and use_python_checker:
                ok, report, _output = run_python_format_check(dat_path, run_dirs["format_reports"])
                status.format_ok = ok
                status.format_report = str(report)
            elif not args.skip_format_check and checker_exe is not None:
                ok, report, _output = run_format_check(dat_path, run_dirs["format_reports"], checker_exe)
                status.format_ok = ok
                status.format_report = str(report)
        except Exception as exc:
            status.errors.append(str(exc))
            write_exception_log(run_dirs["logs"], f"{status.job}_download_check", exc)

    for job in expand_jobs(args):
        status = JobStatus(job=job.label, source=f"ftp:{job.gz_name}")
        statuses.append(status)
        try:
            gz_path = download_job(job, cfg, run_dirs["downloads_gz"])
            status.downloaded = True
            status.gz_path = str(gz_path)
            dat_path = decompress_gzip(gz_path, run_dirs["dat"])
            status.decompressed = True
            status.dat_path = str(dat_path)
            attach_minute_completeness(status)
            metadata_dir, reference_import_file = extract_metadata(dat_path, run_dirs["metadata"], ids_dir, verbose=args.verbose)
            status.metadata_dir = str(metadata_dir)
            status.reference_import_file = str(reference_import_file)
            attach_reference_id_status(status, reference_lookup, reference_cache_warning)
            attach_parent_id_status(status, parent_ids)
            metadata_issues = validate_metadata_ids(metadata_dir, status.job)
            if metadata_issues:
                status.metadata_warnings.extend(f"Metadata warning: {issue}" for issue in metadata_issues)
            status.metadata_ok = True
            if checker_error:
                status.errors.append(f"Format check not run: {checker_error}")
            elif not args.skip_format_check and use_python_checker:
                ok, report, _output = run_python_format_check(dat_path, run_dirs["format_reports"])
                status.format_ok = ok
                status.format_report = str(report)
            elif not args.skip_format_check and checker_exe is not None:
                ok, report, _output = run_format_check(dat_path, run_dirs["format_reports"], checker_exe)
                status.format_ok = ok
                status.format_report = str(report)
        except Exception as exc:
            status.errors.append(str(exc))
            write_exception_log(run_dirs["logs"], f"{status.job}_download_check", exc)
            print(f"ERROR {job.label}: {exc}", file=sys.stderr)

    batch_reference_import = write_batch_reference_import(statuses, run_dirs["metadata"])
    batch_metadata_reports = write_batch_metadata_reports(statuses, run_dirs["metadata"])
    batch_format_report = write_batch_format_report(statuses, run_dirs["format_reports"])
    attach_batch_artifacts(statuses, batch_reference_import, batch_format_report, batch_metadata_reports)

    status_json = run_dirs["root"] / "status.json"
    status_json.write_text(json.dumps([asdict(status) for status in statuses], indent=2), encoding="utf-8")
    dashboard_path = resolve_project_path(args.dashboard or cfg.get("paths", "dashboard", fallback="dashboard.html"))
    write_run_index(run_dirs, statuses, dashboard_path=dashboard_path)
    print(f"Run directory: {run_dirs['root']}")
    print(f"Status JSON:   {status_json}")
    print(f"Dashboard:     {dashboard_path}")
    return 1 if any(status.errors for status in statuses) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="INI config path")
    parser.add_argument("--output-root", help="Output root; defaults to config paths.output_root")
    parser.add_argument("--dashboard", help="Central dashboard path; defaults to BSRN/dashboard.html")
    parser.add_argument("--archive-run", action="store_true", help="Keep a timestamped output/runs/<timestamp> folder instead of replacing output/current")
    parser.add_argument("--run-id", help="Optional run folder name under output/runs")
    parser.add_argument("--ids-dir", help="Directory containing BSRN_IDs.txt")
    parser.add_argument("--refresh-bsrn-ids", action="store_true", help="Download a fresh BSRN_IDs.txt before checks")
    parser.add_argument("--refresh-reference-ids", action="store_true", help=f"Download a fresh {BSRN_REFERENCE_IDS_FILE} before checks")
    parser.add_argument("--job", action="append", help="BSRN job code, e.g. cab0425 or cab0425.dat.gz")
    parser.add_argument("--jobs-file", help="Text file with one BSRN job code per line")
    parser.add_argument("--station", action="append", help="Station acronym; repeatable")
    parser.add_argument("--year", action="append", type=int, help="Four-digit year; repeatable")
    parser.add_argument("--month", action="append", type=int, help="Month 1-12; repeatable")
    parser.add_argument("--local-file", action="append", help="Local .dat file to process without FTP; repeatable")
    parser.add_argument("--checker-exe", help="Trusted legacy f_check executable; normal runs use the Python checker")
    parser.add_argument("--force-compile", action="store_true", help="Explicitly compile and use legacy f_check instead of the Python checker")
    parser.add_argument("--skip-format-check", action="store_true", help="Skip format checking")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run_workflow(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
