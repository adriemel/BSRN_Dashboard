#!/usr/bin/env python3
"""Start or reuse the local BSRN dashboard server, then open the dashboard."""

from __future__ import annotations

import argparse
import http.client
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_SCRIPT = PROJECT_ROOT / "scripts" / "bsrn_dashboard_server.py"
LOG_PATH = PROJECT_ROOT / "output" / "logs" / "dashboard_launcher_server.log"


def dashboard_responds(host: str, port: int) -> bool:
    try:
        conn = http.client.HTTPConnection(host, port, timeout=1.0)
        conn.request("GET", "/")
        response = conn.getresponse()
        body = response.read(4096).decode("utf-8", errors="ignore")
        return response.status == 200 and "BSRN Workflow Dashboard" in body
    except OSError:
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def start_server(host: str, port: int) -> subprocess.Popen:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_handle = LOG_PATH.open("a", encoding="utf-8", errors="replace")
    log_handle.write(f"\n--- starting dashboard server on {host}:{port} ---\n")
    log_handle.flush()
    creationflags = 0
    if sys.platform.startswith("win"):
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    return subprocess.Popen(
        [sys.executable, str(SERVER_SCRIPT), "--host", host, "--port", str(port)],
        cwd=PROJECT_ROOT,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )


def wait_for_server(host: str, port: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if dashboard_responds(host, port):
            return True
        time.sleep(0.25)
    return False


def launch_dashboard(args: argparse.Namespace) -> int:
    url = f"http://{args.host}:{args.port}/"
    if not dashboard_responds(args.host, args.port):
        start_server(args.host, args.port)
        if not wait_for_server(args.host, args.port, args.timeout):
            print(f"Could not start BSRN dashboard at {url}. See {LOG_PATH}.", file=sys.stderr)
            return 1
    if not args.no_open:
        webbrowser.open(url)
    print(f"BSRN dashboard: {url}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--timeout", default=12.0, type=float)
    parser.add_argument("--no-open", action="store_true", help="Start/reuse the server but do not open a browser")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    return launch_dashboard(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
