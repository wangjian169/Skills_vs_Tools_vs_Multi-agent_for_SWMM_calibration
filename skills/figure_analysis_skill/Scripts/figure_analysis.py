import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import os
import base64
from typing import Any, Dict, List, Optional

from langchain.chat_models import init_chat_model
from langchain_core.messages import SystemMessage, HumanMessage



SYSTEM_PROMPT = (
    "You are an expert vision-language assistant. "
    "Follow the user's task instructions strictly. "
    "Be precise, structured, and do not invent details that are not visible."
)


def encode_image_to_data_url(image_path: str) -> Optional[str]:
    if not image_path or not os.path.exists(image_path):
        return None

    ext = os.path.splitext(image_path)[1].lower()
    if ext in [".jpg", ".jpeg"]:
        mime = "image/jpeg"
    elif ext == ".webp":
        mime = "image/webp"
    else:
        mime = "image/png"

    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")

    return f"data:{mime};base64,{b64}"


def _normalize_resp_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                txt = item.get("text")
                if txt:
                    parts.append(str(txt))
            elif item is not None:
                parts.append(str(item))
        return "\n".join(parts).strip()
    return ""


def analyze_generated_figures(task_str: str, figure_info: List[Dict[str, Any]]) -> str:
    """
    Public skill interface:
    - task_str: natural-language task description (tells the model what to do)
    - figure_info: [{"path": "...png"}, ...], must include at least path
    Returns: model output text
    """
    if not os.environ.get("HF_TOKEN", ""):
        raise RuntimeError("Figure analysis API key is empty.")

    if not task_str or not isinstance(task_str, str):
        raise ValueError("task_str must be a non-empty string.")

    if not isinstance(figure_info, list) or len(figure_info) == 0:
        raise ValueError("figure_info must be a non-empty list.")


    llm = init_chat_model(
        'Qwen/Qwen2.5-VL-72B-Instruct',
        model_provider="huggingface",
        backend="endpoint",
        temperature=0,
        max_new_tokens=1000,
    )

    image_contents: List[Dict[str, Any]] = []
    for fig in figure_info:
        img_path = fig.get("path")
        data_url = encode_image_to_data_url(img_path)
        if not data_url:
            continue
        image_contents.append(
            {
                "type": "image_url",
                "image_url": {"url": data_url},
            }
        )

    if not image_contents:
        raise ValueError("No valid image files found (check figure_info paths).")

    # Key point: send the natural-language task description as a text part to the model
    human_content: List[Dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                "Please analyse the images provided after the task description below, then return the results.\n\n"
                f"Task description:\n{task_str}"
            ),
        }
    ]
    human_content.extend(image_contents)

    try:
        resp = llm.invoke(
            [
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=human_content),
            ]
        )
    except Exception as exc:
        raise RuntimeError(f"VLM request failed: {type(exc).__name__}: {exc}") from exc

    result = _normalize_resp_content(getattr(resp, "content", None))
    if not result:
        raise RuntimeError("VLM returned empty content.")
    return result


def run_analyze_figures(task_str, figure_info) -> str:
    result = analyze_generated_figures(task_str=task_str, figure_info=figure_info)
    summary = f"Figure analysis result:\n{result}"
    print(summary)
    return summary


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Analyse figures using Vision-Language Model (VLM)")
    ap.add_argument("--task", required=True, help="Natural language task description for the VLM")
    ap.add_argument("--figures", nargs="+", required=True, help="Paths to image files to analyse")
    args = ap.parse_args()

    run_analyze_figures(args.task, [{"path": p} for p in args.figures])
