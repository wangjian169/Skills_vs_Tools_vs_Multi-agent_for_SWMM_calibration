---
name: compute_results_skill
description: Validate calibrated SWMM model against independent events and compute NSE
---

## Results Validation (NSE Computation)

Run a calibrated SWMM model against one or more independent rainfall events and compute
Nash-Sutcliffe Efficiency (NSE) for each observed variable and overall.

### Pipeline Steps

1. Read each rainfall event file and observation CSV.
2. Prepare a temporary INP with the event's timeseries and time window.
3. Run SWMM and align simulated outputs to observation timestamps.
4. Compute per-column and overall NSE.

### Usage

```bash
python -m skills.compute_results_skill.Scripts.compute_results \
  --calibrated_inp <calibrated.inp path> \
  --event_paths <event1.txt> <event2.txt> \
  --obs_csv_paths <event1.csv> <event2.csv>
```

| Parameter | Description | Default |
|-----------|-------------|---------|
| --calibrated_inp | Path to calibrated SWMM .inp file | (required) |
| --event_paths | List of rainfall event .txt file paths | (required) |
| --obs_csv_paths | List of observation CSV paths (same length as event_paths) | (required) |

### Output

Prints per-event and per-column NSE results:
- Event file name
- Overall NSE (mean across all observed columns)
- Per-column NSE values
