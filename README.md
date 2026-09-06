# BSRN Dashboard Workflow

Local dashboard workflow for BSRN station-to-archive files. It downloads (or loads from /input) and checks monthly `.dat` files, runs metadata and format checks, creates QC reports and plots, and can generate PANGAEA import files (for curator use only). Needs a python environment, the packages are detailed in requirements.txt

## Quick Start

On Windows, double-click:

```text
Start-BSRN-Workflow.cmd
```

This starts the local dashboard server and opens:

```text
http://127.0.0.1:8765/
```

## Run From FTP

In the dashboard, enter one or more BSRN job codes such as:

```text
cab0325
cab0425
```

FTP download requires a local `config/bsrn_workflow.ini` file with valid BSRN FTP login details. This file is intentionally ignored by Git. Curators can obtain the login details from Amelie Driemel, `amelie.driemel@awi.de`.

## Run Local DAT Files

Place existing `.dat` files in the `input` folder, then enter paths such as:

```text
input\cab0425.dat
input\pay0123.dat
```

The workflow copies these files into the current run workspace before checking them.
In the local dashboard, you can also drag/drop or select `.dat` files to fill the local-file field with `input\filename.dat` paths.

## Outputs

Current run outputs are written under:

```text
output\current
```

The dashboard links metadata reports, format-check reports, QC HTML reports, QC plots, readable data exports, and generated import folders when available. QC continuation creates the interactive HTML QC report by default; the slower static report is optional with `--include-static-qc-report`.
After download/check has produced a DAT file, use `Export all data` or a row's `Export data` action to write user-facing CSVs to the run's `data_exports` folder. QC continuation also refreshes these exports.

Readable data exports are separate from PANGAEA import artifacts. They are written as per-logical-record CSVs such as `CAB_2025-04_LR0100_radiation.csv`, use readable parameter names where possible, and include a `Date/Time` column for time-based records. LR4000 remains excluded.

## PANGAEA Import Files

Import-file generation is for BSRN curator upload to PANGAEA. Non-curator users can ignore the import-generation step and use the `data_exports` CSVs plus the dashboard QC reports.

## Python Dependencies

Runtime dependencies are listed in:

```text
requirements.txt
```

On the current setup, the bundled Python runtime is used by the launcher and documented commands.

## Performance

Downloads prefetch two files at a time while metadata and format checks remain
sequential. Set `download_workers = 1` in the `[ftp]` section of your local config
for serial transfers; values from 1 to 4 are supported. The command-line
`--download-workers` option overrides this setting. FTP failures remain attached
to the individual job, and the existing FTP/FTPS transport is retained.

QC parses each monthly DAT once and gives independent copies to its report
generators. QC calculations, thresholds, plot resolution and exported values are
unchanged. Static QC reports remain optional.

Readable CSV exports are reused when the DAT content, export/parser source code,
and every generated CSV still match their saved hashes. Changing or deleting a
CSV, modifying the DAT, or updating parser code triggers regeneration. Cache
manifests live under the run's `data_exports/.cache` folder. This also avoids
repeating an earlier `Export data` operation when continuing to QC.

For an existing local DAT, use the local-file input to avoid another FTP transfer.
Remote jobs still download fresh files so upstream corrections are picked up.
