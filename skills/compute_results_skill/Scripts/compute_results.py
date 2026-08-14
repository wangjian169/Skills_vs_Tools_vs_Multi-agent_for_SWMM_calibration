import os
import sys
import tempfile
import shutil
from typing import List, Dict, Tuple, Optional

import numpy as np
import pandas as pd
from pyswmm import Simulation, Nodes, Links

# Allow running this file directly (`python ...\compute_results.py`) by
# ensuring the project root is on sys.path for absolute `skills.*` imports.
if __package__ in (None, ""):
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    _PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "..", ".."))
    if _PROJECT_ROOT not in sys.path:
        sys.path.insert(0, _PROJECT_ROOT)

from skills.calibrate.Scripts.inp_time_utils import (
    read_rain_event, prepare_inp_for_event, TS_NAME, EVENT_SEP,
)


# =========================
# Hard-coded configuration
# =========================
TIME_TOL_SEC = 60         # Observation time-alignment tolerance (seconds)

NODE_VARS = {"depth", "head", "volume", "lateral_inflow", "total_inflow", "flooding"}
LINK_VARS = {"flow", "depth", "velocity", "volume"}


# =========================
# Observation CSV: first column time, remaining columns are observations (node_1_depth / link_L12_flow)
# =========================
def read_obs_csv(obs_csv: str) -> Tuple[pd.Series, List[str], pd.DataFrame]:
    df = pd.read_csv(obs_csv)
    if df.shape[1] < 2:
        raise ValueError("Observation CSV must contain at least 2 columns: time + observation")

    tcol = df.columns[0]
    df[tcol] = pd.to_datetime(df[tcol], errors="coerce")
    df = df.dropna(subset=[tcol]).sort_values(tcol).reset_index(drop=True)
    df = df.rename(columns={tcol: "Time"})
    obs_cols = list(df.columns[1:])
    return df["Time"], obs_cols, df


def parse_obs_specs(obs_cols: List[str]) -> List[Tuple[str, str, str, str]]:
    specs = []
    for col in obs_cols:
        toks = col.split("_")
        if len(toks) < 3:
            raise ValueError(f"Column name must match node_<ID>_<var> or link_<ID>_<var>: {col}")
        t = toks[0].lower()
        obj_id = toks[1]
        var = "_".join(toks[2:]).lower()
        if t == "node" and var not in NODE_VARS:
            raise ValueError(f"Unsupported node variable: {var} (column {col})")
        if t == "link" and var not in LINK_VARS:
            raise ValueError(f"Unsupported link variable: {var} (column {col})")
        if t not in ("node", "link"):
            raise ValueError(f"Column name must start with node/link: {col}")
        specs.append((t, obj_id, var, col))
    return specs




# =========================
# SWMM: sample by observation timestamps
# =========================
def read_swmm_value(nodes: Nodes, links: Links, obj_type: str, obj_id: str, var: str) -> float:
    if obj_type == "node":
        n = nodes[obj_id]
        if var == "depth": return n.depth
        if var == "head": return n.head
        if var == "volume": return n.volume
        if var == "lateral_inflow": return n.lateral_inflow
        if var == "total_inflow": return n.total_inflow
        if var == "flooding": return n.flooding
    if obj_type == "link":
        l = links[obj_id]
        if var == "flow": return l.flow
        if var == "depth": return l.depth
        if var == "velocity": return l.velocity
        if var == "volume": return l.volume
    raise ValueError(f"Failed to read: {obj_type}_{obj_id}_{var}")


def run_swmm_sample(inp_path: str, specs: List[Tuple[str, str, str, str]], obs_times: pd.Series) -> pd.DataFrame:
    obs_times = pd.to_datetime(obs_times).dt.tz_localize(None)
    obs_times64 = obs_times.to_numpy(dtype="datetime64[s]")

    time_list = []
    store: Dict[str, List[float]] = {col: [] for (_, _, _, col) in specs}

    with Simulation(inp_path) as sim:
        nodes = Nodes(sim)
        links = Links(sim)
        for _ in sim:
            t = pd.to_datetime(sim.current_time).to_datetime64().astype("datetime64[s]")
            time_list.append(t)
            for obj_type, obj_id, var, col in specs:
                store[col].append(float(read_swmm_value(nodes, links, obj_type, obj_id, var)))

    out_df = pd.DataFrame({"_t": np.array(time_list, dtype="datetime64[s]")})
    for col, vals in store.items():
        out_df[col] = np.array(vals, dtype=float)
    out_df = out_df.sort_values("_t").reset_index(drop=True)

    aligned = pd.DataFrame({"_t_obs": obs_times64})
    aligned = pd.merge_asof(
        aligned.sort_values("_t_obs"),
        out_df.sort_values("_t"),
        left_on="_t_obs",
        right_on="_t",
        direction="nearest",
        tolerance=pd.Timedelta(seconds=TIME_TOL_SEC),
    )
    if aligned.isna().any().any():
        raise RuntimeError("Time alignment failed: check timestep or increase TIME_TOL_SEC")

    res = pd.DataFrame({"Time": pd.to_datetime(aligned["_t_obs"])})
    for _, _, _, col in specs:
        res[col] = aligned[col].to_numpy(dtype=float)
    return res


# =========================
# NSE
# =========================
def nse(y_true: np.ndarray, y_sim: np.ndarray) -> float:
    y_true = np.asarray(y_true, dtype=float)
    y_sim = np.asarray(y_sim, dtype=float)
    denom = np.sum((y_true - np.mean(y_true)) ** 2)
    if denom <= 1e-12:
        return float("nan")
    return 1.0 - float(np.sum((y_true - y_sim) ** 2) / denom)


def compute_event_nse(obs_df: pd.DataFrame, sim_df: pd.DataFrame, obs_cols: List[str]) -> Tuple[float, Dict[str, float]]:
    per_col: Dict[str, float] = {}
    scores = []
    for col in obs_cols:
        score = nse(obs_df[col].to_numpy(dtype=float), sim_df[col].to_numpy(dtype=float))
        per_col[col] = score
        if not np.isnan(score):
            scores.append(score)
    overall = float(np.mean(scores)) if scores else float("nan")
    return overall, per_col


# =========================
# Main: multiple events + csv
# =========================
def validate_events_nse(calibrated_inp, event_paths, obs_csv_paths):
    if len(event_paths) != len(obs_csv_paths):
        raise ValueError("event_paths and obs_csv_paths must have the same length")

    results = []

    for i in range(len(event_paths)):
        event_path = event_paths[i]
        obs_csv = obs_csv_paths[i]

        print(f"\n=== Event {i+1}/{len(event_paths)} ===")
        print(f"[1/5] Reading event: {os.path.basename(event_path)}")
        rain_df = read_rain_event(event_path)
        print(f"      {rain_df['Time'].iloc[0]} -> {rain_df['Time'].iloc[-1]}  points={len(rain_df)}")

        print(f"[2/5] Reading observations: {os.path.basename(obs_csv)}")
        obs_times, obs_cols, obs_df = read_obs_csv(obs_csv)
        specs = parse_obs_specs(obs_cols)
        print(f"      cols={len(obs_cols)}  T={len(obs_times)}  obs {obs_times.iloc[0]} -> {obs_times.iloc[-1]}")

        print("[3/5] Building event-specific INP (write TIMESERIES + update OPTIONS period) and running SWMM...")

        td = tempfile.mkdtemp(prefix="swmm_event_")  # Avoid TemporaryDirectory to prevent WinError 32
        try:
            work_inp = os.path.join(td, "work_event.inp")
            shutil.copy2(calibrated_inp, work_inp)

            prepare_inp_for_event(calibrated_inp, work_inp, rain_df, obs_times)
            sim_df = run_swmm_sample(work_inp, specs, obs_times)

            print("[4/5] Computing NSE...")
            overall, per_col = compute_event_nse(obs_df, sim_df, obs_cols)
            print(f"      NSE overall = {overall:.4f}")
            for col in sorted(per_col.keys()):
                print(f"      - {col}: {per_col[col]:.4f}")

            print("[5/5] Event completed")
            results.append({
                "event": event_path,
                "obs_csv": obs_csv,
                "nse_overall": overall,
                "nse_by_column": per_col,
            })

        except Exception as e:
            print(f"[ERROR] Event failed: {e}")
            print(f"        Temporary directory kept for troubleshooting: {td}")
            raise
        else:
            # Clean up only after a successful run
            shutil.rmtree(td, ignore_errors=True)

    return results


def run_validate(calibrated_inp, event_paths, obs_csv_paths) -> str:
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        results = validate_events_nse(
            calibrated_inp=calibrated_inp,
            event_paths=event_paths,
            obs_csv_paths=obs_csv_paths,
        )
    progress = buf.getvalue()
    lines = ["Validation complete.\n"]
    for r in results:
        lines.append(f"Event: {r['event']}")
        lines.append(f"  NSE overall: {r['nse_overall']:.4f}")
        for col, val in r["nse_by_column"].items():
            lines.append(f"  - {col}: {val:.4f}")
    summary = (progress.rstrip("\n") + "\n" if progress.strip() else "") + "\n".join(lines)
    print(summary)
    return summary


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Validate calibrated SWMM model against independent events (NSE)")
    ap.add_argument("--calibrated_inp", required=True, help="Path to calibrated SWMM .inp file")
    ap.add_argument("--event_paths", nargs="+", required=True, help="Rainfall event .txt file paths")
    ap.add_argument("--obs_csv_paths", nargs="+", required=True, help="Observation CSV paths (same length as event_paths)")
    args = ap.parse_args()

    run_validate(
        calibrated_inp=args.calibrated_inp,
        event_paths=args.event_paths,
        obs_csv_paths=args.obs_csv_paths,
    )
