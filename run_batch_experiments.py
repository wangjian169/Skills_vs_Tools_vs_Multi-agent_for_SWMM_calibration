# -*- coding: utf-8 -*-
"""Batch benchmark runner for tasks.json.

Loop order:
1. models
2. architectures
3. tasks

For every (model, arch, task) run, this script:
- executes the task through the selected DeepAgent architecture
- aggregates total token usage for the whole run
- records whether the expected skill/tool path was actually invoked
- appends one row to total_results.csv
- clears data/results after the run finishes

Notes on success criteria:
- skills: success means the expected SKILL.md was read successfully
- tools: success means the expected tool completed successfully
- multi: success prefers the expected tool completing successfully; if internal
  tool callbacks are not surfaced, it falls back to the expected subagent task
  completing successfully
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import csv
import json
import os
import shutil
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from uuid import uuid4

from Agent import (
    RuntimeConfig,
    _build_stream_config,
    _configure_langsmith_tracing,
    build_agent,
    init_chat_model,
    run_once,
)

try:
    from langchain_core.callbacks import BaseCallbackHandler
except Exception:  # pragma: no cover - fallback for older langchain layouts
    from langchain.callbacks.base import BaseCallbackHandler  # type: ignore

try:
    import httpx
except Exception:  # pragma: no cover - optional at import time
    httpx = None  # type: ignore

try:
    from huggingface_hub.errors import HfHubHTTPError
except Exception:  # pragma: no cover - optional at import time
    HfHubHTTPError = None  # type: ignore


PROJECT_ROOT = Path(__file__).resolve().parent
TASKS_JSON = PROJECT_ROOT / "tasks.json"
RESULTS_DIR = PROJECT_ROOT / "data" / "results"
TOTAL_RESULTS_CSV = PROJECT_ROOT / "total_results.csv"
TRACE_ROOT = PROJECT_ROOT / "logs" / "batch_runs"

DEFAULT_MODELS = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "Qwen/Qwen3-235B-A22B-Instruct-2507",
    "Qwen/Qwen3-32B",
    "Qwen/Qwen3-14B",
    "Qwen/Qwen3-8B",
    "Qwen/Qwen3-4B-Instruct-2507",
]

DEFAULT_ARCHES = ["skills", "tools", "multi"]

EXPECTED_TOOL_BY_CATEGORY = {
    "gis_to_inp": "convert_gis_to_inp",
    "select_params": "select_params",
    "calibrate": "calibrate_es_ilu",
    "valid": "validate_model",
    "generate_plot": "generate_plots",
}

EXPECTED_SKILL_BY_CATEGORY = {
    "gis_to_inp": "/skills/gis-to-inp/SKILL.md",
    "select_params": "/skills/intent_sensitive_selection/SKILL.md",
    "calibrate": "/skills/calibrate/SKILL.md",
    "valid": "/skills/compute_results_skill/SKILL.md",
    "generate_plot": "/skills/plot_figures/SKILL.md",
}

EXPECTED_SUBAGENT_BY_CATEGORY = {
    "gis_to_inp": "gis_converter",
    "select_params": "calibrator",
    "calibrate": "calibrator",
    "valid": "validator_analyst",
    "generate_plot": "validator_analyst",
}

CSV_COLUMNS = [
    "timestamp_utc",
    "model",
    "arch",
    "task_id",
    "category",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "success",
    "success_reason",
    "expected_target",
    "completed_tools",
    "completed_subagents",
    "completed_skill_reads",
    "trace_path",
    "final_answer_excerpt",
    "error",
]

TRANSIENT_FAILURE_REASON_PREFIX = "transient upstream model error"
RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
RETRYABLE_ERROR_TOKENS = (
    "500 internal server error",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
    "429 too many requests",
    "server error '500",
    "internal error - we're working hard to fix this as soon as possible",
    "rate limit",
    "temporarily unavailable",
    "connection reset",
    "connection aborted",
    "remoteprotocolerror",
    "read timeout",
    "timed out",
)


def _slugify(value: str) -> str:
    safe = []
    for ch in value:
        if ch.isalnum() or ch in ("-", "_", "."):
            safe.append(ch)
        else:
            safe.append("_")
    return "".join(safe).strip("_") or "item"


def _iter_exception_chain(exc: BaseException) -> Iterable[BaseException]:
    seen: Set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        yield current
        seen.add(id(current))
        current = current.__cause__ or current.__context__


def _extract_status_code(exc: BaseException) -> Optional[int]:
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    return None


def _flatten_exception_text(exc: BaseException) -> str:
    parts: List[str] = []
    for item in _iter_exception_chain(exc):
        text = str(item).strip()
        if text:
            parts.append(text)
    return "\n".join(parts)


def _looks_like_transient_error_text(text: str) -> bool:
    normalized = str(text or "").lower()
    if not normalized:
        return False
    if TRANSIENT_FAILURE_REASON_PREFIX in normalized:
        return True
    return any(token in normalized for token in RETRYABLE_ERROR_TOKENS)


def _is_transient_model_error(exc: BaseException) -> bool:
    for item in _iter_exception_chain(exc):
        status_code = _extract_status_code(item)
        if status_code in RETRYABLE_STATUS_CODES:
            return True
        if isinstance(item, (TimeoutError, asyncio.TimeoutError)):
            return True
        if httpx is not None and isinstance(item, httpx.TransportError):
            return True
        if HfHubHTTPError is not None and isinstance(item, HfHubHTTPError):
            if _looks_like_transient_error_text(str(item)):
                return True

    return _looks_like_transient_error_text(_flatten_exception_text(exc))


def _format_transient_failure_reason(exc: BaseException) -> str:
    status_code = None
    for item in _iter_exception_chain(exc):
        status_code = _extract_status_code(item)
        if status_code is not None:
            break

    details: List[str] = []
    if status_code is not None:
        details.append(f"HTTP {status_code}")

    error_text = _flatten_exception_text(exc)
    if "router.huggingface.co" in error_text.lower():
        details.append("router.huggingface.co")

    if not details and error_text:
        details.append(_truncate_text(error_text.replace("\r", " ").replace("\n", " "), limit=160))

    suffix = f" ({', '.join(details)})" if details else ""
    return f"{TRANSIENT_FAILURE_REASON_PREFIX}{suffix}"


def _retry_delay_seconds(attempt: int, base_delay: float, max_delay: float) -> float:
    return min(max_delay, base_delay * (2 ** max(0, attempt - 1)))


def _load_tasks(path: Path) -> List[Dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("tasks.json must contain a JSON array.")
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("Each task item must be an object.")
        for key in ("id", "category", "description"):
            if key not in item:
                raise ValueError(f"Task item missing required key: {key}")
    return data


def _normalize_usage(usage: Dict[str, Any]) -> Dict[str, int]:
    result = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if not isinstance(usage, dict):
        return result

    if isinstance(usage.get("prompt_tokens"), int):
        result["prompt_tokens"] = usage["prompt_tokens"]
    elif isinstance(usage.get("input_tokens"), int):
        result["prompt_tokens"] = usage["input_tokens"]

    if isinstance(usage.get("completion_tokens"), int):
        result["completion_tokens"] = usage["completion_tokens"]
    elif isinstance(usage.get("output_tokens"), int):
        result["completion_tokens"] = usage["output_tokens"]

    if isinstance(usage.get("total_tokens"), int):
        result["total_tokens"] = usage["total_tokens"]
    else:
        result["total_tokens"] = result["prompt_tokens"] + result["completion_tokens"]
    return result


def _extract_usage_from_message(msg: Any) -> Dict[str, int]:
    for attr in ("usage_metadata", "response_metadata", "additional_kwargs"):
        meta = getattr(msg, attr, None)
        if not isinstance(meta, dict):
            continue
        if "token_usage" in meta and isinstance(meta["token_usage"], dict):
            return _normalize_usage(meta["token_usage"])
        if "usage" in meta and isinstance(meta["usage"], dict):
            return _normalize_usage(meta["usage"])
        if any(k in meta for k in ("input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens")):
            return _normalize_usage(meta)
    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _extract_usage_from_llm_result(response: Any) -> Dict[str, int]:
    llm_output = getattr(response, "llm_output", None)
    if isinstance(llm_output, dict):
        if isinstance(llm_output.get("token_usage"), dict):
            return _normalize_usage(llm_output["token_usage"])
        if isinstance(llm_output.get("usage"), dict):
            return _normalize_usage(llm_output["usage"])
        if any(k in llm_output for k in ("input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens")):
            return _normalize_usage(llm_output)

    generations = getattr(response, "generations", None) or []
    for generation_group in generations:
        for generation in generation_group:
            message = getattr(generation, "message", None)
            if message is None:
                continue
            usage = _extract_usage_from_message(message)
            if usage["total_tokens"] > 0:
                return usage

    return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _sum_usages(usages: Iterable[Dict[str, int]]) -> Dict[str, int]:
    total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for usage in usages:
        total["prompt_tokens"] += usage.get("prompt_tokens", 0)
        total["completion_tokens"] += usage.get("completion_tokens", 0)
        total["total_tokens"] += usage.get("total_tokens", 0)
    if total["total_tokens"] == 0:
        total["total_tokens"] = total["prompt_tokens"] + total["completion_tokens"]
    return total


def _parse_tool_input(input_str: Any, inputs: Any = None) -> Dict[str, Any]:
    if isinstance(inputs, dict):
        return inputs
    if isinstance(input_str, dict):
        return input_str
    if not isinstance(input_str, str):
        return {}

    text = input_str.strip()
    if not text:
        return {}

    for parser in (json.loads, ast.literal_eval):
        try:
            value = parser(text)
        except Exception:
            continue
        if isinstance(value, dict):
            return value
    return {}


@dataclass
class CallbackTracker(BaseCallbackHandler):
    llm_usages: List[Dict[str, int]] = field(default_factory=list)
    _tool_runs: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    completed_tools: List[str] = field(default_factory=list)
    failed_tools: List[str] = field(default_factory=list)
    completed_subagents: List[str] = field(default_factory=list)
    completed_skill_reads: List[str] = field(default_factory=list)

    def on_llm_end(self, response: Any, **kwargs: Any) -> Any:
        self.llm_usages.append(_extract_usage_from_llm_result(response))

    def on_tool_start(self, serialized: Dict[str, Any], input_str: str, **kwargs: Any) -> Any:
        name = (serialized or {}).get("name") or (serialized or {}).get("id") or "unknown_tool"
        parsed = _parse_tool_input(input_str, kwargs.get("inputs"))
        run_id = str(kwargs.get("run_id") or uuid4())
        self._tool_runs[run_id] = {
            "name": str(name),
            "parsed": parsed,
        }

    def on_tool_end(self, output: Any, **kwargs: Any) -> Any:
        run_id = str(kwargs.get("run_id") or "")
        record = self._tool_runs.pop(run_id, None)
        if not record:
            return

        tool_name = record["name"]
        parsed = record["parsed"]
        self.completed_tools.append(tool_name)

        if tool_name == "task":
            subagent = parsed.get("subagent_type")
            if isinstance(subagent, str) and subagent:
                self.completed_subagents.append(subagent)

        if tool_name == "read_file":
            file_path = parsed.get("file_path")
            if isinstance(file_path, str) and file_path.endswith("/SKILL.md"):
                self.completed_skill_reads.append(file_path)

    def on_tool_error(self, error: BaseException, **kwargs: Any) -> Any:
        run_id = str(kwargs.get("run_id") or "")
        record = self._tool_runs.pop(run_id, None)
        if record:
            self.failed_tools.append(record["name"])

    @property
    def total_usage(self) -> Dict[str, int]:
        return _sum_usages(self.llm_usages)


def _build_prompt(task: Dict[str, Any]) -> str:
    # Keep task input semantics aligned with Agent.py: only the task text changes.
    return str(task["description"])


def _build_runtime(args: argparse.Namespace) -> RuntimeConfig:
    return RuntimeConfig(
        api_key=os.environ.get(args.api_key_env),
        windows_project_root=args.windows_project_root,
        python_exe=args.python_exe,
        shell_timeout=args.shell_timeout,
        llm_max_tokens=args.llm_max_tokens,
        llm_temperature=args.llm_temperature,
    )


def _resolve_model_builder(model_name: str, requested_builder: str) -> str:
    if requested_builder != "auto":
        return requested_builder
    if model_name.startswith("deepseek:"):
        return "init_chat_model"
    return "huggingface"


def _build_explicit_model(model_name: str, args: argparse.Namespace) -> Optional[Any]:
    builder = _resolve_model_builder(model_name, args.model_builder)
    if builder == "huggingface":
        return init_chat_model(
            model_name,
            model_provider="huggingface",
            backend="endpoint",
            temperature=args.llm_temperature,
            max_new_tokens=args.llm_max_tokens,
            timeout=args.model_timeout_sec,
        )
    if builder == "init_chat_model":
        return None
    return None


def _model_desc(model_name: str, args: argparse.Namespace) -> str:
    builder = _resolve_model_builder(model_name, args.model_builder)
    if builder == "huggingface":
        return f"{model_name} [HUGGINGFACE]"
    return model_name


def _clear_results_dir(results_dir: Path) -> None:
    results_dir.mkdir(parents=True, exist_ok=True)
    for child in list(results_dir.iterdir()):
        for attempt in range(3):
            try:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=False)
                else:
                    child.unlink(missing_ok=True)
                break
            except FileNotFoundError:
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(1.0)


def _ensure_csv_header(csv_path: Path) -> None:
    if csv_path.exists() and csv_path.stat().st_size > 0:
        return
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()


def _append_csv_row(csv_path: Path, row: Dict[str, Any]) -> None:
    _ensure_csv_header(csv_path)
    with csv_path.open("a", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writerow(row)


def _load_completed_keys(csv_path: Path) -> Set[Tuple[str, str, str]]:
    if not csv_path.exists():
        return set()
    completed: Set[Tuple[str, str, str]] = set()
    with csv_path.open("r", newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            model = row.get("model")
            arch = row.get("arch")
            task_id = row.get("task_id")
            if model and arch and task_id and not _row_is_transient_failure(row):
                completed.add((model, arch, task_id))
    return completed


def _row_is_transient_failure(row: Dict[str, Any]) -> bool:
    success_value = str(row.get("success") or "").strip().lower()
    if success_value in {"1", "true", "yes", "y"}:
        return False
    combined_text = "\n".join(
        str(row.get(field) or "")
        for field in ("success_reason", "error")
    )
    return _looks_like_transient_error_text(combined_text)


def _should_persist_summary_row(row: Dict[str, Any]) -> bool:
    return not _row_is_transient_failure(row)


def _evaluate_success(task: Dict[str, Any], arch: str, tracker: CallbackTracker) -> Tuple[bool, str, str]:
    category = str(task["category"])
    expected_tool = str(task.get("tool_hint") or EXPECTED_TOOL_BY_CATEGORY[category])
    expected_skill = EXPECTED_SKILL_BY_CATEGORY[category]
    expected_subagent = EXPECTED_SUBAGENT_BY_CATEGORY[category]

    if arch == "skills":
        if expected_skill in tracker.completed_skill_reads:
            return True, f"read expected skill file: {expected_skill}", expected_skill
        return False, f"expected skill file not read: {expected_skill}", expected_skill

    if expected_tool in tracker.completed_tools:
        return True, f"completed expected tool: {expected_tool}", expected_tool

    if arch == "multi" and expected_subagent in tracker.completed_subagents:
        return True, f"completed expected subagent task: {expected_subagent}", expected_subagent

    if arch == "multi":
        reason = (
            f"expected tool {expected_tool} or subagent {expected_subagent}; "
            f"completed_tools={tracker.completed_tools}, completed_subagents={tracker.completed_subagents}"
        )
        return False, reason, f"{expected_tool}|{expected_subagent}"

    reason = f"expected tool not completed: {expected_tool}; completed_tools={tracker.completed_tools}"
    return False, reason, expected_tool


def _truncate_text(text: str, limit: int = 300) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def _trace_contains_tool(trace_text: str, tool_name: str) -> bool:
    candidates = [
        f"tool ({tool_name})",
        f"'name': '{tool_name}'",
        f'"name": "{tool_name}"',
    ]
    return any(token in trace_text for token in candidates)


def _trace_contains_subagent(trace_text: str, subagent_name: str) -> bool:
    candidates = [
        f"'subagent_type': '{subagent_name}'",
        f'"subagent_type": "{subagent_name}"',
        f"subagent_type.: .{subagent_name}.",
    ]
    return any(token in trace_text for token in candidates)


def _hydrate_tracker_from_trace(
    tracker: CallbackTracker,
    trace_path: Path,
    task: Dict[str, Any],
    arch: str,
) -> None:
    if not trace_path.exists():
        return

    trace_text = trace_path.read_text(encoding="utf-8", errors="replace")
    expected_tool = str(task.get("tool_hint") or EXPECTED_TOOL_BY_CATEGORY[str(task["category"])])
    expected_skill = EXPECTED_SKILL_BY_CATEGORY[str(task["category"])]
    expected_subagent = EXPECTED_SUBAGENT_BY_CATEGORY[str(task["category"])]

    if expected_tool not in tracker.completed_tools and _trace_contains_tool(trace_text, expected_tool):
        tracker.completed_tools.append(expected_tool)
    if arch == "skills" and expected_skill not in tracker.completed_skill_reads and expected_skill in trace_text:
        tracker.completed_skill_reads.append(expected_skill)
    if arch == "multi" and expected_subagent not in tracker.completed_subagents and _trace_contains_subagent(trace_text, expected_subagent):
        tracker.completed_subagents.append(expected_subagent)


def _make_trace_path(model: str, arch: str, task_id: str) -> Path:
    path = TRACE_ROOT / _slugify(model) / arch
    path.mkdir(parents=True, exist_ok=True)
    return path / f"{task_id}.md"


def _run_task(
    *,
    agent: Any,
    model_name: str,
    model_desc: str,
    arch: str,
    task: Dict[str, Any],
    args: argparse.Namespace,
    results_dir: Path,
    trace_enabled: bool,
    trace_project: str,
) -> Dict[str, Any]:
    thread_id = f"batch-{uuid4().hex[:12]}"
    run_name = f"batch-{_slugify(model_name)}-{arch}-{task['id']}"
    trace_path = _make_trace_path(model_name, arch, str(task["id"]))
    max_attempts = max(1, int(args.transient_retries) + 1)

    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            print(
                f"[RETRY] model={model_name} arch={arch} task={task['id']} "
                f"attempt={attempt}/{max_attempts}"
            )
            _clear_results_dir(results_dir)
            trace_path.unlink(missing_ok=True)

        tracker = CallbackTracker()
        attempt_run_name = run_name if attempt == 1 else f"{run_name}-retry{attempt}"
        stream_config = _build_stream_config(
            arch=arch,
            trace_enabled=trace_enabled,
            trace_project=trace_project,
            run_name=attempt_run_name,
            thread_id=thread_id,
        )
        stream_config["callbacks"] = [tracker]
        stream_config["recursion_limit"] = args.recursion_limit

        try:
            async def _guarded():
                return await asyncio.wait_for(
                    run_once(
                        agent=agent,
                        task=_build_prompt(task),
                        arch=arch,
                        model=model_desc,
                        trace_full=args.trace_full,
                        md_path=trace_path,
                        stream_config=stream_config,
                    ),
                    timeout=args.task_timeout,
                )
            result = asyncio.run(_guarded())
        except Exception as exc:
            timeout_note = (
                f" (task timed out after {args.task_timeout}s)"
                if isinstance(exc, asyncio.TimeoutError) else ""
            )
            zero_tokens = tracker.total_usage["total_tokens"] == 0
            if attempt < max_attempts and zero_tokens:
                print(
                    f"[WARN ] model={model_name} arch={arch} task={task['id']} "
                    f"attempt {attempt}/{max_attempts} failed{timeout_note}"
                    f"{' (total_tokens=0, model not invoked)' if zero_tokens else ''}"
                    f"; retrying in 5s"
                )
                time.sleep(5.0)
                continue
            if zero_tokens:
                raise RuntimeError(
                    f"{TRANSIENT_FAILURE_REASON_PREFIX}: "
                    f"total_tokens=0 after {max_attempts} attempt(s)"
                ) from exc
            raise

        _hydrate_tracker_from_trace(tracker, trace_path, task, arch)
        success, success_reason, expected_target = _evaluate_success(task, arch, tracker)
        tracker_usage = tracker.total_usage
        result_usage = _normalize_usage(result.get("usage", {}))
        usage = tracker_usage if tracker_usage["total_tokens"] > 0 else result_usage

        # Silent failure: run completed but model was never actually called
        if usage["total_tokens"] == 0:
            if attempt < max_attempts:
                print(
                    f"[WARN ] model={model_name} arch={arch} task={task['id']} "
                    f"attempt {attempt}/{max_attempts} total_tokens=0 (model not invoked)"
                    f"; retrying in 5s"
                )
                time.sleep(5.0)
                continue
            raise RuntimeError(
                f"{TRANSIENT_FAILURE_REASON_PREFIX}: "
                f"total_tokens=0 after {max_attempts} attempt(s)"
            )

        return {
            "usage": usage,
            "success": success,
            "success_reason": success_reason,
            "expected_target": expected_target,
            "completed_tools": tracker.completed_tools,
            "completed_subagents": tracker.completed_subagents,
            "completed_skill_reads": tracker.completed_skill_reads,
            "trace_path": str(trace_path),
            "final_answer": result.get("final_answer", ""),
        }

    raise RuntimeError("unreachable")


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Batch-run selected models, architectures, and tasks.")
    ap.add_argument("--tasks-json", default=str(TASKS_JSON))
    ap.add_argument("--results-dir", default=str(RESULTS_DIR))
    ap.add_argument("--summary-csv", default=str(TOTAL_RESULTS_CSV))
    ap.add_argument("--trace-root", default=str(TRACE_ROOT))
    ap.add_argument("--windows-project-root", default=str(PROJECT_ROOT))
    ap.add_argument("--python-exe", default=(os.environ.get("SWMM_PYTHON_EXE") or sys.executable))
    ap.add_argument("--models", nargs="*", default=list(DEFAULT_MODELS))
    ap.add_argument("--arches", nargs="*", default=list(DEFAULT_ARCHES), choices=DEFAULT_ARCHES)
    ap.add_argument("--task-ids", nargs="*", default=[], help="Optional subset of task ids to run.")
    ap.add_argument("--resume", action="store_true", default=False, help="Skip rows already present in total_results.csv.")
    ap.add_argument("--model-builder", choices=["auto", "init_chat_model", "huggingface"], default="auto")
    ap.add_argument("--api-key-env", default="OPENAI_API_KEY")
    ap.add_argument("--llm-max-tokens", type=int, default=3000)
    ap.add_argument("--llm-temperature", type=float, default=0.0)
    ap.add_argument("--shell-timeout", type=int, default=600)
    ap.add_argument("--model-timeout-sec", type=int, default=300)
    ap.add_argument("--recursion-limit", type=int, default=50,
                    help="Max LangGraph supersteps before aborting (prevents infinite loops).")
    ap.add_argument("--task-timeout", type=int, default=1800,
                    help="Wall-clock timeout (seconds) for a single task execution. Default 30min.")
    ap.add_argument("--transient-retries", type=int, default=5)
    ap.add_argument("--retry-backoff-base-sec", type=float, default=20.0)
    ap.add_argument("--retry-backoff-max-sec", type=float, default=30.0)
    ap.add_argument("--trace", type=lambda x: str(x).lower() in ("1", "true", "yes", "y"), default=True)
    ap.add_argument("--trace-project", default="swmm_agent-gpt_with_1R")
    ap.add_argument("--trace-full", action="store_true", default=False)
    return ap.parse_args()


def main() -> None:
    args = _parse_args()

    tasks_path = Path(args.tasks_json).resolve()
    results_dir = Path(args.results_dir).resolve()
    summary_csv = Path(args.summary_csv).resolve()
    global TRACE_ROOT
    TRACE_ROOT = Path(args.trace_root).resolve()
    TRACE_ROOT.mkdir(parents=True, exist_ok=True)

    tasks = _load_tasks(tasks_path)
    if args.task_ids:
        wanted = set(args.task_ids)
        tasks = [task for task in tasks if str(task["id"]) in wanted]

    completed_keys = _load_completed_keys(summary_csv) if args.resume else set()
    runtime = _build_runtime(args)
    trace_ok, trace_msg = _configure_langsmith_tracing(
        enabled=args.trace,
        project=args.trace_project,
    )

    print(f"Loaded {len(tasks)} tasks from {tasks_path}")
    print(f"Models: {args.models}")
    print(f"Architectures: {args.arches}")
    print(f"Results CSV: {summary_csv}")
    if args.trace:
        status = "OK" if trace_ok else "WARN"
        print(f"[{status}] {trace_msg}")

    for model_name in args.models:
        explicit_model = None
        model_build_error = ""
        resolved_builder = _resolve_model_builder(model_name, args.model_builder)
        model_desc = _model_desc(model_name, args)
        print(f"[MODEL] {model_name} -> builder={resolved_builder}")
        try:
            explicit_model = _build_explicit_model(model_name, args)
        except Exception:
            model_build_error = traceback.format_exc()

        for arch in args.arches:
            agent = None
            agent_build_error = model_build_error
            if not agent_build_error:
                try:
                    agent = build_agent(
                        arch=arch,
                        model_name=model_name,
                        model=explicit_model,
                        runtime=runtime,
                    )
                except Exception:
                    agent_build_error = traceback.format_exc()

            for task in tasks:
                task_key = (model_name, arch, str(task["id"]))
                if task_key in completed_keys:
                    print(f"[SKIP] model={model_name} arch={arch} task={task['id']}")
                    continue

                print(f"[RUN ] model={model_name} arch={arch} task={task['id']}")
                _clear_results_dir(results_dir)

                row: Dict[str, Any] = {
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "model": model_name,
                    "arch": arch,
                    "task_id": task["id"],
                    "category": task["category"],
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "success": False,
                    "success_reason": "",
                    "expected_target": "",
                    "completed_tools": "",
                    "completed_subagents": "",
                    "completed_skill_reads": "",
                    "trace_path": "",
                    "final_answer_excerpt": "",
                    "error": "",
                }

                try:
                    if agent_build_error:
                        raise RuntimeError(agent_build_error)

                    outcome = _run_task(
                        agent=agent,
                        model_name=model_name,
                        model_desc=model_desc,
                        arch=arch,
                        task=task,
                        args=args,
                        results_dir=results_dir,
                        trace_enabled=trace_ok,
                        trace_project=args.trace_project,
                    )
                    usage = outcome["usage"]
                    row.update(
                        {
                            "prompt_tokens": usage["prompt_tokens"],
                            "completion_tokens": usage["completion_tokens"],
                            "total_tokens": usage["total_tokens"],
                            "success": outcome["success"],
                            "success_reason": outcome["success_reason"],
                            "expected_target": outcome["expected_target"],
                            "completed_tools": "|".join(outcome["completed_tools"]),
                            "completed_subagents": "|".join(outcome["completed_subagents"]),
                            "completed_skill_reads": "|".join(outcome["completed_skill_reads"]),
                            "trace_path": outcome["trace_path"],
                            "final_answer_excerpt": _truncate_text(outcome["final_answer"]),
                        }
                    )
                except Exception as exc:
                    row["error"] = traceback.format_exc()
                    row["success"] = False
                    if _is_transient_model_error(exc):
                        row["success_reason"] = _format_transient_failure_reason(exc)
                    elif not row["success_reason"]:
                        row["success_reason"] = "run failed before expected invocation completed"
                finally:
                    has_tokens = int(row.get("total_tokens") or 0) > 0
                    if has_tokens and _should_persist_summary_row(row):
                        _append_csv_row(summary_csv, row)
                    else:
                        reason = "total_tokens=0" if not has_tokens else "transient failure"
                        print(
                            f"[DEFER] model={model_name} arch={arch} task={task['id']} "
                            f"{reason}; summary row not persisted"
                        )
                    _clear_results_dir(results_dir)
                    if has_tokens and (row["success"] or not _row_is_transient_failure(row)):
                        completed_keys.add(task_key)

                print(
                    f"[DONE] model={model_name} arch={arch} task={task['id']} "
                    f"success={row['success']} total_tokens={row['total_tokens']}"
                )


if __name__ == "__main__":
    main()
