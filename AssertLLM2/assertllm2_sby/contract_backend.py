from __future__ import annotations

import importlib
import inspect
import os
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .manifest import redacted_mapping, utc_now_iso, write_json

ADAPTER_VERSION = "assertllm2-sby-contract-backend-1"
CONTRACT_ENGINE_ENTRYPOINT_ENV = "ASSERTLLM2_SBY_CONTRACT_ENGINE_ENTRYPOINT"


class ContractBackendError(RuntimeError):
    pass


def infer(request: dict[str, Any], outdir: str | Path) -> dict[str, Any]:
    """AssertLLM2-SBY contract-inference entrypoint for a local backend."""
    output_dir = Path(outdir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = utc_now_iso()
    start = time.time()
    request_payload = _jsonable(request)
    write_json(
        output_dir / "contract_request.json",
        redacted_mapping(request_payload) if isinstance(request_payload, dict) else request_payload,
    )

    try:
        normalized = _validate_request(request)
        backend_payload = _backend_payload(normalized, output_dir)
        engine, engine_entrypoint = _resolve_engine_callable()
        engine_result = _call_engine(engine, request, output_dir, backend_payload)
        response = _response_from_engine_result(
            engine_result,
            normalized,
            started_at=started_at,
            completed_at=utc_now_iso(),
            runtime_s=round(time.time() - start, 3),
            engine_entrypoint=engine_entrypoint,
        )
        _write_success_logs(output_dir, response, engine_result)
        return response
    except Exception as exc:  # noqa: BLE001 - this is a process boundary
        response = _error_response(
            request,
            exc,
            started_at=started_at,
            runtime_s=round(time.time() - start, 3),
        )
        write_json(output_dir / "contract_error.json", response["error"])
        write_json(output_dir / "contract_result.json", response)
        return response


def generate_assertions(request: dict[str, Any], outdir: str | Path) -> dict[str, Any]:
    return infer(request, outdir)


def _validate_request(request: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise ContractBackendError("request must be a JSON object")
    required = ("design_key", "rtl_files", "top_module", "mode")
    missing = [name for name in required if request.get(name) in (None, "")]
    if missing:
        raise ContractBackendError(f"request missing required fields: {', '.join(missing)}")
    rtl_files = _string_list(request.get("rtl_files"), "rtl_files")
    if not rtl_files:
        raise ContractBackendError("request rtl_files must contain at least one RTL file")
    return {
        **dict(request),
        "rtl_files": rtl_files,
        "include_dirs": _string_list(request.get("include_dirs", []), "include_dirs"),
        "defines": _string_list(request.get("defines", []), "defines"),
        "parameters": _dict_field(request.get("parameters", {}), "parameters"),
        "clocks": _string_list(request.get("clocks", []), "clocks"),
        "reset": request.get("reset"),
        "spec_files": _string_list(request.get("spec_files", []), "spec_files"),
        "raw_spec_files": _string_list(request.get("raw_spec_files", []), "raw_spec_files"),
        "support_files": _string_list(request.get("support_files", []), "support_files"),
        "working_directory": str(request.get("working_directory") or request.get("design_dir") or ""),
    }


def _string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ContractBackendError(f"request {field} must be an array")
    return [str(item) for item in value]


def _dict_field(value: Any, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ContractBackendError(f"request {field} must be an object")
    return dict(value)


def _backend_payload(request: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "source": "AssertLLM2-SBY",
        "adapter_version": ADAPTER_VERSION,
        "output_dir": str(output_dir),
        "request": request,
        "design": {
            "design_key": request["design_key"],
            "design_name": request.get("design_name"),
            "category": request.get("category"),
            "top_module": request["top_module"],
            "rtl_files": request["rtl_files"],
            "include_dirs": request["include_dirs"],
            "defines": request["defines"],
            "parameters": request["parameters"],
            "clocks": request["clocks"],
            "reset": request.get("reset"),
            "blackbox_modules": _string_list(request.get("blackbox_modules", []), "blackbox_modules"),
            "mode": request["mode"],
            "spec_files": request["spec_files"],
            "raw_spec_files": request["raw_spec_files"],
            "support_files": request["support_files"],
            "working_directory": request["working_directory"],
            "buggy_rtl_files": _string_list(request.get("buggy_rtl_files", []), "buggy_rtl_files"),
            "clean_rtl_visible_to_generator": request.get("clean_rtl_visible_to_generator"),
            "merged_buggy_rtl_dirs": _string_list(request.get("merged_buggy_rtl_dirs", []), "merged_buggy_rtl_dirs"),
        },
    }


def _resolve_engine_callable() -> tuple[Callable[..., Any], str]:
    entrypoint = os.environ.get(CONTRACT_ENGINE_ENTRYPOINT_ENV)
    if not entrypoint:
        raise ContractBackendError(
            "contract backend engine entrypoint is not configured. Set "
            f"{CONTRACT_ENGINE_ENTRYPOINT_ENV}=module:function"
        )
    return _load_entrypoint(entrypoint), entrypoint


def _load_entrypoint(entrypoint: str) -> Callable[..., Any]:
    if ":" not in entrypoint:
        raise ContractBackendError("entrypoint must use module:function format")
    module_name, function_name = entrypoint.split(":", 1)
    module = importlib.import_module(module_name)
    func = getattr(module, function_name, None)
    if not callable(func):
        raise ContractBackendError(f"entrypoint is not callable: {entrypoint}")
    return func


def _call_engine(
    engine: Callable[..., Any],
    request: dict[str, Any],
    output_dir: Path,
    backend_payload: dict[str, Any],
) -> Any:
    try:
        signature = inspect.signature(engine)
    except (TypeError, ValueError):
        return engine(request, output_dir)
    parameters = signature.parameters
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values()):
        return engine(request=request, output_dir=output_dir, assertllm2=backend_payload)
    if "request" in parameters and ("output_dir" in parameters or "outdir" in parameters):
        kwargs = {"request": request}
        kwargs["output_dir" if "output_dir" in parameters else "outdir"] = output_dir
        if "assertllm2" in parameters:
            kwargs["assertllm2"] = backend_payload
        return engine(**kwargs)
    positional = [
        param for param in parameters.values()
        if param.kind in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    ]
    if len(positional) >= 2:
        return engine(request, output_dir)
    if len(positional) == 1:
        if positional[0].name in {"request", "contract_request", "backend_request"}:
            return engine(request)
        return engine(backend_payload)
    return engine()


def _response_from_engine_result(
    engine_result: Any,
    request: dict[str, Any],
    *,
    started_at: str,
    completed_at: str,
    runtime_s: float,
    engine_entrypoint: str,
) -> dict[str, Any]:
    payload = _mapping_from_result(engine_result)
    assertions = _normalize_assertions(payload)
    statistics = _statistics_from_result(payload, len(assertions), runtime_s)
    return {
        "schema_version": "1.0",
        "adapter": "AssertLLM2-SBY Contract Backend",
        "adapter_version": ADAPTER_VERSION,
        "generator_version": str(payload.get("generator_version") or payload.get("version") or "contract-backend"),
        "model": str(payload.get("model") or payload.get("engine") or "local contract backend"),
        "design_key": request.get("design_key"),
        "mode": request.get("mode"),
        "engine_entrypoint": engine_entrypoint,
        "started_at": started_at,
        "completed_at": completed_at,
        "runtime_s": runtime_s,
        "statistics": statistics,
        "assertions": assertions,
    }


def _mapping_from_result(result: Any) -> dict[str, Any]:
    if result is None:
        return {"assertions": []}
    if isinstance(result, Mapping):
        return dict(result)
    if isinstance(result, str):
        return {"assertions": [result]}
    if isinstance(result, Sequence) and not isinstance(result, (bytes, bytearray)):
        return {"assertions": list(result)}
    data: dict[str, Any] = {}
    for name in ("assertions", "svas", "generated_assertions", "properties", "statistics", "stats", "model", "version"):
        if hasattr(result, name):
            data[name] = getattr(result, name)
    if data:
        return data
    raise ContractBackendError(f"unsupported contract backend result type: {type(result).__name__}")


def _normalize_assertions(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = (
        payload.get("assertions")
        or payload.get("svas")
        or payload.get("generated_assertions")
        or payload.get("properties")
        or []
    )
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, Sequence):
        raise ContractBackendError("contract backend result assertions must be an array or string")
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(raw, start=1):
        if isinstance(item, str):
            text = item.strip()
            if text:
                out.append({"assertion_id": f"contract_{idx:04d}", "sva": text})
        elif isinstance(item, Mapping):
            text = item.get("sva") or item.get("text") or item.get("assertion") or item.get("property")
            if isinstance(text, str) and text.strip():
                out.append({
                    "assertion_id": str(item.get("assertion_id") or item.get("id") or f"contract_{idx:04d}"),
                    "label": item.get("label") or item.get("name"),
                    "sva": text.strip(),
                    "contract_family": item.get("contract_family") or item.get("family"),
                    "target": item.get("target") or item.get("signal") or item.get("interface"),
                    "source_locations": item.get("source_locations") or item.get("source_rtl_locations") or [],
                    "prompt": item.get("prompt"),
                    "model": item.get("model"),
                })
        else:
            raise ContractBackendError(f"unsupported assertion item type: {type(item).__name__}")
    return out


def _statistics_from_result(payload: Mapping[str, Any], assertion_count: int, runtime_s: float) -> dict[str, Any]:
    stats = payload.get("statistics") or payload.get("stats") or {}
    if not isinstance(stats, Mapping):
        stats = {"raw_statistics": _jsonable(stats)}
    return {
        **dict(stats),
        "assertion_count": assertion_count,
        "runtime_s": runtime_s,
    }


def _write_success_logs(output_dir: Path, response: dict[str, Any], engine_result: Any) -> None:
    write_json(output_dir / "contract_assertions.json", response["assertions"])
    write_json(output_dir / "contract_stats.json", response["statistics"])
    write_json(output_dir / "contract_engine_result.json", _jsonable(engine_result))
    write_json(output_dir / "contract_result.json", response)


def _error_response(request: Any, exc: Exception, *, started_at: str, runtime_s: float) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "adapter": "AssertLLM2-SBY Contract Backend",
        "adapter_version": ADAPTER_VERSION,
        "generator_version": ADAPTER_VERSION,
        "model": "local contract backend",
        "design_key": request.get("design_key") if isinstance(request, Mapping) else None,
        "mode": request.get("mode") if isinstance(request, Mapping) else None,
        "started_at": started_at,
        "completed_at": utc_now_iso(),
        "runtime_s": runtime_s,
        "assertions": [],
        "statistics": {
            "assertion_count": 0,
            "runtime_s": runtime_s,
            "failed": True,
        },
        "error": {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        },
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "to_json"):
        return _jsonable(value.to_json())
    if hasattr(value, "__dict__"):
        return _jsonable(vars(value))
    return repr(value)
