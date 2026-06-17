from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from .assertion_parser import cleanup_assertion_file, extract_assertions
from .manifest import env_flag, redacted_mapping, utc_now_iso, write_json
from .models import AssertionCandidate, AssertionClassification, GenerationBlocked, GenerationResult, IsolatedWorkspace
from .paths import PACKAGE_ROOT
from .runtime_config import generator_defaults

VENDOR_DIR = PACKAGE_ROOT / "vendor"
if VENDOR_DIR.is_dir() and str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))

SPEC_ONLY_SYSTEM_PROMPT = """You are the bounded assertion proposer for AssertLLM2-SBY.

Generate candidate SystemVerilog Assertions from the provided specification only.
The input is bug-prevention isolated: no RTL, mutants, prior results, or golden
answers are available. Do not claim formal validity. Do not invent hidden
implementation details. If the specification lacks enough signal-level detail,
return an empty assertions array.

Output only JSON:
{"assertions":[{"label":str|null,"sva":str,"citation":str|null,"notes":str|null}]}.
"""


def _read_exposed_text(workspace: IsolatedWorkspace) -> str:
    parts = []
    for exposed in workspace.exposed_files:
        data = exposed.workspace_path.read_bytes()
        text = data.decode("utf-8", errors="ignore")
        parts.append(f"## {exposed.role}: {exposed.relpath}\n{text}")
    return "\n\n---\n\n".join(parts)


def _build_user_prompt(workspace: IsolatedWorkspace) -> str:
    return (
        "Project: AssertLLM2-SBY\n"
        f"Generation mode: {workspace.mode.value}\n"
        f"Specification source: {workspace.spec_source.value}\n\n"
        "Permitted input files are listed in the isolation manifest. Use only the text below.\n\n"
        f"{_read_exposed_text(workspace)}"
    )


def _anthropic_transport(system_prompt: str, user_prompt: str, config: dict[str, Any]) -> dict[str, Any]:
    try:
        import llm_client as anthropic_client  # type: ignore
    except Exception as exc:  # pragma: no cover - environment-specific
        raise GenerationBlocked(f"could not import the bundled Anthropic client: {exc}") from exc

    if not env_flag("ASSERTLLM2_SBY_ENABLE_CLOUD_LLM"):
        raise GenerationBlocked("cloud LLM disabled; set ASSERTLLM2_SBY_ENABLE_CLOUD_LLM=1")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise GenerationBlocked("ANTHROPIC_API_KEY is not set")
    if getattr(anthropic_client, "requests", None) is None:
        raise GenerationBlocked("bundled Anthropic client requests dependency is unavailable")

    defaults = generator_defaults()
    model = str(config.get("model") or defaults["model"] or getattr(anthropic_client, "LLM_MODEL", ""))
    max_tokens = int(config.get("max_tokens") or defaults["max_tokens"])
    temperature_value = config.get("temperature")
    temperature = float(
        defaults["temperature"] if temperature_value is None else temperature_value
    )
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    headers = {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": getattr(anthropic_client, "API_VERSION", "2023-06-01"),
        "content-type": "application/json",
    }
    try:
        response = anthropic_client.requests.post(
            getattr(anthropic_client, "API_URL", "https://api.anthropic.com/v1/messages"),
            headers=headers,
            json=body,
            timeout=float(config.get("timeout", defaults["timeout"])),
        )
    except Exception as exc:  # noqa: BLE001 - preserve fail-closed generation behavior
        raise GenerationBlocked(f"model API request failed: {type(exc).__name__}: {exc}") from exc
    raw_body = response.text
    if response.status_code != 200:
        raise GenerationBlocked(f"model API returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except Exception:
        payload = {}
    text = ""
    for block in payload.get("content", []) if isinstance(payload, dict) else []:
        if block.get("type") == "text":
            text += block.get("text", "")
    usage = payload.get("usage") if isinstance(payload, dict) else None
    return {
        "provider": "anthropic",
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "configured_max_output_tokens": max_tokens,
        "stop_reason": payload.get("stop_reason") if isinstance(payload, dict) else None,
        "stop_sequence": payload.get("stop_sequence") if isinstance(payload, dict) else None,
        "raw_http_body": raw_body,
        "text": text,
        "usage": usage,
    }


def _looks_incomplete(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.count("```") % 2:
        return True
    if stripped.count("{") > stripped.count("}"):
        return True
    if stripped.count("[") > stripped.count("]"):
        return True
    if re.search(r"\bproperty\s+[A-Za-z_]\w*\s*;", stripped) and "endproperty" not in stripped:
        return True
    return False


def _assertion_text_from_model_text(text: str) -> tuple[str, bool]:
    if not text.strip():
        return "", False
    try:
        payload = json.loads(text)
        if isinstance(payload, dict) and isinstance(payload.get("assertions"), list):
            chunks = []
            for row in payload["assertions"]:
                if not isinstance(row, dict):
                    continue
                label = row.get("label")
                sva = row.get("sva")
                citation = row.get("citation")
                if not isinstance(sva, str) or not sva.strip():
                    continue
                if citation:
                    chunks.append(f"// citation: {citation}")
                if label:
                    chunks.append(f"// label: {label}")
                chunks.append(sva.strip())
            return "\n\n".join(chunks), False
    except json.JSONDecodeError:
        pass
    return text, _looks_incomplete(text)


def _candidate_from_incomplete(text: str, ordinal: int) -> AssertionCandidate:
    import hashlib

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return AssertionCandidate(
        assertion_id=f"sby_assert_{ordinal:04d}_{digest}",
        text=text,
        classification=AssertionClassification.TRUNCATED_OR_INVALID_OUTPUT,
        reasons=("truncated_or_invalid_model_output",),
        label=None,
    )


def generate_assertions(
    workspace: IsolatedWorkspace,
    *,
    output_dir: Path | None = None,
    config: dict[str, Any] | None = None,
    transport: Callable[[str, str, dict[str, Any]], dict[str, Any]] | None = None,
) -> GenerationResult:
    defaults = generator_defaults()
    merged_config = {
        "model": defaults["model"],
        "temperature": defaults["temperature"],
        "max_tokens": defaults["max_tokens"],
        **{k: v for k, v in (config or {}).items() if v is not None},
    }
    cfg = redacted_mapping(merged_config)
    outdir = (output_dir or workspace.root / "generation").resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    system_prompt = SPEC_ONLY_SYSTEM_PROMPT
    user_prompt = _build_user_prompt(workspace)
    (outdir / "model_system_prompt.txt").write_text(system_prompt, encoding="utf-8")
    (outdir / "model_user_prompt.txt").write_text(user_prompt, encoding="utf-8")
    start = time.time()
    stdout = io.StringIO()
    stderr = io.StringIO()
    metadata: dict[str, Any] = {
        "started_at": utc_now_iso(),
        "adapter_generator": "spec_only_llm_client",
        "generator_config": cfg,
        "workspace_manifest": str(workspace.manifest_path),
        "prompt_files": {
            "system": str(outdir / "model_system_prompt.txt"),
            "user": str(outdir / "model_user_prompt.txt"),
        },
    }
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            response = (transport or _anthropic_transport)(system_prompt, user_prompt, cfg)
        raw_path = outdir / "raw_model_response.json"
        write_json(raw_path, redacted_mapping(response))
        stop_reason = response.get("stop_reason")
        assertion_text, incomplete_shape = _assertion_text_from_model_text(str(response.get("text") or ""))
        assertions_path = outdir / "extracted_assertions.sv"
        assertions_path.write_text(assertion_text, encoding="utf-8")
        cleanup_report = cleanup_assertion_file(assertions_path, outdir / "assertions.cleaned.sv")
        candidates_list = extract_assertions(assertion_text)
        appears_truncated = bool(stop_reason == "max_tokens" or incomplete_shape)
        if appears_truncated:
            candidates_list.append(_candidate_from_incomplete(str(response.get("text") or ""), len(candidates_list) + 1))
        candidates = tuple(candidates_list)
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        metadata.update({
            "completed_at": utc_now_iso(),
            "runtime_s": round(time.time() - start, 3),
            "provider": response.get("provider"),
            "model": response.get("model"),
            "temperature": response.get("temperature"),
            "max_tokens": response.get("max_tokens"),
            "configured_max_output_tokens": response.get("configured_max_output_tokens") or cfg.get("max_tokens"),
            "stop_reason": stop_reason,
            "stop_sequence": response.get("stop_sequence"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "appears_truncated": appears_truncated,
            "retry_count": 0,
            "attempts_per_design": 1,
            "thinking": "none",
            "api_version": "2023-06-01",
            "prompt_template": "AssertLLM2/assertllm2_sby/generator.py::SPEC_ONLY_SYSTEM_PROMPT",
            "user_prompt_builder": "AssertLLM2/assertllm2_sby/generator.py::_build_user_prompt",
            "usage": response.get("usage"),
            "syntax_cleanup": cleanup_report,
            "stdout_path": str(outdir / "stdout.txt"),
            "stderr_path": str(outdir / "stderr.txt"),
        })
        (outdir / "stdout.txt").write_text(stdout.getvalue(), encoding="utf-8")
        (outdir / "stderr.txt").write_text(stderr.getvalue(), encoding="utf-8")
        result = GenerationResult(
            design_key=workspace.design_key,
            workspace=workspace.root,
            output_dir=outdir,
            succeeded=True,
            blocked_reason=None,
            raw_response_path=raw_path,
            assertions_path=assertions_path,
            candidates=candidates,
            metadata=metadata,
        )
        write_json(outdir / "generation_result.json", result.to_json())
        return result
    except GenerationBlocked as exc:
        metadata.update({
            "completed_at": utc_now_iso(),
            "runtime_s": round(time.time() - start, 3),
            "blocked_reason": str(exc),
            "stdout_path": str(outdir / "stdout.txt"),
            "stderr_path": str(outdir / "stderr.txt"),
        })
        (outdir / "stdout.txt").write_text(stdout.getvalue(), encoding="utf-8")
        (outdir / "stderr.txt").write_text(stderr.getvalue(), encoding="utf-8")
        result = GenerationResult(
            design_key=workspace.design_key,
            workspace=workspace.root,
            output_dir=outdir,
            succeeded=False,
            blocked_reason=str(exc),
            raw_response_path=None,
            assertions_path=None,
            candidates=(),
            metadata=metadata,
        )
        write_json(outdir / "generation_result.json", result.to_json())
        return result
    except Exception as exc:  # noqa: BLE001 - preserve exact failure for audit
        metadata.update({
            "completed_at": utc_now_iso(),
            "runtime_s": round(time.time() - start, 3),
            "exception_type": type(exc).__name__,
            "exception": str(exc),
            "traceback": traceback.format_exc(),
        })
        write_json(outdir / "generation_exception.json", metadata)
        raise


def load_generation_result_from_artifacts(
    artifact_dir: Path,
    *,
    output_dir: Path | None = None,
) -> GenerationResult:
    source = artifact_dir.resolve()
    result_path = source / "generation_result.json"
    if not result_path.is_file():
        raise GenerationBlocked(f"generation_result.json not found: {result_path}")
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    target = (output_dir or source).resolve()
    target.mkdir(parents=True, exist_ok=True)
    if target != source:
        for item in source.iterdir():
            if item.is_file():
                (target / item.name).write_bytes(item.read_bytes())
    candidates = tuple(
        AssertionCandidate(
            assertion_id=row["assertion_id"],
            text=row.get("text") or "",
            classification=AssertionClassification(row.get("classification") or AssertionClassification.INVALID_OUTPUT.value),
            reasons=tuple(row.get("reasons") or ()),
            label=row.get("label"),
        )
        for row in payload.get("candidates", [])
    )
    metadata = dict(payload.get("metadata") or {})
    metadata["reused_from_generation_artifacts"] = str(source)
    raw_response_path = target / Path(payload["raw_response_path"]).name if payload.get("raw_response_path") else None
    assertions_path = target / Path(payload["assertions_path"]).name if payload.get("assertions_path") else None
    return GenerationResult(
        design_key=payload["design_key"],
        workspace=Path(payload.get("workspace") or "."),
        output_dir=target,
        succeeded=bool(payload.get("succeeded")),
        blocked_reason=payload.get("blocked_reason"),
        raw_response_path=raw_response_path if raw_response_path and raw_response_path.exists() else None,
        assertions_path=assertions_path if assertions_path and assertions_path.exists() else None,
        candidates=candidates,
        metadata=metadata,
    )
