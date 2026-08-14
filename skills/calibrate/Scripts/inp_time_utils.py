"""Shared INP time-preparation utilities for calibration scripts.

Extracted from compute_results.py to avoid duplication across GA, Bayes-MH,
and ES-ILU calibration scripts.

Uses pure-text INP manipulation to avoid swmm_api dependency
(which triggers asyncio/_overlapped errors on some Windows systems).
"""

import os
import datetime
from typing import List, Tuple, Optional

import pandas as pd


TS_NAME = "RG_TS"
EVENT_SEP = ";"


def read_rain_event(rain_path: str, sep: str = EVENT_SEP) -> pd.DataFrame:
    with open(rain_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.read().splitlines()

    df = None
    if len(lines) >= 3 and ("Time" in lines[1]) and (sep in lines[1]):
        header = [c.strip() for c in lines[1].split(sep)]
        rows = []
        for ln in lines[2:]:
            ln = ln.strip()
            if not ln:
                continue
            parts = [p.strip() for p in ln.split(sep)]
            if len(parts) >= 2:
                rows.append(parts[:2])
        if len(header) >= 2:
            df = pd.DataFrame(rows, columns=[header[0], header[1]])

    if df is None:
        for sp in [sep, ",", "\t"]:
            try:
                df_try = pd.read_csv(rain_path, sep=sp)
                if df_try.shape[1] >= 2:
                    df = df_try
                    break
            except Exception:
                continue
        if df is None:
            raise ValueError(f"Could not parse rainfall file: {rain_path}")

    cols = list(df.columns)
    time_col = "Time" if "Time" in cols else cols[0]
    val_col = [c for c in cols if c != time_col][0]

    df[time_col] = pd.to_datetime(df[time_col], errors="coerce")
    df[val_col] = pd.to_numeric(df[val_col], errors="coerce")
    df = df.dropna(subset=[time_col]).sort_values(time_col).reset_index(drop=True)
    df = df.rename(columns={time_col: "Time", val_col: "rain"})[["Time", "rain"]]
    return df


# --------------- pure-text INP helpers ---------------

def _read_inp_lines(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.readlines()


def _write_inp_lines(lines: List[str], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def _find_section_bounds(lines: List[str], section: str) -> Tuple[Optional[int], Optional[int]]:
    """Return (start, end) where start is the first line after [SECTION] header
    and end is the line number of the next section header (or EOF)."""
    header = f"[{section}]".upper()
    start = None
    for i, line in enumerate(lines):
        if line.strip().upper() == header:
            start = i + 1
            break
    if start is None:
        return None, None
    for j in range(start, len(lines)):
        s = lines[j].strip()
        if s.startswith("[") and s.endswith("]"):
            return start, j
    return start, len(lines)


def _replace_timeseries(lines: List[str], ts_name: str, rain_df: pd.DataFrame) -> List[str]:
    """Remove existing lines for ts_name and append new rain data."""
    start, end = _find_section_bounds(lines, "TIMESERIES")

    # Build new timeseries lines
    new_ts_lines = []
    for _, row in rain_df.iterrows():
        dt = pd.to_datetime(row["Time"])
        date_str = dt.strftime("%m/%d/%Y")
        time_str = dt.strftime("%H:%M")
        val = float(row["rain"])
        new_ts_lines.append(f"{ts_name}  {date_str}  {time_str}  {val}\n")

    if start is None:
        # No [TIMESERIES] section exists — create one before [END] or at EOF
        insert_pos = len(lines)
        for i, line in enumerate(lines):
            if line.strip().upper() == "[END]":
                insert_pos = i
                break
        new_section = ["\n", "[TIMESERIES]\n"] + new_ts_lines + ["\n"]
        return lines[:insert_pos] + new_section + lines[insert_pos:]

    # Remove old lines belonging to ts_name
    kept = []
    for i in range(start, end):
        s = lines[i].strip()
        if not s or s.startswith(";"):
            kept.append(lines[i])
            continue
        toks = s.split()
        if toks and toks[0].upper() == ts_name.upper():
            continue  # drop old entry
        kept.append(lines[i])

    # Insert new ts data at the end of the kept section
    return lines[:start] + kept + new_ts_lines + ["\n"] + lines[end:]


def _update_raingages(lines: List[str], ts_name: str, rain_df: pd.DataFrame) -> List[str]:
    """Update all RAINGAGES to reference TIMESERIES ts_name with correct interval."""
    start, end = _find_section_bounds(lines, "RAINGAGES")
    if start is None:
        return lines

    # Compute rainfall interval from rain_df
    times = pd.to_datetime(rain_df["Time"])
    if len(times) >= 2:
        delta_sec = int(times.diff().dropna().median().total_seconds())
        h, rem = divmod(delta_sec, 3600)
        m = rem // 60
        interval_str = f"{h}:{m:02d}"
    else:
        interval_str = "0:01"

    for i in range(start, end):
        s = lines[i].strip()
        if not s or s.startswith(";"):
            continue
        toks = s.split()
        if len(toks) >= 6:
            # Format: Name  Format  Interval  SCF  Source  SourceName
            toks[1] = "INTENSITY"
            toks[2] = interval_str
            # toks[3] = SCF, keep as-is
            toks[4] = "TIMESERIES"
            toks[5] = ts_name
            lines[i] = "  ".join(toks[:6]) + "\n"
    return lines


def _update_options_period(lines: List[str], start_dt, end_dt) -> List[str]:
    """Update OPTIONS section START/END/REPORT_START dates and times."""
    start, end = _find_section_bounds(lines, "OPTIONS")
    if start is None:
        return lines

    replacements = {
        "START_DATE": start_dt.strftime("%m/%d/%Y"),
        "START_TIME": start_dt.strftime("%H:%M:%S"),
        "END_DATE": end_dt.strftime("%m/%d/%Y"),
        "END_TIME": end_dt.strftime("%H:%M:%S"),
        "REPORT_START_DATE": start_dt.strftime("%m/%d/%Y"),
        "REPORT_START_TIME": start_dt.strftime("%H:%M:%S"),
    }

    for i in range(start, end):
        s = lines[i].strip()
        if not s or s.startswith(";"):
            continue
        toks = s.split()
        if len(toks) >= 2 and toks[0] in replacements:
            lines[i] = f"{toks[0]}  {replacements[toks[0]]}\n"
    return lines


# --------------- main entry point ---------------

def prepare_inp_for_event(base_inp, out_inp, rain_df, obs_times):
    """Pure-text INP modification:
    1. Replace/create TIMESERIES with rain_df data (name = TS_NAME)
    2. Update all RAINGAGES to reference TIMESERIES TS_NAME with correct interval
    3. Update OPTIONS START/END/REPORT_START dates from observation window
    """
    lines = _read_inp_lines(base_inp)

    # --- A) Replace TIMESERIES ---
    lines = _replace_timeseries(lines, TS_NAME, rain_df)

    # --- B) Update RAINGAGES to reference TS_NAME ---
    lines = _update_raingages(lines, TS_NAME, rain_df)

    # --- C) Update OPTIONS dates ---
    start_dt = pd.to_datetime(obs_times.iloc[0])
    end_dt = pd.to_datetime(obs_times.iloc[-1])
    lines = _update_options_period(lines, start_dt, end_dt)

    _write_inp_lines(lines, out_inp)
