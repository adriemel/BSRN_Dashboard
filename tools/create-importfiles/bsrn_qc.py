#!/usr/bin/env python3
"""
BSRN Quality Check Pipeline
============================
Single-script replacement for the BSRN Toolbox + Jupyter notebook workflow.
Reads station-to-archive .dat files, extracts data, computes solar geometry,
runs all three QC check levels, and generates an HTML report with diagnostic plots.

Usage:
    python bsrn_qc.py file1.dat [file2.dat ...]
    python bsrn_qc.py /path/to/folder/           # processes all .dat files

Output:
    qc_reports/{STATION}_{YYYY-MM}_QC_report.html   (one per file)
    qc_reports/batch_summary.html                    (if multiple files)

Author: Built for Amelie / BSRN network
"""

import sys
import os
import glob
import datetime as dt
import base64
import io
import warnings
from pathlib import Path
from collections import OrderedDict

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from jinja2 import Environment

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning, module="matplotlib")


# =============================================================================
# MODULE 1: .dat FILE PARSER
# =============================================================================

# BSRN station name lookup (3-letter codes)
# BSRN station name lookup (3-letter codes)
STATION_NAMES = {
    "ABS": "Abashiri",
    "ALE": "Alert",
    "ASP": "Alice Springs",
    "BAR": "Barrow",
    "BER": "Bermuda",
    "BIL": "Billings",
    "BON": "Bondville",
    "BOS": "Boulder, SURFRAD",
    "BOU": "Boulder",
    "BRB": "Brasilia",
    "BUD": "Budapest",
    "CAB": "Cabauw",
    "CAM": "Camborne",
    "CAP": "Cape Baranova",
    "CAR": "Carpentras",
    "CLH": "Chesapeake Light",
    "CNR": "Cener",
    "COC": "Cocos Island",
    "DAA": "De Aar",
    "DAR": "Darwin",
    "DOM": "Concordia Station",
    "DRA": "Desert Rock",
    "DWN": "Darwin Met Office",
    "E13": "Southern Great Plains",
    "ENA": "Eastern North Atlantic",
    "EUR": "Eureka",
    "FLO": "Florianopolis",
    "FPE": "Fort Peck",
    "FUA": "Fukuoka",
    "GAN": "Gandhinagar",
    "GCR": "Goodwin Creek",
    "GIM": "Granite Island",
    "GOB": "Gobabeb",
    "GUR": "Gurgaon",
    "GVN": "Neumayer Station",
    "HOW": "Howrah",
    "ILO": "Ilorin",
    "INO": "Magurele",
    "ISH": "Ishigakijima",
    "IZA": "Izana",
    "KWA": "Kwajalein",
    "LAU": "Lauder",
    "LER": "Lerwick",
    "LIN": "Lindenberg",
    "LMP": "Lampedusa",
    "LRC": "Langley Research Center",
    "LYU": "Lanyu Island",
    "MAN": "Momote",
    "MNM": "Minamitorishima",
    "NAU": "Nauru Island",
    "NEW": "Newcastle",
    "NYA": "Ny-Ålesund",
    "OHY": "Observatory of Huancayo",
    "PAL": "Palaiseau",
    "PAR": "Paramaribo",
    "PAY": "Payerne",
    "PSU": "Rock Springs",
    "PTR": "Petrolina",
    "QIQ": "Qiqihar",
    "REG": "Regina",
    "RLM": "Rolim de Moura",
    "RUN": "Reunion Island, University",
    "SAP": "Sapporo",
    "SBO": "Sede Boqer",
    "SEL": "Selegua",
    "SMS": "São Martinho da Serra",
    "SON": "Sonnblick",
    "SOV": "Solar Village",
    "SPO": "South Pole",
    "SXF": "Sioux Falls",
    "SYO": "Syowa",
    "TAM": "Tamanrasset",
    "TAT": "Tateno",
    "TIK": "Tiksi",
    "TIR": "Tiruvallur",
    "TOR": "Toravere",
    "XIA": "Xianghe",
    "YUS": "Yushan",
}


def _load_station_names_from_bsrn_ids():
    ids_file = Path(__file__).resolve().parent / "BSRN_IDs.txt"
    if not ids_file.exists():
        return {}
    names = {}
    in_station_section = False
    header_seen = False
    for raw_line in ids_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            in_station_section = line.lower() == "[station]"
            header_seen = False
            continue
        if not in_station_section:
            continue
        if not header_seen:
            header_seen = True
            continue
        parts = raw_line.split("\t")
        if len(parts) >= 3 and parts[0].strip().isdigit():
            names[parts[1].strip().upper()] = parts[2].strip()
    return names


STATION_NAMES.update(_load_station_names_from_bsrn_ids())


def parse_dat_file(filepath):
    """
    Parse a BSRN station-to-archive .dat file.

    Returns:
        df: pandas DataFrame with datetime index and columns for all measurements
        metadata: dict with station info (name, lat, lon, elevation, month, year, etc.)
    """
    filepath = Path(filepath)
    with open(filepath, "r", errors="ignore") as f:
        lines = f.readlines()

    # --- Extract 3-letter station code from filename ---
    stem = filepath.stem  # e.g. "cab0425"
    station_code = stem[:3].upper()
    station_name = STATION_NAMES.get(station_code, station_code)

    # --- Find all block positions ---
    blocks = {}
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("*C") or stripped.startswith("*U"):
            blocks[stripped] = i

    # --- Parse *C0001 or *U0001: station number, month, year ---
    month, year = None, None
    block_0001 = "*C0001" if "*C0001" in blocks else ("*U0001" if "*U0001" in blocks else None)
    if block_0001:
        idx = blocks[block_0001] + 1
        parts = lines[idx].split()
        if len(parts) >= 3:
            # Format: station_number month year version
            month = int(parts[1])
            year = int(parts[2])

    # --- Parse *U0004 or *C0004: lat, lon, elevation ---
    # BSRN encodes coordinates as: stored_lat = lat + 90, stored_lon = lon + 180
    # (to avoid negative values). We decode back.
    lat, lon, elevation = np.nan, np.nan, 0
    block_0004 = "*U0004" if "*U0004" in blocks else ("*C0004" if "*C0004" in blocks else None)
    if block_0004:
        idx = blocks[block_0004]
        for j in range(idx + 1, min(idx + 20, len(lines))):
            parts = lines[j].split()
            try:
                if len(parts) >= 3:
                    # Skip separator lines: standard is "-1 -1 -1" but some stations
                    # use "1 0 0" or other integer-only variants between PI entries.
                    # The real coordinate line always contains decimal values.
                    if all(p.lstrip('-').isdigit() for p in parts):
                        continue
                    # Skip phone number lines — they start with '+' (e.g. "+81 29 851 4424")
                    # and would be misread as coordinates (e.g. Japan country code +81 → v0=81
                    # falls within the valid 0–180 range).
                    if parts[0].startswith('+'):
                        continue
                    v0, v1, v2 = float(parts[0]), float(parts[1]), float(parts[2])
                    # Encoded lat is 0-180 (=lat+90), encoded lon is 0-360 (=lon+180)
                    if 0 <= v0 <= 180 and 0 <= v1 <= 360:
                        lat = v0 - 90.0
                        lon = v1 - 180.0
                        elevation = v2
                        break
            except (ValueError, IndexError):
                continue

    # --- Parse *U0002 or *C0002: PI info ---
    pi_name = ""
    block_0002 = "*U0002" if "*U0002" in blocks else ("*C0002" if "*C0002" in blocks else None)
    if block_0002:
        idx = blocks[block_0002]
        # PI name is typically 2 lines after the block marker (skipping -1 -1 -1 line)
        for j in range(idx + 1, min(idx + 5, len(lines))):
            line = lines[j].strip()
            # Skip separator lines: standard is "-1 -1 -1" but some stations use
            # "1 0 0" or other small-integer variants to separate PI entries.
            if line and not all(p.lstrip('-').isdigit() for p in line.split()):
                # BSRN format: Name is everything before the phone number
                # Phone numbers typically start with + or are at the end
                # We'll take everything before a phone-like pattern (starts with + or all digits with -)
                parts = line.split()
                # Find where the phone number starts (usually a sequence starting with + or digit)
                name_parts = []
                for part in parts:
                    # Stop when we hit something that looks like a phone number
                    if part.startswith('+') or (part.replace('-', '').replace(' ', '').isdigit() and len(part) > 5):
                        break
                    name_parts.append(part)
                pi_name = ' '.join(name_parts).strip()
                break

    # --- Parse *U0003 or *C0003: @LR4000_CONST directives ---
    lr4000_constants = {}  # {wrmcid: [{sens, k0, k1, k2, k3, f0}, ...]}
    block_0003 = "*U0003" if "*U0003" in blocks else ("*C0003" if "*C0003" in blocks else None)
    if block_0003:
        idx = blocks[block_0003]
        # Scan through LR0003 looking for @LR4000_CONST lines
        for j in range(idx + 1, min(idx + 200, len(lines))):  # Check up to 200 lines
            line = lines[j].strip()
            if line.startswith("*"):
                break  # Next block started
            if line.startswith("@LR4000CONST"):
                # Format: @LR4000CONST, serial, wrmcid, cert, sens, k0, k1, k2, k3, f0
                # Some files use an & line-continuation: the numerical values (sens..f0)
                # appear on the *next* line rather than on the same line.
                # e.g.:  @LR4000CONST,33974F3,39040,CAL_XXX,&    |
                #                    3.5, 0.00000, 0.00000, 1.00310, 7.47000, 0.00000 |
                raw = line.rstrip().rstrip('|').rstrip()
                if raw.endswith('&'):
                    # Strip trailing & (and any separator chars) then grab continuation
                    header = raw[:-1].rstrip(', ')
                    for k in range(j + 1, min(j + 5, len(lines))):
                        cont = lines[k].strip().rstrip('|').strip()
                        if cont and not cont.startswith('*'):
                            raw = header + ',' + cont
                            break
                parts = [p.strip() for p in raw.split(',')]
                if len(parts) >= 10:
                    try:
                        wrmcid = int(parts[2])
                        sens = float(parts[4]) if parts[4] != "ND" else 1.0
                        k0 = float(parts[5]) if parts[5] != "ND" else 0.0
                        k1 = float(parts[6]) if parts[6] != "ND" else 0.0
                        k2 = float(parts[7]) if parts[7] != "ND" else 1.0
                        k3 = float(parts[8]) if parts[8] != "ND" else 0.0
                        f0 = float(parts[9]) if parts[9] != "ND" else 0.0
                        
                        # Store constants - multiple entries per wrmcid for instrument changes
                        if wrmcid not in lr4000_constants:
                            lr4000_constants[wrmcid] = []
                        lr4000_constants[wrmcid].append({
                            'sens': sens, 'k0': k0, 'k1': k1, 'k2': k2, 'k3': k3, 'f0': f0
                        })
                    except (ValueError, IndexError):
                        continue

    # --- Parse *C0009 or *U0009: Instrument assignments ---
    # LR0009 tells us which instrument (wrmcid) was used for each radiation component
    # Format: day, hour, minute, rad_quantity_id, wrmcid, ...
    # rad_quantity 5 = LWD (downwelling longwave), 132 = LWU (upwelling longwave)
    instrument_assignments = {}  # {rad_quantity: [(start_minute_of_month, wrmcid), ...]}
    block_0009 = "*C0009" if "*C0009" in blocks else ("*U0009" if "*U0009" in blocks else None)
    if block_0009:
        idx = blocks[block_0009]
        for j in range(idx + 1, min(idx + 50, len(lines))):
            line = lines[j].strip()
            if line.startswith("*"):
                break
            parts = line.split()
            if len(parts) >= 5:
                try:
                    day_start = int(parts[0])
                    hour_start = int(parts[1])
                    min_start = int(parts[2])
                    rad_quantity = int(parts[3])
                    wrmcid = int(parts[4])
                    
                    # Calculate minute of month (day starts at 1)
                    if day_start == -1:
                        minute_of_month = 0  # Instrument active from start
                    else:
                        minute_of_month = (day_start - 1) * 1440 + hour_start * 60 + min_start
                    
                    if rad_quantity not in instrument_assignments:
                        instrument_assignments[rad_quantity] = []
                    instrument_assignments[rad_quantity].append((minute_of_month, wrmcid))
                except (ValueError, IndexError):
                    continue

    metadata = {
        "station_code": station_code,
        "station_name": station_name,
        "latitude": lat,
        "longitude": lon,
        "elevation": elevation,
        "month": month,
        "year": year,
        "pi_name": pi_name,
        "filename": filepath.name,
        "lr4000_constants": lr4000_constants,
        "instrument_assignments": instrument_assignments,
    }

    # --- Parse *C0100 or *U0100: basic (downward) measurements ---
    # Format: 2 lines per record
    #   Line 1: day  minute  SWD  SWD_sd  SWD_min  SWD_max  DIR  DIR_sd  DIR_min  DIR_max
    #   Line 2: DIF  DIF_sd  DIF_min  DIF_max  LWD  LWD_sd  LWD_min  LWD_max  T2  RH  P
    c0100_data = []
    block_0100 = "*C0100" if "*C0100" in blocks else ("*U0100" if "*U0100" in blocks else None)
    if block_0100:
        start = blocks[block_0100] + 1
        # Find end of block (next * marker or end of file)
        end = len(lines)
        for i in range(start, len(lines)):
            if lines[i].strip().startswith("*"):
                end = i
                break

        i = start
        while i + 1 < end:
            line1 = lines[i].strip()
            line2 = lines[i + 1].strip()
            if not line1 or line1.startswith("*"):
                i += 1
                continue

            p1 = line1.split()
            p2 = line2.split()

            try:
                day = int(p1[0])
                minute = int(p1[1])
                swd = float(p1[2])
                dir_val = float(p1[6])  # DIR is at position 6
                dif = float(p2[0])      # DIF is first on line 2
                lwd = float(p2[4])      # LWD at position 4 on line 2
                t2 = float(p2[8])       # T2 at position 8
                rh = float(p2[9])       # RH at position 9
                pres = float(p2[10])    # Pressure at position 10

                c0100_data.append({
                    "day": day, "minute": minute,
                    "SWD": swd, "DIR": dir_val, "DIF": dif,
                    "LWD": lwd, "T2": t2, "RH": rh, "P": pres,
                })
            except (ValueError, IndexError):
                pass  # skip malformed records

            i += 2

    # --- Parse *C0300 or *U0300: upward measurements ---
    # Format: 1 line per record
    #   day  minute  SWU  SWU_sd  SWU_min  SWU_max  LWU  LWU_sd  LWU_min  LWU_max  [more...]
    c0300_data = {}
    block_0300 = "*C0300" if "*C0300" in blocks else ("*U0300" if "*U0300" in blocks else None)
    has_upward = block_0300 is not None
    if has_upward:
        start = blocks[block_0300] + 1
        end = len(lines)
        for i in range(start, len(lines)):
            if lines[i].strip().startswith("*"):
                end = i
                break

        for i in range(start, end):
            line = lines[i].strip()
            if not line or line.startswith("*"):
                continue
            p = line.split()
            try:
                day = int(p[0])
                minute = int(p[1])
                swu = float(p[2])
                lwu = float(p[6])
                c0300_data[(day, minute)] = {"SWU": swu, "LWU": lwu}
            except (ValueError, IndexError):
                continue

    # --- Parse *C4000 or *U4000: LR4000 pyrgeometer dome/body temperatures ---
    # Format: 1 line per record
    #   day  minute  td1_down td2_down td3_down tb_down Uemf_down td1_up td2_up td3_up tb_up Uemf_up
    c4000_data = {}
    block_4000 = "*C4000" if "*C4000" in blocks else ("*U4000" if "*U4000" in blocks else None)
    has_lr4000 = block_4000 is not None
    if has_lr4000:
        start = blocks[block_4000] + 1
        end = len(lines)
        for i in range(start, len(lines)):
            if lines[i].strip().startswith("*"):
                end = i
                break

        for i in range(start, end):
            line = lines[i].strip()
            if not line or line.startswith("*"):
                continue
            p = line.split()
            try:
                day = int(p[0])
                minute = int(p[1])
                # Downwelling: td1, td2, td3, tb, Uemf (positions 2-6)
                td_down = [float(p[2]), float(p[3]), float(p[4])]
                tb_down = float(p[5])
                uemf_down = float(p[6])
                # Upwelling: td1, td2, td3, tb, Uemf (positions 7-11) - if available
                td_up = None
                tb_up = None
                uemf_up = None
                if len(p) >= 12:
                    td_up = [float(p[7]), float(p[8]), float(p[9])]
                    tb_up = float(p[10])
                    uemf_up = float(p[11])
                
                c4000_data[(day, minute)] = {
                    "td_down": td_down, "tb_down": tb_down, "uemf_down": uemf_down,
                    "td_up": td_up, "tb_up": tb_up, "uemf_up": uemf_up
                }
            except (ValueError, IndexError):
                continue

    # --- Build DataFrame ---
    if not c0100_data:
        raise ValueError(f"No *C0100 or *U0100 data block found in {filepath.name}")

    df = pd.DataFrame(c0100_data)

    # Build datetime from year, month, day, minute
    if year is None or month is None:
        raise ValueError(f"Could not determine year/month from {filepath.name}. "
                        f"No *C0001 or *U0001 block found with valid year/month data.")

    df["datetime"] = df.apply(
        lambda r: dt.datetime(year, month, int(r["day"]))
        + dt.timedelta(minutes=int(r["minute"])),
        axis=1,
    )
    df = df.set_index("datetime").drop(columns=["day", "minute"])

    # Merge upward data if present
    if has_upward and c0300_data:
        # Build a matching datetime series for C0300
        swu_vals = []
        lwu_vals = []
        for idx_dt in df.index:
            day = idx_dt.day
            minute = idx_dt.hour * 60 + idx_dt.minute
            key = (day, minute)
            if key in c0300_data:
                swu_vals.append(c0300_data[key]["SWU"])
                lwu_vals.append(c0300_data[key]["LWU"])
            else:
                swu_vals.append(np.nan)
                lwu_vals.append(np.nan)
        df["SWU"] = swu_vals
        df["LWU"] = lwu_vals

    # Replace BSRN missing-value sentinels with NaN
    # IMPORTANT: small negative values (e.g. -1, -2, -3 W/m²) are legitimate
    # nighttime readings (instrument thermal offsets), NOT missing data.
    # Only the -999 / -999.0 sentinel indicates a truly missing minute.
    # For T2/RH the sentinel is -99.9, for pressure -999.
    for col in ["SWD", "DIR", "DIF", "LWD", "SWU", "LWU"]:
        if col in df.columns:
            df.loc[df[col] <= -999, col] = np.nan
    # Also mark corresponding _sd/_min/_max as NaN when the main value is missing
    for base in ["SWD", "DIR", "DIF", "LWD", "SWU", "LWU"]:
        if base in df.columns:
            missing_mask = df[base].isna()
            for suffix in ["_sd", "_min", "_max"]:
                scol = base + suffix
                if scol in df.columns:
                    df.loc[missing_mask, scol] = np.nan
    if "T2" in df.columns:
        df.loc[df["T2"] <= -99, "T2"] = np.nan
    if "RH" in df.columns:
        df.loc[df["RH"] <= -99, "RH"] = np.nan
    if "P" in df.columns:
        df.loc[df["P"] <= -999, "P"] = np.nan

    metadata["has_upward"] = has_upward
    metadata["has_lr4000"] = has_lr4000
    metadata["n_records"] = len(df)

    # Merge LR4000 data if present
    if has_lr4000 and c4000_data:
        # Add LR4000 columns to dataframe
        td1_down_vals, td2_down_vals, td3_down_vals = [], [], []
        tb_down_vals, uemf_down_vals = [], []
        
        for idx_dt in df.index:
            day = idx_dt.day
            minute = idx_dt.hour * 60 + idx_dt.minute
            key = (day, minute)
            if key in c4000_data:
                td_down = c4000_data[key]["td_down"]
                td1_down_vals.append(td_down[0])
                td2_down_vals.append(td_down[1])
                td3_down_vals.append(td_down[2])
                tb_down_vals.append(c4000_data[key]["tb_down"])
                uemf_down_vals.append(c4000_data[key]["uemf_down"])
            else:
                td1_down_vals.append(np.nan)
                td2_down_vals.append(np.nan)
                td3_down_vals.append(np.nan)
                tb_down_vals.append(np.nan)
                uemf_down_vals.append(np.nan)
        
        df["td1_down"] = td1_down_vals
        df["td2_down"] = td2_down_vals
        df["td3_down"] = td3_down_vals
        df["tb_down"] = tb_down_vals
        df["uemf_down"] = uemf_down_vals
        
        # Clean LR4000 missing values (-999, -99.9)
        for col in ["td1_down", "td2_down", "td3_down"]:
            df.loc[df[col] <= -99, col] = np.nan
        for col in ["tb_down", "uemf_down"]:
            df.loc[df[col] <= -999, col] = np.nan

    return df, metadata


# =============================================================================
# MODULE 2: SOLAR AUXILIARY DATA (Iqbal 1983 / Spencer 1971)
# =============================================================================

def _day_angle(day_of_year):
    """Day angle in radians (Spencer 1971)."""
    return 2 * np.pi * (day_of_year - 1) / 365.0


def _earth_sun_distance(day_of_year):
    """
    Earth-sun distance correction factor (1/r^2).
    Returns the factor to multiply the solar constant by.
    Spencer (1971).
    """
    gamma = _day_angle(day_of_year)
    return (1.000110 + 0.034221 * np.cos(gamma) + 0.001280 * np.sin(gamma)
            + 0.000719 * np.cos(2 * gamma) + 0.000077 * np.sin(2 * gamma))


def _solar_declination(day_of_year):
    """Solar declination in radians (Spencer 1971)."""
    gamma = _day_angle(day_of_year)
    return (0.006918 - 0.399912 * np.cos(gamma) + 0.070257 * np.sin(gamma)
            - 0.006758 * np.cos(2 * gamma) + 0.000907 * np.sin(2 * gamma)
            - 0.002697 * np.cos(3 * gamma) + 0.00148 * np.sin(3 * gamma))


def _equation_of_time(day_of_year):
    """Equation of time in minutes (Spencer 1971)."""
    gamma = _day_angle(day_of_year)
    return 229.18 * (0.000075 + 0.001868 * np.cos(gamma)
                     - 0.032077 * np.sin(gamma)
                     - 0.014615 * np.cos(2 * gamma)
                     - 0.04089 * np.sin(2 * gamma))


def compute_solar_auxiliary(df, lat, lon):
    """
    Compute solar position and auxiliary data for QC checks.

    Uses the Iqbal 1983 / Spencer 1971 algorithms, matching the
    BSRN Toolbox "Iqbal 1983" option.

    Adds columns: SZA, Mu0, Sa, SumSW, extra_radiation
    """
    SOLAR_CONSTANT = 1367.0  # W/m^2 (BSRN standard)

    doy = df.index.dayofyear.values.astype(float)

    # Earth-sun distance factor and extraterrestrial radiation
    eccentricity = _earth_sun_distance(doy)
    Sa = SOLAR_CONSTANT * eccentricity  # adjusted solar constant

    # Solar declination
    decl = _solar_declination(doy)

    # Hour angle (degrees -> radians)
    # Solar time = standard time + equation_of_time + 4*(longitude)
    eot = _equation_of_time(doy)
    hours = df.index.hour + df.index.minute / 60.0
    solar_time = hours * 60 + eot + 4 * lon  # in minutes
    hour_angle = np.deg2rad((solar_time / 4.0 - 180))  # degrees to radians

    # Solar zenith angle
    lat_rad = np.deg2rad(lat)
    cos_sza = (np.sin(lat_rad) * np.sin(decl)
               + np.cos(lat_rad) * np.cos(decl) * np.cos(hour_angle))
    cos_sza = np.clip(cos_sza, -1, 1)
    sza = np.rad2deg(np.arccos(cos_sza))

    # Mu0 = cos(SZA), clipped to >= 0
    mu0 = np.clip(cos_sza, 0, None)

    df["SZA"] = sza
    df["Mu0"] = mu0
    df["Sa"] = Sa
    df["extra_radiation"] = Sa

    # SumSW = DIF + DIR * Mu0
    if "DIF" in df.columns and "DIR" in df.columns:
        df["SumSW"] = df["DIF"] + df["DIR"] * df["Mu0"]
    else:
        df["SumSW"] = np.nan

    return df


# =============================================================================
# MODULE 2B: LR4000 PYRGEOMETER CHECK
# =============================================================================

def check_lr4000(df, metadata):
    """
    Check LR4000 data: recalculate LWD from dome/body temperatures and thermopile signal.

    Compares submitted C0100 LWD values against two recalculated values:

    1. Full equation  (L_flleq in the reference LR4000checker.sh):
           LWD_full = k0 + Uemf·(1 + k1·σ·Tb³) + k2·σ·Tb⁴ - k3·σ·(Td⁴ - Tb⁴)
       Calibration constants k0..k3 come from *U0003 @LR4000CONST, matched by the
       active instrument wrmcid from *U0009.
       If no constants are found for the active wrmcid, all constants default to 0
       — this matches the AWK reference script, where accessing an undefined array
       element returns 0 (not 1).  With k2=0 the equation reduces to:
           LWD_full = Uemf
       which gives near-zero differences when the wrmcid in *U0003 and *U0009 differ
       but the station's C4000 Uemf already encodes the same value as submitted LWD.
       Expected max diff for a well-calibrated instrument: 0–2 W/m².

    2. Simple equation  (L_smpeq in reference, labelled "simple:U+sT4"):
           LWD_simple = Uemf + σ·Tb⁴
       Always shows a large offset (~σ·Tb⁴ ≈ 430–550 W/m²).  Informational only.

    Returns a dict with statistics if LR4000 data is present, None otherwise.
    """
    if not metadata.get("has_lr4000", False):
        return None

    # Check if we have the required columns
    required = ["LWD", "tb_down", "uemf_down"]
    if not all(col in df.columns for col in required):
        return None

    # Stefan-Boltzmann constant
    SIGMA = 5.67e-8  # W/(m²·K⁴)

    # Get instrument assignments and constants from metadata
    lr4000_constants = metadata.get("lr4000_constants", {})
    instrument_assignments = metadata.get("instrument_assignments", {})

    # Determine which instrument (wrmcid) to use for LWD (rad_quantity = 5)
    lwd_assignments = instrument_assignments.get(5, [])

    # Default constants when the wrmcid lookup fails.
    # ALL constants default to 0 — this matches AWK's implicit behaviour for
    # undefined array elements (AWK returns 0, not 1, for unset numerics).
    # In particular k2=0, so the full equation reduces to just Uemf when no
    # matching @LR4000CONST entry is found for the active wrmcid.
    default_constants = {'sens': 1.0, 'k0': 0.0, 'k1': 0.0, 'k2': 0.0, 'k3': 0.0, 'f0': 0.0}

    # Function to get constants for a given minute of the month
    def get_constants_for_minute(minute_of_month):
        # Find which instrument was active at this time
        active_wrmcid = None
        for start_min, wrmcid in sorted(lwd_assignments):
            if minute_of_month >= start_min:
                active_wrmcid = wrmcid
            else:
                break

        if active_wrmcid and active_wrmcid in lr4000_constants:
            # Use the last (most recent) set of constants for this instrument
            return lr4000_constants[active_wrmcid][-1]
        return default_constants

    # Filter to valid records for simple calculation
    valid_simple = df[["LWD", "tb_down", "uemf_down"]].copy()
    valid_simple = valid_simple[(valid_simple["LWD"] > -900) &
                                (valid_simple["tb_down"] > -90) &
                                (valid_simple["uemf_down"] > -900)]

    if len(valid_simple) == 0:
        return None

    # Calculate minute of month for each record
    valid_simple["minute_of_month"] = valid_simple.index.map(
        lambda dt: (dt.day - 1) * 1440 + dt.hour * 60 + dt.minute
    )

    # Get constants for each record
    valid_simple["constants"] = valid_simple["minute_of_month"].apply(get_constants_for_minute)

    # Extract individual constants
    valid_simple["k0"] = valid_simple["constants"].apply(lambda c: c['k0'])
    valid_simple["k1"] = valid_simple["constants"].apply(lambda c: c['k1'])
    valid_simple["k2"] = valid_simple["constants"].apply(lambda c: c['k2'])
    valid_simple["k3"] = valid_simple["constants"].apply(lambda c: c['k3'])

    # Convert Celsius to Kelvin
    Tb_K_simple = valid_simple["tb_down"] + 273.15

    # Uemf in LR4000: net thermopile signal in W/m² (= U_raw / sensitivity C)
    U_Wm2 = valid_simple["uemf_down"]

    # Calculate LWD using simple equation: Uemf + σ·Tb⁴
    LWD_simple = U_Wm2 + SIGMA * (Tb_K_simple ** 4)
    diff_simple = LWD_simple - valid_simple["LWD"]

    # Statistics for simple equation
    max_diff_simple_idx = diff_simple.abs().idxmax()
    max_diff_simple = diff_simple.loc[max_diff_simple_idx]

    result = {
        "n_total": len(df),
        "n_lr4000": len(valid_simple),
        "n_missing": len(df) - len(valid_simple),
        "pct_missing": 100 * (len(df) - len(valid_simple)) / len(df) if len(df) > 0 else 0,
        "has_full": False,  # Will update below
        # Simple equation results
        "simple_max_diff": max_diff_simple,
        "simple_max_diff_time": (max_diff_simple_idx.strftime("%Y-%m-%d %H:%M")
                                 if pd.notna(max_diff_simple_idx) else "N/A"),
        "simple_mean_diff": diff_simple.mean(),
        "simple_std_diff": diff_simple.std(),
        "simple_median_diff": diff_simple.median(),
    }

    # Full equation calculation
    # Check if any record has k3≠0 (needs dome temps for the k3 term)
    needs_dome_temps = (valid_simple["k3"].abs() > 1e-10).any()

    if needs_dome_temps:
        # Check if we have dome temperatures
        dome_cols = ["td1_down", "td2_down", "td3_down"]
        if all(col in df.columns for col in dome_cols):
            # Merge dome temp columns into valid_simple
            for col in dome_cols:
                valid_simple[col] = df.loc[valid_simple.index, col]

            # Calculate mean dome temperature using only available (non-NaN) sensors.
            # Stations often populate only td1; td2/td3 are -99.99 → NaN.
            # Using nanmean across the three columns matches the reference LR4000checker
            # behaviour of averaging whatever dome probes are actually present.
            Td_mean_series = valid_simple[dome_cols].mean(axis=1, skipna=True)

            # Require at least one valid dome temperature
            valid_full = valid_simple[Td_mean_series.notna()].copy()
            valid_full["Td_mean"] = Td_mean_series[valid_full.index]

            if len(valid_full) > 0:
                Td_mean_K = valid_full["Td_mean"] + 273.15
                Tb_full_K = valid_full["tb_down"] + 273.15
                U_Wm2_full = valid_full["uemf_down"]

                # Full equation with dome temps (per-record constants)
                LWD_full = (valid_full["k0"] +
                            U_Wm2_full * (1 + valid_full["k1"] * SIGMA * (Tb_full_K ** 3)) +
                            valid_full["k2"] * SIGMA * (Tb_full_K ** 4) -
                            valid_full["k3"] * SIGMA * ((Td_mean_K ** 4) - (Tb_full_K ** 4)))

                diff_full = LWD_full - valid_full["LWD"]

                # Statistics for full equation
                max_diff_full_idx = diff_full.abs().idxmax()
                max_diff_full = diff_full.loc[max_diff_full_idx]

                result["has_full"] = True
                result["n_full"] = len(valid_full)
                result["full_max_diff"] = max_diff_full
                result["full_max_diff_time"] = (max_diff_full_idx.strftime("%Y-%m-%d %H:%M")
                                                if pd.notna(max_diff_full_idx) else "N/A")
                result["full_mean_diff"] = diff_full.mean()
                result["full_std_diff"] = diff_full.std()
                result["full_median_diff"] = diff_full.median()
                result["full_needs_dome"] = True
    else:
        # k3=0 for all records — full equation without dome term:
        #   LWD_full = k0 + Uemf·(1 + k1·σ·Tb³) + k2·σ·Tb⁴
        # When constants default to 0 (lookup miss): k2=0 → LWD_full = Uemf
        Tb_K = valid_simple["tb_down"] + 273.15
        U_Wm2 = valid_simple["uemf_down"]

        LWD_full = (valid_simple["k0"] +
                    U_Wm2 * (1 + valid_simple["k1"] * SIGMA * (Tb_K ** 3)) +
                    valid_simple["k2"] * SIGMA * (Tb_K ** 4))

        diff_full = LWD_full - valid_simple["LWD"]

        # Statistics for full equation
        max_diff_full_idx = diff_full.abs().idxmax()
        max_diff_full = diff_full.loc[max_diff_full_idx]

        result["has_full"] = True
        result["n_full"] = len(valid_simple)
        result["full_max_diff"] = max_diff_full
        result["full_max_diff_time"] = (max_diff_full_idx.strftime("%Y-%m-%d %H:%M")
                                        if pd.notna(max_diff_full_idx) else "N/A")
        result["full_mean_diff"] = diff_full.mean()
        result["full_std_diff"] = diff_full.std()
        result["full_median_diff"] = diff_full.median()
        result["full_needs_dome"] = False

    return result


# =============================================================================
# MODULE 3: QUALITY CHECKS
# =============================================================================

# Bit flag definitions (matching QualityCheckRecommendedV20.cpp exactly)
LT_PHYSICAL_POSSIBLE = 0
GT_PHYSICAL_POSSIBLE = 1
LT_EXTREMELY_RARE = 2
GT_EXTREMELY_RARE = 3
LT_COMPARISON = 4
GT_COMPARISON = 5

SIGMA = 5.67e-8  # Stefan-Boltzmann constant


def run_qc_checks(df):
    """
    Run all three QC check levels on the data.

    Adds QC code columns: SWDQc, DIFQc, DIRQc, LWDQc, SWUQc, LWUQc, T2Qc
    Each is a bit-flag integer (0 = all OK).
    """
    n = len(df)
    params_to_check = ["SWD", "DIF", "DIR", "LWD", "SWU", "LWU", "T2"]
    for p in params_to_check:
        df[f"{p}Qc"] = 0

    Sa = df["Sa"].values
    Mu0 = df["Mu0"].values
    SZA = df["SZA"].values

    # Helper: set bit flag
    def set_flag(col, mask, bit):
        qc_col = f"{col}Qc"
        if qc_col in df.columns:
            df.loc[mask, qc_col] = df.loc[mask, qc_col].values.astype(int) | (1 << bit)

    # ─── Physically Possible Limits ─────────────────────────────────
    if "SWD" in df.columns:
        set_flag("SWD", df["SWD"] < -4, LT_PHYSICAL_POSSIBLE)
        set_flag("SWD", df["SWD"] > Sa * 1.5 * np.power(Mu0, 1.2) + 100, GT_PHYSICAL_POSSIBLE)

    if "DIF" in df.columns:
        set_flag("DIF", df["DIF"] < -4, LT_PHYSICAL_POSSIBLE)
        set_flag("DIF", df["DIF"] > Sa * 0.95 * np.power(Mu0, 1.2) + 50, GT_PHYSICAL_POSSIBLE)

    if "DIR" in df.columns:
        set_flag("DIR", df["DIR"] < -4, LT_PHYSICAL_POSSIBLE)
        set_flag("DIR", df["DIR"] > Sa, GT_PHYSICAL_POSSIBLE)

    if "SWU" in df.columns:
        set_flag("SWU", df["SWU"] < -4, LT_PHYSICAL_POSSIBLE)
        set_flag("SWU", df["SWU"] > Sa * 1.2 * np.power(Mu0, 1.2) + 50, GT_PHYSICAL_POSSIBLE)

    if "LWD" in df.columns:
        set_flag("LWD", df["LWD"] < 40, LT_PHYSICAL_POSSIBLE)
        set_flag("LWD", df["LWD"] > 700, GT_PHYSICAL_POSSIBLE)

    if "LWU" in df.columns:
        set_flag("LWU", df["LWU"] < 40, LT_PHYSICAL_POSSIBLE)
        set_flag("LWU", df["LWU"] > 900, GT_PHYSICAL_POSSIBLE)

    # ─── Extremely Rare Limits ──────────────────────────────────────
    # All Mu0-scaled upper limits and the shortwave lower limits are only
    # physically meaningful when the sun is above the horizon (Mu0 > 0).
    # At nighttime, small negative or low-positive SW values are instrument
    # noise and should not be flagged as "extremely rare".
    sun_up = Mu0 > 0

    if "SWD" in df.columns:
        set_flag("SWD", sun_up & (df["SWD"] < -2), LT_EXTREMELY_RARE)
        set_flag("SWD", sun_up & (df["SWD"] > Sa * 1.2 * np.power(Mu0, 1.2) + 50), GT_EXTREMELY_RARE)

    if "DIF" in df.columns:
        set_flag("DIF", sun_up & (df["DIF"] < -2), LT_EXTREMELY_RARE)
        set_flag("DIF", sun_up & (df["DIF"] > Sa * 0.75 * np.power(Mu0, 1.2) + 30), GT_EXTREMELY_RARE)

    if "DIR" in df.columns:
        set_flag("DIR", sun_up & (df["DIR"] < -2), LT_EXTREMELY_RARE)
        set_flag("DIR", sun_up & (df["DIR"] > Sa * 0.95 * np.power(Mu0, 0.2) + 10), GT_EXTREMELY_RARE)

    if "SWU" in df.columns:
        set_flag("SWU", sun_up & (df["SWU"] < -2), LT_EXTREMELY_RARE)
        set_flag("SWU", sun_up & (df["SWU"] > Sa * 1.2 * np.power(Mu0, 1.2) + 50), GT_EXTREMELY_RARE)

    if "LWD" in df.columns:
        set_flag("LWD", df["LWD"] < 60, LT_EXTREMELY_RARE)
        set_flag("LWD", df["LWD"] > 500, GT_EXTREMELY_RARE)

    if "LWU" in df.columns:
        set_flag("LWU", df["LWU"] < 60, LT_EXTREMELY_RARE)
        set_flag("LWU", df["LWU"] > 700, GT_EXTREMELY_RARE)

    # ─── Comparison Checks ──────────────────────────────────────────

    # A. SWD / SumSW ratio
    if "SWD" in df.columns and "SumSW" in df.columns:
        ratio = df["SWD"] / df["SumSW"]

        # SZA < 75 and SumSW > 50
        mask_a = (SZA < 75) & (df["SumSW"] > 50)
        set_flag("SWD", mask_a & (ratio < 0.92), LT_COMPARISON)
        if "DIR" in df.columns:
            set_flag("DIR", mask_a & (ratio < 0.92), GT_COMPARISON)
        if "DIF" in df.columns:
            set_flag("DIF", mask_a & (ratio < 0.92), GT_COMPARISON)

        set_flag("SWD", mask_a & (ratio > 1.08), GT_COMPARISON)
        if "DIR" in df.columns:
            set_flag("DIR", mask_a & (ratio > 1.08), LT_COMPARISON)
        if "DIF" in df.columns:
            set_flag("DIF", mask_a & (ratio > 1.08), LT_COMPARISON)

        # 75 < SZA < 93 and SumSW > 50
        mask_b = (SZA >= 75) & (SZA < 93) & (df["SumSW"] > 50)
        set_flag("SWD", mask_b & (ratio < 0.75), LT_COMPARISON)
        if "DIR" in df.columns:
            set_flag("DIR", mask_b & (ratio < 0.75), GT_COMPARISON)
        if "DIF" in df.columns:
            set_flag("DIF", mask_b & (ratio < 0.75), GT_COMPARISON)

        set_flag("SWD", mask_b & (ratio > 1.15), GT_COMPARISON)
        if "DIR" in df.columns:
            set_flag("DIR", mask_b & (ratio > 1.15), LT_COMPARISON)
        if "DIF" in df.columns:
            set_flag("DIF", mask_b & (ratio > 1.15), LT_COMPARISON)

    # B. DIF / SWD ratio
    if "DIF" in df.columns and "SWD" in df.columns:
        dif_ratio = df["DIF"] / df["SWD"]
        mask_c = (SZA < 75) & (df["SWD"] > 50)
        set_flag("DIF", mask_c & (dif_ratio >= 1.05), GT_COMPARISON)
        set_flag("SWD", mask_c & (dif_ratio >= 1.05), LT_COMPARISON)

        mask_d = (SZA >= 75) & (SZA < 93) & (df["SWD"] > 50)
        set_flag("DIF", mask_d & (dif_ratio >= 1.10), GT_COMPARISON)
        set_flag("SWD", mask_d & (dif_ratio >= 1.10), LT_COMPARISON)

    # C. SWU comparison
    if "SWU" in df.columns:
        if "DIF" in df.columns and "DIR" in df.columns and "SumSW" in df.columns:
            clean_sw = (df["DIFQc"] == 0) & (df["DIRQc"] == 0) & (df["SumSW"] > 50) & df["SumSW"].notna()
            set_flag("SWU", clean_sw & (df["SWU"] > df["SumSW"]), GT_COMPARISON)
            set_flag("DIF", clean_sw & (df["SWU"] > df["SumSW"]), LT_COMPARISON)
            set_flag("DIR", clean_sw & (df["SWU"] > df["SumSW"]), LT_COMPARISON)

        if "SWD" in df.columns:
            no_dif_dir = True
            if "DIF" in df.columns and "DIR" in df.columns:
                no_dif_dir = False
            if no_dif_dir:
                clean_swd = (df["SWDQc"] == 0) & (df["SWD"] > 50)
                set_flag("SWU", clean_swd & (df["SWU"] > df["SWD"]), GT_COMPARISON)
                set_flag("SWD", clean_swd & (df["SWU"] > df["SWD"]), LT_COMPARISON)

    # D. LWD vs T2 (Stefan-Boltzmann)
    if "LWD" in df.columns and "T2" in df.columns:
        T_K = df["T2"] + 273.15  # Kelvin
        clean_t2 = df["T2Qc"] == 0
        set_flag("LWD", clean_t2 & (df["LWD"] <= 0.4 * SIGMA * T_K**4), LT_COMPARISON)
        set_flag("T2", clean_t2 & (df["LWD"] <= 0.4 * SIGMA * T_K**4), GT_COMPARISON)
        set_flag("LWD", clean_t2 & (df["LWD"] >= SIGMA * T_K**4 + 25), GT_COMPARISON)
        set_flag("T2", clean_t2 & (df["LWD"] >= SIGMA * T_K**4 + 25), LT_COMPARISON)

    # E. LWU vs T2
    if "LWU" in df.columns and "T2" in df.columns:
        T_K = df["T2"] + 273.15
        clean_t2 = df["T2Qc"] == 0
        set_flag("LWU", clean_t2 & (df["LWU"] <= SIGMA * (T_K - 15)**4), LT_COMPARISON)
        set_flag("T2", clean_t2 & (df["LWU"] <= SIGMA * (T_K - 15)**4), GT_COMPARISON)
        set_flag("LWU", clean_t2 & (df["LWU"] >= SIGMA * (T_K + 25)**4), GT_COMPARISON)
        set_flag("T2", clean_t2 & (df["LWU"] >= SIGMA * (T_K + 25)**4), LT_COMPARISON)

    # F. LWD vs LWU
    if "LWD" in df.columns and "LWU" in df.columns:
        set_flag("LWD", df["LWD"] >= df["LWU"] + 25, GT_COMPARISON)
        set_flag("LWU", df["LWD"] >= df["LWU"] + 25, LT_COMPARISON)
        set_flag("LWD", df["LWD"] <= df["LWU"] - 300, LT_COMPARISON)
        set_flag("LWU", df["LWD"] <= df["LWU"] - 300, GT_COMPARISON)

    return df


def summarize_qc_flags(df):
    """Create a summary table of QC flag counts per parameter per check level."""
    qc_cols = [c for c in df.columns if c.endswith("Qc")]
    rows = []
    for qc_col in qc_cols:
        param = qc_col.replace("Qc", "")
        vals = df[qc_col].values.astype(int)
        row = {
            "Parameter": param,
            "Total records": len(vals),
            "All OK (0)": int(np.sum(vals == 0)),
            "< Phys. Possible": int(np.sum((vals >> LT_PHYSICAL_POSSIBLE) & 1)),
            "> Phys. Possible": int(np.sum((vals >> GT_PHYSICAL_POSSIBLE) & 1)),
            "< Extremely Rare": int(np.sum((vals >> LT_EXTREMELY_RARE) & 1)),
            "> Extremely Rare": int(np.sum((vals >> GT_EXTREMELY_RARE) & 1)),
            "< Comparison": int(np.sum((vals >> LT_COMPARISON) & 1)),
            "> Comparison": int(np.sum((vals >> GT_COMPARISON) & 1)),
            "Any flag": int(np.sum(vals != 0)),
        }
        rows.append(row)
    return pd.DataFrame(rows)


# =============================================================================
# MODULE 4: DIAGNOSTIC PLOTS
# =============================================================================

def _fig_to_base64(fig):
    """Convert matplotlib figure to base64 PNG string."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("utf-8")


def plot_radiation_timeseries(df, metadata):
    """Radiation time series + histograms (7 rows x 2 cols)."""
    rad_cols = ["DIF", "DIR", "SWD", "LWD"]
    if metadata.get("has_upward"):
        rad_cols += ["SWU", "LWU"]
    available = [c for c in rad_cols if c in df.columns and df[c].notna().any()]

    nrows = len(available)
    if nrows == 0:
        return None

    fig, axes = plt.subplots(nrows=nrows, ncols=2, figsize=(14, 2.8 * nrows),
                             gridspec_kw={"width_ratios": [4, 1]})
    if nrows == 1:
        axes = axes.reshape(1, 2)

    colors = plt.cm.tab10(np.linspace(0, 1, 10))
    for i, col in enumerate(available):
        data = df[col].dropna()
        # Time series
        axes[i, 0].plot(data.index, data.values, c=colors[i], linewidth=0.3, alpha=0.8)
        axes[i, 0].set_ylabel("W/m²")
        axes[i, 0].set_title(col, fontsize=10, fontweight="bold")
        axes[i, 0].xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
        # Histogram
        axes[i, 1].hist(data.values, bins=60, color=colors[i], edgecolor="none", alpha=0.8,
                        orientation="horizontal")
        axes[i, 1].set_xlabel("Count")
        axes[i, 1].set_yscale("linear")

    fig.suptitle(f"{metadata['station_name']} ({metadata['station_code']}) — "
                 f"{metadata['year']}-{metadata['month']:02d} — Radiation",
                 fontsize=13, fontweight="bold")
    fig.tight_layout()
    return _fig_to_base64(fig)


def plot_qc_flags_timeseries(df, metadata):
    """QC flag values over time for radiation parameters (comparison flags only)."""
    qc_cols = [c for c in ["SWDQc", "DIFQc", "DIRQc", "LWDQc", "SWUQc", "LWUQc"]
               if c in df.columns]
    if not qc_cols:
        return None

    nrows = len(qc_cols)
    fig, axes = plt.subplots(nrows=nrows, figsize=(14, 1.8 * nrows), sharex=True)
    if nrows == 1:
        axes = [axes]

    # Bit masks for comparison flags
    LT_COMPARISON_MASK = 1 << 4  # bit 4 - less than comparison
    GT_COMPARISON_MASK = 1 << 5  # bit 5 - greater than comparison

    for i, col in enumerate(qc_cols):
        vals = df[col].values.astype(int)
        
        # Separate flags: bit 4 (< comparison) and bit 5 (> comparison)
        lt_flags = (vals & LT_COMPARISON_MASK) != 0
        gt_flags = (vals & GT_COMPARISON_MASK) != 0
        
        # Plot < comparison flags (bit 4) in blue
        axes[i].fill_between(df.index, 0, lt_flags.astype(float),
                             color="steelblue", alpha=0.9, linewidth=0.7, label="< Comparison")
        
        # Plot > comparison flags (bit 5) in red
        axes[i].fill_between(df.index, 0, gt_flags.astype(float),
                             color="tomato", alpha=0.9, linewidth=0.7, label="> Comparison")
        
        axes[i].set_ylabel(col.replace("Qc", ""), fontsize=9)
        axes[i].set_ylim(-0.1, 1.5)
        axes[i].set_yticks([0, 1])
        axes[i].set_yticklabels(["OK", "Flag"])
        
        # Count both types
        n_lt = int(lt_flags.sum())
        n_gt = int(gt_flags.sum())
        n_total = n_lt + n_gt
        pct = 100 * n_total / len(vals) if len(vals) > 0 else 0
        
        # Show counts for both flag types
        axes[i].text(0.99, 0.85, f"{n_lt} < comp, {n_gt} > comp ({pct:.1f}%)",
                     transform=axes[i].transAxes, ha="right", fontsize=8, 
                     bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.3))
        
        # Add legend only to first subplot
        if i == 0:
            axes[i].legend(loc='upper left', fontsize=8, framealpha=0.9)

    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    fig.suptitle("QC Comparison Flags Over Time", fontsize=12, fontweight="bold")
    fig.tight_layout()
    return _fig_to_base64(fig)


def plot_meteo(df, metadata):
    """Meteorological time series (T2, P, RH)."""
    meteo_map = {"T2": ("Air Temperature", "°C"),
                 "P": ("Station Pressure", "hPa"),
                 "RH": ("Relative Humidity", "%")}
    available = [(k, v) for k, v in meteo_map.items()
                 if k in df.columns and df[k].notna().any()]
    if not available:
        return None

    nrows = len(available)
    fig, axes = plt.subplots(nrows=nrows, figsize=(14, 2.5 * nrows), sharex=True)
    if nrows == 1:
        axes = [axes]

    for i, (col, (label, unit)) in enumerate(available):
        data = df[col].dropna()
        axes[i].plot(data.index, data.values, linewidth=0.4, color=f"C{i}")
        axes[i].set_ylabel(f"{unit}")
        axes[i].set_title(f"{label} ({col})", fontsize=10, fontweight="bold")

    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    fig.suptitle(f"Meteorology — {metadata['station_name']}", fontsize=12, fontweight="bold")
    fig.tight_layout()
    return _fig_to_base64(fig)


def plot_missing_data(df, metadata):
    """Missing data diagnostic (hourly resampled NaN counts)."""
    check_cols = [c for c in ["SWD", "DIF", "DIR", "LWD"] if c in df.columns]
    if not check_cols:
        return None

    hourly_missing = df[check_cols].isna().astype(float).resample("1h").sum()
    nrows = len(check_cols)
    fig, axes = plt.subplots(nrows=nrows, figsize=(14, 2 * nrows), sharex=True)
    if nrows == 1:
        axes = [axes]

    for i, col in enumerate(check_cols):
        axes[i].bar(hourly_missing.index, hourly_missing[col].values,
                    width=1 / 24, color=f"C{i}", alpha=0.8)
        axes[i].set_ylabel("Missing\nmin/hour")
        axes[i].set_title(col, fontsize=10)
        axes[i].set_ylim(0, 65)
        axes[i].axhline(y=60, color="red", linestyle="--", alpha=0.3)

    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    fig.suptitle("Missing Data per Hour", fontsize=12, fontweight="bold")
    fig.tight_layout()
    return _fig_to_base64(fig)


def plot_long_dutton(df, metadata):
    """Long & Dutton limit plots: measurements vs SZA with QC envelopes."""
    if "SZA" not in df.columns:
        return None

    params = [("SWD", 1.5, 1.2, 100, 1.2, 1.2, 50),
              ("DIF", 0.95, 1.2, 50, 0.75, 1.2, 30),
              ("DIR", 1.0, 0, 0, 0.95, 0.2, 10)]  # DIR upper: Sa*factor*Mu0^exp + offset

    available = [(p, *rest) for p, *rest in params if p in df.columns and df[p].notna().any()]
    if not available:
        return None

    mask_day = df["SZA"] < 90
    nrows = len(available)
    fig, axes = plt.subplots(nrows=nrows, figsize=(9, 3.5 * nrows), sharex=True)
    if nrows == 1:
        axes = [axes]

    for i, (param, ppl_f, ppl_e, ppl_o, erl_f, erl_e, erl_o) in enumerate(available):
        sub = df[mask_day].copy()
        if len(sub) == 0:
            continue

        # Compute limits
        if param == "DIR":
            ppl_upper = sub["Sa"]
            erl_upper = sub["Sa"] * erl_f * np.power(sub["Mu0"], erl_e) + erl_o
        else:
            ppl_upper = sub["Sa"] * ppl_f * np.power(sub["Mu0"], ppl_e) + ppl_o
            erl_upper = sub["Sa"] * erl_f * np.power(sub["Mu0"], erl_e) + erl_o

        axes[i].scatter(sub["SZA"], sub[param], s=0.5, alpha=0.15, c="k", label="Measurement")
        axes[i].scatter(sub["SZA"], ppl_upper, s=0.5, alpha=0.5, c="green", label="Phys. possible limit")
        axes[i].scatter(sub["SZA"], erl_upper, s=0.5, alpha=0.5, c="red", label="Extremely rare limit")
        axes[i].set_ylabel(f"{param} [W/m²]")
        axes[i].set_title(param, fontsize=11, fontweight="bold")
        axes[i].legend(loc="upper right", markerscale=8, fontsize=8)

    axes[-1].set_xlabel("Solar Zenith Angle [°]")
    axes[-1].set_xlim(0, 93)
    fig.suptitle("Long & Dutton QC Limits", fontsize=12, fontweight="bold")
    fig.tight_layout()
    return _fig_to_base64(fig)


def plot_dif_swd_ratio(df, metadata):
    """DIF/SWD ratio vs SZA."""
    if not {"DIF", "SWD", "SZA"}.issubset(df.columns):
        return None

    mask = (df["SWD"] > 50) & (df["SZA"] < 93)
    sub = df[mask].copy()
    if len(sub) == 0:
        return None

    sub["K_t"] = sub["DIF"] / sub["SWD"]

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.scatter(sub["SZA"], sub["K_t"], s=0.3, alpha=0.5, c="k")
    ax.plot([0, 75, 75, 93], [1.05, 1.05, 1.10, 1.10], c="red", linewidth=1.5,
            label="QC threshold")
    ax.set_xlabel("Solar Zenith Angle [°]")
    ax.set_ylabel("DIF / SWD")
    ax.set_ylim(0, 1.4)
    ax.set_xlim(20, 93)
    ax.set_title("Diffuse Ratio vs SZA", fontweight="bold")
    ax.legend()
    fig.tight_layout()
    return _fig_to_base64(fig)


def plot_swd_sumsw_ratio(df, metadata):
    """SWD/SumSW ratio vs SZA."""
    if not {"SWD", "SumSW", "SZA"}.issubset(df.columns):
        return None

    mask = (df["SZA"] < 93) & (df["SumSW"] > 50)
    sub = df[mask].copy()
    if len(sub) == 0:
        return None

    sub["ratio"] = sub["SWD"] / sub["SumSW"]

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.scatter(sub["SZA"], sub["ratio"], s=0.5, alpha=0.5, c="k")
    ax.plot([0, 75, 75, 93], [1.08, 1.08, 1.15, 1.15], c="red", linewidth=1.5)
    ax.plot([0, 75, 75, 93], [0.92, 0.92, 0.85, 0.85], c="red", linewidth=1.5)
    ax.set_xlabel("Solar Zenith Angle [°]")
    ax.set_ylabel("SWD / SumSW")
    ax.set_ylim(0.5, 1.5)
    ax.set_xlim(20, 93)
    ax.set_title("SWD / SumSW Ratio vs SZA", fontweight="bold")
    fig.tight_layout()
    return _fig_to_base64(fig)


def plot_swd_vs_sumsw_scatter(df, metadata):
    """SWD vs SumSW scatter plot with 1:1 line."""
    if not {"SWD", "SumSW"}.issubset(df.columns):
        return None

    mask = df["SWD"].notna() & df["SumSW"].notna()
    sub = df[mask]
    if len(sub) == 0:
        return None

    fig, ax = plt.subplots(figsize=(7, 7))
    ax.scatter(sub["SWD"], sub["SumSW"], s=5, alpha=0.15, c="steelblue")
    maxval = max(sub["SWD"].max(), sub["SumSW"].max()) * 1.05
    ax.plot([0, maxval], [0, maxval], c="red", linewidth=1, label="1:1 line")
    ax.set_xlabel("SWD [W/m²]")
    ax.set_ylabel("SumSW [W/m²]")
    ax.set_title("SWD vs SumSW (= DIF + DIR·cos(SZA))", fontweight="bold")
    ax.legend()
    ax.set_aspect("equal")
    fig.tight_layout()
    return _fig_to_base64(fig)


def plot_swd_minus_sumsw(df, metadata):
    """SWD - SumSW difference time series."""
    if not {"SWD", "SumSW"}.issubset(df.columns):
        return None

    diff = df["SWD"] - df["SumSW"]
    mask = diff.notna()
    if mask.sum() == 0:
        return None

    fig, ax = plt.subplots(figsize=(14, 4))
    ax.plot(diff[mask].index, diff[mask].values, linewidth=0.3, c="steelblue", alpha=0.8)
    ax.axhline(y=0, color="red", linewidth=1)
    ax.set_ylabel("SWD - SumSW [W/m²]")
    ax.set_title("SWD − SumSW Difference", fontweight="bold")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    fig.tight_layout()
    return _fig_to_base64(fig)


def generate_all_plots(df, metadata):
    """Generate all diagnostic plots. Returns dict of {name: base64_png}."""
    plots = OrderedDict()

    result = plot_radiation_timeseries(df, metadata)
    if result:
        plots["Radiation Time Series"] = result

    result = plot_qc_flags_timeseries(df, metadata)
    if result:
        plots["QC Flags Over Time"] = result

    result = plot_meteo(df, metadata)
    if result:
        plots["Meteorology"] = result

    result = plot_missing_data(df, metadata)
    if result:
        plots["Missing Data"] = result

    result = plot_long_dutton(df, metadata)
    if result:
        plots["Long & Dutton Limits"] = result

    result = plot_dif_swd_ratio(df, metadata)
    if result:
        plots["DIF/SWD Ratio vs SZA"] = result

    result = plot_swd_sumsw_ratio(df, metadata)
    if result:
        plots["SWD/SumSW Ratio vs SZA"] = result

    result = plot_swd_vs_sumsw_scatter(df, metadata)
    if result:
        plots["SWD vs SumSW Scatter"] = result

    result = plot_swd_minus_sumsw(df, metadata)
    if result:
        plots["SWD − SumSW Difference"] = result

    return plots


# =============================================================================
# MODULE 5: HTML REPORT GENERATOR
# =============================================================================

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BSRN QC Report — {{ meta.station_name }} {{ meta.year }}-{{ '%02d'|format(meta.month) }}</title>
<style>
  :root { --accent: #2563eb; --bg: #f8fafc; --card: #fff; --border: #e2e8f0; --text: #1e293b; }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: var(--bg); color: var(--text); line-height: 1.6; padding: 2rem; max-width: 1400px; margin: 0 auto; }
  h1 { font-size: 1.8rem; color: var(--accent); border-bottom: 3px solid var(--accent); padding-bottom: 0.5rem; margin-bottom: 1.5rem; }
  h2 { font-size: 1.3rem; color: var(--text); margin: 2rem 0 1rem 0; padding-bottom: 0.3rem; border-bottom: 1px solid var(--border); }
  .meta-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin-bottom: 2rem; }
  .meta-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 1rem; }
  .meta-card .label { font-size: 0.75rem; text-transform: uppercase; color: #64748b; letter-spacing: 0.05em; }
  .meta-card .value { font-size: 1.1rem; font-weight: 600; margin-top: 0.2rem; }
  table { width: 100%; border-collapse: collapse; margin: 1rem 0; background: var(--card); border-radius: 8px; overflow: hidden; }
  th, td { padding: 0.5rem 0.8rem; text-align: right; border-bottom: 1px solid var(--border); font-size: 0.85rem; }
  th { background: #f1f5f9; font-weight: 600; text-align: right; }
  th:first-child, td:first-child { text-align: left; }
  tr:hover { background: #f8fafc; }
  .flag-nonzero { color: #dc2626; font-weight: 600; }
  .plot-section { margin: 1.5rem 0; }
  .plot-section img { width: 100%; border: 1px solid var(--border); border-radius: 8px; }
  .summary-badge { display: inline-block; padding: 0.2rem 0.6rem; border-radius: 4px; font-size: 0.8rem; font-weight: 600; }
  .badge-ok { background: #dcfce7; color: #166534; }
  .badge-warn { background: #fef3c7; color: #92400e; }
  .badge-fail { background: #fee2e2; color: #991b1b; }
  footer { margin-top: 3rem; padding-top: 1rem; border-top: 1px solid var(--border); font-size: 0.8rem; color: #94a3b8; }
  .nav { position: sticky; top: 0; background: var(--bg); padding: 0.5rem 0; z-index: 10; border-bottom: 1px solid var(--border); margin-bottom: 1rem; }
  .nav a { color: var(--accent); text-decoration: none; margin-right: 1rem; font-size: 0.85rem; }
  .nav a:hover { text-decoration: underline; }
</style>
</head>
<body>

<h1>BSRN Quality Check Report</h1>

<div class="meta-grid">
  <div class="meta-card"><div class="label">Station</div><div class="value">{{ meta.station_name }} ({{ meta.station_code }})</div></div>
  <div class="meta-card"><div class="label">Period</div><div class="value">{{ meta.year }}-{{ '%02d'|format(meta.month) }}</div></div>
  <div class="meta-card"><div class="label">Location</div><div class="value">{{ '%.3f'|format(meta.latitude) }}°, {{ '%.3f'|format(meta.longitude) }}°</div></div>
  <div class="meta-card"><div class="label">Elevation</div><div class="value">{{ meta.elevation }} m</div></div>
  <div class="meta-card"><div class="label">Records</div><div class="value">{{ meta.n_records }}</div></div>
  <div class="meta-card"><div class="label">Upward Radiation</div><div class="value">{{ 'Yes' if meta.has_upward else 'No' }}</div></div>
  {% if meta.pi_name %}<div class="meta-card"><div class="label">PI</div><div class="value">{{ meta.pi_name }}</div></div>{% endif %}
  <div class="meta-card"><div class="label">Source File</div><div class="value">{{ meta.filename }}</div></div>
</div>

<nav class="nav">
  <a href="#qc-summary">QC Summary</a>
  {% if lr4000 %}<a href="#lr4000-check">LR4000 Check</a>{% endif %}
  <a href="#minima">Minima</a>
  {% for name in plots.keys() %}<a href="#plot-{{ loop.index }}">{{ name }}</a> {% endfor %}
</nav>

<h2 id="qc-summary">QC Flag Summary</h2>
<table>
<tr>
  <th>Parameter</th><th>Records</th><th>All OK</th>
  <th>&lt; Phys.</th><th>&gt; Phys.</th>
  <th>&lt; Rare</th><th>&gt; Rare</th>
  <th>&lt; Comp.</th><th>&gt; Comp.</th>
  <th>Any Flag</th>
</tr>
{% for row in qc_summary %}
<tr>
  <td><strong>{{ row.Parameter }}</strong></td>
  <td>{{ row['Total records'] }}</td>
  <td>{{ row['All OK (0)'] }}</td>
  <td class="{{ 'flag-nonzero' if row['< Phys. Possible'] > 0 }}">{{ row['< Phys. Possible'] }}</td>
  <td class="{{ 'flag-nonzero' if row['> Phys. Possible'] > 0 }}">{{ row['> Phys. Possible'] }}</td>
  <td class="{{ 'flag-nonzero' if row['< Extremely Rare'] > 0 }}">{{ row['< Extremely Rare'] }}</td>
  <td class="{{ 'flag-nonzero' if row['> Extremely Rare'] > 0 }}">{{ row['> Extremely Rare'] }}</td>
  <td class="{{ 'flag-nonzero' if row['< Comparison'] > 0 }}">{{ row['< Comparison'] }}</td>
  <td class="{{ 'flag-nonzero' if row['> Comparison'] > 0 }}">{{ row['> Comparison'] }}</td>
  <td class="{{ 'flag-nonzero' if row['Any flag'] > 0 }}">{{ row['Any flag'] }}</td>
</tr>
{% endfor %}
</table>

{% if lr4000 %}
<h2 id="lr4000-check">LR4000 Pyrgeometer Check</h2>
<p style="margin-bottom: 1rem; color: #64748b; font-size: 0.9rem;">
Comparison of submitted LWD values with recalculated values from dome/body temperatures (LR4000).
</p>
<table style="max-width: 800px;">
<tr><th style="text-align: left;">Metric</th><th>Value</th></tr>
<tr><td>Total records</td><td>{{ lr4000.n_total }}</td></tr>
<tr><td>LR4000 available</td><td>{{ lr4000.n_lr4000 }} ({{ '%.1f'|format(100 - lr4000.pct_missing) }}%)</td></tr>
<tr><td>LR4000 missing</td><td class="{{ 'flag-nonzero' if lr4000.pct_missing > 10 }}">{{ lr4000.n_missing }} ({{ '%.1f'|format(lr4000.pct_missing) }}%)</td></tr>
</table>

<h3 style="font-size: 1.1rem; margin-top: 1.5rem; margin-bottom: 0.5rem;">Simple Equation: Uemf + σ·Tb⁴ (informational)</h3>
<p style="margin-bottom: 0.5rem; color: #64748b; font-size: 0.85rem; font-style: italic;">
Uncalibrated estimate. The large offset (≈ σ·Tb⁴ ≈ 430–550 W/m²) is expected and not a data quality concern.
</p>
<table style="max-width: 800px;">
<tr><th style="text-align: left;">Statistic</th><th>Value</th></tr>
<tr><td>Mean difference</td><td>{{ '%.2f'|format(lr4000.simple_mean_diff) }} W/m²</td></tr>
<tr><td>Std. deviation</td><td>{{ '%.2f'|format(lr4000.simple_std_diff) }} W/m²</td></tr>
<tr><td>Median difference</td><td>{{ '%.2f'|format(lr4000.simple_median_diff) }} W/m²</td></tr>
<tr><td><strong>Max difference (simple)</strong></td><td>{{ '%.2f'|format(lr4000.simple_max_diff) }} W/m² at {{ lr4000.simple_max_diff_time }}</td></tr>
</table>

{% if lr4000.has_full %}
<h3 style="font-size: 1.1rem; margin-top: 1.5rem; margin-bottom: 0.5rem;">Full Equation: k0 + Uemf·(1 + k1·σ·Tb³) + k2·σ·Tb⁴{% if lr4000.full_needs_dome %} − k3·σ·(Td⁴ − Tb⁴){% endif %}</h3>
<p style="margin-bottom: 0.5rem; color: #64748b; font-size: 0.85rem; font-style: italic;">
Calibrated estimate using *U0003 @LR4000CONST coefficients. Small values (0–2 W/m²) confirm consistent calibration. Large values indicate a data-quality issue.
</p>
<table style="max-width: 800px;">
<tr><th style="text-align: left;">Statistic</th><th>Value</th></tr>
<tr><td>Records available</td><td>{{ lr4000.n_full }}{% if lr4000.full_needs_dome %} (with dome temps){% endif %}</td></tr>
<tr><td>Mean difference</td><td>{{ '%.2f'|format(lr4000.full_mean_diff) }} W/m²</td></tr>
<tr><td>Std. deviation</td><td>{{ '%.2f'|format(lr4000.full_std_diff) }} W/m²</td></tr>
<tr><td>Median difference</td><td>{{ '%.2f'|format(lr4000.full_median_diff) }} W/m²</td></tr>
<tr><td><strong>Max difference (full)</strong></td><td class="{{ 'flag-nonzero' if lr4000.full_max_diff|abs > 5 }}"><strong>{{ '%.2f'|format(lr4000.full_max_diff) }} W/m²</strong> at {{ lr4000.full_max_diff_time }}</td></tr>
</table>
{% if not lr4000.full_needs_dome %}
<p style="margin-top: 0.5rem; color: #64748b; font-size: 0.85rem; font-style: italic;">
Note: k3=0, so dome temperature term not needed
</p>
{% endif %}
{% endif %}
{% endif %}

<h2 id="minima">Parameter Minima</h2>
<table>
<tr><th>Parameter</th><th>Minimum Value</th></tr>
{% for param, val in minima.items() %}
<tr><td>{{ param }}</td><td>{{ '%.1f'|format(val) if val == val else 'N/A' }}</td></tr>
{% endfor %}
</table>

{% for name, img in plots.items() %}
<h2 id="plot-{{ loop.index }}">{{ name }}</h2>
<div class="plot-section">
  <img src="data:image/png;base64,{{ img }}" alt="{{ name }}">
</div>
{% endfor %}

<footer>
  Generated {{ now }} by BSRN QC Pipeline &middot; QC checks based on Long & Dutton (2002), BSRN Recommended V2.0
</footer>

</body>
</html>
"""


BATCH_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>BSRN QC Batch Summary</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #f8fafc; color: #1e293b; padding: 2rem; max-width: 1200px; margin: 0 auto; }
  h1 { color: #2563eb; border-bottom: 3px solid #2563eb; padding-bottom: 0.5rem; }
  table { width: 100%; border-collapse: collapse; margin: 1.5rem 0; background: #fff; border-radius: 8px; overflow: hidden; }
  th, td { padding: 0.6rem 1rem; text-align: right; border-bottom: 1px solid #e2e8f0; font-size: 0.9rem; }
  th { background: #f1f5f9; font-weight: 600; }
  th:first-child, td:first-child { text-align: left; }
  a { color: #2563eb; }
  .flag-nonzero { color: #dc2626; font-weight: 600; }
  footer { margin-top: 2rem; font-size: 0.8rem; color: #94a3b8; border-top: 1px solid #e2e8f0; padding-top: 1rem; }
</style>
</head>
<body>
<h1>BSRN QC Batch Summary</h1>
<p>Processed {{ files|length }} file(s) on {{ now }}</p>
<table>
<tr><th>Station</th><th>Period</th><th>Records</th><th>Upward</th><th>Total Flags</th><th>Report</th></tr>
{% for f in files %}
<tr>
  <td>{{ f.station_name }} ({{ f.station_code }})</td>
  <td>{{ f.year }}-{{ '%02d'|format(f.month) }}</td>
  <td>{{ f.n_records }}</td>
  <td>{{ 'Yes' if f.has_upward else 'No' }}</td>
  <td class="{{ 'flag-nonzero' if f.total_flags > 0 }}">{{ f.total_flags }}</td>
  <td><a href="{{ f.report_filename }}">View</a></td>
</tr>
{% endfor %}
</table>
<footer>Generated by BSRN QC Pipeline</footer>
</body>
</html>
"""


def generate_report(df, metadata, qc_summary_df, plots, output_path, lr4000_report=None):
    """Generate the HTML report for a single file."""
    # Compute minima
    numeric_cols = ["SWD", "DIF", "DIR", "LWD", "SWU", "LWU", "T2", "RH", "P", "SZA", "SumSW"]
    minima = {}
    for col in numeric_cols:
        if col in df.columns and df[col].notna().any():
            minima[col] = df[col].min()

    # Convert QC summary to list of dicts
    qc_rows = qc_summary_df.to_dict("records")

    template = Environment(autoescape=True).from_string(HTML_TEMPLATE)
    html = template.render(
        meta=metadata,
        qc_summary=qc_rows,
        minima=minima,
        plots=plots,
        lr4000=lr4000_report,
        now=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        f.write(html)

    return output_path


def generate_batch_summary(file_infos, output_dir):
    """Generate batch summary HTML."""
    template = Environment(autoescape=True).from_string(BATCH_TEMPLATE)
    html = template.render(
        files=file_infos,
        now=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    path = Path(output_dir) / "batch_summary.html"
    with open(path, "w") as f:
        f.write(html)
    return path


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

def _display_dashboard_link(url="http://localhost:8501"):
    """
    Render a large clickable link in a Jupyter notebook cell output.
    Falls back to a plain print() if IPython is not available
    (e.g. when called from the CLI).
    """
    try:
        from IPython.display import display, HTML
        display(HTML(f"""
        <div style="margin:12px 0; padding:14px 18px; background:#f0f7ff;
                    border-left:4px solid #1a73e8; border-radius:6px;
                    font-family:sans-serif;">
          <span style="font-size:1.1em;">☀️ <strong>BSRN QC Dashboard is ready</strong></span><br>
          <a href="{url}" target="_blank"
             style="font-size:1.3em; color:#1a73e8; font-weight:bold;
                    text-decoration:none; letter-spacing:0.01em;">
            🚀 &nbsp;Open Dashboard &nbsp;→ &nbsp;{url}
          </a><br>
          <span style="font-size:0.85em; color:#555; margin-top:4px; display:block;">
            Upload your .dat file(s) in the sidebar &nbsp;·&nbsp;
            Stop: Kernel → Restart
          </span>
        </div>
        """))
    except ImportError:
        print(f"\n  ✅  Dashboard ready → {url}")
        print("  Upload your .dat file(s) in the sidebar to explore.")


def _launch_dashboard():
    """
    Launch the Streamlit dashboard as a background process, then poll
    localhost:8501 until the HTTP server is actually accepting connections,
    and open the browser automatically.  Surfaces any startup errors.
    Returns the subprocess.Popen object (or None on failure).
    """
    import subprocess
    import time
    import sys as _sys
    import urllib.request
    import urllib.error

    URL = "http://localhost:8501"

    # ── 1. Check streamlit_app.py exists ────────────────────────────────────
    app_path = Path(__file__).parent / "streamlit_app.py"
    if not app_path.exists():
        print(f"\n  [dashboard] streamlit_app.py not found at {app_path}")
        print("             Make sure streamlit_app.py is in the same folder as bsrn_qc.py.")
        return None

    # ── 2. Pre-flight: verify streamlit + plotly are importable ─────────────
    missing = []
    for pkg in ("streamlit", "plotly"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"\n  [dashboard] Missing package(s): {', '.join(missing)}")
        print(f"             Fix:  pip install {' '.join(missing)}")
        print("             Then re-run this cell.")
        return None

    # ── 3. If a dashboard is already running on 8501, just show the link ───────
    try:
        urllib.request.urlopen(URL, timeout=1)
        print(f"\n  [dashboard] Server already running.")
        _display_dashboard_link(URL)
        return None
    except Exception:
        pass  # not running yet — start it

    # ── 4. Launch using the exact Python interpreter of this kernel ──────────
    #   Guarantees the subprocess finds the same installed packages.
    python_exe = _sys.executable
    cmd = [python_exe, "-m", "streamlit", "run", str(app_path),
           "--server.headless", "true",
           "--server.port", "8501"]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,   # merge stderr into stdout
            text=True,
        )
    except Exception as e:
        print(f"\n  [dashboard] Failed to start subprocess: {e}")
        return None

    # ── 5. Poll until HTTP server responds or process dies (30 s timeout) ────
    print("\n  Starting dashboard", end="", flush=True)
    deadline = time.time() + 30
    server_up = False

    while time.time() < deadline:
        # Check if process already died
        if proc.poll() is not None:
            break
        # Try to reach the HTTP server
        try:
            urllib.request.urlopen(URL, timeout=1)
            server_up = True
            break
        except urllib.error.URLError:
            print(".", end="", flush=True)
            time.sleep(1)
        except Exception:
            time.sleep(1)

    print()  # newline after the dots

    # ── 6. Report outcome ────────────────────────────────────────────────────
    if server_up:
        print("\n  ✅  Dashboard is ready.")
        _display_dashboard_link(URL)
        return proc

    # Process died — collect and show its output
    output = ""
    try:
        output, _ = proc.communicate(timeout=3)
    except Exception:
        pass

    print("\n" + "=" * 60)
    print("  [dashboard] Streamlit failed to start.  Error output:")
    print("-" * 60)
    for line in (output or "No output captured.").splitlines()[:40]:
        print("  " + line)
    print("=" * 60)
    print("\n  To debug, run this in a terminal:")
    print(f"       python -m streamlit run \"{app_path}\"")
    return None


def process_one_file(filepath, output_dir):
    """Full pipeline for one .dat file. Returns metadata dict with results."""
    filepath = Path(filepath)
    print(f"\n{'='*60}")
    print(f"Processing: {filepath.name}")
    print(f"{'='*60}")

    # 1. Parse
    print("  [1/5] Parsing .dat file...")
    df, metadata = parse_dat_file(filepath)
    print(f"        {metadata['station_name']} ({metadata['station_code']}), "
          f"{metadata['year']}-{metadata['month']:02d}, "
          f"{len(df)} records, "
          f"lat={metadata['latitude']:.3f}, lon={metadata['longitude']:.3f}")

    # 2. Solar auxiliary data
    print("  [2/5] Computing solar geometry (Iqbal 1983)...")
    df = compute_solar_auxiliary(df, metadata["latitude"], metadata["longitude"])
    print(f"        SZA range: {df['SZA'].min():.1f}° – {df['SZA'].max():.1f}°")

    # 2b. LR4000 check (if available)
    lr4000_report = check_lr4000(df, metadata)
    if lr4000_report:
        print("  [2b]  LR4000 check...")
        print(f"        LR4000 records: {lr4000_report['n_lr4000']}/{lr4000_report['n_total']} "
              f"({lr4000_report['pct_missing']:.1f}% missing)")
        print(f"        Simple eq. max diff: {lr4000_report['simple_max_diff']:.2f} W/m² at {lr4000_report['simple_max_diff_time']}")
        if lr4000_report['has_full']:
            print(f"        Full eq. max diff: {lr4000_report['full_max_diff']:.2f} W/m² at {lr4000_report['full_max_diff_time']}")
    else:
        lr4000_report = None
        print("  [2b]  No LR4000 data available")

    # 3. QC checks
    print("  [3/5] Running quality checks...")
    df = run_qc_checks(df)
    qc_summary = summarize_qc_flags(df)
    total_flags = int(qc_summary["Any flag"].sum())
    print(f"        Total flagged values: {total_flags}")

    # 4. Plots
    print("  [4/5] Generating diagnostic plots...")
    plots = generate_all_plots(df, metadata)
    print(f"        Generated {len(plots)} plot(s)")

    # 5. Report
    report_name = f"{metadata['station_code']}_{metadata['year']}-{metadata['month']:02d}_QC_report.html"
    report_path = Path(output_dir) / report_name
    print(f"  [5/5] Writing report → {report_path}")
    generate_report(df, metadata, qc_summary, plots, report_path, lr4000_report)

    metadata["total_flags"] = total_flags
    metadata["report_filename"] = report_name
    return metadata


def main():
    """CLI entry point."""
    if len(sys.argv) < 2:
        print("Usage: python bsrn_qc.py <file1.dat> [file2.dat ...] [folder/] [--dashboard]")
        print("       Output goes to qc_reports/ next to the input files.")
        print("       --dashboard   Open the interactive Streamlit dashboard after processing.")
        sys.exit(1)

    # Strip --dashboard flag before collecting file args
    args = sys.argv[1:]
    launch_dash = "--dashboard" in args
    args = [a for a in args if a != "--dashboard"]

    # Collect input files
    dat_files = []
    for arg in args:
        p = Path(arg)
        if p.is_dir():
            dat_files.extend(sorted(p.glob("*.dat")))
        elif p.is_file() and p.suffix.lower() == ".dat":
            dat_files.append(p)
        else:
            print(f"Warning: skipping {arg} (not a .dat file or folder)")

    if not dat_files:
        print("No .dat files found.")
        sys.exit(1)

    # Output directory: qc_reports/ next to the first input file
    output_dir = dat_files[0].parent / "qc_reports"
    output_dir.mkdir(exist_ok=True)
    print(f"\nBSRN Quality Check Pipeline")
    print(f"{'='*60}")
    print(f"Input files:  {len(dat_files)}")
    print(f"Output dir:   {output_dir}")

    # Process each file
    results = []
    for f in dat_files:
        try:
            info = process_one_file(f, output_dir)
            results.append(info)
        except Exception as e:
            print(f"\n  ERROR processing {f.name}: {e}")
            import traceback
            traceback.print_exc()

    # Batch summary
    if len(results) > 1:
        summary_path = generate_batch_summary(results, output_dir)
        print(f"\nBatch summary → {summary_path}")

    print(f"\nDone! {len(results)}/{len(dat_files)} files processed successfully.")
    print(f"Reports in: {output_dir}/")

    if launch_dash:
        proc = _launch_dashboard()
        if proc is not None:
            try:
                proc.wait()   # Keep alive until Ctrl+C
            except KeyboardInterrupt:
                proc.terminate()
                print("\nDashboard stopped.")


# =============================================================================
# NOTEBOOK-FRIENDLY API
# =============================================================================

def _plot_inline(plot_func, df, metadata):
    """Call a plot function but display inline instead of returning base64."""
    # Temporarily swap the backend
    old_backend = matplotlib.get_backend()
    # Save original _fig_to_base64 behavior — we override the close
    import types

    # Re-create the figure by calling the raw plotting logic
    # We'll monkey-patch _fig_to_base64 to just show instead
    original = globals().get("_fig_to_base64")

    def _show_fig(fig):
        fig.set_dpi(100)
        plt.show()
        return ""  # return empty string so callers don't break

    globals()["_fig_to_base64"] = _show_fig
    try:
        plot_func(df, metadata)
    finally:
        globals()["_fig_to_base64"] = original


def run_notebook(filepath, output_dir=None, save_html=True, show_plots=True,
                 launch_dashboard=False):
    """
    Run the full QC pipeline from a Jupyter notebook.

    Usage in a notebook cell:
        from bsrn_qc import run_notebook
        df, meta, qc = run_notebook("cab0425.dat")

        # Open the interactive Streamlit dashboard after processing:
        df, meta, qc = run_notebook("cab0425.dat", launch_dashboard=True)

    Parameters
    ----------
    filepath : str or Path
        Path to a .dat file.
    output_dir : str or Path, optional
        Where to save the HTML report. Defaults to qc_reports/ next to the file.
    save_html : bool
        Whether to also save an HTML report (default True).
    show_plots : bool
        Whether to display plots inline in the notebook (default True).
    launch_dashboard : bool
        Open the interactive Streamlit dashboard in the browser after processing
        (default False). Requires: pip install streamlit plotly

    Returns
    -------
    df : DataFrame
        The processed data with all QC columns.
    metadata : dict
        Station metadata.
    qc_summary : DataFrame
        QC flag summary table.
    """
    from IPython.display import display, HTML as IPHTML

    filepath = Path(filepath)

    # 1. Parse
    print(f"Parsing {filepath.name}...")
    df, metadata = parse_dat_file(filepath)
    print(f"  {metadata['station_name']} ({metadata['station_code']}), "
          f"{metadata['year']}-{metadata['month']:02d}, "
          f"{len(df)} records")

    # 2. Solar auxiliary
    print("Computing solar geometry...")
    df = compute_solar_auxiliary(df, metadata["latitude"], metadata["longitude"])

    # 2b. LR4000 check
    lr4000_report = check_lr4000(df, metadata)
    if lr4000_report:
        print(f"\nLR4000 Check: {lr4000_report['n_lr4000']}/{lr4000_report['n_total']} records")
        print(f"  Simple eq. max diff = {lr4000_report['simple_max_diff']:.2f} W/m²")
        if lr4000_report['has_full']:
            print(f"  Full eq. max diff = {lr4000_report['full_max_diff']:.2f} W/m²")

    # 3. QC
    print("Running quality checks...")
    df = run_qc_checks(df)
    qc_summary = summarize_qc_flags(df)

    # Display QC summary as a styled table
    print("\nQC Flag Summary:")
    display(qc_summary.style.applymap(
        lambda v: "color: red; font-weight: bold" if isinstance(v, (int, float)) and v > 0 else "",
        subset=[c for c in qc_summary.columns if c not in ["Parameter", "Total records", "All OK (0)"]]
    ))

    # 4. Show plots inline
    if show_plots:
        print("\nGenerating plots...")
        # Switch to inline-friendly mode
        original_b64 = globals()["_fig_to_base64"]

        def _show_and_return(fig):
            plt.show()
            return ""

        globals()["_fig_to_base64"] = _show_and_return
        try:
            plot_radiation_timeseries(df, metadata)
            plot_qc_flags_timeseries(df, metadata)
            plot_meteo(df, metadata)
            plot_missing_data(df, metadata)
            plot_long_dutton(df, metadata)
            plot_dif_swd_ratio(df, metadata)
            plot_swd_sumsw_ratio(df, metadata)
            plot_swd_vs_sumsw_scatter(df, metadata)
            plot_swd_minus_sumsw(df, metadata)
        finally:
            globals()["_fig_to_base64"] = original_b64

    # 5. Optionally save HTML report
    if save_html:
        if output_dir is None:
            output_dir = filepath.parent / "qc_reports"
        output_dir = Path(output_dir)
        output_dir.mkdir(exist_ok=True)

        # 5a. Static HTML report (matplotlib, for archiving / sharing)
        plots = generate_all_plots(df, metadata)
        report_name = (f"{metadata['station_code']}_"
                       f"{metadata['year']}-{metadata['month']:02d}_QC_report.html")
        report_path = output_dir / report_name
        generate_report(df, metadata, qc_summary, plots, report_path, lr4000_report)
        print(f"\nStatic HTML report  → {report_path}")

        # 5b. Interactive HTML report (Plotly, opens directly in browser)
        try:
            from interactive_report import (generate_interactive_report,
                                             show_notebook_link)
            iname = (f"{metadata['station_code']}_"
                     f"{metadata['year']}-{metadata['month']:02d}"
                     f"_QC_report_interactive.html")
            ipath = output_dir / iname
            generate_interactive_report(df, metadata, qc_summary,
                                        ipath, lr4000_report)
            print(f"Interactive report  → {ipath}")
            show_notebook_link(ipath)
        except ImportError:
            print("  (interactive_report.py not found — skipping interactive report)")
        except Exception as _e:
            print(f"  (interactive report skipped: {_e})")

    return df, metadata, qc_summary


def run_notebook_batch(folder_or_files, output_dir=None, show_plots=True,
                       launch_dashboard=False):
    """
    Batch-process multiple .dat files from a notebook.

    Usage:
        from bsrn_qc import run_notebook_batch
        results = run_notebook_batch("/path/to/dat/files/")

        # Open the interactive dashboard after all files are processed:
        results = run_notebook_batch("/path/to/dat/files/", launch_dashboard=True)

    Parameters
    ----------
    folder_or_files : str, Path, or list
        A folder path, or a list of file paths.
    output_dir : str or Path, optional
        Where to save reports.
    show_plots : bool
        Show plots inline for each file.
    launch_dashboard : bool
        Open the interactive Streamlit dashboard after all files are processed
        (default False). Requires: pip install streamlit plotly

    Returns
    -------
    results : list of (df, metadata, qc_summary) tuples
    """
    if isinstance(folder_or_files, (str, Path)):
        p = Path(folder_or_files)
        if p.is_dir():
            dat_files = sorted(p.glob("*.dat"))
        else:
            dat_files = [p]
    else:
        dat_files = [Path(f) for f in folder_or_files]

    if not dat_files:
        print("No .dat files found.")
        return []

    results = []
    for f in dat_files:
        try:
            result = run_notebook(f, output_dir=output_dir,
                                  save_html=True, show_plots=show_plots)
            results.append(result)
        except Exception as e:
            print(f"\nERROR processing {f.name}: {e}")
            import traceback
            traceback.print_exc()

    # Generate batch summary
    if len(results) > 1 and output_dir:
        file_infos = []
        for df, meta, qc in results:
            meta["total_flags"] = int(qc["Any flag"].sum())
            meta["report_filename"] = f"{meta['station_code']}_{meta['year']}-{meta['month']:02d}_QC_report.html"
            file_infos.append(meta)
        generate_batch_summary(file_infos, output_dir)
        print(f"\nBatch summary saved → {Path(output_dir) / 'batch_summary.html'}")

    # Interactive reports are generated per-file inside run_notebook() above.
    return results


if __name__ == "__main__":
    main()
