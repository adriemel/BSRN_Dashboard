# bsrn_download_check.py — Walkthrough

## What Changed
**2026-06-10 (palette B)** — Static dashboard template recolored to match the live server: dark petrol header with BSRN orange title/accent/primary buttons, light petrol sidebar, unified warning amber #ffbd3d, terracotta error color #c4573b.

**2026-06-10 (later)** — Static dashboard template: header color changed to CMYK 0/26/76/0 (`#ffbd3d`) and the stepper state classes renamed (`state-ok` etc.) so they no longer collide with the global badge colors; steps now render as colored dots/connectors with dark, readable text.

**2026-06-10** —
1. **DAT file read cache:** the same station file used to be read from disk and decoded four to five times per run (reference import, LR0002 repair, minute completeness, format check). `read_dat_text` now keeps the last few decoded files in memory, keyed by path plus modification time and size, so a changed file is always re-read.
2. **Faster illegal-character check:** the format checker scanned every character of every line in a Python loop. It now uses precompiled regular expressions that encode the exact same allowed-character rules, letting the regex engine (C speed) find the first violation. Reports are unchanged.
3. **FTP credentials off the command line:** the curl download previously received `--user name:password` as a visible process argument (readable in Task Manager / process listings). Credentials are now piped to curl via a stdin config (`--config -`), so they never appear in the process list.
4. **Static dashboard restructure:** the generated `dashboard.html` / `output/current/index.html` snapshot now shows a workflow stepper (File → Metadata → Format → QC → Approval → Import) per job, a full-width "Next step" card, cards ordered by workflow stage, and refreshed typography/background. Mirrors the live dashboard server.

## What This File Does
This is the workflow entry point for Tool 1: it downloads `.dat.gz` station files from the BSRN FTP server (or takes local files), unzips them, extracts the LR0001–LR0009 metadata check files, checks staff/method IDs against the PANGAEA lookup, runs the Python-native format checker, computes minute completeness, writes `status.json`, and regenerates the static dashboard snapshot.

## Section-by-Section Walkthrough (changed parts only)

#### DAT text cache (`_DAT_TEXT_CACHE`, `read_dat_text`)
**What it does:** Remembers the decoded text of up to four recently read station files. Before using a cached copy it compares the file's current modification time and size; any difference forces a fresh read.
**Why it exists:** Several independent steps in one run each need the full file text. Reading and decoding a multi-megabyte file once instead of five times shaves noticeable time off every job.
**To change it:** If memory is ever a concern with very large batches, lower `_DAT_TEXT_CACHE_MAX`. Do not cache without the mtime/size check — the curator sometimes fixes a file and reruns.

#### Illegal-character scanners (`_ILLEGAL_*_RE`, `check_format_illegal_characters`)
**What it does:** For data records (number above 99, except 1000) only space, `+`, `-`, `.`, and digits are allowed; for the others, printable ASCII (plus tab in record 3). A regex per rule finds the first forbidden character in a line.
**Why it exists:** Same rules as before, much faster on month-long minute files.
**To change it:** If the allowed-character rules ever change, update both the regex and the explanatory text in `format_illegal_character_errors` together.

#### Credential handling in `download_job`
**What it does:** Builds the curl command without the password and writes `user = "name:password"` into curl's stdin config channel instead. Backslashes and quotes in credentials are escaped.
**Why it exists:** Command-line arguments of running processes are visible to other local programs; passwords should never travel that way.
**To change it:** If you switch FTP accounts, nothing changes here — credentials still come from `config/bsrn_workflow.ini` or the `BSRN_FTP_USER`/`BSRN_FTP_PASSWORD` environment variables.

#### Static dashboard template (inside `write_run_index`)
**What it does:** Produces the read-only HTML snapshot. The detail view now starts with a six-step workflow stepper, followed by a prominent "Next step" card and the artifact cards in processing order (Files, QC, Data exports, Import, Minute completeness).
**Why it exists:** The previous layout mixed status badges and cards without a visible notion of "where am I in the workflow".
**To change it:** The stepper labels and their data sources live in the small JavaScript `stepper(row)` function near the bottom of the template. Colors come from the CSS variables at the top (curator yellow/teal palette); change them there only.

## Things to Know
- The snapshot (`dashboard.html`, `output/current/index.html`) is regenerated on the next workflow action; the copies on disk right now still show the old layout until then.
- The live dashboard server (`bsrn_dashboard_server.py`) has its own near-identical template. Layout changes must be made in both places — a known duplication, kept deliberately because the two differ in interactivity (forms vs. links).
- Verification commands (bundled runtime):
  `...python.exe -m py_compile scripts\bsrn_download_check.py scripts\bsrn_dashboard_server.py tools\download-extract\BSRN_Toolbox_py\logic\ingestor.py`
  `...python.exe tests\security_regression_checks.py`
  `...python.exe scripts\bsrn_download_check.py --local-file input\tat0426.dat`
