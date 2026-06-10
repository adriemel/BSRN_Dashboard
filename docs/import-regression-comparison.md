# BSRN Import-File Regression Comparison

Use `scripts/bsrn_compare_imports.py` to compare generated PANGAEA import files with Tool 1 examples under:

```text
tools/create-importfiles/importfiles-examples
```

The helper is read-only. It does not run download/check, QC, curator approval, import generation, network refreshes, FTP downloads, or PANGAEA submission. Generate import files through the normal workflow first, then compare them.

## Known Regression Command

After the known regression jobs have generated import files under `output/runs` or `output/current/import_files`, run:

```powershell
C:\Users\desir\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe scripts\bsrn_compare_imports.py --generated-dir output\runs --known-regression-jobs --mode semantic --ignore-metaheader-timestamp --json-output output\runs\m15_import_compare_known_jobs_semantic.json
```

For the current default workspace only:

```powershell
C:\Users\desir\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe scripts\bsrn_compare_imports.py --generated-dir output\current\import_files --known-regression-jobs --mode semantic --ignore-metaheader-timestamp --json-output output\current\import_compare_known_jobs.json
```

Strict raw-text comparison is also available:

```powershell
C:\Users\desir\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe scripts\bsrn_compare_imports.py --generated-dir output\runs --known-regression-jobs --mode strict --ignore-metaheader-timestamp
```

## Modes

- `--mode strict`: compares lines exactly. Use `--ignore-metaheader-timestamp` to ignore the first `// METAHEADER` timestamp line.
- `--mode semantic`: parses the metaheader and data table where possible. Numeric strings compare by numeric value, so values such as `909` and `909.0` are equal.
- `--json-output <path>`: writes the same report as JSON while keeping terminal output readable.
- `--job <EVENT_YYYY-MM>`: limits comparison to one or more jobs.
- `--known-regression-jobs`: compares `DRA_2025-02`, `DRA_2025-03`, `PAY_2023-01`, `GVN_2023-01`, `CAB_2025-04`, and `TAT_2026-04`.

## Reported Differences

The harness reports:

- missing generated files
- generated files with no matching example for requested jobs
- line-count differences
- metaheader/header field differences
- `ParameterIDs`, method, format, and table-header differences
- first data-row mismatches
- review notes for known acceptable differences such as generated `ParentID` additions and current required header constants in older fixtures

The command exits with code `0` only when all requested files match and no requested generated/example files are missing. A nonzero result can still be useful: it means the harness found differences that need review.

## Current Fixture Notes

Milestone 15 verification showed the harness correctly isolates current expected and unresolved differences:

- DRA LR0500, LR1300, and LR3010 examples match generated files in both strict and semantic mode when the timestamp line is ignored.
- DRA LR0100 examples differ because generated files now include station `ParentID`, added after the older Tool 1 examples.
- PAY/GVN/CAB LR0100 or LR0100+LR0300 examples also differ where generated files include `ParentID`.
- Several PAY/GVN data differences remain visible as regression findings, including blank generated missing values versus older `-9.9` example values and some PAY LR3030 row-value differences.
- TAT 2026-04 Tool 1 examples are available for `LR0100+LR0300`, `LR1000`, and `LR1100`. A normal generated comparison requires a valid `tat0426` workflow row with a numeric PANGAEA reference ID and current curator QC approval.
