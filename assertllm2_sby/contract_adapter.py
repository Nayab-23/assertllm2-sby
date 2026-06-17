from __future__ import annotations

import importlib
import json
import os
import subprocess
import time
import traceback
from pathlib import Path
from typing import Any

from .assertion_parser import cleanup_assertion_file, extract_assertions
from .manifest import redacted_mapping, sha256_file, utc_now_iso, write_json
from .models import AssertionCandidate, GenerationBlocked, GenerationMode, GenerationResult, DesignRecord, ValidationError
from .runtime_config import load_adapter_config


def _relpath(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path.resolve())


def contract_request(design: DesignRecord, mode: GenerationMode) -> dict[str, Any]:
    if mode not in {GenerationMode.RTL_CONTRACT, GenerationMode.BUG_HUNTING, GenerationMode.JUDGE_ONLY}:
        raise ValidationError(f"contract inference requires an RTL-visible mode, got: {mode.value}")
    return {
        "schema_version": "1.0",
        "adapter": "AssertLLM2-SBY",
        "design_key": design.key,
        "category": design.category,
        "design_name": design.design_name,
        "top_module": design.top_module,
        "rtl_files": [str(p) for p in design.rtl_files],
        "include_dirs": [str(p) for p in design.include_dirs],
        "defines": list(design.defines),
        "parameters": design.parameters,
        "clocks": list(design.clocks),
        "reset": design.reset,
        "blackbox_modules": list(design.blackbox_modules),
        "mode": mode.value,
    }


def source_visibility_manifest(design: DesignRecord, mode: GenerationMode) -> dict[str, Any]:
    files = [design.spec_md, *design.raw_specs, *design.rtl_files]
    rows = []
    for path in files:
        if not path.is_file():
            continue
        if path == design.spec_md:
            role = "spec_md"
        elif path in design.raw_specs:
            role = "raw_spec"
        else:
            role = "rtl"
        rows.append({
            "path": str(path.resolve()),
            "relpath": _relpath(path, design.design_dir),
            "role": role,
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        })
    return {
        "schema_version": "1.0",
        "adapter": "AssertLLM2-SBY",
        "created_at": utc_now_iso(),
        "design_key": design.key,
        "mode": mode.value,
        "rtl_visible_to_generator": mode in {GenerationMode.RTL_CONTRACT, GenerationMode.BUG_HUNTING},
        "buggy_rtl_visible_to_generator": mode == GenerationMode.BUG_HUNTING,
        "visible_files": rows,
        "official_jaspergold_result": False,
    }


def _contract_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    root = dict((load_adapter_config().get("contract_inference") or {}))
    root.update({k: v for k, v in (config or {}).items() if v is not None})
    entrypoint_env = str(root.get("python_entrypoint_env", "ASSERTLLM2_SBY_CONTRACT_PYTHON_ENTRYPOINT"))
    executable_env = str(root.get("executable_env", "ASSERTLLM2_SBY_CONTRACT_EXECUTABLE"))
    tool_root_env = str(root.get("tool_root_env", "ASSERTLLM2_SBY_CONTRACT_TOOL_ROOT"))
    if os.environ.get(entrypoint_env):
        root["python_entrypoint"] = os.environ[entrypoint_env]
    if os.environ.get(executable_env):
        root["executable"] = os.environ[executable_env]
    if os.environ.get(tool_root_env):
        root["tool_root"] = os.environ[tool_root_env]
    return root


def _load_python_entrypoint(entrypoint: str):
    if ":" not in entrypoint:
        raise GenerationBlocked("contract python_entrypoint must be module:function")
    module_name, function_name = entrypoint.split(":", 1)
    module = importlib.import_module(module_name)
    func = getattr(module, function_name, None)
    if not callable(func):
        raise GenerationBlocked(f"contract python_entrypoint is not callable: {entrypoint}")
    return func


def _call_python_entrypoint(entrypoint: str, request: dict[str, Any], outdir: Path) -> dict[str, Any]:
    func = _load_python_entrypoint(entrypoint)
    response = func(request, outdir)
    if response is None:
        response_path = outdir / "contract_response.json"
        if not response_path.is_file():
            raise GenerationBlocked(f"contract adapter returned None and did not write {response_path}")
        response = json.loads(response_path.read_text(encoding="utf-8"))
    if not isinstance(response, dict):
        raise GenerationBlocked("contract adapter response must be a JSON object")
    return response


def _call_executable(executable: str, request_path: Path, outdir: Path, timeout: float) -> dict[str, Any]:
    proc = subprocess.run(
        [executable, str(request_path), str(outdir)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )
    (outdir / "contract_tool.stdout.txt").write_text(proc.stdout, encoding="utf-8")
    (outdir / "contract_tool.stderr.txt").write_text(proc.stderr, encoding="utf-8")
    if proc.returncode != 0:
        raise GenerationBlocked(f"contract executable exited {proc.returncode}")
    response_path = outdir / "contract_response.json"
    if response_path.is_file():
        return json.loads(response_path.read_text(encoding="utf-8"))
    if proc.stdout.strip():
        payload = json.loads(proc.stdout)
        if isinstance(payload, dict):
            return payload
    raise GenerationBlocked("contract executable produced no JSON response")


def _assertion_rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    rows = response.get("assertions")
    if rows is None and response.get("assertions_path"):
        path = Path(str(response["assertions_path"]))
        return [{"sva": path.read_text(encoding="utf-8"), "label": None}]
    if not isinstance(rows, list):
        raise GenerationBlocked("contract adapter response must include an assertions array")
    normalized: list[dict[str, Any]] = []
    for idx, row in enumerate(rows, start=1):
        if isinstance(row, str):
            normalized.append({"assertion_id": f"contract_{idx:04d}", "sva": row})
        elif isinstance(row, dict):
            text = row.get("sva") or row.get("text") or row.get("assertion")
            if isinstance(text, str) and text.strip():
                normalized.append({**row, "sva": text})
    return normalized


def _write_assertion_outputs(
    response: dict[str, Any],
    outdir: Path,
    design: DesignRecord,
    mode: GenerationMode,
    cfg: dict[str, Any],
) -> tuple[Path, Path, tuple[AssertionCandidate, ...], dict[str, Any]]:
    rows = _assertion_rows(response)
    chunks: list[str] = []
    meta_rows: list[dict[str, Any]] = []
    for idx, row in enumerate(rows, start=1):
        assertion_id = str(row.get("assertion_id") or row.get("id") or f"contract_{idx:04d}")
        label = row.get("label")
        if label:
            chunks.append(f"// label: {label}")
        chunks.append(str(row["sva"]).strip())
        meta_rows.append({
            "assertion_id": assertion_id,
            "label": label,
            "contract_family": row.get("contract_family"),
            "signal_or_interface_target": row.get("signal") or row.get("target"),
            "source_locations": row.get("source_locations") or row.get("source_rtl_locations") or [],
            "generator_version": response.get("generator_version") or cfg.get("generator_version"),
            "prompt": row.get("prompt"),
            "model": row.get("model") or response.get("model"),
            "visibility_mode": mode.value,
            "rtl_visible_to_generator": mode in {GenerationMode.RTL_CONTRACT, GenerationMode.BUG_HUNTING},
        })
    assertions_path = outdir / "assertions.sv"
    assertions_path.write_text("\n\n".join(chunks), encoding="utf-8")
    cleanup_report = cleanup_assertion_file(assertions_path, outdir / "assertions.cleaned.sv")
    candidates = tuple(extract_assertions(assertions_path.read_text(encoding="utf-8")))
    for idx, candidate in enumerate(candidates):
        if idx < len(meta_rows):
            meta_rows[idx]["assertion_id"] = candidate.assertion_id
            meta_rows[idx]["label"] = meta_rows[idx]["label"] or candidate.label
    meta_path = outdir / "assertions_meta.json"
    write_json(meta_path, {
        "schema_version": "1.0",
        "adapter": "AssertLLM2-SBY",
        "design_key": design.key,
        "mode": mode.value,
        "generator": "contract-inference",
        "assertions": meta_rows,
        "official_jaspergold_result": False,
    })
    return assertions_path, meta_path, candidates, cleanup_report


def generate_contract_assertions(
    design: DesignRecord,
    *,
    mode: GenerationMode = GenerationMode.RTL_CONTRACT,
    output_dir: Path,
    config: dict[str, Any] | None = None,
) -> GenerationResult:
    outdir = output_dir.resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    request = contract_request(design, mode)
    request_path = outdir / "contract_request.json"
    write_json(request_path, request)
    visibility = source_visibility_manifest(design, mode)
    visibility_path = outdir / "source_visibility_manifest.json"
    write_json(visibility_path, visibility)
    cfg = redacted_mapping(_contract_config(config))
    start = time.time()
    metadata: dict[str, Any] = {
        "started_at": utc_now_iso(),
        "adapter_generator": "contract_inference",
        "generator_config": cfg,
        "request_path": str(request_path),
        "source_visibility_manifest": str(visibility_path),
        "mode": mode.value,
    }
    try:
        if cfg.get("python_entrypoint"):
            response = _call_python_entrypoint(str(cfg["python_entrypoint"]), request, outdir)
        elif cfg.get("executable"):
            response = _call_executable(str(cfg["executable"]), request_path, outdir, float(cfg.get("timeout_seconds", 60)))
        else:
            raise GenerationBlocked(
                "contract inference adapter is not configured; set "
                "ASSERTLLM2_SBY_CONTRACT_PYTHON_ENTRYPOINT or ASSERTLLM2_SBY_CONTRACT_EXECUTABLE"
            )
        raw_path = outdir / "contract_response.json"
        write_json(raw_path, redacted_mapping(response))
        assertions_path, meta_path, candidates, cleanup_report = _write_assertion_outputs(response, outdir, design, mode, cfg)
        metadata.update({
            "completed_at": utc_now_iso(),
            "runtime_s": round(time.time() - start, 3),
            "provider": "contract-inference",
            "model": response.get("model"),
            "generator_version": response.get("generator_version"),
            "assertions_meta_path": str(meta_path),
            "syntax_cleanup": cleanup_report,
            "stdout_path": str(outdir / "contract_tool.stdout.txt") if (outdir / "contract_tool.stdout.txt").exists() else None,
            "stderr_path": str(outdir / "contract_tool.stderr.txt") if (outdir / "contract_tool.stderr.txt").exists() else None,
        })
        result = GenerationResult(
            design_key=design.key,
            workspace=outdir,
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
        })
        result = GenerationResult(
            design_key=design.key,
            workspace=outdir,
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
    except Exception as exc:  # noqa: BLE001 - preserve adapter diagnostics
        metadata.update({
            "completed_at": utc_now_iso(),
            "runtime_s": round(time.time() - start, 3),
            "exception_type": type(exc).__name__,
            "exception": str(exc),
            "traceback": traceback.format_exc(),
        })
        write_json(outdir / "generation_exception.json", metadata)
        raise
