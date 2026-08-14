---
name: plot_figures
description: Generate calibration/validation time-series and parameter comparison figures
---

## Figure Generation (Calibration & Validation Plots)

Generate two publication-quality figures comparing calibration methods:

1. **Time-series plot** — Observed vs. simulated node variable for calibration and validation events (2 subplots).
2. **Parameter comparison** — Bar chart of the 11 subcatchment parameters: true vs. each calibrated method.

### Usage

```bash
python -m skills.plot_figures.Scripts.plot \
  --calibrated_inps <calibrated_1.inp> <calibrated_2.inp> \
  --node_id 18 \
  --calib_event <event1.txt> \
  --valid_event <event2.txt> \
  --calib_obs <event1.csv> \
  --valid_obs <event2.csv> \
  --out_dir <output_directory> \
  --labels GA Bayes-MH ES-ILU \
  --time_tol_sec 60 \
  --calibrated_params slope imperv zero_imperv
```

| Parameter | Description | Default |
|-----------|-------------|---------|
| --calibrated_inps | List of calibrated INP file paths (one per method) | (required) |
| --node_id | Node ID for time-series plot | (required) |
| --calib_event | Calibration event rainfall file | (required) |
| --valid_event | Validation event rainfall file | (required) |
| --calib_obs | Calibration observation CSV | (required) |
| --valid_obs | Validation observation CSV | (required) |
| --out_dir | Output directory for saved figures | (required) |
| --labels | Custom labels for each method | None (uses filenames) |
| --time_tol_sec | Time alignment tolerance in seconds | 60 |
| --calibrated_params | Whitespace-separated list of param names to include in fig2; auto-detected from INP diff if omitted | None |

### Output

Saves two figure files to `--out_dir`:
- `fig1_timeseries_calib_valid.png`
- `fig2_params_group.png`
