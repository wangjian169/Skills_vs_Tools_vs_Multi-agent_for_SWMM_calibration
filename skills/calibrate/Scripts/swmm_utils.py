# -*- coding: utf-8 -*-
"""
swmm_utils.py

Shared SWMM utilities used by ES_ILU.py and
skills/intent_sensitive_selection/Scripts/select_global_params.py.

Extracted from GA_calibrate_swmm.py and ES_ILU.py to provide a single source
of truth for constants, data structures, and helper functions.
"""

import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd
from pyswmm import Simulation, Nodes, Links

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TIME_TOL_SEC = 60  # nearest-match tolerance for timestamp alignment (seconds)

NODE_VARS = {"depth", "head", "volume", "lateral_inflow", "total_inflow", "flooding"}
LINK_VARS = {"flow", "depth", "velocity", "volume"}

# Relative observation error used to build the measurement covariance matrix
SIGMA_REL = 0.033

# Table 3 fixed parameter ranges (Width uses +/-50% of initial value)
RANGES = {
    "slope":        (0.01,  10.0),
    "imperv":       (0.0,  100.0),
    "n_imperv":     (0.01,   0.04),
    "n_perv":       (0.1,    0.8),
    "dstor_imperv": (0.2,    5.0),
    "dstor_perv":   (2.0,   10.0),
    "zero_imperv":  (0.0,  100.0),
    "maxrate":      (20.0,  80.0),
    "minrate":      (0.0,   10.0),
    "decay":        (2.0,    7.0),
}

PARAM_NAMES: Dict[int, str] = {
    0: "width",       1: "slope",        2: "imperv",
    3: "n_imperv",    4: "n_perv",       5: "dstor_imperv",
    6: "dstor_perv",  7: "zero_imperv",  8: "maxrate",
    9: "minrate",     10: "decay",
}

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ObsSpec:
    obj_type: str   # "node" or "link"
    obj_id: str
    var: str
    col: str


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _resolve_param_indices(raw: List[Union[int, str]]) -> List[int]:
    """Accept ints, canonical names, or common aliases -> sorted unique List[int]."""
    name_to_idx = {v: k for k, v in PARAM_NAMES.items()}
    name_to_idx.update({
        "width": 0, "%slope": 1, "percent_slope": 1,
        "%imperv": 2, "percent_imperv": 2,
        "n-imperv": 3, "n-perv": 4,
        "dstore-imperv": 5, "s-imperv": 5,
        "dstore-perv": 6, "s-perv": 6,
        "%zero-imperv": 7, "zero-imperv": 7,
        "max_rate": 8, "min_rate": 9,
        "decay_coeff": 10, "decaycoeff": 10,
    })
    resolved = []
    for p in raw:
        if isinstance(p, int):
            if 0 <= p <= 10:
                resolved.append(p)
            else:
                raise ValueError(f"param index must be 0-10, got {p}")
        elif isinstance(p, str):
            key = p.lower().strip().replace(" ", "_")
            if key in name_to_idx:
                resolved.append(name_to_idx[key])
            else:
                raise ValueError(f"Unknown param: {p!r}. Valid: {list(PARAM_NAMES.values())}")
        else:
            raise TypeError(f"Expected int or str, got {type(p)}")
    return sorted(set(resolved))


def nse(sim: np.ndarray, obs: np.ndarray) -> float:
    """Nash-Sutcliffe Efficiency for a 1-D vector (concatenated observation series)."""
    sim = np.asarray(sim, dtype=float).reshape(-1)
    obs = np.asarray(obs, dtype=float).reshape(-1)
    if sim.size != obs.size:
        raise ValueError(f"NSE: sim/obs dimension mismatch {sim.size} vs {obs.size}")
    denom = float(np.sum((obs - float(np.mean(obs))) ** 2))
    if denom <= 0.0:
        return float("nan")
    return 1.0 - float(np.sum((obs - sim) ** 2)) / denom


def load_obs_csv(obs_csv: str) -> Tuple[pd.Series, List[str], np.ndarray, pd.DataFrame]:
    """Load an observation CSV into (times, col_names, concatenated_array, full_dataframe).

    Returns a 4-tuple so callers that only need the first three values can unpack
    the DataFrame into ``_`` (e.g. ``times, cols, d_obs, _ = load_obs_csv(path)``).
    """
    df = pd.read_csv(obs_csv)
    if df.shape[1] < 2:
        raise ValueError("Observation CSV must contain at least 2 columns: time + observation")
    tcol = df.columns[0]
    df[tcol] = pd.to_datetime(df[tcol])
    df = df.sort_values(tcol).reset_index(drop=True).rename(columns={tcol: "datetime"})
    obs_cols = list(df.columns[1:])
    d_obs = np.concatenate([df[c].to_numpy(dtype=float) for c in obs_cols], axis=0)
    return df["datetime"], obs_cols, d_obs, df


def parse_obs_specs(obs_cols: List[str]) -> List[ObsSpec]:
    """Parse observation column names into ObsSpec objects."""
    specs: List[ObsSpec] = []
    for col in obs_cols:
        toks = col.split("_")
        if len(toks) < 3:
            raise ValueError(
                f"Column name must follow node_<ID>_<var> or link_<ID>_<var>: {col}"
            )
        obj_type = toks[0].lower()
        obj_id = toks[1]
        var = "_".join(toks[2:]).lower()
        if obj_type == "node":
            if var not in NODE_VARS:
                raise ValueError(f"Unsupported node variable {var!r} in {col}")
        elif obj_type == "link":
            if var not in LINK_VARS:
                raise ValueError(f"Unsupported link variable {var!r} in {col}")
        else:
            raise ValueError(f"Column type must be 'node' or 'link': {col}")
        specs.append(ObsSpec(obj_type=obj_type, obj_id=obj_id, var=var, col=col))
    return specs


# ---------------------------------------------------------------------------
# SWMM forward model
# ---------------------------------------------------------------------------

class SwmmForward:
    def __init__(self, inp_path: str, specs: List[ObsSpec], obs_times: pd.Series):
        self.inp_path = inp_path
        self.specs = specs
        self.obs_times = pd.to_datetime(obs_times).dt.tz_localize(None)

    def run(self) -> np.ndarray:
        obs_times64 = self.obs_times.to_numpy(dtype="datetime64[s]")
        time_list: List[np.datetime64] = []
        series: Dict[str, List[float]] = {s.col: [] for s in self.specs}

        with Simulation(self.inp_path) as sim:
            nodes = Nodes(sim)
            links = Links(sim)
            for _ in sim:
                t = pd.to_datetime(sim.current_time).to_datetime64().astype("datetime64[s]")
                time_list.append(t)
                for s in self.specs:
                    series[s.col].append(float(self._read(nodes, links, s)))

        out_df = pd.DataFrame({"_t": np.array(time_list, dtype="datetime64[s]")})
        for col, vals in series.items():
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
            raise RuntimeError(
                "Observation timestamps could not be aligned with SWMM output: "
                "check model timestep or TIME_TOL_SEC"
            )
        return np.concatenate([aligned[s.col].to_numpy(dtype=float) for s in self.specs], axis=0)

    def _read(self, nodes: Nodes, links: Links, s: ObsSpec) -> float:
        if s.obj_type == "node":
            n = nodes[s.obj_id]
            if s.var == "depth":          return n.depth
            if s.var == "head":           return n.head
            if s.var == "volume":         return n.volume
            if s.var == "lateral_inflow": return n.lateral_inflow
            if s.var == "total_inflow":   return n.total_inflow
            if s.var == "flooding":       return n.flooding
        if s.obj_type == "link":
            l = links[s.obj_id]
            if s.var == "flow":     return l.flow
            if s.var == "depth":    return l.depth
            if s.var == "velocity": return l.velocity
            if s.var == "volume":   return l.volume
        raise ValueError(f"Failed to read observation mapping: {s}")


# ---------------------------------------------------------------------------
# INP editor
# ---------------------------------------------------------------------------

class SwmmInpEditor:
    def __init__(self, inp_path: str):
        with open(inp_path, "r", encoding="utf-8", errors="ignore") as f:
            self.lines = f.readlines()

    def write(self, out_inp: str) -> None:
        with open(out_inp, "w", encoding="utf-8") as f:
            f.writelines(self.lines)

    def _find_bounds(self, sec: str) -> Tuple[Optional[int], Optional[int]]:
        header = f"[{sec.upper()}]"
        start = None
        for i, line in enumerate(self.lines):
            if line.strip().upper() == header:
                start = i + 1
                break
        if start is None:
            return None, None
        end = len(self.lines)
        for j in range(start, len(self.lines)):
            t = self.lines[j].strip()
            if t.startswith("[") and t.endswith("]"):
                end = j
                break
        return start, end

    def read_width_m0(self, subcatchments: List[str]) -> Dict[str, float]:
        """Read initial Width values from [SUBCATCHMENTS] (column index 5)."""
        start, end = self._find_bounds("SUBCATCHMENTS")
        if start is None:
            raise ValueError("INP is missing [SUBCATCHMENTS]")
        m0: Dict[str, float] = {}
        for i in range(start, end):
            s = self.lines[i].strip()
            if not s or s.startswith(";") or (s.startswith("[") and s.endswith("]")):
                continue
            toks = s.split()
            if toks[0] in subcatchments:
                if len(toks) < 7:
                    raise ValueError(f"[SUBCATCHMENTS] row has too few fields: {toks[0]}")
                m0[toks[0]] = float(toks[5])
        missing = [sc for sc in subcatchments if sc not in m0]
        if missing:
            raise ValueError(f"These subcatchments were not found in [SUBCATCHMENTS]: {missing}")
        return m0

    def read_initial_params(self, subcatchments: List[str]) -> np.ndarray:
        """Read all 11 initial parameter values per subcatchment from the INP.

        Returns flat ndarray of shape (11 * N_sc,) with layout:
        [width, slope, imperv, n_imperv, n_perv, dstor_imperv, dstor_perv,
         zero_imperv, maxrate, minrate, decay] repeated per subcatchment.
        """
        n_sc = len(subcatchments)
        m0 = np.zeros(11 * n_sc, dtype=float)
        sc_idx = {sc: k for k, sc in enumerate(subcatchments)}

        # [SUBCATCHMENTS]: %Imperv=col4, Width=col5, %Slope=col6
        start, end = self._find_bounds("SUBCATCHMENTS")
        if start is None:
            raise ValueError("Missing [SUBCATCHMENTS]")
        for i in range(start, end):
            s = self.lines[i].strip()
            if not s or s.startswith(";") or (s.startswith("[") and s.endswith("]")):
                continue
            toks = s.split()
            if toks[0] in sc_idx:
                base = sc_idx[toks[0]] * 11
                m0[base + 0] = float(toks[5])  # width
                m0[base + 1] = float(toks[6])  # slope
                m0[base + 2] = float(toks[4])  # imperv

        # [SUBAREAS]: N-Imperv=1, N-Perv=2, Dstore-Imperv=3, Dstore-Perv=4, %Zero=5
        start, end = self._find_bounds("SUBAREAS")
        if start is None:
            raise ValueError("Missing [SUBAREAS]")
        for i in range(start, end):
            s = self.lines[i].strip()
            if not s or s.startswith(";") or (s.startswith("[") and s.endswith("]")):
                continue
            toks = s.split()
            if toks[0] in sc_idx:
                base = sc_idx[toks[0]] * 11
                m0[base + 3] = float(toks[1])
                m0[base + 4] = float(toks[2])
                m0[base + 5] = float(toks[3])
                m0[base + 6] = float(toks[4])
                m0[base + 7] = float(toks[5])

        # [INFILTRATION]: MaxRate=1, MinRate=2, DecayCoeff=3
        start, end = self._find_bounds("INFILTRATION")
        if start is None:
            raise ValueError("Missing [INFILTRATION]")
        for i in range(start, end):
            s = self.lines[i].strip()
            if not s or s.startswith(";") or (s.startswith("[") and s.endswith("]")):
                continue
            toks = s.split()
            if toks[0] in sc_idx:
                base = sc_idx[toks[0]] * 11
                m0[base + 8]  = float(toks[1])
                m0[base + 9]  = float(toks[2])
                m0[base + 10] = float(toks[3])

        return m0

    def apply_params_case1_11(
        self,
        subcatchments: List[str],
        m_vec: np.ndarray,
        width_m0: Dict[str, float],
    ) -> None:
        """Write all 11 parameters per subcatchment back to self.lines.

        Parameter order per subcatchment (11):
        0 Width, 1 %Slope, 2 %Imperv, 3 N-Imperv, 4 N-Perv,
        5 Dstore-Imperv, 6 Dstore-Perv, 7 %Zero-Imperv,
        8 MaxRate, 9 MinRate, 10 DecayCoeff.
        Width is clamped to (0.5*m0, 1.5*m0) after writing.
        """
        self._apply_subcatchments(subcatchments, m_vec)
        self._apply_subareas(subcatchments, m_vec)
        self._apply_infiltration(subcatchments, m_vec)
        self._clamp_width(subcatchments, width_m0)

    def _apply_subcatchments(self, subcatchments: List[str], m_vec: np.ndarray) -> None:
        start, end = self._find_bounds("SUBCATCHMENTS")
        if start is None:
            raise ValueError("INP is missing [SUBCATCHMENTS]")
        idx_map = {sc: k for k, sc in enumerate(subcatchments)}
        for i in range(start, end):
            s = self.lines[i].strip()
            if not s or s.startswith(";") or (s.startswith("[") and s.endswith("]")):
                continue
            toks = s.split()
            sc = toks[0]
            if sc not in idx_map:
                continue
            base = idx_map[sc] * 11
            toks[5] = self._fmt(m_vec[base + 0])  # Width
            toks[6] = self._fmt(m_vec[base + 1])  # %Slope
            toks[4] = self._fmt(m_vec[base + 2])  # %Imperv
            self.lines[i] = "\t".join(toks) + "\n"

    def _apply_subareas(self, subcatchments: List[str], m_vec: np.ndarray) -> None:
        start, end = self._find_bounds("SUBAREAS")
        if start is None:
            raise ValueError("INP is missing [SUBAREAS]")
        idx_map = {sc: k for k, sc in enumerate(subcatchments)}
        for i in range(start, end):
            s = self.lines[i].strip()
            if not s or s.startswith(";") or (s.startswith("[") and s.endswith("]")):
                continue
            toks = s.split()
            sc = toks[0]
            if sc not in idx_map:
                continue
            base = idx_map[sc] * 11
            toks[1] = self._fmt(m_vec[base + 3])  # N-Imperv
            toks[2] = self._fmt(m_vec[base + 4])  # N-Perv
            toks[3] = self._fmt(m_vec[base + 5])  # Dstore-Imperv
            toks[4] = self._fmt(m_vec[base + 6])  # Dstore-Perv
            toks[5] = self._fmt(m_vec[base + 7])  # %Zero-Imperv
            self.lines[i] = "\t".join(toks) + "\n"

    def _apply_infiltration(self, subcatchments: List[str], m_vec: np.ndarray) -> None:
        start, end = self._find_bounds("INFILTRATION")
        if start is None:
            raise ValueError("INP is missing [INFILTRATION]")
        idx_map = {sc: k for k, sc in enumerate(subcatchments)}
        for i in range(start, end):
            s = self.lines[i].strip()
            if not s or s.startswith(";") or (s.startswith("[") and s.endswith("]")):
                continue
            toks = s.split()
            sc = toks[0]
            if sc not in idx_map:
                continue
            base = idx_map[sc] * 11
            toks[1] = self._fmt(m_vec[base + 8])   # MaxRate
            toks[2] = self._fmt(m_vec[base + 9])   # MinRate
            toks[3] = self._fmt(m_vec[base + 10])  # DecayCoeff
            self.lines[i] = "\t".join(toks) + "\n"

    def _clamp_width(self, subcatchments: List[str], width_m0: Dict[str, float]) -> None:
        """Clamp Width (already written to self.lines) to (0.5*m0, 1.5*m0)."""
        start, end = self._find_bounds("SUBCATCHMENTS")
        idx_map = {sc: k for k, sc in enumerate(subcatchments)}
        for i in range(start, end):
            s = self.lines[i].strip()
            if not s or s.startswith(";") or (s.startswith("[") and s.endswith("]")):
                continue
            toks = s.split()
            sc = toks[0]
            if sc not in idx_map:
                continue
            m0 = width_m0[sc]
            w = float(toks[5])
            w = min(max(w, 0.5 * m0), 1.5 * m0)
            toks[5] = self._fmt(w)
            self.lines[i] = "\t".join(toks) + "\n"

    def _fmt(self, x: float) -> str:
        if abs(x) >= 1e4 or (abs(x) > 0 and abs(x) < 1e-3):
            return f"{x:.6e}"
        return f"{x:.6f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# Bounds helper (used by select_global_params and GA calibration)
# ---------------------------------------------------------------------------

def make_bounds(
    inp_path: str,
    subcatchments: List[str],
    param_indices: Optional[List[int]] = None,
) -> Tuple[np.ndarray, np.ndarray, Dict[str, float]]:
    """Build lower/upper bound arrays for all 11*N_sc parameters.

    Width bounds are ±50% of the initial value read from the INP.
    If param_indices is given, non-selected parameters are frozen at
    their current INP values (lb == ub == initial value).

    Returns (lb, ub, width_m0).
    """
    ed = SwmmInpEditor(inp_path)
    w0 = ed.read_width_m0(subcatchments)
    lb = np.zeros(11 * len(subcatchments), dtype=float)
    ub = np.zeros(11 * len(subcatchments), dtype=float)
    for k, sc in enumerate(subcatchments):
        base = k * 11
        lb[base + 0] = 0.5 * w0[sc]
        ub[base + 0] = 1.5 * w0[sc]
        lb[base + 1],  ub[base + 1]  = RANGES["slope"]
        lb[base + 2],  ub[base + 2]  = RANGES["imperv"]
        lb[base + 3],  ub[base + 3]  = RANGES["n_imperv"]
        lb[base + 4],  ub[base + 4]  = RANGES["n_perv"]
        lb[base + 5],  ub[base + 5]  = RANGES["dstor_imperv"]
        lb[base + 6],  ub[base + 6]  = RANGES["dstor_perv"]
        lb[base + 7],  ub[base + 7]  = RANGES["zero_imperv"]
        lb[base + 8],  ub[base + 8]  = RANGES["maxrate"]
        lb[base + 9],  ub[base + 9]  = RANGES["minrate"]
        lb[base + 10], ub[base + 10] = RANGES["decay"]

    if param_indices is not None:
        initial = ed.read_initial_params(subcatchments)
        frozen = set(range(11)) - set(param_indices)
        for k in range(len(subcatchments)):
            base = k * 11
            for pi in frozen:
                lb[base + pi] = initial[base + pi]
                ub[base + pi] = initial[base + pi]

    return lb, ub, w0
