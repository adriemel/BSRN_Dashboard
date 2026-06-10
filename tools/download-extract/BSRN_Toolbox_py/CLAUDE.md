# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

The BSRN Toolbox is a Python/PyQt5 desktop application for the Baseline Surface Radiation Network (BSRN). It downloads station-to-archive radiation data files from FTP servers, converts binary `.dat.gz` files into TAB-separated ASCII, and runs quality control checks per the "BSRN Global Network recommended QC tests, V2.0."

Maintained by the Alfred Wegener Institute (AWI). BSD-3 license.

## Running the Application

```bash
# Install dependencies
pip3 install PyQt5 openpyxl requests

# Launch GUI
python3 start.pyw
```

There is no build step, no linter, and no test suite. `test.py` is an ad-hoc script for manual testing, not a test framework.

## Architecture

**MVC + Observer pattern** with PyQt5 signals/slots:

```
start.pyw → Controller → creates Model, View, Observer
                           │        │         │
                           │        │         └─ PyQt signals bridging Model↔View
                           │        └─ PyQt5 GUI (loads .ui XML files from mvc/data/)
                           └─ App state, config, references to all logic subsystems
```

**Controller** (`mvc/controller.py`) — Bootstrap only. Creates QApplication, instantiates Model→View→Observer, enters event loop.

**Model** (`mvc/model.py`) — Singleton state container. Holds config (read/saved to `~/bsrn_user_data/cfg.txt` via ConfigParser), station/year/month lists, and references to all logic subsystems: TaskManager, LocalWorkingDatabase, BsrnIdSystem, Converter, SmartPrinter.

**View** (`mvc/view.py`) — All GUI code. Loads UI layouts from `mvc/data/*.ui` files. Contains inner helper classes: `FileListView`, `PrintConsole`, `Progress`, `Workflow`. Launches background work via Worker classes.

**Observer** (`mvc/observer.py`) — Defines pyqtSignals (print, progress, workflow, statusbar, buffer). Connected to View slots. All logic modules output to the GUI through Observer, never directly.

### Logic Layer (`logic/`)

| Module | Role |
|--------|------|
| `task_manager.py` | Orchestrates workflows: download→ingest→QC→export. Main methods: `download_station_data_and_check()`, `process_station_data()`, `check_availability_on_server()` |
| `ingestor.py` | Parses binary BSRN file format. Returns OrderedDict with metadata records (0001-0009) and data records (0100, 0300, 0400, etc.) |
| `converter.py` | Converts parsed data to TAB-separated ASCII output |
| `local_working_database.py` | Local buffer/cache system. Downloads via FTP (`ftplib`), stores in `~/bsrn_user_data/buffer/`, supports import/export as ZIP |
| `selection.py` | Flexible file/data selection. Loads from filenames or (station, month, year) tuples. Provides iterator interface for processing pipelines |
| `bsrn_id_system.py` | Station metadata lookup. Downloads `BSRN_IDs.txt` from PANGAEA, parses sections: [station], [staff], [methods], etc. |
| `helper.py` | Utilities: `Result` (structured error/warn/info/data container returned by all operations), `SmartPrinter` (unified logging with verbosity levels), `FileTools`, `PrettyText` |
| `worker.py` | QThread wrappers for background tasks. Workers emit results via signals when done |
| `qc.py` | Quality control — currently a stub (6 lines) |

### Key Patterns

- **Result object**: Every major operation returns a `Result` carrying errors, warnings, info, and a data dict. Check `res.is_err()` before using `res.get_data("key")`.
- **Stoppable**: Base class in `helper.py` for cancellable operations. TaskManager and LocalWorkingDatabase inherit it; supports parent-child abort propagation.
- **SmartPrinter**: Routes output to console or GUI via Observer. Three levels: `normal()`, `verbose()`, `debug()`. Always accessed through Model.
- **Selection iterator**: `for idx, (base, file, path) in sel:` — iterates over validated file entries after `sel.init()`.

## User Data

All persistent data lives in `~/bsrn_user_data/`:
- `cfg.txt` — ConfigParser config (sections `[DL]` for download settings, `[ETC]` for app settings)
- `buffer/` — Cached downloaded data files
- `ids/` — BSRN station lookup data (`BSRN_IDs.txt`)
- `tmp/` — Temporary files
- `bak/` — Backups of previous working directories

## Conventions

- German comments are mixed with English code throughout (e.g., "Instanzvariablen", "Daten müssen in einem Dictionary liegen")
- Private members use name-mangling (`self.__field`), accessed via explicit getters/setters
- Station codes are 3-letter uppercase strings (e.g., "BAR", "ALE", "IZA")
- BSRN filenames follow the pattern `{station3}{month2}{year2}.dat.gz` (e.g., `abs1223.dat.gz` = station ABS, month 12, year 2023)
- Year list and station list are hardcoded in `Model.__init__()` — must be updated manually when new stations/years are added
- FTP passwords are base64-encoded (not encrypted) in the config file
