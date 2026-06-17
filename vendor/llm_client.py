#!/usr/bin/env python3
"""
Anthropic LLM client used by the generation path.

This is a thin, dependency-light wrapper over the Anthropic Messages API.
It returns parsed JSON or nothing; callers reject any output that does not
match their schema.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - requests is a project dep
    requests = None


LLM_MODEL = os.environ.get("ASSERTLLM2_SBY_LLM_MODEL", "claude-sonnet-4-6")

# Per the sprint spec.
LLM_MAX_TOKENS = int(os.environ.get("ASSERTLLM2_SBY_LLM_MAX_TOKENS", "1000"))

# Sampling temperature. Default 0.0 = greedy/deterministic, which is what the
# fail-closed proposer wants (reproducible candidates). Override per-run with
LLM_TEMPERATURE = float(os.environ.get("ASSERTLLM2_SBY_LLM_TEMPERATURE", "0.0"))

# Optional JSONL audit log of every call's provenance.
_LLM_LOG_PATH = os.environ.get("ASSERTLLM2_SBY_LLM_LOG")
_CALL_LOG: list[dict] = []


def _log_call(*, model: str, temperature: float, max_tokens: int,
              system: str, user: str, outcome: str) -> None:
    """Record one call's provenance. Never raises into the analysis path."""
    entry = {
        "ts": round(time.time(), 3),
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "system_chars": len(system or ""),
        "user_chars": len(user or ""),
        "outcome": outcome,  # "ok" | "no_key" | "http_<code>" | "network_error" | "unparseable"
    }
    _CALL_LOG.append(entry)
    try:
        print(f"[llm_client] model={model} temperature={temperature} "
              f"max_tokens={max_tokens} outcome={outcome}", file=sys.stderr)
        if _LLM_LOG_PATH:
            with open(_LLM_LOG_PATH, "a") as fh:
                fh.write(json.dumps(entry) + "\n")
    except Exception:  # pragma: no cover - logging must never break analysis
        pass


def get_call_log() -> list[dict]:
    """Return the in-memory provenance log (model/temperature per call) so a run
    can embed exactly which model + temperature produced its numbers."""
    return list(_CALL_LOG)

API_URL = os.environ.get("ANTHROPIC_API_URL", "https://api.anthropic.com/v1/messages")
API_VERSION = "2023-06-01"
_API_KEY_ENV = "ANTHROPIC_API_KEY"

_TIMEOUT = float(os.environ.get("ASSERTLLM2_SBY_LLM_TIMEOUT", "30"))


def api_key_present() -> bool:
    return bool(os.environ.get(_API_KEY_ENV))


def is_available() -> bool:
    """True iff a real LLM call can be made (requests importable + key present)."""
    return requests is not None and api_key_present()


class LLMUnavailable(Exception):
    """Raised internally when a call is attempted with no key/requests. Callers
    should prefer is_available() and fall back to a stub rather than catch this."""


def _strip_code_fences(text: str) -> str:
    """LLMs sometimes wrap JSON in ```json ... ``` despite instructions. Strip a
    single fenced block if present; otherwise return the text unchanged."""
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    return m.group(1).strip() if m else text.strip()


def _extract_first_json(text: str):
    """Best-effort recovery: pull the first balanced {...} or [...] object out of a
    response that may carry leading/trailing prose (a common real failure mode
    even when the system prompt forbids it). Returns the parsed object or None.

    Tried only AFTER a direct parse and a single-fence strip both fail, so a clean
    JSON response is never reinterpreted — this only rescues dirty ones."""
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start == -1:
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except ValueError:
                        break  # malformed; give up on this opener
    return None


def call_json(
    system: str,
    user: str,
    *,
    model: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> Any | None:
    """Make one Messages API call and return the response parsed as JSON.

    Returns the parsed object on success, or None on ANY failure (no key, network
    error, non-200, unparseable body, model returned prose instead of JSON). The
    caller treats None as "the proposer abstained" and falls back to its stub or
    to INCONCLUSIVE — NEVER trusts a partial/raw string.

    The model is instructed (by the caller's system prompt) to output only JSON;
    we additionally strip code fences and json.loads() the result. If that fails,
    we return None rather than hand back unvalidated text.
    """
    eff_model = model or LLM_MODEL
    eff_max_tokens = max_tokens or LLM_MAX_TOKENS
    eff_temperature = LLM_TEMPERATURE if temperature is None else temperature
    if not is_available():
        _log_call(model=eff_model, temperature=eff_temperature,
                  max_tokens=eff_max_tokens, system=system, user=user, outcome="no_key")
        return None
    headers = {
        "x-api-key": os.environ[_API_KEY_ENV],
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }
    body = {
        "model": eff_model,
        "max_tokens": eff_max_tokens,
        "temperature": eff_temperature,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    try:
        resp = requests.post(API_URL, headers=headers, json=body, timeout=_TIMEOUT)
    except Exception:
        _log_call(model=eff_model, temperature=eff_temperature,
                  max_tokens=eff_max_tokens, system=system, user=user, outcome="network_error")
        return None
    if resp.status_code != 200:
        _log_call(model=eff_model, temperature=eff_temperature, max_tokens=eff_max_tokens,
                  system=system, user=user, outcome=f"http_{resp.status_code}")
        return None
    try:
        data = resp.json()
        # Messages API: content is a list of blocks; take the first text block.
        blocks = data.get("content", [])
        text = next((b.get("text", "") for b in blocks if b.get("type") == "text"), "")
        if not text.strip():
            _log_call(model=eff_model, temperature=eff_temperature, max_tokens=eff_max_tokens,
                      system=system, user=user, outcome="unparseable")
            return None
        # Parse, tolerant of the model's real (non-deterministic) output shapes:
        # clean JSON -> single ```json fence -> leading/trailing prose around a
        # balanced object. Each step is tried only if the prior fails, so a clean
        # response is never reinterpreted.
        stripped = _strip_code_fences(text)
        try:
            parsed = json.loads(stripped)
        except ValueError:
            parsed = _extract_first_json(text)  # None if even recovery fails
        _log_call(model=eff_model, temperature=eff_temperature, max_tokens=eff_max_tokens,
                  system=system, user=user,
                  outcome="ok" if parsed is not None else "unparseable")
        return parsed
    except (ValueError, KeyError, TypeError, AttributeError):
        # Unparseable -> abstain. Never return raw text.
        _log_call(model=eff_model, temperature=eff_temperature, max_tokens=eff_max_tokens,
                  system=system, user=user, outcome="unparseable")
        return None


__all__ = [
    "LLM_MODEL", "LLM_MAX_TOKENS", "LLM_TEMPERATURE", "API_URL",
    "api_key_present", "is_available", "call_json", "LLMUnavailable",
    "get_call_log",
]
