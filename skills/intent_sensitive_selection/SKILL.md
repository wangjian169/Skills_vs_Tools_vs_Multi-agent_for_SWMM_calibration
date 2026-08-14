---
name: intent_sensitive_selection
description: Select a global subset of SWMM subcatchment parameters (same param indices for all subcatchments) using intent-driven sensitivity analysis across one or more event/observation pairs. Use when 11 parameters per subcatchment are too many to calibrate and you need a reduced --param_indices set for GA, Bayes-MH, or ES-ILU.
---

## Intent-Driven Global Parameter Selection

Select which parameter indices to calibrate from the 11 SWMM subcatchment parameters.
The selected indices are global: every subcatchment uses the same selected set.

### Inputs

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `base_inp` | string | yes | Base SWMM INP path |
| `obs_csv_paths` | list[string] | yes | Observation CSV paths (obs1, obs2, ...) |
| `event_paths` | list[string] | yes | Rain event txt paths (event1, event2, ...) |
| `subcatchments` | list[string] | yes | Subcatchment IDs to calibrate |
| `intent` | string | yes | `peak`, `volume`, `hydrograph_shape`, `infiltration`, `balanced` |
| `top_k` | int | yes | Number of final selected indices |
| `infiltration_model` | string | no | `horton` (default) or `green-ampt` |
| `method` | string | no | `morris` (currently implemented) |
| `r` | int | no | Number of Morris trajectories (default 20) |
| `delta` | float | no | Morris perturbation step in normalized space (default 0.2) |
| `seed` | int | no | Random seed |

### Multi-Event Pairing Rule

`obs_csv_paths` and `event_paths` must have the same length.
They are paired by position:
- pair 1: `obs_csv_paths[0]` with `event_paths[0]`
- pair 2: `obs_csv_paths[1]` with `event_paths[1]`
- ...

### Intent Candidate Pools (11-parameter setup)

Parameter index map:
`0 width, 1 slope, 2 imperv, 3 n_imperv, 4 n_perv, 5 dstor_imperv, 6 dstor_perv, 7 zero_imperv, 8 maxrate, 9 minrate, 10 decay`

- `peak`: `[0, 1, 2, 3, 5, 7, 8]`
- `volume`: `[2, 4, 6, 8, 9, 10]`
- `hydrograph_shape`: `[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]`
- `infiltration` + `horton`: `[8, 9, 10]`
- `infiltration` + `green-ampt`: fallback to `[8, 9, 10]` in this 11-parameter script
- `balanced`: `[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]`

### Usage

```bash
python -m skills.intent_sensitive_selection.Scripts.select_global_params \
  --base_inp <model.inp> \
  --obs_csv_paths <event1.csv> <event2.csv> \
  --event_paths <event1.txt> <event2.txt> \
  --subcatchments 1 2 3 4 5 6 7 8 \
  --intent peak \
  --top_k 3 \
  --r 20 \
  --delta 0.2 \
  --seed 7
```

### Outputs

Printed once to stdout:
- `selected_param_indices` (global indices for all subcatchments)
- `selected_param_names`
- `param_indices_cli` (directly reusable in calibrate scripts)
- `candidate_param_indices`
- `ranking` (`idx`, `name`, `mu_star`, `sigma`, `rank`)
- `diagnostics` (`n_events`, `n_evaluations`, `method`, `seed`, runtime)

### Time Handling Policy

Do not manually edit INP [OPTIONS] dates for event matching.
This skill prepares event-specific INP files from each `event_path` and corresponding `obs_csv_paths` timestamp range.
