# BSRN Import-File Metadata Map

This note maps the metadata/header fields needed for BSRN reference import files and later PANGAEA data import files. It is based on the current workflow code, the original Tool 1 C++ sources, and the checked import-file examples.

Important workflow boundary:

- The reference import file is created during the first download/check step, alongside LR0001-LR0009 metadata outputs.
- Data import files are created later, only after format check, QC generation, curator review, and curator approval.
- Data import file `ReferenceIDs` must use the PANGAEA reference ID for the station-to-archive `.dat.gz` file. That ID comes from the BSRN reference list and must be refreshed/cached explicitly, not fetched silently during import generation.

## Source Files

- `scripts/bsrn_download_check.py`: current download/check step, LR0001-LR0009 metadata extraction, local `BSRN_IDs.txt` refresh gate.
- `tools/create-importfiles/c++originals/doAll.cpp`: Tool 1 orchestration. Metadata conversion includes `CreateReferenceImportFile`; data import conversion calls `readBsrnReferenceIDs(false)` first.
- `tools/create-importfiles/c++originals/ApplicationTools.cpp`: shared station/staff/method/reference lookup helpers and JSON metaheader writers.
- `tools/create-importfiles/c++originals/CreateReferenceImportFile.cpp`: reference import file creation.
- `tools/create-importfiles/c++originals/BasicMeasurementsConverter.cpp`, `UltraVioletMeasurementsConverter.cpp`, `SYNOPConverter.cpp`: representative data import header writers.
- `tools/create-importfiles/importfiles-examples/*.txt`: concrete output examples.

## Reference Import File

The reference import file is not a JSON-metaheader data import file. It is a tab-separated reference import table with this header:

```text
Author(s)    Year    Title    URI    PublicationStatus    PublicationType
```

Tool 1 creates one row per station-month file:

| Reference field | Source | Notes |
| --- | --- | --- |
| `Author(s)` | LR0002 station scientist name mapped through `BSRN_IDs.txt` `[Staff]` via `findPiID()` | `CreateReferenceImportFile.cpp` reads LR0002 and uses `InputStr.left(38).simplified()` for the station scientist. Missing lookup returns `-999`; current workflow records this as a metadata warning for user-facing QC/export flow, while curator-only PANGAEA import generation still needs valid IDs. |
| `Year` | Current system year when the reference file is generated | `ReferenceImportFile()` uses `QDate::currentDate().toString("yyyy")`, not the data year. Example `CAB_2025-04_refImp.txt` has year `2026`. |
| `Title` | Converter constant plus station name from `BSRN_IDs.txt` `[Station]` plus LR0001 year-month | Shape: `BSRN Station-to-archive file for station <StationName> (yyyy-MM)`. If station name already ends with `Station`, Tool 1 does not insert `station`. |
| `URI` | Event label from `BSRN_IDs.txt` `[Station]` plus LR0001 month/year | Shape: `ftp://ftp.bsrn.awi.de/<event-lower>/<event-lower><MMyy>.dat.gz`. |
| `PublicationStatus` | Tool 1 constant | `published`. |
| `PublicationType` | Tool 1 constant | C++ uses `data set`; current example uses `dataset`. Preserve the accepted current example spelling unless curator/PANGAEA requires the older spelling. |

Reference import output filename:

```text
<EventLabel>_<yyyy-MM>_refImp.txt
```

Tool 1 also concatenates batch outputs into `BSRN_RefImp.txt`.

## Data Import File Metaheader

Data import files start with a JSON-like metaheader:

```text
// METAHEADER - BSRN data import at <current timestamp>
{
  ...
}
// METAHEADER END
```

`OpenDataDescriptionHeader()` uses the current local time formatted as `yyyy-MM-ddThh:mm`, matching the inspected examples.

## Field Map

| Header field | Applies to | Source | Tool 1 behavior and implementation note |
| --- | --- | --- | --- |
| `ReferenceIDs` | Data import files only | Explicit/cached PANGAEA BSRN reference list, keyed by station-to-archive FTP URI | `ReferenceOtherVersion()` builds the same FTP URI as the reference import file and looks it up in `BSRN_Reference_IDs.txt`, loaded by `readBsrnReferenceIDs(false)`. If found, it writes `{ "ID": <numeric id>, "RelationTypeID": 13 }`. If not found, old C++ writes the URI string in the `ID` slot. The workflow should instead block data import generation until the numeric ID is available. Refresh source: `https://www.pangaea.de/ddi?request=bsrn/BSRNReferences&format=textfile&charset=UTF-8`. |
| `AuthorIDs` | Data import files only | LR0002 responsible station scientist mapped through `BSRN_IDs.txt` `[Staff]` | Data converters read LR0002, take the station scientist name from the fixed-width name field, and call `findPiID()`. Written as a one-item array, e.g. `[ 31652 ]`. |
| `SourceID` | Data import files only | Station institute ID from `BSRN_IDs.txt` `[Station]` | `findInstituteID(i_StationNumber)` maps LR0001 station number to the station's institute/source ID. |
| `Title` | Data import files only | Converter-specific title constant plus station name from `BSRN_IDs.txt` `[Station]` plus LR0001 year-month | Written by `DatasetTitle(text, stationName, dt)`. Adds `station ` before station names that do not end in `Station`. Examples: `Basic and other measurements of radiation at station Cabauw (2025-04)`, `Ultra-violet measurements from station Payerne (2023-01)`, `Meteorological synoptical observations from Neumayer Station (2023-01)`. |
| `ParentID` | LR0100 or LR0100+LR0300 data import files only | Local `metadata/ParentIDs.txt`, keyed by station acronym/event label | The file is a station-wide tab-separated map with columns `Acronym`, `ParentID`, and optional `Comment`. When a numeric mapping exists, Python import generation writes `ParentID` directly below `Title`. Missing mappings are non-fatal and omit the line. ParentID is not written to LR0500, LR1000, LR1100, LR1200, LR1300, LR3010, LR3030, diagnostic files, manifests, unsupported-record files, or reference import files. |
| `ExportFilename` | Data import files only | Event label from `BSRN_IDs.txt`, converter-specific export token, LR0001 year-month | Written by `ExportFilename(event, text, dt)` as `<EventLabel>_<text>_<yyyy-MM>`. Examples: `CAB_radiation_2025-04`, `PAY_Ultra-violet_2023-01`, `GVN_SYNOP_2023-01`. |
| `EventLabel` | Data import files only | Station event label from `BSRN_IDs.txt` `[Station]`, keyed by LR0001 station number | Written as the station acronym/event label, e.g. `CAB`, `PAY`, `GVN`. The same event label is used for filenames and reference URI construction. |
| `ParameterIDs` | Data import files only | Converter constants plus LR0001/LR0009 selected parameters and method lookup through `BSRN_IDs.txt` `[Methods]`, `[Radiosonde]`, `[Ozonesonde]`, `[Expanded]` | Each entry is built by `Parameter()`: `{ "ID": <PANGAEA parameter>, "PI_ID": <AuthorID>, "MethodID": <method>, "Format": <format>, "Comment": <optional> }`. Date/time parameter `1599` and height-above-ground parameter `56349` use method `43` in radiation/UV files. Record-specific parameters and formats are defined in each converter. Instrument methods are usually resolved from LR0009 WRMC/instrument IDs using `findMethodID()`. |
| `ProjectIDs` | Data import files only | Tool 1 constant | Always `[ 4094 ]` in inspected C++ converters and examples. |
| `TopologicTypeID` | Data import files only | Tool 1 constant | Always `8` in inspected C++ converters and examples. |
| `StatusID` | Data import files only | Tool 1 constant | Always `4` in inspected C++ converters and examples. |
| `CurationLevelID` | Data import files only | Current required Tool 1/PANGAEA constant | Always `30` across stations and logical records. The inspected C++ source and older PAY/GVN examples omit this because they came from an older Tool 1 version; Python import generation must include it. |
| `LicenseID` | Data import files only | Current required Tool 1/PANGAEA constant | Always `107` across stations and logical records. The inspected C++ source and older PAY/GVN examples omit this because they came from an older Tool 1 version; Python import generation must include it. |
| `UserIDs` | Data import files only | Tool 1 constant | Always `[ 1144 ]` in inspected C++ converters and examples. |
| `LoginID` | Data import files only | Current required Tool 1/PANGAEA constant | Always `1` across stations and logical records. Inspected C++ and older PAY/GVN examples write `3`; treat those as older Tool 1 behavior, not the target behavior for the Python port. |

## Shared Data Sources

| Source | Provides | Refresh/cache rule |
| --- | --- | --- |
| LR0001 in `.dat` file | Station number, month, year, available quantity IDs | Parsed from the station-to-archive file. Used for event lookup, date, title, filename, and parameter selection. |
| LR0002 in `.dat` file | Responsible station scientist name | Mapped to PANGAEA staff ID through `BSRN_IDs.txt`. Responsible scientist ID is required before continuing. Deputies are not used for `AuthorIDs`. |
| LR0004 in `.dat` file | Latitude/longitude for data table rows | Used in non-import data output and some converter processing. Import-mode radiation/UV table headers mainly use Date/Time plus height above ground. |
| LR0009 in `.dat` file | Parameter/instrument assignments and change times | Used by data converters to attach method IDs and optional method-change comments to `ParameterIDs`. |
| `tools/create-importfiles/BSRN_IDs.txt` | Station event labels, station names, institute/source IDs, staff IDs, method IDs | Local by default. Refresh only on explicit request, matching existing `--refresh-bsrn-ids` behavior. |
| Cached `BSRN_Reference_IDs.txt` | Numeric PANGAEA reference IDs for station-to-archive FTP URIs | Must be explicit/cached. Data import generation must not silently depend on network state. |
| `metadata/ParentIDs.txt` | Station-wide PANGAEA collection ParentIDs and optional curator comments | Local only. No network refresh is performed. Comments are shown in dashboard Details when configured. |
| Station/job code and LR0001 date | Output filenames and FTP URI suffix | Tool 1 validates that the local filename matches event label, month, and two-digit year. |
| Converter constants | Title text, export filename token, PANGAEA parameter IDs, formats, fixed metadata constants | Must be ported per converter without sample-specific workarounds. |
| Current date/time | Reference publication year and metaheader timestamp | Reference import `Year` uses current year. Data metaheader comment uses current timestamp. |

## Current Required Data Header Constants

Use these constants for every generated data import file, regardless of station or logical record:

| Field | Value |
| --- | --- |
| `ProjectIDs` | `[ 4094 ]` |
| `TopologicTypeID` | `8` |
| `StatusID` | `4` |
| `CurationLevelID` | `30` |
| `LicenseID` | `107` |
| `UserIDs` | `[ 1144 ]` |
| `LoginID` | `1` |

## Implementation Implications

- Add reference import generation during `bsrn_download_check.py` metadata output creation, before QC and before data import generation exists.
- Add a separate explicit refresh/cache path for `BSRN_Reference_IDs.txt`, analogous to the existing `BSRN_IDs.txt` refresh behavior.
- In later data import generation, block when `ReferenceIDs` cannot be resolved to a numeric PANGAEA ID. The old C++ URI fallback should be documented as legacy behavior, not carried into the workflow silently.
- Include the current required constants `CurationLevelID: 30`, `LicenseID: 107`, and `LoginID: 1` for all data import files, following the CAB example rather than the older PAY/GVN outputs.
- Keep constants and station-specific rules in converter modules, copied from the original C++ converters. Do not introduce sample-specific exceptions.
