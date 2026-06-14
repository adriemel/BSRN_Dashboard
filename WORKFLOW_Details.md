# BSRN Dashboard Workflow

A local curator dashboard for archiving BSRN station-to-archive files. One browser window drives a station-month from download to PANGAEA import files. A six-stage stepper (File, Metadata, Format, QC, Approval, Import) shows where each job is, and a "Next step" card holds the buttons that move it forward.

## Steps

1. **Launch:** double-click `Start-BSRN-Workflow.cmd`. This starts the local server and opens `http://127.0.0.1:8765/` (loopback-only, CSRF-protected). FTP downloads need a local `config/bsrn_workflow.ini` with BSRN credentials; local-file-only use needs none.
2. **Load files:** enter job codes (e.g. `cab0325`, `cab0425`) to fetch from FTP, or put `.dat` files in `input/` and reference them as `input\cab0425.dat` (drag/drop and file-pick also work). Submit to start the run.
3. **Download and checks (automatic):** downloads and unzips `.dat.gz` (or copies local files), extracts LR0001-LR0009 metadata, checks staff/method IDs against the PANGAEA lookups, runs the Python-native format checker, computes minute completeness, writes `status.json`, and regenerates the dashboard. Outputs go to `output/current`.
4. **Review:** open the job in the batch list. Metadata and format issues show as warnings and do not block continuation. Minute completeness is shown for review only and does not gate the workflow.
5. **Format gate:** if the format check fails and you can fix it, correct the file in `input/` and re-submit; otherwise return it to the station scientist.
6. **Run QC:** click **Continue to QC** to generate the interactive HTML QC report and plots (static report optional via `--include-static-qc-report`).
7. **Visual QC:** inspect the interactive report and plots. Accept or hold is expert judgement.
8. **Decision:** click **Approve QC** or **Reject QC** (with a note). Recorded per job.
9. **Generate import files** (curator only): after approval, click **Generate import files** to produce the PANGAEA tabular text files with JSON header (`scripts/bsrn_import_files.py`).
10. **Import:** manual upload into PANGAEA, outside the dashboard.

## Data exports

**Export all data** (or a row's **Export data**) writes readable per-logical-record CSVs to the run's `data_exports/` folder, separate from the PANGAEA import files. Non-curator users can skip import generation and use these CSVs plus the QC reports. LR4000 is excluded.

## Gates

| Gate | Continue when | Stop / follow up when |
|---|---|---|
| Format check | Valid, or a small fix reruns clean | Invalid and not fixable locally (return to station scientist) |
| Metadata / reference IDs | Staff/method/reference IDs resolve in PANGAEA | ID missing; create the PANGAEA entry and refresh reference IDs before import |
| Visual QC | Acceptable on expert review | Suspect or bad data (Reject QC) |
| Import readiness | QC approved and import files generate | QC not approved, or generation fails |

Import generation requires `qc_ok`, a numeric PANGAEA reference ID, and current curator approval. Network lookups (`BSRN_IDs.txt`, `BSRN_Reference_IDs.txt`) are refreshed only on explicit request.

## Components

- `bsrn_download_check.py` - download, unzip, metadata, ID checks, format check, minute completeness, dashboard snapshot.
- `bsrn_qc_continue.py` - QC report/plot generation and data-export refresh.
- `bsrn_import_files.py` - PANGAEA import-file generation after the gates pass.
- `bsrn_dashboard_server.py` - local curator dashboard (stepper, action buttons, artifact/plot cards).

The static `dashboard.html` / `output/current/index.html` is a read-only snapshot for viewing run state without the server; workflow actions require the running server.
