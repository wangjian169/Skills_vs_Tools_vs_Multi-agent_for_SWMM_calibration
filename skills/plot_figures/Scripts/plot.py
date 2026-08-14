# -*- coding: utf-8 -*-
"""
plot.py

Based on the v2 implementation, this version follows validate_events_nse to fix how event-specific INP files are built:
- It no longer attempts to replace FILE paths in RAINGAGES/TIMESERIES (this fails when Source=TIMESERIES with embedded data).
- Instead, it directly overwrites all rows of TS_NAME (default RG_TS) in [TIMESERIES] using rainfall parsed from event.txt.
- Run period: forcibly set [OPTIONS] START/END/REPORT_START from the observation time window (obs_times start/end).
Outputs two figures:
1) Time-series panel: (a) calibration and (b) validation.
   - Observations: scatter. Calibrated methods: line plots (same axes for comparison).
2) Parameter comparison panel: one subplot per parameter (Case-1, 11 parameters).
"""

import os
import sys
import tempfile
from dataclasses import dataclass
from typing import List, Tuple, Dict, Optional

import numpy as np
import pandas as pd
if "MPLCONFIGDIR" not in os.environ:
    _mpl = os.path.join(tempfile.gettempdir(), "swmm_agent_mplconfig")
    os.makedirs(_mpl, exist_ok=True)
    os.environ["MPLCONFIGDIR"] = _mpl

os.environ.setdefault("MPLBACKEND", "Agg")

# True/reference SWMM model — always the project root Example1.inp
_TRUE_INP = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "Example1.inp")
)

import matplotlib.pyplot as plt
from pyswmm import Simulation, Nodes

# Allow running this file directly (`python ...\plot.py`) by ensuring the
# project root is on sys.path for absolute `skills.*` imports.
if __package__ in (None, ""):
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    _PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "..", ".."))
    if _PROJECT_ROOT not in sys.path:
        sys.path.insert(0, _PROJECT_ROOT)

from skills.calibrate.Scripts.inp_time_utils import (
    read_rain_event as _read_rain_event_util,
    prepare_inp_for_event as _prepare_inp_for_event_util,
)

TIME_TOL_SEC = 60  # Time-alignment tolerance (seconds)
EVENT_SEP = ";"    # Default separator in event.txt
TS_NAME = "RG_TS"  # TIMESERIES name referenced by INP [RAINGAGES]
NODE_VARS = {"depth", "head", "volume", "lateral_inflow", "total_inflow", "flooding"}
# -----------------------------
# Observation handling
# -----------------------------
@dataclass
class ObsSeries:
    times: pd.Series
    values: np.ndarray
    col: str


def load_node_observation(obs_csv: str, node_id: str) -> ObsSeries:
    df = pd.read_csv(obs_csv)
    if df.shape[1] < 2:
        raise ValueError("observation.csv must contain at least two columns: time column + observation column")

    tcol = df.columns[0]
    df[tcol] = pd.to_datetime(df[tcol], errors="coerce")
    df = df.dropna(subset=[tcol]).sort_values(tcol).reset_index(drop=True)

    prefix = f"node_{node_id}_"
    candidates = [c for c in df.columns[1:] if str(c).startswith(prefix)]
    if not candidates:
        raise ValueError(f"No observation column found for node {node_id} (expected pattern like {prefix}depth)")

    col = candidates[0]
    return ObsSeries(times=df[tcol], values=df[col].to_numpy(dtype=float), col=col)


def infer_node_var_from_col(col: str) -> str:
    toks = str(col).split("_")
    if len(toks) < 3:
        raise ValueError(f"Cannot infer variable from column name: {col}")
    var = "_".join(toks[2:]).lower()
    if var not in NODE_VARS:
        raise ValueError(f"Column variable is not in the supported set: {var}")
    return var


# -----------------------------
# INP utilities
# -----------------------------
_TEMP_REGISTRY: List[tempfile.TemporaryDirectory] = []


def read_inp_lines(inp_path: str) -> List[str]:
    with open(inp_path, "r", encoding="utf-8", errors="ignore") as f:
        return f.readlines()


def write_inp_lines(lines: List[str], out_path: str) -> None:
    with open(out_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


def find_section_bounds(lines: List[str], section: str) -> Tuple[Optional[int], Optional[int]]:
    header = f"[{section}]".upper()
    start = None
    end = None
    for i, line in enumerate(lines):
        if line.strip().upper() == header:
            start = i + 1
            break
    if start is None:
        return None, None
    for j in range(start, len(lines)):
        s = lines[j].strip()
        if s.startswith("[") and s.endswith("]"):
            end = j
            break
    if end is None:
        end = len(lines)
    return start, end


def prepare_inp_for_event(base_inp: str, event_txt: str, obs_times: pd.Series) -> str:
    """
    Build an event-specific INP using swmm-api via inp_time_utils.
    Correctly updates TIMESERIES, RAINGAGES, and OPTIONS.
    """
    rain_df = _read_rain_event_util(event_txt, sep=EVENT_SEP)

    obs_times = pd.to_datetime(obs_times).dropna()
    if obs_times.empty:
        raise ValueError("obs_times is empty; cannot set the run time window")

    td = tempfile.TemporaryDirectory()
    _TEMP_REGISTRY.append(td)
    out_inp = os.path.join(td.name, "event_run.inp")
    _prepare_inp_for_event_util(base_inp, out_inp, rain_df, obs_times)
    return out_inp


# -----------------------------
# SWMM run + alignment
# -----------------------------
def run_swmm_node_series(inp_path: str, node_id: str, var: str) -> pd.DataFrame:
    times: List[pd.Timestamp] = []
    vals: List[float] = []
    with Simulation(inp_path) as sim:
        nodes = Nodes(sim)
        node = nodes[str(node_id)]
        for _ in sim:
            times.append(pd.to_datetime(sim.current_time))
            if var == "depth":
                vals.append(float(node.depth))
            elif var == "head":
                vals.append(float(node.head))
            elif var == "volume":
                vals.append(float(node.volume))
            elif var == "lateral_inflow":
                vals.append(float(node.lateral_inflow))
            elif var == "total_inflow":
                vals.append(float(node.total_inflow))
            elif var == "flooding":
                vals.append(float(node.flooding))
            else:
                raise ValueError(f"Unsupported variable: {var}")
    return pd.DataFrame({"time": pd.to_datetime(times), "value": np.array(vals, dtype=float)}).sort_values("time")


def align_sim_to_obs(sim_df: pd.DataFrame, obs_times: pd.Series, time_tol_sec: int) -> np.ndarray:
    sim_df = sim_df.copy()
    sim_df["time64"] = pd.to_datetime(sim_df["time"]).dt.tz_localize(None).astype("datetime64[s]")
    obs_df = pd.DataFrame({"obs64": pd.to_datetime(obs_times).dt.tz_localize(None).astype("datetime64[s]")})

    merged = pd.merge_asof(
        obs_df.sort_values("obs64"),
        sim_df.sort_values("time64"),
        left_on="obs64",
        right_on="time64",
        direction="nearest",
        tolerance=pd.Timedelta(seconds=time_tol_sec),
    )
    if merged["value"].isna().any():
        raise RuntimeError("Time alignment failed: check SWMM timestep/observation timestamps or increase time_tol_sec")
    return merged["value"].to_numpy(dtype=float)


# -----------------------------
# Parameter extraction
# -----------------------------
def extract_subcatchment_params(inp_path: str) -> Tuple[List[str], Dict[str, Dict[str, float]]]:
    lines = read_inp_lines(inp_path)
    params: Dict[str, Dict[str, float]] = {
        "width": {},
        "slope": {},
        "imperv": {},
        "n_imperv": {},
        "n_perv": {},
        "dstor_imperv": {},
        "dstor_perv": {},
        "zero_imperv": {},
        "maxrate": {},
        "minrate": {},
        "decay": {},
    }
    subs: List[str] = []

    ss, se = find_section_bounds(lines, "SUBCATCHMENTS")
    if ss is not None:
        for i in range(ss, se):
            s = lines[i].strip()
            if (not s) or s.startswith(";") or (s.startswith("[") and s.endswith("]")):
                continue
            toks = s.split()
            if len(toks) < 7:
                continue
            sc = toks[0]
            if sc not in subs:
                subs.append(sc)
            params["imperv"][sc] = float(toks[4])
            params["width"][sc] = float(toks[5])
            params["slope"][sc] = float(toks[6])

    sa, sb = find_section_bounds(lines, "SUBAREAS")
    if sa is not None:
        for i in range(sa, sb):
            s = lines[i].strip()
            if (not s) or s.startswith(";") or (s.startswith("[") and s.endswith("]")):
                continue
            toks = s.split()
            if len(toks) < 6:
                continue
            sc = toks[0]
            params["n_imperv"][sc] = float(toks[1])
            params["n_perv"][sc] = float(toks[2])
            params["dstor_imperv"][sc] = float(toks[3])
            params["dstor_perv"][sc] = float(toks[4])
            params["zero_imperv"][sc] = float(toks[5])

    infs, infe = find_section_bounds(lines, "INFILTRATION")
    if infs is not None:
        for i in range(infs, infe):
            s = lines[i].strip()
            if (not s) or s.startswith(";") or (s.startswith("[") and s.endswith("]")):
                continue
            toks = s.split()
            if len(toks) < 4:
                continue
            sc = toks[0]
            params["maxrate"][sc] = float(toks[1])
            params["minrate"][sc] = float(toks[2])
            params["decay"][sc] = float(toks[3])

    subs_final = [sc for sc in subs if sc in params["width"]]
    return subs_final, params


# -----------------------------
# NSE helper
# -----------------------------
def _nse(obs: np.ndarray, sim: np.ndarray) -> float:
    denom = np.sum((obs - np.mean(obs)) ** 2)
    if denom <= 1e-12:
        return float("nan")
    return 1.0 - float(np.sum((obs - sim) ** 2) / denom)


# -----------------------------
# Plot figure 1
# -----------------------------
def plot_time_series_fig(
    calibrated_inps: List[str],
    node_id: str,
    calib_event_txt: str,
    valid_event_txt: str,
    calib_obs_csv: str,
    valid_obs_csv: str,
    out_dir: str,
    time_tol_sec: int,
    method_labels: Optional[List[str]] = None,
    fig_name: str = "fig1_timeseries_calib_valid.png",
) -> str:
    os.makedirs(out_dir, exist_ok=True)

    calib_obs = load_node_observation(calib_obs_csv, node_id)
    valid_obs = load_node_observation(valid_obs_csv, node_id)

    calib_var = infer_node_var_from_col(calib_obs.col)
    valid_var = infer_node_var_from_col(valid_obs.col)
    if calib_var != valid_var:
        raise ValueError(f"Calibration/validation observation variables do not match: {calib_var} vs {valid_var}")
    var = calib_var

    if method_labels is None:
        method_labels = [os.path.splitext(os.path.basename(p))[0] for p in calibrated_inps]
    if len(method_labels) != len(calibrated_inps):
        raise ValueError("method_labels and calibrated_inps must have the same length")

    calib_inp_for_run = [prepare_inp_for_event(p, calib_event_txt, calib_obs.times) for p in calibrated_inps]
    valid_inp_for_run = [prepare_inp_for_event(p, valid_event_txt, valid_obs.times) for p in calibrated_inps]

    calib_sims = []
    for p in calib_inp_for_run:
        sim_df = run_swmm_node_series(p, node_id, var)
        calib_sims.append(align_sim_to_obs(sim_df, calib_obs.times, time_tol_sec))

    valid_sims = []
    for p in valid_inp_for_run:
        sim_df = run_swmm_node_series(p, node_id, var)
        valid_sims.append(align_sim_to_obs(sim_df, valid_obs.times, time_tol_sec))

    fig, axes = plt.subplots(2, 1, figsize=(12, 8))
    ax0, ax1 = axes

    ax0.scatter(calib_obs.times, calib_obs.values, s=12, label="Observation")
    for y, lab in zip(calib_sims, method_labels):
        score = _nse(calib_obs.values, y)
        ax0.plot(calib_obs.times, y, label=f"{lab} (NSE={score:.3f})")
    ax0.set_title(f"(a) Calibration - node {node_id} ({var})")
    ax0.set_xlabel("Time")
    ax0.set_ylabel(var)
    ax0.legend()

    ax1.scatter(valid_obs.times, valid_obs.values, s=12, label="Observation")
    for y, lab in zip(valid_sims, method_labels):
        score = _nse(valid_obs.values, y)
        ax1.plot(valid_obs.times, y, label=f"{lab} (NSE={score:.3f})")
    ax1.set_title(f"(b) Validation - node {node_id} ({var})")
    ax1.set_xlabel("Time")
    ax1.set_ylabel(var)
    ax1.legend()

    fig.tight_layout()
    out_path = os.path.join(out_dir, fig_name)
    fig.savefig(out_path, dpi=200)
    plt.close(fig)
    return out_path


# -----------------------------
# Plot figure 2: params
# -----------------------------
_ALL_PARAM_NAMES = [
    "width", "slope", "imperv",
    "n_imperv", "n_perv", "dstor_imperv", "dstor_perv", "zero_imperv",
    "maxrate", "minrate", "decay",
]
_PARAM_INDEX_TO_NAME: Dict[int, str] = {i: n for i, n in enumerate(_ALL_PARAM_NAMES)}


def _detect_calibrated_params(
    p_true: Dict[str, Dict[str, float]],
    method_params: List[Dict[str, Dict[str, float]]],
    subs: List[str],
    tol: float = 1e-9,
) -> List[str]:
    """Return param names that differ between true and any calibrated INP."""
    result = []
    for pname in _ALL_PARAM_NAMES:
        changed = False
        for pm in method_params:
            for sc in subs:
                tv = p_true.get(pname, {}).get(sc, np.nan)
                cv = pm.get(pname, {}).get(sc, np.nan)
                if np.isnan(tv) or np.isnan(cv):
                    if not (np.isnan(tv) and np.isnan(cv)):
                        changed = True
                elif abs(tv - cv) > tol:
                    changed = True
                if changed:
                    break
            if changed:
                break
        if changed:
            result.append(pname)
    return result if result else _ALL_PARAM_NAMES  # fallback: show all


def plot_parameter_group_fig(
    calibrated_inps: List[str],
    out_dir: str,
    method_labels: Optional[List[str]] = None,
    fig_name: str = "fig2_params_group.png",
    calibrated_params: Optional[List[str]] = None,
) -> str:
    os.makedirs(out_dir, exist_ok=True)

    if method_labels is None:
        method_labels = [os.path.splitext(os.path.basename(p))[0] for p in calibrated_inps]
    if len(method_labels) != len(calibrated_inps):
        raise ValueError("method_labels and calibrated_inps must have the same length")

    subs_true, p_true = extract_subcatchment_params(_TRUE_INP)
    common = set(subs_true)

    method_params: List[Dict[str, Dict[str, float]]] = []
    for p in calibrated_inps:
        subs_m, pm = extract_subcatchment_params(p)
        common &= set(subs_m)
        method_params.append(pm)

    subs = [sc for sc in subs_true if sc in common]
    if not subs:
        raise ValueError("True INP and calibrated INP share no common subcatchments; cannot plot")

    if calibrated_params is not None:
        # Flatten: handle both ["slope", "imperv"] and ["slope imperv"] input forms
        flat = []
        for item in calibrated_params:
            if isinstance(item, str):
                flat.extend(item.split())
            else:
                flat.append(item)
        calibrated_params = flat
        lower_map = {n.lower(): n for n in _ALL_PARAM_NAMES}
        resolved = []
        skipped = []
        for p in calibrated_params:
            if isinstance(p, int):
                name = _PARAM_INDEX_TO_NAME.get(p)
                if name is not None:
                    resolved.append(name)
                else:
                    skipped.append(repr(p))
            else:
                canonical = lower_map.get(str(p).lower())
                if canonical is not None:
                    resolved.append(canonical)
                else:
                    skipped.append(repr(p))
        if skipped:
            print(
                f"[plot] WARNING: calibrated_params contains unrecognized entries "
                f"(will be ignored): {skipped}\n"
                f"  Valid names: {_ALL_PARAM_NAMES}"
            )
        param_names = resolved if resolved else _ALL_PARAM_NAMES
    else:
        param_names = _detect_calibrated_params(p_true, method_params, subs)

    n_params = len(param_names)
    ncols = 3
    nrows = int(np.ceil(n_params / ncols))

    fig, axes = plt.subplots(nrows, ncols, figsize=(16, 4.2 * nrows))
    axes = np.array(axes).reshape(-1)

    BAR_COLORS = ["#4D4D4D", "#4C72B0", "#DD8452", "#55A868"]

    x = np.arange(len(subs))
    bar_w = 0.8 / (1 + len(calibrated_inps))

    for idx, pname in enumerate(param_names):
        ax = axes[idx]
        y_true = np.array([p_true.get(pname, {}).get(sc, np.nan) for sc in subs], dtype=float)
        ax.bar(x - 0.4 + 0 * bar_w, y_true, width=bar_w, label="True",
               color=BAR_COLORS[0], edgecolor="white")

        for mi, (pm, lab) in enumerate(zip(method_params, method_labels), start=1):
            y_m = np.array([pm.get(pname, {}).get(sc, np.nan) for sc in subs], dtype=float)
            ax.bar(x - 0.4 + mi * bar_w, y_m, width=bar_w, label=lab,
                   color=BAR_COLORS[mi % len(BAR_COLORS)], edgecolor="white")

        ax.set_title(pname)
        ax.set_xticks(x)
        ax.set_xticklabels(subs, rotation=45, ha="right")
        ax.grid(True, axis="y", alpha=0.3)

    for j in range(n_params, axes.size):
        fig.delaxes(axes[j])

    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=min(5, 1 + len(calibrated_inps)))
    fig.tight_layout(rect=(0, 0, 1, 0.95))

    out_path = os.path.join(out_dir, fig_name)
    fig.savefig(out_path, dpi=200)
    plt.close(fig)
    return out_path


# -----------------------------
# Public API
# -----------------------------
def generate_calib_valid_figures(
    calibrated_inp_list: List[str],
    node_id: str,
    calib_event_txt: str,
    valid_event_txt: str,
    calib_obs_csv: str,
    valid_obs_csv: str,
    out_dir: str,
    method_labels: Optional[List[str]] = None,
    time_tol_sec: int = TIME_TOL_SEC,
    calibrated_params: Optional[List[str]] = None,
) -> Tuple[str, str]:
    fig1 = plot_time_series_fig(
        calibrated_inps=calibrated_inp_list,
        node_id=node_id,
        calib_event_txt=calib_event_txt,
        valid_event_txt=valid_event_txt,
        calib_obs_csv=calib_obs_csv,
        valid_obs_csv=valid_obs_csv,
        out_dir=out_dir,
        time_tol_sec=time_tol_sec,
        method_labels=method_labels,
        fig_name="fig1_timeseries_calib_valid.png",
    )
    fig2 = plot_parameter_group_fig(
        calibrated_inps=calibrated_inp_list,
        out_dir=out_dir,
        method_labels=method_labels,
        fig_name="fig2_params_group.png",
        calibrated_params=calibrated_params,
    )
    return fig1, fig2


def run_generate_plots(calibrated_inp_list, node_id,
                       calib_event_txt, valid_event_txt,
                       calib_obs_csv, valid_obs_csv, out_dir,
                       method_labels=None, time_tol_sec=TIME_TOL_SEC,
                       calibrated_params=None) -> str:
    fig1, fig2 = generate_calib_valid_figures(
        calibrated_inp_list=calibrated_inp_list,
        node_id=node_id, calib_event_txt=calib_event_txt,
        valid_event_txt=valid_event_txt, calib_obs_csv=calib_obs_csv,
        valid_obs_csv=valid_obs_csv, out_dir=out_dir,
        method_labels=method_labels, time_tol_sec=time_tol_sec,
        calibrated_params=calibrated_params,
    )
    summary = (
        f"Figures generated.\n"
        f"Time-series plot: {fig1}\n"
        f"Parameter comparison: {fig2}"
    )
    print(summary)
    return summary


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="SWMM calibration/validation plots (overwrite TIMESERIES + set OPTIONS period)")
    ap.add_argument("--calibrated_inps", nargs="+", default=["calibrated_model.inp", "best_ga.inp", "best_mh.inp"])
    ap.add_argument("--node_id", default="18")
    ap.add_argument("--calib_event", default="../WorkSpace/network/Events/event 1.txt")
    ap.add_argument("--valid_event", default="../WorkSpace/network/Events/event 2.txt")
    ap.add_argument("--calib_obs", default="../WorkSpace/observations/event 1.csv")
    ap.add_argument("--valid_obs", default="../WorkSpace/observations/event 2.csv")
    ap.add_argument("--out_dir", default="./figs")
    ap.add_argument("--labels", nargs="*", default=None)
    ap.add_argument("--time_tol_sec", type=int, default=TIME_TOL_SEC)
    ap.add_argument("--calibrated_params", nargs="*", default=None,
                    help="Subset of param names to plot; auto-detect if omitted")
    args = ap.parse_args()

    run_generate_plots(
        calibrated_inp_list=args.calibrated_inps,
        node_id=args.node_id,
        calib_event_txt=args.calib_event,
        valid_event_txt=args.valid_event,
        calib_obs_csv=args.calib_obs,
        valid_obs_csv=args.valid_obs,
        out_dir=args.out_dir,
        method_labels=args.labels,
        time_tol_sec=args.time_tol_sec,
        calibrated_params=args.calibrated_params,
    )
