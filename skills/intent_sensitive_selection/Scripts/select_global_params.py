import argparse
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import pandas as pd
import numpy as np

# Allow running this file directly by ensuring project root is on sys.path.
if __package__ in (None, ""):
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    _PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "..", ".."))
    if _PROJECT_ROOT not in sys.path:
        sys.path.insert(0, _PROJECT_ROOT)

from skills.calibrate.Scripts.swmm_utils import (
    PARAM_NAMES,
    SwmmForward,
    SwmmInpEditor,
    load_obs_csv,
    make_bounds,
    nse,
    parse_obs_specs,
)
from skills.calibrate.Scripts.inp_time_utils import prepare_inp_for_event, read_rain_event

N_PARAMS = 11

INTENT_CANDIDATES: Dict[str, List[int]] = {
    "peak": [0, 1, 2, 3, 5, 7, 8],
    "volume": [2, 4, 6, 8, 9, 10],
    "hydrograph_shape": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
    "infiltration": [8, 9, 10],
    "balanced": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
}


@dataclass
class EventContext:
    prepared_inp: str
    specs: List
    obs_times: pd.Series
    d_obs: np.ndarray
    width_m0: Dict[str, float]


def resolve_candidates(intent: str, infiltration_model: str) -> Tuple[List[int], Optional[str]]:
    if intent not in INTENT_CANDIDATES:
        raise ValueError(f"Unsupported intent: {intent}")

    note = None
    candidates = list(INTENT_CANDIDATES[intent])

    if intent == "infiltration" and infiltration_model.lower() == "green-ampt":
        note = "green-ampt requested, but this 11-parameter script maps infiltration intent to [8, 9, 10]."

    return candidates, note


def build_event_contexts(
    base_inp: str,
    obs_csv_paths: List[str],
    event_paths: List[str],
    subcatchments: List[str],
) -> Tuple[str, List[EventContext]]:
    if len(obs_csv_paths) != len(event_paths):
        raise ValueError("obs_csv_paths and event_paths must have the same length")
    if not obs_csv_paths:
        raise ValueError("obs_csv_paths and event_paths cannot be empty")

    root_dir = tempfile.mkdtemp(prefix="sensitivity_events_")
    contexts: List[EventContext] = []

    for i, (obs_csv, event_txt) in enumerate(zip(obs_csv_paths, event_paths), start=1):
        obs_times, obs_cols, d_obs, _ = load_obs_csv(obs_csv)
        specs = parse_obs_specs(obs_cols)

        rain_df = read_rain_event(event_txt)
        prepared_inp = os.path.join(root_dir, f"prepared_event_{i:03d}.inp")
        prepare_inp_for_event(base_inp, prepared_inp, rain_df, obs_times)

        _, _, w0 = make_bounds(prepared_inp, subcatchments, param_indices=None)
        contexts.append(
            EventContext(
                prepared_inp=prepared_inp,
                specs=specs,
                obs_times=obs_times,
                d_obs=d_obs,
                width_m0=w0,
            )
        )

    return root_dir, contexts


def vector_from_u(
    u: np.ndarray,
    x0: np.ndarray,
    lb: np.ndarray,
    ub: np.ndarray,
    subcatchments: List[str],
    candidate_indices: List[int],
) -> np.ndarray:
    x = x0.copy()
    n_sc = len(subcatchments)

    for local_idx, param_idx in enumerate(candidate_indices):
        ui = float(np.clip(u[local_idx], 0.0, 1.0))
        for sc_idx in range(n_sc):
            full_idx = sc_idx * N_PARAMS + param_idx
            x[full_idx] = lb[full_idx] + ui * (ub[full_idx] - lb[full_idx])

    return x


def evaluate_objective(
    u: np.ndarray,
    cache: Dict[Tuple[float, ...], float],
    contexts: List[EventContext],
    x0: np.ndarray,
    lb: np.ndarray,
    ub: np.ndarray,
    subcatchments: List[str],
    candidate_indices: List[int],
) -> float:
    key = tuple(np.round(u, 8).tolist())
    if key in cache:
        return cache[key]

    x = vector_from_u(u, x0, lb, ub, subcatchments, candidate_indices)

    nse_vals: List[float] = []
    for ctx in contexts:
        with tempfile.TemporaryDirectory(prefix="sensitivity_eval_") as td:
            run_inp = os.path.join(td, "run.inp")
            ed = SwmmInpEditor(ctx.prepared_inp)
            ed.apply_params_case1_11(subcatchments, x, ctx.width_m0)
            ed.write(run_inp)
            sim = SwmmForward(run_inp, ctx.specs, ctx.obs_times).run()
        nse_vals.append(float(nse(sim, ctx.d_obs)))

    mean_nse = float(np.nanmean(nse_vals)) if nse_vals else float("nan")
    objective = float("inf") if np.isnan(mean_nse) else float(1.0 - mean_nse)
    cache[key] = objective
    return objective


def morris_ranking(
    contexts: List[EventContext],
    subcatchments: List[str],
    candidate_indices: List[int],
    x0: np.ndarray,
    lb: np.ndarray,
    ub: np.ndarray,
    r: int,
    delta: float,
    seed: int,
) -> Tuple[List[Dict], int]:
    k = len(candidate_indices)
    if k == 0:
        raise ValueError("No candidate parameters to analyze")
    if r <= 0:
        raise ValueError("r must be positive")
    if not (0.0 < delta < 1.0):
        raise ValueError("delta must be in (0, 1)")

    rng = np.random.default_rng(seed)
    ee_by_param: Dict[int, List[float]] = {idx: [] for idx in candidate_indices}
    cache: Dict[Tuple[float, ...], float] = {}

    for _ in range(r):
        x = rng.random(k)
        order = rng.permutation(k)

        fx = evaluate_objective(x, cache, contexts, x0, lb, ub, subcatchments, candidate_indices)

        for local_idx in order:
            step = delta if x[local_idx] <= (1.0 - delta) else -delta
            x_new = x.copy()
            x_new[local_idx] = float(np.clip(x_new[local_idx] + step, 0.0, 1.0))

            dx = x_new[local_idx] - x[local_idx]
            if abs(dx) < 1e-12:
                continue

            fx_new = evaluate_objective(x_new, cache, contexts, x0, lb, ub, subcatchments, candidate_indices)
            ee = float((fx_new - fx) / dx)

            global_param_idx = candidate_indices[local_idx]
            ee_by_param[global_param_idx].append(ee)

            x = x_new
            fx = fx_new

    ranking: List[Dict] = []
    for idx in candidate_indices:
        arr = np.asarray(ee_by_param[idx], dtype=float)
        mu_star = float(np.mean(np.abs(arr))) if arr.size else 0.0
        sigma = float(np.std(arr, ddof=1)) if arr.size > 1 else 0.0
        ranking.append(
            {
                "idx": idx,
                "name": PARAM_NAMES[idx],
                "mu_star": mu_star,
                "sigma": sigma,
            }
        )

    ranking.sort(key=lambda x: (x["mu_star"], x["sigma"]), reverse=True)
    for rank, row in enumerate(ranking, start=1):
        row["rank"] = rank

    return ranking, len(cache)


def select_global_params(
    base_inp: str,
    obs_csv_paths: List[str],
    event_paths: List[str],
    subcatchments: List[str],
    intent: str,
    top_k: int,
    infiltration_model: str = "horton",
    method: str = "morris",
    r: int = 20,
    delta: float = 0.2,
    seed: int = 7,
) -> Dict:
    if method.lower() != "morris":
        raise ValueError("Only method='morris' is currently implemented")

    candidate_indices, note = resolve_candidates(intent.lower(), infiltration_model.lower())
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    if top_k > len(candidate_indices):
        raise ValueError(f"top_k={top_k} exceeds candidate pool size={len(candidate_indices)}")

    t0 = time.time()
    root_dir, contexts = build_event_contexts(base_inp, obs_csv_paths, event_paths, subcatchments)

    try:
        reference_inp = contexts[0].prepared_inp
        lb, ub, _ = make_bounds(reference_inp, subcatchments, param_indices=None)
        x0 = SwmmInpEditor(reference_inp).read_initial_params(subcatchments)

        ranking, n_evaluations = morris_ranking(
            contexts=contexts,
            subcatchments=subcatchments,
            candidate_indices=candidate_indices,
            x0=x0,
            lb=lb,
            ub=ub,
            r=r,
            delta=delta,
            seed=seed,
        )
    finally:
        shutil.rmtree(root_dir, ignore_errors=True)

    selected = [row["idx"] for row in ranking[:top_k]]
    selected_names = [PARAM_NAMES[i] for i in selected]

    result = {
        "intent": intent.lower(),
        "infiltration_model": infiltration_model.lower(),
        "method": method.lower(),
        "candidate_param_indices": candidate_indices,
        "selected_param_indices": selected,
        "selected_param_names": selected_names,
        "param_indices_cli": "--param_indices " + " ".join(str(i) for i in selected),
        "ranking": ranking,
        "diagnostics": {
            "n_events": len(event_paths),
            "n_evaluations": n_evaluations,
            "r": r,
            "delta": delta,
            "seed": seed,
            "runtime_sec": round(time.time() - t0, 3),
        },
    }
    if note is not None:
        result["note"] = note

    return result


def print_result(result: Dict) -> None:
    print("Global parameter selection complete.")
    print(f"Intent: {result['intent']}")
    print(f"Infiltration model: {result['infiltration_model']}")
    print(f"Candidates: {result['candidate_param_indices']}")
    print(f"Selected indices: {result['selected_param_indices']}")
    print(f"Selected names: {result['selected_param_names']}")
    print(f"CLI: {result['param_indices_cli']}")
    diagnostics = result.get("diagnostics", {})
    print(
        f"Diagnostics: events={diagnostics.get('n_events')}, "
        f"evaluations={diagnostics.get('n_evaluations')}, "
        f"runtime_sec={diagnostics.get('runtime_sec')}"
    )
    note = result.get("note")
    if note:
        print(f"Note: {note}")

    print("Ranking:")
    for row in result.get("ranking", []):
        print(
            f"  rank={row['rank']:>2}  idx={row['idx']:>2}  name={row['name']:<12}  "
            f"mu_star={row['mu_star']:.6g}  sigma={row['sigma']:.6g}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description="Intent-driven global parameter selection for SWMM")
    ap.add_argument("--base_inp", required=True)
    ap.add_argument("--obs_csv_paths", nargs="+", required=True)
    ap.add_argument("--event_paths", nargs="+", required=True)
    ap.add_argument("--subcatchments", nargs="+", required=True)
    ap.add_argument("--intent", required=True, choices=["peak", "volume", "hydrograph_shape", "infiltration", "balanced"])
    ap.add_argument("--top_k", type=int, required=True)
    ap.add_argument("--infiltration_model", default="horton", choices=["horton", "green-ampt"])
    ap.add_argument("--method", default="morris", choices=["morris"])
    ap.add_argument("--r", type=int, default=20)
    ap.add_argument("--delta", type=float, default=0.2)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    result = select_global_params(
        base_inp=args.base_inp,
        obs_csv_paths=args.obs_csv_paths,
        event_paths=args.event_paths,
        subcatchments=args.subcatchments,
        intent=args.intent,
        top_k=args.top_k,
        infiltration_model=args.infiltration_model,
        method=args.method,
        r=args.r,
        delta=args.delta,
        seed=args.seed,
    )
    print_result(result)


if __name__ == "__main__":
    main()
