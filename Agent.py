# -*- coding: utf-8 -*-
"""
Agent.py

Three DeepAgent architectures:
  - Architecture 1: Single Agent with Skills
  - Architecture 2: Single Agent with Tools
  - Architecture 3: Multi Agents
"""

import os
import sys
import argparse
import asyncio
import fnmatch
from dataclasses import dataclass
from uuid import uuid4
from pathlib import Path
from typing import Dict, Any, Iterable, Tuple, List, Optional

from deepagents import create_deep_agent
from deepagents.backends.composite import CompositeBackend
from deepagents.backends import FilesystemBackend
try:
    from deepagents.backends.local_shell import LocalShellBackend
except Exception:
    LocalShellBackend = None
from deepagents.backends.protocol import (
    BackendProtocol,
    EditResult,
    FileDownloadResponse,
    FileUploadResponse,
    WriteResult,
)
from langchain.chat_models import init_chat_model
from langgraph.checkpoint.memory import MemorySaver

from shared_prompts import SYSTEM_PROMPT, TASK_INSTRUCTIONS
from tool_wrappers import (
    ALL_TOOLS,
    convert_gis_to_inp,
    select_params,
    calibrate_es_ilu,
    validate_model,
    generate_plots,
    analyze_figures,
)


DEFAULT_TASK = (
    "Run a full SWMM calibration workflow using only files in the data folder. "
    "First build the base INP from data/GIS (use metadata-based lossless restore if available). "
    "Optionally run parameter selection (Morris sensitivity, intent=balanced, top_k=3) to identify "
    "the most influential parameters before calibrating. "
    "Then calibrate the model using ES-ILU with "
    "data/rainfall/event1.txt and data/observations/event1.csv, for subcatchments ['1','2','3','4','5','6','7','8']. "
    "Validate the calibrated model on data/rainfall/event2.txt and data/observations/event2.csv. "
    "Generate figures for calibration and validation performance, and analyse the figures. "
    "Save outputs only under data/results, and only these artifacts: calibrated INP, figures, and one written analysis report. Do not save any other files. "
    "Use absolute paths throughout and finish with a conclusion summarising model performance."
)

REFINEMENT_TASK = (
    "The full calibration pipeline has already completed in the previous run. "
    "All results (calibrated INP, figures, NSE report, figure analysis) are in data/results/. "
    "Your ONLY task now:\n"
    "1. Read the existing results: the NSE report and figure analysis report in data/results/.\n"
    "2. Based on NSE values and figure quality, decide whether to tune the ES-ILU hyperparameters: "
    "sigma_rel (default 0.033), niter (default 10), ne (default 300).\n"
    "   - Lower sigma_rel tightens the observation constraint. "
    "Higher niter or ne may improve convergence at the cost of runtime.\n"
    "   - If NSE \u2265 0.7, you may conclude calibration is already satisfactory and skip re-calibration.\n"
    "3. If adjustment is warranted, re-run ONLY these steps for TWO micro-tuning runs using last Round as baseline: "
    "calibrate_es_ilu \\u2192 validate_model \\u2192 generate_plots \\u2192 analyze_figures.\n"
    "   - Perform exactly two micro-tuning runs.\n"
    "   - In each run, modify only one hyperparameter (sigma_rel or niter or ne), "
    "and keep the other hyperparameters at last Round baseline values.\n"
    "   - Compare both runs against last Round baseline using NSE and figure quality, and provide final insights.\n"
    "CRITICAL CONSTRAINTS:\n"
    "- Do NOT re-run GIS conversion. The INP already exists.\n"
    "- Do NOT re-run parameter selection (select_params).\n"
    "- Do NOT write any Python scripts or shell commands.\n"
    "- Do NOT repeat steps that already succeeded in the previous run.\n"
    "- Keep previous outputs unchanged; save new outputs with clear suffixes, do not overwrite old files."
)

# ---------------------------------------------------------------------------
# UTF8ShellBackend 鈥?forces UTF-8 decoding so Chinese VLM output isn't lost
# ---------------------------------------------------------------------------
if LocalShellBackend is not None:
    class UTF8ShellBackend(LocalShellBackend):
        """LocalShellBackend that forces UTF-8 decoding for subprocess output."""

        def execute(self, command: str):
            import subprocess as _subprocess
            from deepagents.backends.protocol import ExecuteResponse

            if not command or not isinstance(command, str):
                return ExecuteResponse(output="Error: Command must be a non-empty string.", exit_code=1, truncated=False)

            try:
                result = _subprocess.run(
                    command, check=False, shell=True, capture_output=True,
                    text=True, encoding="utf-8", errors="replace",
                    timeout=self._timeout, env=self._env, cwd=str(self.cwd),
                )
            except _subprocess.TimeoutExpired:
                return ExecuteResponse(output=f"Command timed out after {self._timeout}s.", exit_code=124, truncated=False)

            output_parts = []
            if result.stdout:
                output_parts.append(result.stdout)
            if result.stderr:
                stderr_lines = result.stderr.strip().split("\n")
                output_parts.extend(f"[stderr] {line}" for line in stderr_lines)
            output = "\n".join(output_parts) if output_parts else "<no output>"

            truncated = False
            if len(output) > self._max_output_bytes:
                output = output[:self._max_output_bytes]
                output += f"\n\n... Output truncated at {self._max_output_bytes} bytes."
                truncated = True

            if result.returncode != 0:
                output = f"{output.rstrip()}\n\nExit code: {result.returncode}"

            return ExecuteResponse(output=output, exit_code=result.returncode, truncated=truncated)

_PROJECT_DIR = Path(__file__).resolve().parent
checkpointer = MemorySaver()


@dataclass
class RuntimeConfig:
    api_key: Optional[str]
    windows_project_root: str
    python_exe: str
    shell_timeout: int
    llm_max_tokens: int
    llm_temperature: float


def _build_path_guidance(windows_project_root: str, python_exe: str) -> str:
    return (
        "Path rules (CRITICAL):\n"
        "0) Project root (Windows absolute):\n"
        f"   {windows_project_root}\n"
        "   All real file paths used in execute/python MUST be under this directory.\n"
        "   Do NOT invent paths like D:\\skills\\... or D:\\data\\... unless they are under the project root.\n"
        "\n"
        "1) For filesystem tools (ls, read_file, edit_file, write_file, glob, grep), "
        "use virtual absolute paths starting with '/'. Examples:\n"
        "   - /data/GIS\n"
        "   - /data/observations/event1.csv\n"
        "   - /skills/calibrate/SKILL.md\n"
        "\n"
        "2) For execute tool, use real Windows absolute paths under the project root. Examples:\n"
        f"   - {windows_project_root}\\data\\results\\base_model.inp\n"
        f"   - {windows_project_root}\\skills\\gis-to-inp\\Scripts\\gis_to_inp_cli.py\n"
        "\n"
        "3) When running Python via execute, call the interpreter explicitly:\n"
        f"   {python_exe}\n"
        "\n"
        "4) Do not use shell commands like ls/which/where in execute; "
        "use filesystem tools or 'dir' (cmd) only when necessary."
    )


def _default_runtime_config() -> RuntimeConfig:
    return RuntimeConfig(
        api_key=os.environ.get("OPENAI_API_KEY"),
        windows_project_root=str(_PROJECT_DIR),
        python_exe=os.environ.get("SWMM_PYTHON_EXE") or sys.executable,
        shell_timeout=600,
        llm_max_tokens=3000,
        llm_temperature=0.0,
    )



# ---------------------------------------------------------------------------
# PreloadedSkillsBackend - in-memory backend for skill files (no Windows \\ bug)
# ---------------------------------------------------------------------------
class PreloadedSkillsBackend(BackendProtocol):
    """In-memory backend pre-loaded with skill files from disk.

    Walks *disk_root* once at init, reads every file into memory, and stores
    them under POSIX virtual paths (forward slashes only).  All subsequent
    ``ls_info`` / ``read`` / ``download_files`` calls are served from the
    in-memory dict, completely bypassing ``FilesystemBackend`` and its
    Windows backslash bug.

    Paths stored internally do **not** include the route prefix used by
    ``CompositeBackend`` - the composite layer strips it before calling us.
    For example, if the composite route is ``"/skills/"``, a file at
    ``skills/calibrate/SKILL.md`` on disk is stored here as
    ``/calibrate/SKILL.md``.
    """

    def __init__(self, disk_root: Path):
        self._files: Dict[str, bytes] = {}  # virtual_path -> raw bytes
        self._load_from_disk(disk_root)

    # -- bootstrap ---------------------------------------------------------
    def _load_from_disk(self, disk_root: Path) -> None:
        for dirpath, dirnames, filenames in os.walk(disk_root):
            # skip __pycache__ entirely
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fname in filenames:
                full = Path(dirpath) / fname
                rel = full.relative_to(disk_root).as_posix()  # always "/"
                vpath = f"/{rel}"
                self._files[vpath] = full.read_bytes()

    # -- ls_info -----------------------------------------------------------
    def ls_info(self, path: str) -> list:
        prefix = path.rstrip("/") + "/"
        if prefix == "//":
            prefix = "/"
        children: Dict[str, bool] = {}  # name -> is_dir
        for fpath in self._files:
            if not fpath.startswith(prefix):
                continue
            rest = fpath[len(prefix):]
            if not rest:
                continue
            name = rest.split("/")[0]
            is_dir = "/" in rest
            children[name] = children.get(name, False) or is_dir
        results = []
        for name, is_dir in sorted(children.items()):
            p = prefix + name + ("/" if is_dir else "")
            results.append({"path": p, "is_dir": is_dir, "size": 0})
        return results

    # -- read --------------------------------------------------------------
    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> str:
        data = self._files.get(file_path)
        if data is None:
            return f"Error: file not found: {file_path}"
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        selected = lines[offset:offset + limit]
        numbered = []
        for i, line in enumerate(selected, start=offset + 1):
            numbered.append(f"     {i}\t{line}")
        return "\n".join(numbered)

    # -- grep_raw ----------------------------------------------------------
    def grep_raw(self, pattern: str, path: str | None = None,
                 glob: str | None = None) -> list | str:
        matches: list = []
        search_prefix = "/"
        if path and path != "/":
            search_prefix = path.rstrip("/") + "/"
        for fpath, data in self._files.items():
            if search_prefix != "/" and not fpath.startswith(search_prefix):
                continue
            if glob and not fnmatch.fnmatch(fpath.split("/")[-1], glob):
                continue
            text = data.decode("utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), 1):
                if pattern in line:
                    matches.append({"path": fpath, "line": lineno, "text": line})
        return matches

    # -- glob_info ---------------------------------------------------------
    def glob_info(self, pattern: str, path: str = "/") -> list:
        results: list = []
        for fpath in self._files:
            fname = fpath.split("/")[-1]
            if fnmatch.fnmatch(fpath, pattern) or fnmatch.fnmatch(fname, pattern):
                results.append({
                    "path": fpath,
                    "is_dir": False,
                    "size": len(self._files[fpath]),
                })
        return sorted(results, key=lambda x: x["path"])

    # -- write / edit (in-memory) ------------------------------------------
    def write(self, file_path: str, content: str) -> WriteResult:
        self._files[file_path] = content.encode("utf-8")
        return WriteResult(path=file_path, files_update=None)

    def edit(self, file_path: str, old_string: str, new_string: str,
             replace_all: bool = False) -> EditResult:
        data = self._files.get(file_path)
        if data is None:
            return EditResult(error=f"File not found: {file_path}")
        text = data.decode("utf-8", errors="replace")
        if old_string not in text:
            return EditResult(error=f"old_string not found in {file_path}")
        count = text.count(old_string)
        if not replace_all and count > 1:
            return EditResult(
                error=f"old_string not unique in {file_path} ({count} occurrences)"
            )
        new_text = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
        self._files[file_path] = new_text.encode("utf-8")
        return EditResult(path=file_path, files_update=None,
                          occurrences=count if replace_all else 1)

    # -- upload / download -------------------------------------------------
    def upload_files(self, files: list) -> list:
        results = []
        for fpath, content in files:
            self._files[fpath] = content
            results.append(FileUploadResponse(path=fpath))
        return results

    def download_files(self, paths: list) -> list:
        results = []
        for fpath in paths:
            data = self._files.get(fpath)
            if data is not None:
                results.append(FileDownloadResponse(path=fpath, content=data))
            else:
                results.append(FileDownloadResponse(path=fpath, error="file_not_found"))
        return results



def _with_path_guidance(base_prompt: str, windows_project_root: str, path_guidance: str) -> str:
    return base_prompt + "\n\n" + path_guidance



def _make_sub_prompt(path_guidance: str, *task_keys: str) -> str:
    sections = [TASK_INSTRUCTIONS[k] for k in task_keys]
    sub_agent_prefix = (
        "You are a specialized sub-agent in a SWMM calibration pipeline. "
        "Use the tools provided to you to complete the assigned task. "
        "Always use correct absolute paths per tool.\n\n"
        f"{path_guidance}\n\n"
    )
    return sub_agent_prefix + "\n\n---\n\n".join(sections)


def _build_default_model(
    model_name: str,
    api_key: Optional[str],
    max_tokens: int,
    temperature: float,
) -> Any:
    """Initialise the default LLM via LangChain's init_chat_model."""
    return init_chat_model(
        model=model_name,
        api_key=api_key,
        max_tokens=max_tokens,
        temperature=temperature,
    )


def _build_project_backend(project_root: Path, shell_timeout: int) -> BackendProtocol:
    """Build the default project backend used by all architectures."""
    mpl_dir = project_root / ".matplotlib"
    mpl_dir.mkdir(parents=True, exist_ok=True)

    if LocalShellBackend is not None:
        return UTF8ShellBackend(
            root_dir=str(project_root),
            virtual_mode=True,
            timeout=shell_timeout,
            inherit_env=True,
            env={"MPLCONFIGDIR": str(mpl_dir), "MPLBACKEND": "Agg"},
        )
    return FilesystemBackend(root_dir=str(project_root), virtual_mode=True)


def build_agent_skills(
    model_name: str = "deepseek:deepseek-chat",
    model: Optional[Any] = None,
    runtime: Optional[RuntimeConfig] = None,
):
    runtime = runtime or _default_runtime_config()
    if model is None:
        model = _build_default_model(
            model_name=model_name,
            api_key=runtime.api_key,
            max_tokens=runtime.llm_max_tokens,
            temperature=runtime.llm_temperature,
        )
    project_root = _PROJECT_DIR
    path_guidance = _build_path_guidance(
        windows_project_root=runtime.windows_project_root,
        python_exe=runtime.python_exe,
    )
    skills_root = project_root / "skills"
    skills_mem = PreloadedSkillsBackend(disk_root=skills_root)

    backend = CompositeBackend(
        default=_build_project_backend(project_root, runtime.shell_timeout),
        routes={"/skills/": skills_mem},
    )

    return create_deep_agent(
        model=model,
        backend=backend,
        system_prompt=_with_path_guidance(SYSTEM_PROMPT, runtime.windows_project_root, path_guidance),
        skills=["/skills/"],
        checkpointer=checkpointer,
    )



def build_agent_tools(
    model_name: str = "deepseek:deepseek-chat",
    model: Optional[Any] = None,
    runtime: Optional[RuntimeConfig] = None,
):
    runtime = runtime or _default_runtime_config()
    if model is None:
        model = _build_default_model(
            model_name=model_name,
            api_key=runtime.api_key,
            max_tokens=runtime.llm_max_tokens,
            temperature=runtime.llm_temperature,
        )
    project_root = _PROJECT_DIR
    path_guidance = _build_path_guidance(
        windows_project_root=runtime.windows_project_root,
        python_exe=runtime.python_exe,
    )
    return create_deep_agent(
        model=model,
        backend=_build_project_backend(project_root, runtime.shell_timeout),
        tools=ALL_TOOLS,
        system_prompt=_with_path_guidance(SYSTEM_PROMPT, runtime.windows_project_root, path_guidance),
        checkpointer=checkpointer,
    )


def build_agent_multi(
    model_name: str = "deepseek:deepseek-chat",
    model: Optional[Any] = None,
    runtime: Optional[RuntimeConfig] = None,
):
    runtime = runtime or _default_runtime_config()
    if model is None:
        model = _build_default_model(
            model_name=model_name,
            api_key=runtime.api_key,
            max_tokens=runtime.llm_max_tokens,
            temperature=runtime.llm_temperature,
        )
    project_root = _PROJECT_DIR
    path_guidance = _build_path_guidance(
        windows_project_root=runtime.windows_project_root,
        python_exe=runtime.python_exe,
    )
    gis_converter = {
        "name": "gis_converter",
        "description": (
            "Convert GIS shapefiles to SWMM INP. "
            "Tool: convert_gis_to_inp. "
            "GIS data is in /data/GIS/ (contains Manholes.shp, Links.shp, _swmm_transfer_meta.json). "
            "Output INP to /data/results/. Use gis_path='/data/GIS'."
        ),
        "model": model,
        "tools": [convert_gis_to_inp],
        "system_prompt": _make_sub_prompt(path_guidance, "gis-to-inp"),
    }
    calibrator = {
        "name": "calibrator",
        "description": (
            "Parameter selection and model calibration ONLY. "
            "Tools: select_params (Morris sensitivity), calibrate_es_ilu (ES-ILU calibration). "
            "Does NOT validate, plot, or analyse figures — use validator_analyst for those."
        ),
        "model": model,
        "tools": [select_params, calibrate_es_ilu],
        "system_prompt": _make_sub_prompt(path_guidance, "intent_sensitive_selection", "calibrate"),
    }
    validator_analyst = {
        "name": "validator_analyst",
        "description": (
            "Validation, figure generation, and figure analysis. "
            "Tools: validate_model (run model & compute NSE), "
            "generate_plots (time-series + parameter bar chart figures), "
            "analyze_figures (VLM figure interpretation). "
            "IMPORTANT: calibrated_inp_list must contain ONLY calibrated INP files — "
            "never include the base/original INP from GIS conversion. "
            "When calling generate_plots, always pass calibrated_params if parameter selection was used."
        ),
        "model": model,
        "tools": [validate_model, generate_plots, analyze_figures],
        "system_prompt": _make_sub_prompt(path_guidance, "compute_results", "plot_figures", "figure_analysis"),
    }
    return create_deep_agent(
        model=model,
        backend=_build_project_backend(project_root, runtime.shell_timeout),
        system_prompt=_with_path_guidance(SYSTEM_PROMPT, runtime.windows_project_root, path_guidance),
        subagents=[gis_converter, calibrator, validator_analyst],
        checkpointer=checkpointer,
    )


BUILDERS = {
    "skills": build_agent_skills,
    "tools": build_agent_tools,
    "multi": build_agent_multi,
}


def build_agent(
    arch: str = "tools",
    model_name: str = "deepseek:deepseek-chat",
    model: Optional[Any] = None,
    runtime: Optional[RuntimeConfig] = None,
):
    if arch not in BUILDERS:
        raise ValueError(f"Unknown architecture: {arch!r}. Choose from {list(BUILDERS)}")
    return BUILDERS[arch](model_name=model_name, model=model, runtime=runtime)


def _configure_langsmith_tracing(enabled: bool, project: str) -> Tuple[bool, str]:
    """Configure LangSmith tracing env vars and report readiness."""
    if not enabled:
        print("LangSmith tracing disabled")
        return False, "LangSmith tracing disabled (--trace not set)."

    # Keep both new and legacy env aliases for broader compatibility.
    env_updates = {
        "LANGSMITH_TRACING": "true",
    }
    if project:
        env_updates["LANGSMITH_PROJECT"] = project
    for key, value in env_updates.items():
        os.environ[key] = value

    try:
        import langsmith  # noqa: F401
    except Exception as exc:
        return False, f"LangSmith package not available: {exc}"

    api_key = os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY")
    if not api_key:
        return False, "LangSmith API key missing (set LANGSMITH_API_KEY or LANGCHAIN_API_KEY)."

    project_name = project or os.environ.get("LANGSMITH_PROJECT") or "default"
    return True, f"LangSmith tracing ready. project='{project_name}'."


def _build_stream_config(
    arch: str,
    trace_enabled: bool,
    trace_project: str,
    run_name: str,
    thread_id: str,
) -> Dict[str, Any]:
    tags = ["deepagents", arch]
    metadata: Dict[str, Any] = {}
    if trace_enabled:
        tags.append("langsmith")
        metadata["langsmith_tracing"] = True
        if trace_project:
            metadata["langsmith_project"] = trace_project
    return {
        "configurable": {"thread_id": thread_id},
        "run_name": run_name,
        "tags": tags,
        "metadata": metadata,
    }


def _normalize_usage(usage: Dict[str, Any]) -> Dict[str, int]:
    keys = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for k in keys:
        if k in usage and isinstance(usage[k], int):
            keys[k] = usage[k]
    if keys["prompt_tokens"] == 0 and isinstance(usage.get("input_tokens"), int):
        keys["prompt_tokens"] = usage["input_tokens"]
    if keys["completion_tokens"] == 0 and isinstance(usage.get("output_tokens"), int):
        keys["completion_tokens"] = usage["output_tokens"]
    if keys["total_tokens"] == 0 and isinstance(usage.get("total_tokens"), int):
        keys["total_tokens"] = usage["total_tokens"]
    return keys


def _extract_usage_from_message(msg: Any) -> Dict[str, int]:
    usage = {}
    for attr in ("usage_metadata", "response_metadata", "additional_kwargs"):
        meta = getattr(msg, attr, None)
        if not isinstance(meta, dict):
            continue
        if any(k in meta for k in ("input_tokens", "output_tokens", "total_tokens")):
            usage = meta
            break
        if "token_usage" in meta and isinstance(meta["token_usage"], dict):
            usage = meta["token_usage"]
            break
        if "usage" in meta and isinstance(meta["usage"], dict):
            usage = meta["usage"]
            break
    return _normalize_usage(usage) if usage else {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _sum_usages(usages: Iterable[Dict[str, int]]) -> Dict[str, int]:
    total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for usage in usages:
        total["prompt_tokens"] += usage.get("prompt_tokens", 0)
        total["completion_tokens"] += usage.get("completion_tokens", 0)
        total["total_tokens"] += usage.get("total_tokens", 0)
    if total["total_tokens"] == 0:
        total["total_tokens"] = total["prompt_tokens"] + total["completion_tokens"]
    return total


def _get_message_text(msg: Any) -> str:
    content = getattr(msg, "content", "")
    if isinstance(content, list):
        content = "\n".join(str(x) for x in content)
    return str(content)


def _get_message_role(msg: Any) -> str:
    return getattr(msg, "type", None) or getattr(msg, "role", None) or "unknown"


def _unwrap_messages_container(value: Any) -> Optional[List[Any]]:
    if isinstance(value, list):
        return value
    if hasattr(value, "value"):
        return _unwrap_messages_container(getattr(value, "value"))
    return None


def _extract_messages_from_event(event: Any) -> Optional[List[Any]]:
    if not isinstance(event, dict):
        return None
    if "messages" in event:
        return _unwrap_messages_container(event.get("messages"))
    for val in event.values():
        if isinstance(val, dict):
            found = _extract_messages_from_event(val)
            if found:
                return found
        else:
            found = _unwrap_messages_container(val)
            if found:
                return found
    return None


def _dedupe_messages(messages: List[Any]) -> List[Any]:
    if not messages:
        return messages
    deduped = []
    last_key = None
    for msg in messages:
        key = (_get_message_role(msg), _get_message_text(msg))
        if key == last_key:
            continue
        deduped.append(msg)
        last_key = key
    keys = [(_get_message_role(msg), _get_message_text(msg)) for msg in deduped]
    if len(keys) % 2 == 0:
        half = len(keys) // 2
        if keys[:half] == keys[half:]:
            return deduped[:half]
    return deduped


def _calculate_usage(
    messages: Iterable[Any],
) -> Dict[str, int]:
    usages = [_extract_usage_from_message(msg) for msg in messages]
    total_usage = _sum_usages(usages)
    return total_usage


def _truncate_text(text: str, limit: int = 1200) -> Tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n...[truncated]...", True


def _format_trace(messages: Iterable[Any], truncate: bool = True) -> str:
    lines = []
    for idx, msg in enumerate(messages, 1):
        role = _get_message_role(msg)
        name = getattr(msg, "name", None)
        header = f"[{idx}] {role}"
        if name:
            header += f" ({name})"
        lines.append(header)

        content = _get_message_text(msg)
        if truncate:
            content, _ = _truncate_text(content)
        if content:
            lines.append(content)

        tool_calls = getattr(msg, "tool_calls", None) or getattr(msg, "additional_kwargs", {}).get("tool_calls")
        if tool_calls:
            lines.append("tool_calls:")
            for call in tool_calls:
                lines.append(str(call))
        lines.append("-" * 40)
    return "\n".join(lines)


def _format_markdown(
    task: str,
    arch: str,
    model: str,
    trace: str,
    usage: Dict[str, int],
    stream_repr: List[str],
) -> str:
    lines = [
        "# DeepAgent Run Trace",
        "",
        "## Task",
        "",
        task,
        "",
        "## Config",
        "",
        f"- arch: {arch}",
        f"- model: {model}",
        "",
        "## Trace",
        "",
        "```",
        trace,
        "```",
        "",
        "## Stream Events (repr)",
        "",
        "```",
        "\n".join(stream_repr),
        "```",
        "",
        "## Token Usage (Total)",
        "",
        f"- prompt_tokens: {usage['prompt_tokens']}",
        f"- completion_tokens: {usage['completion_tokens']}",
        f"- total_tokens: {usage['total_tokens']}",
    ]
    lines.append("")
    return "\n".join(lines)


async def run_once(
    agent,
    task: str,
    arch: str,
    model: str,
    trace_full: bool,
    md_path: Path,
    stream_config: Dict[str, Any],
) -> Dict[str, Any]:
    payload = {"messages": [{"role": "user", "content": task}]}
    messages: List[Any] = []
    stream_messages: List[Any] = []
    stream_repr: List[str] = []

    print("\n=== Streaming ===")
    event_count = 0
    async for event in agent.astream(payload, config=stream_config):
        event_count += 1
        header = f"--- event {event_count} ---"
        body = repr(event)
        stream_repr.append(f"{header}\n{body}")
        print(f"\n{header}")
        print(body)
        new_messages = _extract_messages_from_event(event)
        if new_messages:
            stream_messages.extend(new_messages)
    print("\n\n=== Streaming Done ===")

    if stream_messages:
        messages = stream_messages
    else:
        result = agent.invoke(payload, config=stream_config)
        messages = result.get("messages", [])

    messages = _dedupe_messages(messages)
    trace_text = _format_trace(messages, truncate=not trace_full)
    total_usage = _calculate_usage(messages)
    final_answer = _get_message_text(messages[-1]) if messages else ""

    print("\n=== Trace ===")
    print(trace_text)
    print("\n=== Token Usage (Total) ===")
    print(
        f"prompt_tokens={total_usage['prompt_tokens']} | "
        f"completion_tokens={total_usage['completion_tokens']} | "
        f"total_tokens={total_usage['total_tokens']}"
    )
    if final_answer:
        print("\n=== Final Answer ===")
        print(final_answer)

    md_text = _format_markdown(
        task,
        arch,
        model,
        trace_text,
        total_usage,
        stream_repr,
    )
    md_path.write_text(md_text, encoding="utf-8")
    return {
        "usage": total_usage,
        "final_answer": final_answer,
        "trace_path": str(md_path),
    }


def _build_iteration_task(base_task: str, iteration: int, prev_answer: str = "") -> str:
    if iteration <= 1:
        return base_task
    prefix = (
        f"=== Round {iteration - 1} summary ===\n{prev_answer}\n\n"
        if prev_answer else ""
    )
    return prefix + REFINEMENT_TASK


def _resolve_iteration_trace_path(base_md_path: Path, iteration: int, total: int) -> Path:
    if total <= 1:
        return base_md_path
    suffix = base_md_path.suffix or ".md"
    return base_md_path.with_name(f"{base_md_path.stem}.iter{iteration}{suffix}")


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="SWMM Calibration DeepAgent")
    ap.add_argument("--arch", choices=["skills", "tools", "multi"], default="multi")
    ap.add_argument("--model", default="openai/gpt-oss-20b", choices=["deepseek:deepseek-chat", "Qwen/Qwen3-235B-A22B-Instruct-2507", "Qwen/Qwen3-32B",
                                                                                                    "Qwen/Qwen3-14B", "Qwen/Qwen3-8B", "Qwen/Qwen3-4B-Instruct-2507",
                                                                                                    "openai/gpt-oss-120b", "openai/gpt-oss-20b"])
    ap.add_argument(
        "--model-builder",
        choices=["init_chat_model", "huggingface"],
        default="huggingface",
        help="Model constructor: init_chat_model or HuggingFace endpoint.",
    )
    ap.add_argument("--api-key-env", default="OPENAI_API_KEY", help="Env var name for init_chat_model API key.")
    ap.add_argument("--llm-max-tokens", type=int, default=3000)
    ap.add_argument("--llm-temperature", type=float, default=0.0)
    ap.add_argument("--windows-project-root", default=str(_PROJECT_DIR))
    ap.add_argument("--python-exe", default=(os.environ.get("SWMM_PYTHON_EXE") or sys.executable))
    ap.add_argument("--shell-timeout", type=int, default=600)
    ap.add_argument("--run-name", default="multi_gpt20")
    ap.add_argument("--task", default=DEFAULT_TASK)
    ap.add_argument("--iterations", type=int, default=1,
                    help="Number of sequential refinement rounds.")
    ap.add_argument("--trace-full", action="store_true")
    ap.add_argument("--trace-md", default="run_trace.md", help="Markdown output path.")
    ap.add_argument("--trace", default=True, action="store_true", help="Enable LangSmith tracing")
    ap.add_argument("--trace-project", default="swmm_agent-gpt_with_1R", help="LangSmith project name")
    return ap.parse_args()


def _build_explicit_model(args: argparse.Namespace) -> Tuple[Optional[Any], str]:
    model_desc = args.model
    explicit_model: Optional[Any] = None

    if args.model_builder == "huggingface":
        explicit_model = init_chat_model(
            args.model,
            model_provider="huggingface",
            backend="endpoint",
            temperature=args.llm_temperature,
            max_new_tokens=args.llm_max_tokens,
            timeout=300,
        )
        model_desc = f"{args.model} [HUGGINGFACE]"

    return explicit_model, model_desc


def main() -> None:

    args = _parse_args()
    task = args.task.strip() if args.task else ""
    if not task:
        raise SystemExit("Empty task.")

    explicit_model, model_desc = _build_explicit_model(args)
    runtime = RuntimeConfig(
        api_key=os.environ.get(args.api_key_env),
        windows_project_root=args.windows_project_root,
        python_exe=args.python_exe,
        shell_timeout=args.shell_timeout,
        llm_max_tokens=args.llm_max_tokens,
        llm_temperature=args.llm_temperature,
    )

    trace_ok, trace_msg = _configure_langsmith_tracing(
        enabled=args.trace,
        project=args.trace_project,
    )

    agent = build_agent(
        arch=args.arch,
        model_name=args.model,
        model=explicit_model,
        runtime=runtime,
    )
    print(f"Architecture '{args.arch}' agent created successfully with model '{model_desc}'.")
    if args.trace:
        status = "OK" if trace_ok else "WARN"
        print(f"[{status}] {trace_msg}")

    if args.iterations < 1:
        raise SystemExit("--iterations must be >= 1.")

    base_md_path = Path(args.trace_md).resolve()
    iteration_usages: List[Dict[str, int]] = []
    prev_final_answer: str = ""

    for i in range(1, args.iterations + 1):
        iter_task = _build_iteration_task(base_task=task, iteration=i, prev_answer=prev_final_answer)
        iter_md_path = _resolve_iteration_trace_path(base_md_path, i, args.iterations)
        iter_run_name = args.run_name if args.iterations == 1 else f"{args.run_name}-iter{i}"
        iter_thread_id = f"swmm-agent-run-{uuid4().hex[:8]}"
        iter_stream_config = _build_stream_config(
            arch=args.arch,
            trace_enabled=trace_ok,
            trace_project=args.trace_project,
            run_name=iter_run_name,
            thread_id=iter_thread_id,
        )
        print(f"\n=== Iteration {i}/{args.iterations} ===")
        result = asyncio.run(
            run_once(
                agent=agent,
                task=iter_task,
                arch=args.arch,
                model=model_desc,
                trace_full=args.trace_full,
                md_path=iter_md_path,
                stream_config=iter_stream_config,
            )
        )
        iteration_usages.append(result["usage"])
        prev_final_answer = result.get("final_answer", "")
        print(f"[Iteration {i}] Trace: {iter_md_path}")

    if args.iterations > 1:
        total_usage = _sum_usages(iteration_usages)
        print("\n=== Iterative Summary ===")
        print(f"iterations={args.iterations}")
        print(
            f"aggregate_prompt_tokens={total_usage['prompt_tokens']} | "
            f"aggregate_completion_tokens={total_usage['completion_tokens']} | "
            f"aggregate_total_tokens={total_usage['total_tokens']}"
        )

if __name__ == "__main__":
    main()
