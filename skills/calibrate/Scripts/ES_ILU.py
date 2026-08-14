import os
import sys
import shutil
import tempfile
from typing import List, Tuple, Dict, Optional, Union

import numpy as np
import pandas as pd

# Allow running this file directly (`python ...\ES_ILU.py`) by ensuring the
# project root is on sys.path for absolute `skills.*` imports.
if __package__ in (None, ""):
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    _PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "..", ".."))
    if _PROJECT_ROOT not in sys.path:
        sys.path.insert(0, _PROJECT_ROOT)

from skills.calibrate.Scripts.inp_time_utils import read_rain_event, prepare_inp_for_event
from skills.calibrate.Scripts.swmm_utils import (
    PARAM_NAMES, RANGES, NODE_VARS, LINK_VARS, TIME_TOL_SEC, SIGMA_REL,
    ObsSpec, _resolve_param_indices, parse_obs_specs, nse, load_obs_csv,
    SwmmForward, SwmmInpEditor,
)


# =========================
# Paper-consistent constants (Case 1)
# =========================
NE = 300
NITER = 10
BETA = 0.2
SEED = 7


def make_C_D(d_obs: np.ndarray, sigma_rel: float = SIGMA_REL) -> np.ndarray:
    std = sigma_rel * np.maximum(np.abs(d_obs), 1e-6)
    return np.diag(std * std)


# =========================
# ES-ILU specific bounds builder
# =========================
def make_bounds_for_88(
    subcatchments: List[str],
    width_m0: Dict[str, float],
    param_indices: Optional[List[int]] = None,
    initial_params: Optional[np.ndarray] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    lower = []
    upper = []
    for sc in subcatchments:
        w0 = width_m0[sc]
        lower.extend([
            0.5 * w0, RANGES["slope"][0], RANGES["imperv"][0],
            RANGES["n_imperv"][0], RANGES["n_perv"][0],
            RANGES["dstor_imperv"][0], RANGES["dstor_perv"][0],
            RANGES["zero_imperv"][0], RANGES["maxrate"][0],
            RANGES["minrate"][0], RANGES["decay"][0],
        ])
        upper.extend([
            1.5 * w0, RANGES["slope"][1], RANGES["imperv"][1],
            RANGES["n_imperv"][1], RANGES["n_perv"][1],
            RANGES["dstor_imperv"][1], RANGES["dstor_perv"][1],
            RANGES["zero_imperv"][1], RANGES["maxrate"][1],
            RANGES["minrate"][1], RANGES["decay"][1],
        ])
    lb = np.array(lower, dtype=float)
    ub = np.array(upper, dtype=float)

    if param_indices is not None and initial_params is not None:
        frozen = set(range(11)) - set(param_indices)
        for k in range(len(subcatchments)):
            base = k * 11
            for pi in frozen:
                lb[base + pi] = initial_params[base + pi]
                ub[base + pi] = initial_params[base + pi]

    return lb, ub


# =========================
# ES-ILU core
# =========================
def es_ilu_step(
    M: np.ndarray,
    G: np.ndarray,
    d_obs: np.ndarray,
    C_D: np.ndarray,
    rng: np.random.Generator,
) -> np.ndarray:
    m_dim, Ne = M.shape
    d_dim = d_obs.shape[0]
    N_L = max(2, min(int(np.floor(BETA * Ne)), Ne))

    diag = np.diag(C_D)
    C_D_inv = np.diag(1.0 / np.maximum(diag, 1e-12))
    C_D_sqrt = np.diag(np.sqrt(np.maximum(diag, 0.0)))

    M_anom = M - M.mean(axis=1, keepdims=True)
    C_MM = (M_anom @ M_anom.T) / (Ne - 1)
    C_MM_inv = np.linalg.inv(C_MM + 1e-10 * np.eye(m_dim))

    M_new = np.zeros_like(M)

    for j in range(Ne):
        m_j = M[:, j]
        J1 = np.zeros(Ne, dtype=float)
        J2 = np.zeros(Ne, dtype=float)

        for k in range(Ne):
            e = (d_obs - G[:, k]).reshape(d_dim, 1)
            J1[k] = float((e.T @ C_D_inv @ e)[0, 0])
            dm = (M[:, k] - m_j).reshape(m_dim, 1)
            J2[k] = float((dm.T @ C_MM_inv @ dm)[0, 0])

        J1_max = np.max(J1) if np.max(J1) > 0 else 1.0
        J2_max = np.max(J2) if np.max(J2) > 0 else 1.0
        J = (J1 / J1_max) + (J2 / J2_max)

        local_idx = np.argsort(J)[:N_L]
        M_loc = M[:, local_idx]
        G_loc = G[:, local_idx]

        M_loc_anom = M_loc - M_loc.mean(axis=1, keepdims=True)
        G_loc_anom = G_loc - G_loc.mean(axis=1, keepdims=True)
        C_MG = (M_loc_anom @ G_loc_anom.T) / (N_L - 1)
        C_GG = (G_loc_anom @ G_loc_anom.T) / (N_L - 1)

        z = rng.standard_normal(d_dim)
        d_j = d_obs + np.sqrt(float(NITER)) * (C_D_sqrt @ z)     # alpha_i = NITER
        A = C_GG + float(NITER) * C_D + 1e-10 * np.eye(d_dim)

        M_loc_a = np.zeros_like(M_loc)
        for kk in range(N_L):
            innov = (d_j - G_loc[:, kk]).reshape(d_dim, 1)
            delta = C_MG @ np.linalg.solve(A, innov)
            M_loc_a[:, kk] = (M_loc[:, kk].reshape(m_dim, 1) + delta).reshape(m_dim)

        pick = int(rng.integers(0, N_L))
        M_new[:, j] = M_loc_a[:, pick]

    return M_new


def select_best_member(G: np.ndarray, d_obs: np.ndarray, C_D: np.ndarray) -> int:
    w = 1.0 / np.maximum(np.diag(C_D), 1e-12)
    best = 0
    best_cost = np.inf
    for j in range(G.shape[1]):
        e = d_obs - G[:, j]
        cost = float(np.sum(w * (e * e)))
        if cost < best_cost:
            best_cost = cost
            best = j
    return best


# =========================
# Main API
# =========================
def calibrate_es_ilu_case1(
    inp_path: str,
    obs_csv: str,
    event_txt: str,
    subcatchments: List[str],
    out_inp: str,
    param_indices: Optional[List[Union[int, str]]] = None,
    sigma_rel: float = SIGMA_REL,
    niter: int = NITER,
    ne: int = NE,
) -> str:
    log_path = out_inp.rsplit('.', 1)[0] + '_progress.log'
    _logf = open(log_path, 'w', encoding='utf-8')
    def _log(msg): _logf.write(msg + '\n'); _logf.flush()

    resolved_indices = None
    if param_indices is not None:
        resolved_indices = _resolve_param_indices(param_indices)
        active_names = [PARAM_NAMES[i] for i in resolved_indices]
        _log(f"[ES-ILU] Selective calibration: active params = {resolved_indices} ({active_names})")
    else:
        _log("[ES-ILU] Calibrating all 11 parameters per subcatchment")

    if len(subcatchments) != 8:
        _log(f"[WARN] Paper Case 1 uses 8 subcatchments; you provided {len(subcatchments)}. Continuing.")

    _log("[1/7] Reading observation CSV...")
    obs_times, obs_cols, d_obs, obs_df = load_obs_csv(obs_csv)

    # Prepare base INP with correct timeseries & time window
    rain_df = read_rain_event(event_txt)
    prepared_dir = tempfile.mkdtemp(prefix="esilu_prepared_")
    prepared_inp = os.path.join(prepared_dir, "prepared_base.inp")
    prepare_inp_for_event(inp_path, prepared_inp, rain_df, obs_times)
    inp_path = prepared_inp  # use prepared INP for all subsequent operations
    _log(f"[ES-ILU] Prepared INP with event timeseries and time window from {event_txt}")
    specs = parse_obs_specs(obs_cols)
    C_D = make_C_D(d_obs, sigma_rel)
    _log(f"      observation points={len(obs_cols)}, samples per point={len(obs_times)}, observation vector size={d_obs.size}")

    _log("[2/7] Reading Width m0 for each subcatchment from INP (used for Width +/-50% bounds)...")
    base_editor = SwmmInpEditor(inp_path)
    width_m0 = base_editor.read_width_m0(subcatchments)
    initial_params = None
    if resolved_indices is not None:
        initial_params = base_editor.read_initial_params(subcatchments)
    lower, upper = make_bounds_for_88(
        subcatchments, width_m0,
        param_indices=resolved_indices,
        initial_params=initial_params,
    )
    m_dim = lower.size
    _log(f"      parameter dimension={m_dim} (= {len(subcatchments)}x11)")

    rng = np.random.default_rng(SEED)
    _log("[3/7] Initialising prior ensemble (uniform sampling within paper ranges)...")
    M = lower.reshape(-1, 1) + (upper - lower).reshape(-1, 1) * rng.random((m_dim, ne))

    for it in range(1, niter + 1):
        _log(f"[4/7] Iter {it}/{niter}: Forward simulation, NE={ne} runs (most time-consuming step)...")
        G = np.zeros((d_obs.size, ne), dtype=float)

        for j in range(ne):
            if (j + 1) % 10 == 0 or j == 0 or (j + 1) == ne:
                _log(f"      - running member {j+1}/{ne}")

            with tempfile.TemporaryDirectory() as td:
                work_inp = os.path.join(td, "work.inp")
                shutil.copy2(inp_path, work_inp)
                ed = SwmmInpEditor(work_inp)
                ed.apply_params_case1_11(subcatchments, M[:, j], width_m0)
                ed.write(work_inp)
                fw = SwmmForward(work_inp, specs, obs_times)
                G[:, j] = fw.run()

        nse_vals = np.array([nse(G[:, j], d_obs) for j in range(ne)], dtype=float)
        nse_mean = float(np.nanmean(nse_vals))
        nse_std = float(np.nanstd(nse_vals, ddof=1))
        _log(f"      NSE mean={nse_mean:.6f}, NSE std={nse_std:.6f}")

        _log(f"[5/7] Iter {it}/{niter}: ES-ILU update (alpha=niter, beta=0.2)...")
        M = es_ilu_step(M, G, d_obs, C_D, rng)

        # clip to bounds
        M = np.minimum(np.maximum(M, lower.reshape(-1, 1)), upper.reshape(-1, 1))

    _log("[6/7] Selecting best member and exporting calibrated INP...")
    _log("      - final forward runs for best selection...")
    G_final = np.zeros((d_obs.size, ne), dtype=float)
    for j in range(ne):
        if (j + 1) % 20 == 0 or j == 0 or (j + 1) == ne:
            _log(f"      - final member {j+1}/{ne}")
        with tempfile.TemporaryDirectory() as td:
            work_inp = os.path.join(td, "work.inp")
            shutil.copy2(inp_path, work_inp)
            ed = SwmmInpEditor(work_inp)
            ed.apply_params_case1_11(subcatchments, M[:, j], width_m0)
            ed.write(work_inp)
            fw = SwmmForward(work_inp, specs, obs_times)
            G_final[:, j] = fw.run()

    best_idx = select_best_member(G_final, d_obs, C_D)
    m_best = M[:, best_idx].copy()

    shutil.copy2(inp_path, out_inp)
    out_editor = SwmmInpEditor(out_inp)
    out_editor.apply_params_case1_11(subcatchments, m_best, width_m0)
    out_editor.write(out_inp)

    shutil.rmtree(prepared_dir, ignore_errors=True)
    _logf.close()
    param_info = "all 11"
    if resolved_indices is not None:
        active_names = [PARAM_NAMES[i] for i in resolved_indices]
        param_info = f"{resolved_indices} ({active_names})"
    summary = (
        f"ES-ILU calibration complete.\n"
        f"Ensemble: NE={ne}, Iterations: {niter}\n"
        f"Best member: {best_idx}/{ne}\n"
        f"Subcatchments: {subcatchments}\n"
        f"Calibrated params: {param_info}\n"
        f"Output INP: {out_inp}\n"
    )
    print(summary)
    return summary


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        description="SWMM calibration using ES-ILU (Ensemble Smoother with Iterative Local Updating)"
    )
    ap.add_argument("--inp_path", required=True, help="Path to the base SWMM .inp file")
    ap.add_argument("--obs_csv", required=True, help="Path to observation CSV")
    ap.add_argument("--event_txt", required=True,
                    help="Path to rainfall event .txt file (semicolon-separated)")
    ap.add_argument("--subcatchments", nargs="+", required=True,
                    help="List of subcatchment IDs")
    ap.add_argument("--out_inp", required=True, help="Output path for calibrated INP")
    ap.add_argument("--param_indices", nargs="+", type=int, default=None,
                    help="Parameter indices (0-10) to calibrate. Omit for all 11.")
    ap.add_argument("--sigma_rel", type=float, default=SIGMA_REL,
                    help="Relative measurement error std dev (default: 0.033)")
    ap.add_argument("--niter", type=int, default=NITER,
                    help="Number of assimilation iterations (default: 10)")
    ap.add_argument("--ne", type=int, default=NE,
                    help="Ensemble size (default: 300)")
    args = ap.parse_args()

    calibrate_es_ilu_case1(
        inp_path=args.inp_path,
        obs_csv=args.obs_csv,
        event_txt=args.event_txt,
        subcatchments=args.subcatchments,
        out_inp=args.out_inp,
        param_indices=args.param_indices,
        sigma_rel=args.sigma_rel,
        niter=args.niter,
        ne=args.ne,
    )
