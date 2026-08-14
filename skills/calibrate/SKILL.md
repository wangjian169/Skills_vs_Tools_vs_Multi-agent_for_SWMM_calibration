---
name: calibrate
description: Calibrate SWMM subcatchment parameters using ES-ILU
---

## SWMM Model Calibration

Calibrate SWMM subcatchment parameters by minimizing the discrepancy between simulated
and observed hydraulic variables using the Ensemble Smoother with Iterative Local Updating
(ES-ILU) algorithm.

Each run optimizes the same **11 parameters per subcatchment** (Width, %Slope,
%Imperv, N-Imperv, N-Perv, Dstore-Imperv, Dstore-Perv, %Zero-Imperv, MaxRate, MinRate,
DecayCoeff) using the ranges defined in the 11-parameter table.

---

### Calibration Time Policy (CRITICAL — read carefully)

The base INP [OPTIONS] dates (e.g. START_DATE 01/01/1998) will NOT match the
observation CSV timestamps (e.g. 2012-06-29). This is expected and normal.
**Do NOT read the INP to check dates. Do NOT edit [OPTIONS] time fields.
Do NOT create scripts to fix dates.**
The calibration code internally overwrites the run period from obs timestamps.
Just call the calibration tool directly. If it fails with a time error, report
the exact error and ask the user before any manual edits.

---

### ES-ILU (Ensemble Smoother with Iterative Local Updating)

Ensemble data-assimilation method. Uses 300 ensemble members over 10 iterations with
localized updates (beta=0.2). Default configuration (tunable): NE=300, NITER=10,
SIGMA_REL=0.033. BETA=0.2 and SEED=7 remain fixed.

#### Usage

```bash
python D:\Code\From raw data to calibrated SWMM model\skills\calibrate\Scripts\ES_ILU.py \
  --inp_path <model.inp path> \
  --obs_csv <observations.csv path> \
  --event_txt <event.txt path> \
  --subcatchments 1 2 3 4 5 6 7 8 \
  --out_inp <output.inp path> \
  --param_indices 1 2 8 \
  --sigma_rel 0.033 \
  --niter 10 \
  --ne 300
```

| Parameter | Description | Default |
|-----------|-------------|---------|
| --inp_path | Path to the base SWMM .inp file | (required) |
| --obs_csv | Path to observation CSV | (required) |
| --event_txt | Path to rainfall event .txt file (semicolon-separated). Provides the timeseries and time window for the simulation. | (required) |
| --subcatchments | List of subcatchment IDs (Case 1 expects 8) | (required) |
| --out_inp | Output path for calibrated INP | (required) |
| --param_indices | Parameter indices (0-10) to calibrate. Omit for all 11. 0=width 1=slope 2=imperv 3=n_imperv 4=n_perv 5=dstor_imperv 6=dstor_perv 7=zero_imperv 8=maxrate 9=minrate 10=decay | None (all) |
| --sigma_rel | Relative measurement error std dev | 0.033 |
| --niter | Number of assimilation iterations | 10 |
| --ne | Ensemble size | 300 |

Output: Writes the calibrated INP file to `--out_inp`.
