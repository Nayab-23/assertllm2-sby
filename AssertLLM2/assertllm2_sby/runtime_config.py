from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .paths import config_path

CONFIG_PATH = config_path()


def _scalar(value: str) -> Any:
    text = value.strip().strip('"').strip("'")
    if text.lower() == "true":
        return True
    if text.lower() == "false":
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def load_adapter_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip() or ":" not in line:
            continue
        indent = len(line) - len(line.lstrip(" "))
        key, value = line.strip().split(":", 1)
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value.strip():
            parent[key] = _scalar(value)
        else:
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
    return root


def generator_defaults(path: Path = CONFIG_PATH) -> dict[str, Any]:
    generator = load_adapter_config(path).get("generator") or {}
    max_tokens_env = str(generator.get("max_tokens_env", "ASSERTLLM2_SBY_LLM_MAX_TOKENS"))
    temperature_env = str(generator.get("temperature_env", "ASSERTLLM2_SBY_LLM_TEMPERATURE"))
    timeout_env = str(generator.get("timeout_env", "ASSERTLLM2_SBY_LLM_TIMEOUT"))
    model_env = str(generator.get("model_env", "ASSERTLLM2_SBY_LLM_MODEL"))
    return {
        "model": os.environ.get(model_env) or generator.get("model_default", "claude-sonnet-4-6"),
        "temperature": float(os.environ.get(temperature_env) or generator.get("temperature_default", 0.0)),
        "max_tokens": int(os.environ.get(max_tokens_env) or generator.get("max_tokens_default", 4096)),
        "timeout": float(os.environ.get(timeout_env) or generator.get("timeout_seconds_default", 30)),
        "attempts_per_design": generator.get("attempts_per_design", 1),
        "retry_count": generator.get("retry_count", 0),
        "thinking": generator.get("thinking", "none"),
    }
