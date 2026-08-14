# -*- coding: utf-8 -*-
"""
tool_wrappers.py

Wrapper functions shared by Architecture 2 (Single Agent with Tools) and
Architecture 3 (Multi Agents).

Each function:
- Keeps the exact signature of the original script entry function (parameter names, types, defaults).
- Uses docstrings aligned with TASK_INSTRUCTIONS to stay consistent with SKILL.md content.
- Lazily imports and calls the original script function internally.
- Returns a formatted string suitable for LLM consumption.
"""

import functools
import importlib
import os
import threading
import traceback
from pathlib import Path, PurePosixPath
from typing import List, Dict, Any, Optional, Union


_PROJECT_ROOT = Path(__file__).resolve().parent
_SWMM_SIMULATION_LOCK = threading.RLock()


def _safe_tool(func):
    """Decorator: catch exceptions and return error string instead of crashing."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            tb = traceback.format_exc()
            return f"[TOOL ERROR] {func.__name__} failed:\n{e}\n\nTraceback:\n{tb}"
    return wrapper


def _normalize_local_path(path: str) -> str:
    """Map virtual paths like /data/... to real project-absolute paths."""
    if not isinstance(path, str) or not path:
        return path

    raw = path.strip()
    if not raw:
        return raw

    if raw.startswith("/"):
        rel_parts = [p for p in PurePosixPath(raw).parts if p != "/"]
        return str((_PROJECT_ROOT.joinpath(*rel_parts)).resolve())

    if os.path.isabs(raw):
        return raw
    return str((_PROJECT_ROOT / raw).resolve())


# ===========================================================================
# 1. select_params  ->  select_global_params.select_global_params
# ===========================================================================
@_safe_tool
def select_params(
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
) -> str:
    """Select SWMM calibration parameters using intent-driven Morris sensitivity analysis.

    Runs Morris method to rank the 11 subcatchment parameters by their influence on the
    objective (1 - NSE) and returns the top-k most influential indices. The selected
    indices are global: every subcatchment uses the same set.

    Args:
        base_inp: Base SWMM INP path.
        obs_csv_paths: Observation CSV paths (one per event, paired with event_paths).
        event_paths: Rain event txt paths (one per event, paired with obs_csv_paths).
        subcatchments: Subcatchment IDs to calibrate.
        intent: Calibration intent — one of: peak, volume, hydrograph_shape,
            infiltration, balanced. Determines the candidate parameter pool.
            - peak: [0, 1, 2, 3, 5, 7, 8]
            - volume: [2, 4, 6, 8, 9, 10]
            - hydrograph_shape: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
            - infiltration (horton): [8, 9, 10]
            - balanced: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        top_k: Number of final selected parameter indices to return.
        infiltration_model: "horton" (default) or "green-ampt".
        method: Sensitivity method — only "morris" is currently implemented.
        r: Number of Morris trajectories (default 20).
        delta: Morris perturbation step in normalized space (default 0.2).
        seed: Random seed for reproducibility.

    Returns:
        Formatted string with selected_param_indices, selected_param_names,
        param_indices_cli, ranking, and diagnostics.

    Parameter index map:
        0=width, 1=slope, 2=imperv, 3=n_imperv, 4=n_perv,
        5=dstor_imperv, 6=dstor_perv, 7=zero_imperv,
        8=maxrate, 9=minrate, 10=decay.

    Time Handling Policy:
        Do not manually edit INP [OPTIONS] dates for event matching.
        This tool prepares event-specific INP files internally from each event_path
        and the corresponding obs_csv_paths timestamp range.
    """
    import io
    import contextlib
    from skills.intent_sensitive_selection.Scripts.select_global_params import (
        select_global_params,
        print_result,
    )

    base_inp = _normalize_local_path(base_inp)
    obs_csv_paths = [_normalize_local_path(p) for p in obs_csv_paths]
    event_paths = [_normalize_local_path(p) for p in event_paths]

    with _SWMM_SIMULATION_LOCK:
        result = select_global_params(
            base_inp=base_inp,
            obs_csv_paths=obs_csv_paths,
            event_paths=event_paths,
            subcatchments=subcatchments,
            intent=intent,
            top_k=top_k,
            infiltration_model=infiltration_model,
            method=method,
            r=r,
            delta=delta,
            seed=seed,
        )

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        print_result(result)
    return buf.getvalue()


# ===========================================================================
# 2. calibrate_es_ilu  ->  ES_ILU.calibrate_es_ilu_case1
# ===========================================================================
@_safe_tool
def calibrate_es_ilu(
    inp_path: str,
    obs_csv: str,
    event_txt: str,
    subcatchments: List[str],
    out_inp: str,
    param_indices: Optional[List[Union[int, str]]] = None,
    sigma_rel: float = 0.033,
    niter: int = 10,
    ne: int = 300,
) -> str:
    """Calibrate SWMM subcatchment parameters using Ensemble Smoother with Iterative Local Updating (ES-ILU).

    Uses ensemble members over assimilation iterations with localized updates.
    Optimises 11 parameters per subcatchment. Case 1 expects 8 subcatchments.

    Args:
        inp_path: Path to the base SWMM .inp file.
        obs_csv: Path to observation CSV.
        event_txt: Path to rainfall event .txt file (semicolon-separated).
                   Used to inject timeseries and set the simulation period.
        subcatchments: List of subcatchment IDs.
        out_inp: Output path for calibrated INP.
        param_indices: Parameter indices (0-10) or names to calibrate. None = all 11.
            0=width, 1=slope, 2=imperv, 3=n_imperv, 4=n_perv,
            5=dstor_imperv, 6=dstor_perv, 7=zero_imperv,
            8=maxrate, 9=minrate, 10=decay.
            Accepts ints or strings: e.g. [1, 2, 8] or ["slope", "imperv", "maxrate"].
        sigma_rel: Relative measurement error std dev for the observation covariance matrix
            (default 0.033, matching the paper value).
        niter: Number of ES-ILU assimilation iterations (default 10, matching the paper value).
        ne: Ensemble size — number of members (default 300, matching the paper value).

    Returns:
        Summary string with calibration results.
    """
    es_ilu_mod = importlib.import_module("skills.calibrate.Scripts.ES_ILU")

    inp_path = _normalize_local_path(inp_path)
    obs_csv = _normalize_local_path(obs_csv)
    event_txt = _normalize_local_path(event_txt)
    out_inp = _normalize_local_path(out_inp)

    with _SWMM_SIMULATION_LOCK:
        return es_ilu_mod.calibrate_es_ilu_case1(
            inp_path=inp_path,
            obs_csv=obs_csv,
            event_txt=event_txt,
            subcatchments=subcatchments,
            out_inp=out_inp,
            param_indices=param_indices,
            sigma_rel=sigma_rel,
            niter=niter,
            ne=ne,
        )


# ===========================================================================
# 5. validate_model  ->  compute_results.validate_events_nse
# ===========================================================================
@_safe_tool
def validate_model(
    calibrated_inp: str,
    event_paths: List[str],
    obs_csv_paths: List[str],
) -> str:
    """Validate a calibrated SWMM model against independent rainfall events and compute NSE.

    Runs SWMM for each event, aligns outputs to observation timestamps, and computes
    per-column and overall Nash-Sutcliffe Efficiency.

    Args:
        calibrated_inp: Path to calibrated SWMM .inp file.
        event_paths: List of rainfall event .txt file paths.
        obs_csv_paths: List of observation CSV paths (same length as event_paths).

    Returns:
        Summary string with NSE results per event and column.
    """
    from skills.compute_results_skill.Scripts.compute_results import run_validate

    calibrated_inp = _normalize_local_path(calibrated_inp)
    event_paths = [_normalize_local_path(p) for p in event_paths]
    obs_csv_paths = [_normalize_local_path(p) for p in obs_csv_paths]

    with _SWMM_SIMULATION_LOCK:
        return run_validate(
            calibrated_inp=calibrated_inp,
            event_paths=event_paths,
            obs_csv_paths=obs_csv_paths,
        )


# ===========================================================================
# 6. generate_plots  ->  plot.generate_calib_valid_figures
# ===========================================================================
@_safe_tool
def generate_plots(
    calibrated_inp_list: List[str],
    node_id: str,
    calib_event_txt: str,
    valid_event_txt: str,
    calib_obs_csv: str,
    valid_obs_csv: str,
    out_dir: str,
    method_labels: Optional[List[str]] = None,
    time_tol_sec: int = 60,
    calibrated_params: Optional[List[str]] = None,
) -> str:
    """Generate calibration/validation comparison figures.

    Produces two figures: (1) time-series comparison (obs vs. sim for calibration and
    validation events), (2) parameter comparison bar charts (true vs. methods, auto-detecting
    which parameters were actually modified during calibration). The reference model is
    always Example1.inp at the project root.

    Args:
        calibrated_inp_list: List of calibrated INP file paths (one per method).
        node_id: Node ID for time-series plot.
        calib_event_txt: Calibration event rainfall file.
        valid_event_txt: Validation event rainfall file.
        calib_obs_csv: Calibration observation CSV.
        valid_obs_csv: Validation observation CSV.
        out_dir: Output directory for saved figures.
        method_labels: Custom labels for each method (default: filenames).
        time_tol_sec: Time alignment tolerance in seconds.
        calibrated_params: Explicit list of param names to plot in fig2; auto-detected
            from INP diff if None.

    Returns:
        Summary string with paths to generated figure files.
    """
    from skills.plot_figures.Scripts.plot import run_generate_plots

    calibrated_inp_list = [_normalize_local_path(p) for p in calibrated_inp_list]
    calib_event_txt = _normalize_local_path(calib_event_txt)
    valid_event_txt = _normalize_local_path(valid_event_txt)
    calib_obs_csv = _normalize_local_path(calib_obs_csv)
    valid_obs_csv = _normalize_local_path(valid_obs_csv)
    out_dir = _normalize_local_path(out_dir)

    with _SWMM_SIMULATION_LOCK:
        return run_generate_plots(
            calibrated_inp_list=calibrated_inp_list,
            node_id=node_id, calib_event_txt=calib_event_txt,
            valid_event_txt=valid_event_txt, calib_obs_csv=calib_obs_csv,
            valid_obs_csv=valid_obs_csv, out_dir=out_dir,
            method_labels=method_labels, time_tol_sec=time_tol_sec,
            calibrated_params=calibrated_params,
        )


# ===========================================================================
# 7. analyze_figures  ->  figure_analysis.analyze_generated_figures
# ===========================================================================
@_safe_tool
def analyze_figures(
    task_str: str,
    figure_info: List[Dict[str, Any]],
) -> str:
    """Analyse generated figures using a Vision-Language Model (VLM).

    Sends images to a Qwen vision-language model via Hugging Face endpoint with a task description and returns
    the model's structured text interpretation.

    Args:
        task_str: Natural language task description for the VLM.
        figure_info: List of dicts, each with a "path" key pointing to an image file.

    Returns:
        The VLM's text analysis response.
    """
    from skills.figure_analysis_skill.Scripts.figure_analysis import run_analyze_figures

    normalized_figure_info: List[Dict[str, Any]] = []
    for fig in figure_info:
        if isinstance(fig, dict):
            fig_copy = dict(fig)
            if isinstance(fig_copy.get("path"), str):
                fig_copy["path"] = _normalize_local_path(fig_copy["path"])
            normalized_figure_info.append(fig_copy)
        else:
            normalized_figure_info.append(fig)

    return run_analyze_figures(task_str=task_str, figure_info=normalized_figure_info)


# ===========================================================================
# 8. convert_gis_to_inp  ->  transfer.gis-to-inp
# ===========================================================================
@_safe_tool
def convert_gis_to_inp(
    gis_path: str,
    output_inp: str,
    links_path: Optional[str] = None,
    default_roughness: float = 0.013,
    preserve_all_sections: bool = True,
    rebuild_core_from_gis: bool = False,
) -> str:
    """Convert GIS shapefiles (Manholes + Links) to a SWMM .inp model file.

    Supports three conversion modes:
    - Lossless restore: restore original INP byte-for-byte from metadata.
    - Preserve non-core, rebuild core: keep non-network sections from metadata,
      rebuild core network sections from current GIS geometry.
    - GIS-only minimal: create a minimal INP from GIS only (no metadata needed).

    Args:
        gis_path: Directory containing Manholes.shp or path to Manholes.shp directly.
        output_inp: Output path for the generated .inp file.
        links_path: Explicit path to Links.shp (auto-resolved from gis_path if None).
        default_roughness: Manning's roughness for conduits (default 0.013).
        preserve_all_sections: Use metadata for lossless/non-core preservation.
        rebuild_core_from_gis: Rebuild core network sections from GIS instead of metadata.

    Returns:
        Summary string with conversion mode, output path, and network statistics.
    """
    gis_mod = importlib.import_module("skills.gis-to-inp.Scripts.gis_to_inp_cli")

    gis_path = _normalize_local_path(gis_path)
    output_inp = _normalize_local_path(output_inp)
    if links_path is not None:
        links_path = _normalize_local_path(links_path)

    return gis_mod.run_gis_to_inp(
        gis_path=gis_path,
        output_inp=output_inp,
        links_path=links_path,
        default_roughness=default_roughness,
        preserve_all_sections=preserve_all_sections,
        rebuild_core_from_gis=rebuild_core_from_gis,
    )


# ===========================================================================
# ALL_TOOLS: tool list consumed by Agent.py
# ===========================================================================
ALL_TOOLS = [
    convert_gis_to_inp,
    select_params,
    calibrate_es_ilu,
    validate_model,
    generate_plots,
    analyze_figures,
]
