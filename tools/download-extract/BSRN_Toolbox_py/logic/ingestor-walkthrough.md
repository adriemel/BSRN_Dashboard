# ingestor.py — Walkthrough

## What Changed
**2026-06-10** — Added a module-level cache for the field-extraction patterns. Until now, every data row in a station file re-parsed its pattern string (regex expansion, constraint extraction, number conversion) from scratch; for a one-month 1-minute file that was roughly 90,000 redundant parses. Each distinct pattern is now parsed exactly once and reused. Extraction results and all error reports are byte-identical to before; only the speed changes. Also moved two error-message strings in the line-length check so they are only built when a line is actually too long.

## What This File Does
This file reads a raw BSRN station-to-archive `.dat` file line by line and turns it into a structured data object. It knows the layout of every logical record type (LR0001 metadata through LR4000 instrument temperatures), checks each value against the allowed ranges and missing-data codes from the BSRN technical plan, and collects a detailed report of everything imported plus any errors or warnings.

## The Big Picture
Think of it as a customs officer with a rulebook: every line of the file passes the desk, the officer looks up the stamp on the line (the record marker like `*C0100`), opens the matching page of the rulebook (the pattern string like `X,I2(1-31),X,I4(0-1439),...`), and either files the values into the right drawer or writes an incident report. The change made here is that the officer now keeps the rulebook pages open instead of re-reading them for every single line.

## Section-by-Section Walkthrough

#### Pattern cache helpers (top of file, `_expand_pattern`, `_parse_pattern_part`)
**What it does:** Two small functions that pre-digest the pattern strings. `_expand_pattern` normalizes a pattern and expands repetitions like `3[X,I2]` into `X,I2,X,I2,X,I2`. `_parse_pattern_part` takes one field definition like `F5(0-255!-99.9)` and extracts its type, width, allowed range, allowed values, and missing-data codes, already converted to numbers. Both remember their results (`lru_cache`), so each distinct pattern is processed once per program run.
**Why it exists:** These computations are identical for every row of the same record type. Doing them once instead of ~90,000 times is the main speed win for extraction.
**To change it:** Nothing to maintain here unless the BSRN pattern syntax itself changes. If a pattern is malformed, errors behave exactly as before because failed parses are deliberately not cached and re-raise on every row.

#### `_PatternDefinitionError`
**What it does:** A small internal error type that carries the report category alongside the message.
**Why it exists:** The original code logged a category ("Range has more than two parts" etc.) at the moment of failure. Since parsing now happens in a shared helper, the helper hands the category back to the row loop, which logs it exactly as before.
**To change it:** Don't remove the re-raise in the row loop; the per-row error counting in reports depends on it.

#### `Ingestor.ingest` (the big method)
**What it does:** Opens the file (gzip or plain), walks every line, dispatches on the record number (rec 1 station/date, rec 2 scientist, ... rec 100 basic measurements, etc.), and calls `ingest_row` with the column names and pattern for that line type. Afterwards it assembles the human-readable report.
**Why it exists:** This is the single source of truth for how each BSRN logical record is laid out.
**To change it:** To support a new logical record, add an `elif i_rec_num == ...` branch with the column names and pattern. Everything else (caching, checking, reporting) comes along for free.

#### `check_line_length_max`
**What it does:** Flags lines longer than 80 characters.
**Why it exists:** The BSRN format requires 80-character lines.
**To change it:** The error strings are now built only when a line is actually too long; keep new diagnostics inside the `if` for the same reason.

## Things to Know
- The cache assumes pattern strings are constants, which they are in this file. If a future change ever builds patterns dynamically per row with unbounded variety, switch `lru_cache(maxsize=None)` to a bounded size.
- The returned constraint containers from the cache are tuples; the row loop converts them to fresh lists per row so that any error message that prints the container looks exactly like before (`['0', '90']`, not `('0', '90')`).
- Verified behaviors that must not drift: per-row error counts, the report tables, and the extracted value structure (`odic_data`). The regression fixture is `input/tat0426.dat`.
