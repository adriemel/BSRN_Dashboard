# BSRN Station Coverage Audit

Date: 2026-06-05

## Source Of Truth

The workflow now treats `tools/create-importfiles/BSRN_IDs.txt` `[Station]` as the local source of truth for valid station event labels. Normal runs still use the local cache only; refreshing `BSRN_IDs.txt` remains explicit through `--refresh-bsrn-ids`.

## Findings

- Current `BSRN_IDs.txt` lists 84 station event labels.
- The previous hard-coded `scripts/bsrn_download_check.py` `STATIONS` list had 76 labels.
- `PAR` was present in `BSRN_IDs.txt` as station ID `66`, event label `PAR`, name `Paramaribo`, source ID `1057`, but it was missing from the hard-coded validation list.
- Other labels present in `BSRN_IDs.txt` but missing from the old validation list were `GIM`, `LMP`, `LYU`, `OHY`, `QIQ`, `RUN`, `SEL`, and `YUS`.
- The old validation list contained `EFS`, which is not present in the current `BSRN_IDs.txt`.
- The QC station-name dictionaries were stale for some current stations. They now overlay names from `BSRN_IDs.txt` at runtime while retaining the existing dictionary as fallback.
- `NPT` was removed from the local station cache because it is no longer a station. This also removes the previous duplicate numeric station ID `87` conflict with `MIN`.

## Impact

- Job input validation and FTP download/check were affected: a valid job such as `par0425` was rejected before FTP access or metadata extraction.
- Metadata extraction was indirectly affected because the non-GUI toolbox adapter exposed the same stale station list to toolbox filename validation.
- QC parsing/reporting was only partially affected: unknown names fell back to the acronym, but reports could miss the proper station name for stations absent from the QC dictionary.
- Readable data exports were not blocked by the global station list because they derive the event label from `status.job` and DAT metadata.
- PANGAEA import generation depends on `BSRN_IDs.txt` for station ID to event/name/source mapping, so `PAR` was already covered there once a status row could be produced.
- If a future station cache contains duplicate numeric station IDs, reference-import and data-import metadata resolve station ID plus filename/status event label instead of relying on numeric ID alone.
- Dashboard display follows status rows and QC metadata; it benefits from the corrected validation and QC station-name lookup.
- `metadata/ParentIDs.txt` already contains `par` with ParentID `979681`.

## Implemented Fix

- Added `scripts/bsrn_station_registry.py` to parse station event labels and names from local `BSRN_IDs.txt`.
- `scripts/bsrn_download_check.py` now validates job codes against that parsed station list, including custom `--ids-dir` when provided.
- The toolbox adapter now exposes stations derived from its configured ID directory.
- Reference-import generation and data-import metadata loading now disambiguate duplicate numeric station IDs with the event label from the DAT filename or status row.
- `tools/qc-graphs/bsrn_qc.py` and `tools/create-importfiles/bsrn_qc.py` now update their station-name lookup from local `BSRN_IDs.txt`.

## Remaining Notes

- Missing ParentID mappings are still non-fatal and only omit the `ParentID` line from LR0100/LR0100+LR0300 import files.
- User-facing QC/export flow is non-blocking for metadata ID findings and format warnings. PANGAEA import generation still requires `qc_ok=true`, a numeric `pangaea_reference_id`, and current curator QC approval.
- LR4000 remains excluded from user-facing exports and PANGAEA import generation.
