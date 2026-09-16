# Autonomous SWMM Calibration with AI Agent Architectures

This repository contains the code used for the manuscript **"Towards Autonomous Urban Drainage Modelling: Evaluating AI Agent Architectures for Automated SWMM Calibration"**.

The experiments evaluate how different AI-agent architectures perform on an automated SWMM modelling and calibration workflow. The workflow covers GIS-to-INP conversion, intent-driven parameter selection, ES-ILU calibration, validation with NSE, figure generation, and optional vision-language figure analysis.

## Agent Architectures

Three architectures are implemented with the same underlying SWMM scripts and data:

- `skills`: a single agent reads Markdown skill files and decides how to run the corresponding scripts.
- `tools`: a single agent calls registered Python tools directly.
- `multi`: a coordinator delegates tasks to specialist sub-agents for GIS conversion, calibration, validation, plotting, and analysis.

## Repository Layout

```text
Agent.py                  # Builds and runs the three agent architectures
run_batch_experiments.py  # Main script for repeated benchmark experiments
tasks.json                # Task definitions used by the batch runner
shared_prompts.py         # Shared system prompts and task instructions
tool_wrappers.py          # Python tool wrappers exposed to agents
skills/                   # Skill documents and SWMM workflow scripts
data/                     # Input INP, GIS, rainfall, and observation files
logs/                     # Saved experiment traces and result artifacts
requirements.txt          # Python dependencies
```

`data/results/` is scratch space for active runs. The batch runner clears it between tasks. Preserved experiment artifacts are stored under `logs/`.

## Setup

Python 3.10 or 3.11 is recommended.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

The code reads model credentials from environment variables. No API keys are stored in the repository.

For Hugging Face endpoint models and VLM figure analysis:

```powershell
$env:HF_TOKEN = Read-Host "HF token"
```

For DeepSeek through LangChain:

```powershell
$env:DEEPSEEK_API_KEY = Read-Host "DeepSeek API key"
```

Optional LangSmith tracing:

```powershell
$env:LANGSMITH_API_KEY = Read-Host "LangSmith API key"
```


## Outputs

Experiment traces and preserved artifacts are written under `logs/`, including:

- agent run traces;
- calibrated SWMM INP files;
- ES-ILU progress logs;
- calibration and validation figures;
- generated analysis reports.

Temporary outputs from active runs are written to `data/results/` and are not intended for archival.

## Input Data

The included example data cover one SWMM model, GIS network files, two rainfall events, and corresponding observations:

- `data/base_model.inp`
- `data/GIS/`
- `data/rainfall/event1.txt`
- `data/rainfall/event2.txt`
- `data/observations/event1.csv`
- `data/observations/event2.csv`

## Notes

ES-ILU calibration can be computationally expensive. The default full configuration uses `ne=300` ensemble members and `niter=10` iterations. Several tasks in `tasks.json` use smaller values for smoke tests and architecture comparisons.

## 📖 Citation

If you use this project in your research, please cite:

Wang, J., Liu, S., Fu, G., *et al.* (2026). *Towards autonomous urban drainage modelling: evaluating AI agent architectures for automated SWMM calibration.* **npj Clean Water**. https://doi.org/10.1038/s41545-026-00638-8

<details>
<summary><strong>BibTeX</strong></summary>

```bibtex
@article{wang2026autonomous,
  title   = {Towards autonomous urban drainage modelling: evaluating AI agent architectures for automated {SWMM} calibration},
  author  = {Wang, J. and Liu, S. and Fu, G. and others},
  journal = {npj Clean Water},
  year    = {2026},
  doi     = {10.1038/s41545-026-00638-8},
  url     = {https://doi.org/10.1038/s41545-026-00638-8}
}
```

</details>
