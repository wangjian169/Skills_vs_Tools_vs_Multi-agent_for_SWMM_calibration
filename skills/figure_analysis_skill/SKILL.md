---
name: figure_analysis_skill
description: Analyze generated figures using a Vision-Language Model (VLM)
---

## Figure Analysis (Vision-Language Model)

Use a Vision-Language Model (VLM) to automatically analyze generated figures and produce
structured text interpretations. The backend uses Qwen3-VL via the DashScope API.

### Usage

```bash
python skills/figure_analysis_skill/Scripts/figure_analysis.py \
  --task "Sensitivity results: imperv (mu*=1.03), zero_imperv (mu*=0.87), slope (mu*=0.36). Calibration NSE=0.948, Validation NSE=0.820. Analyze the figures: 1) fit quality in calibration and validation events; 2) subcatchment-level insights from the parameter bar chart (which subcatchments changed most, spatial heterogeneity); 3) overall calibration assessment." \
  --figures <fig1.png> <fig2.png>
```

| Parameter | Description | Default |
|-----------|-------------|---------|
| --task | Natural language task description for the VLM | (required) |
| --figures | Paths to image files to analyze (one or more) | (required) |

### Output

Prints the VLM's text analysis response to stdout.
