# BSRN Toolbox C++ Source Overview

| File | Purpose |
|---|---|
| **ApplicationCreateMenu.cpp** | Builds the Qt application menus and connects menu actions to slots |
| **ApplicationErrors.cpp** | Centralized error handler — maps error codes to user-facing messages |
| **ApplicationInit.cpp** | Main window constructor; loads BSRN_IDs.txt and initializes application state |
| **ApplicationMainWindow.cpp** | Entry point (`main()`), window lifecycle, help display, version string |
| **ApplicationPreferences.cpp** | Saves/restores user preferences (station flags, month flags, paths) |
| **ApplicationTools.cpp** | Core utility — notably `ReferenceImportFile()` which builds the PANGAEA import header (title, authors, year, event label) |
| **AssignmentConverter.cpp** | Converts LR-assignment records from station-to-archive files into import format |
| **AstroData.cpp** | Calculates astronomical parameters (solar geometry) from station data |
| **AuxiliaryDataRecommendedV20.cpp** | Computes recommended auxiliary parameters (V2.0 standard) and manages their output order |
| **BasicMeasurementsConverter.cpp** | Parses and converts LR0100 basic radiation measurements into import files |
| **BsrnData.cpp** | Core data model — reads and parses station-to-archive file sections into a structured object |
| **BuildStatusFiles.cpp** | Generates status files listing which export files were created for a given station-month |
| **CheckStationToArchiveFiles.cpp** | Validates station-to-archive files for format/content correctness |
| **CompressFiles.cpp** | Compresses output files (wrapper around compression logic) |
| **ConcatenateFiles.cpp** | Merges multiple files into one, with optional skipping of header lines |
| **ConvertEOL.cpp** | Converts line endings between Windows/Mac/Unix formats |
| **ConvertFiles.cpp** | Generic file format converter (delimiter, missing value substitution) |
| **CreateReferenceImportFile.cpp** | Creates PANGAEA reference/citation import files from station-to-archive input |
| **CreateReplaceDatabase.cpp** | Builds a database of dataset IDs to replace/update in PANGAEA |
| **DownloadStationToArchiveFiles.cpp** | Downloads station-to-archive files from the BSRN FTP server |
| **DownloadStationToArchiveFiles_mod.cpp** | Modified/alternate version of the download logic (likely iterates differently over stations/months) |
| **ExpandedMeasurementsConverter.cpp** | Parses LR1300 expanded measurements and converts them to import format |
| **FileIDConverter.cpp** | Maps station/date identifiers to PANGAEA file/event label conventions |
| **MessagesConverter.cpp** | Converts the LR-messages section of station-to-archive files |
| **OtherMeasurementsAtXmConverter.cpp** | Converts LR3xxx "other measurements at height Xm" records |
| **OtherMinuteMeasurementsConverter.cpp** | Converts LR0300 other minute-resolution measurements into monthly import files |
| **OzoneEquipmentConverter.cpp** | Converts ozone instrument/method metadata into import format |
| **OzoneMeasurementsConverter.cpp** | Converts LR1200 ozone measurements into monthly PANGAEA import files |
| **QualityCheckRecommendedV20.cpp** | Implements BSRN V2.0 QC (physically possible, extremely rare, comparison checks) |
| **RadiationInstrumentsConverter.cpp** | Converts radiation instrument metadata (methods) into import format |
| **RadiosondeEquipmentConverter.cpp** | Converts radiosonde instrument metadata into import format |
| **RadiosondeMeasurementsConverter.cpp** | Parses LR1100 radiosonde measurements and converts them |
| **SYNOPConverter.cpp** | Parses and validates SYNOP weather code records from station-to-archive files |
| **ScientistIDConverter.cpp** | Maps scientist names to PANGAEA staff/PI IDs for import headers |
| **SearchString_ApplicationCreateMenu.cpp** | Search-string variant of the menu creator (likely for a "search" UI mode) |
| **SearchString_ApplicationInit.cpp** | Search-string variant of the application initializer |
| **StationDescriptionConverter.cpp** | Converts station description metadata section into import format |
| **StationHistoryConverter.cpp** | Converts station history/change log records into import format |
| **UltraVioletMeasurementsConverter.cpp** | Parses UV radiation measurements (LR records) and converts them to import files |
| **Webfile.cpp** | Qt-based URL/web file abstraction used by download functions |
| **decompressFiles.cpp** | Decompresses downloaded station-to-archive files |
| **doAll.cpp** | Orchestrates full pipeline runs: `doAllMetadataConverter()`, `doAllDataConverter()`, `doAllImportConverter()` |
| **httpget.cpp** | Low-level HTTP file download using Qt network |
| **simplecrypt.cpp** | Third-party simple XOR encryption (used for storing FTP credentials) |
| **solpos.cpp** | NREL solar position algorithm — computes solar zenith/azimuth for QC and astro calculations |

## Notes on PANGAEA import file creation

The most relevant files for building PANGAEA import files with metadata headers:

- **`ApplicationTools.cpp`** — `ReferenceImportFile()` assembles the header (title, authors, year, event label)
- **`CreateReferenceImportFile.cpp`** — top-level orchestrator for the reference import process
- **`ScientistIDConverter.cpp`** — resolves PI/staff IDs used in headers
- **`doAll.cpp`** — `doAllImportConverter()` ties all measurement converters together
- Individual `*Converter.cpp` files emit the data body for each measurement type
