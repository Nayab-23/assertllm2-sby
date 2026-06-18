from __future__ import annotations

import json
import os
import re
import shutil
import sys
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Any


class AdapterFailure(RuntimeError):
    pass


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.name
    return str(value)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, default=_json_default) + "\n", encoding="utf-8")


def _sanitize_identifier(text: str) -> str:
    ident = re.sub(r"[^A-Za-z0-9_]", "_", text).strip("_")
    if not ident:
        ident = "contract"
    if ident[0].isdigit():
        ident = f"contract_{ident}"
    return ident


def _common_root(paths: list[Path]) -> Path:
    if not paths:
        raise AdapterFailure("no RTL/include paths were provided")
    return Path(os.path.commonpath([str(path) for path in paths]))


def _maybe_add_sys_path(path: Path) -> None:
    text = str(path)
    if text not in sys.path:
        sys.path.insert(0, text)


def _resolve_assertneuro_root() -> Path:
    env_root = os.environ.get("ASSERTNEURO_ROOT")
    candidates: list[Path] = []
    if env_root:
        candidates.append(Path(env_root).expanduser().resolve())
    for entry in sys.path:
        if not entry:
            continue
        path = Path(entry).expanduser()
        if path.name == "sable" and (path / "oracle_contracts.py").is_file():
            return path.parent.resolve()
        if (path / "sable" / "oracle_contracts.py").is_file():
            return path.resolve()
    for candidate in candidates:
        if (candidate / "sable" / "oracle_contracts.py").is_file():
            return candidate
    raise AdapterFailure(
        "Could not locate AssertNeuro/Sable. Export ASSERTNEURO_ROOT to the polaris-sable root "
        "and ensure PYTHONPATH includes that root."
    )


def _load_sable_modules() -> tuple[Any, Path]:
    root = _resolve_assertneuro_root()
    _maybe_add_sys_path(root)
    _maybe_add_sys_path(root / "sable")
    try:
        import oracle_contracts as oc  # type: ignore
    except ModuleNotFoundError as exc:
        raise AdapterFailure(f"Failed to import Sable oracle_contracts from {root}") from exc
    return oc, root


def _require_list(mapping: Mapping[str, Any], key: str) -> list[Any]:
    value = mapping.get(key)
    if not isinstance(value, list):
        raise AdapterFailure(f"request[{key!r}] must be a list")
    return value


def _resolve_primary_rtl_file(rtl_files: list[Path], top_module: str | None) -> Path:
    if not rtl_files:
        raise AdapterFailure("request did not include any RTL files")
    if not top_module:
        return rtl_files[0]
    module_re = re.compile(rf"\bmodule\s+{re.escape(top_module)}\b")
    for rtl_file in rtl_files:
        try:
            text = rtl_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if module_re.search(text):
            return rtl_file
    return rtl_files[0]


def _normalize_request(request: Mapping[str, Any]) -> dict[str, Any]:
    rtl_files = [Path(item).expanduser().resolve() for item in _require_list(request, "rtl_files")]
    include_dirs = [Path(item).expanduser().resolve() for item in _require_list(request, "include_dirs")]
    top_module = request.get("top_module")
    if top_module is not None and not isinstance(top_module, str):
        raise AdapterFailure("request['top_module'] must be a string or null")
    clocks = [str(item) for item in _require_list(request, "clocks")]
    reset = request.get("reset")
    if reset is not None and not isinstance(reset, str):
        raise AdapterFailure("request['reset'] must be a string or null")
    defines = [str(item) for item in _require_list(request, "defines")]
    blackboxes = [str(item) for item in _require_list(request, "blackbox_modules")]
    parameters = request.get("parameters") or {}
    if not isinstance(parameters, Mapping):
        raise AdapterFailure("request['parameters'] must be an object")
    primary_rtl = _resolve_primary_rtl_file(rtl_files, top_module)
    return {
        "design_key": str(request.get("design_key") or "unknown_design"),
        "design_name": str(request.get("design_name") or request.get("design_key") or primary_rtl.stem),
        "mode": str(request.get("mode") or "rtl-contract"),
        "top_module": top_module,
        "rtl_files": rtl_files,
        "include_dirs": include_dirs,
        "clocks": clocks,
        "reset": reset,
        "defines": defines,
        "parameters": {str(key): str(value) for key, value in parameters.items()},
        "blackbox_modules": blackboxes,
        "primary_rtl": primary_rtl,
    }


def _build_project_config(normalized: Mapping[str, Any]) -> dict[str, Any]:
    paths = [*normalized["rtl_files"], *normalized["include_dirs"]]
    analysis_clock_override = os.environ.get("ASSERTLLM2_SABLE_ANALYSIS_CLOCK")
    analysis_clock = analysis_clock_override or (normalized["clocks"][0] if normalized["clocks"] else None)
    return {
        "repo_root": str(_common_root(paths or normalized["rtl_files"])),
        "source_files": [str(path) for path in normalized["rtl_files"]],
        "include_dirs": [str(path) for path in normalized["include_dirs"]],
        "defines": {name: "1" for name in normalized["defines"]},
        "parameters": dict(normalized["parameters"]),
        "allowed_blackboxes": list(normalized["blackbox_modules"]),
        "clock": analysis_clock,
        "reset": normalized["reset"],
    }


def _disable_iff(reset_name: str | None, active_low: bool) -> str:
    if not reset_name:
        return ""
    reset_expr = f"!{reset_name}" if active_low else reset_name
    return f" disable iff ({reset_expr})"


def _emit_signal(name: str, signal_map: Mapping[str, str]) -> str:
    return signal_map.get(name, name)


def _join_terms(terms: list[str]) -> str:
    if not terms:
        return "1'b1"
    if len(terms) == 1:
        return terms[0]
    return "(" + " && ".join(terms) + ")"


def _stable_assertion(
    channel: Mapping[str, Any],
    *,
    clock: str,
    reset_name: str | None,
    active_low: bool,
    signal_map: Mapping[str, str],
) -> dict[str, Any]:
    prefix = str(channel["prefix"])
    valid = _emit_signal(str(channel["valid"]), signal_map)
    ready = _emit_signal(str(channel["ready"]), signal_map)
    payload = [_emit_signal(str(name), signal_map) for name in channel.get("payload", [])]
    consequent_terms = [valid] + [f"({name} == $past({name}))" for name in payload]
    label = _sanitize_identifier(f"sable_stable_{prefix}")
    sva = (
        f"{label}: assert property (@(posedge {clock}){_disable_iff(reset_name, active_low)} "
        f"({valid} && !{ready}) |=> {_join_terms(consequent_terms)});"
    )
    return {
        "assertion_id": label,
        "label": label,
        "sva": sva,
        "contract_family": "stable",
        "target": prefix,
        "signal_names": [str(channel["valid"]), str(channel["ready"]), *[str(name) for name in channel.get("payload", [])]],
    }


def _access_hit_assertion(
    pair: Mapping[str, Any],
    *,
    clock: str,
    reset_name: str | None,
    active_low: bool,
    signal_map: Mapping[str, str],
) -> dict[str, Any]:
    hit = _emit_signal(str(pair["response_valid"]), signal_map)
    access = _emit_signal(str(pair["request_valid"]), signal_map)
    label = _sanitize_identifier(f"sable_access_hit_{pair['response_prefix']}")
    sva = (
        f"{label}: assert property (@(posedge {clock}){_disable_iff(reset_name, active_low)} "
        f"{hit} |-> {access});"
    )
    return {
        "assertion_id": label,
        "label": label,
        "sva": sva,
        "contract_family": "access_hit_contract",
        "target": str(pair["response_prefix"]),
        "signal_names": [str(pair["request_valid"]), str(pair["response_valid"])],
    }


def _memory_request_assertion(
    iface: Mapping[str, Any],
    *,
    clock: str,
    reset_name: str | None,
    active_low: bool,
    signal_map: Mapping[str, str],
) -> dict[str, Any] | None:
    stall_name = iface.get("stall")
    if not stall_name:
        return None
    valid = _emit_signal(str(iface["valid"]), signal_map)
    stall = _emit_signal(str(stall_name), signal_map)
    label = _sanitize_identifier(f"sable_mem_quiescence_{iface['prefix']}")
    sva = (
        f"{label}: assert property (@(posedge {clock}){_disable_iff(reset_name, active_low)} "
        f"{stall} |-> !{valid});"
    )
    return {
        "assertion_id": label,
        "label": label,
        "sva": sva,
        "contract_family": "memory_request_quiescence",
        "target": str(iface["prefix"]),
        "signal_names": [str(iface["valid"]), str(stall_name)],
    }


def _copy_sable_workdir(record: Mapping[str, Any], outdir: Path) -> str | None:
    workdir = record.get("workdir")
    if not workdir:
        return None
    src = Path(str(workdir))
    if not src.is_dir():
        return None
    dst = outdir / "sable_workdir"
    shutil.copytree(src, dst, dirs_exist_ok=True)
    return str(dst)


def infer(request: Mapping[str, Any], outdir: str | Path) -> dict[str, Any]:
    outdir = Path(outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    normalized = _normalize_request(request)
    _write_json(outdir / "sable_adapter_request.normalized.json", normalized)

    oc, sable_root = _load_sable_modules()
    project_config = _build_project_config(normalized)
    _write_json(outdir / "sable_project_config.json", project_config)

    depth = int(os.environ.get("ASSERTLLM2_SABLE_DEPTH", "20"))
    sv_frontend = os.environ.get("ASSERTLLM2_SABLE_SV_FRONTEND", "auto")
    probe_dir = outdir / "sable_probe"
    probe_dir.mkdir(parents=True, exist_ok=True)

    blackboxed: list[str] = []
    with oc.project_context(project_config):
        ports, probe_output = oc.probe_ports(
            normalized["primary_rtl"],
            normalized["top_module"],
            probe_dir,
            sv_frontend=sv_frontend,
            blackbox_sink=blackboxed,
        )
    (outdir / "sable_probe_output.txt").write_text(probe_output or "", encoding="utf-8")
    if not ports:
        raise AdapterFailure("Sable probe_ports failed; inspect sable_probe_output.txt")

    signal_map = oc.build_signal_emit_map(ports)
    _write_json(
        outdir / "sable_signal_emit_map.json",
        {"signals": signal_map, "blackboxed_modules": sorted(set(blackboxed))},
    )

    record = oc.analyze_file(
        normalized["primary_rtl"],
        normalized["top_module"],
        depth,
        True,
        preserve_on_failure=True,
        sv_frontend=sv_frontend,
        project_config=project_config,
    )
    _write_json(outdir / "sable_analysis_record.json", record)

    copied_workdir = _copy_sable_workdir(record, outdir)
    if copied_workdir:
        _write_json(outdir / "sable_artifact_manifest.json", {"copied_workdir": copied_workdir})

    clock = project_config.get("clock")
    if not clock:
        raise AdapterFailure("No analysis clock is available for Sable export")
    reset_name = project_config.get("reset")
    active_low = bool(reset_name and oc.is_active_low_reset(reset_name))

    assertions: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for channel in record.get("channels", []) or []:
        if channel.get("role") != "output":
            skipped.append({"kind": "stable", "target": channel.get("prefix"), "reason": "input_role_becomes_environment_assumption"})
            continue
        assertions.append(
            _stable_assertion(
                channel,
                clock=clock,
                reset_name=reset_name,
                active_low=active_low,
                signal_map=signal_map,
            )
        )

    for pair in record.get("access_hit_pairs", []) or []:
        if pair.get("response_role") != "output":
            skipped.append({"kind": "access_hit_contract", "target": pair.get("response_prefix"), "reason": "non_output_response"})
            continue
        assertions.append(
            _access_hit_assertion(
                pair,
                clock=clock,
                reset_name=reset_name,
                active_low=active_low,
                signal_map=signal_map,
            )
        )

    for iface in record.get("memory_request_interfaces", []) or []:
        exported = _memory_request_assertion(
            iface,
            clock=clock,
            reset_name=reset_name,
            active_low=active_low,
            signal_map=signal_map,
        )
        if exported is None:
            skipped.append({"kind": "memory_request_quiescence", "target": iface.get("prefix"), "reason": "no_stall_guard_signal"})
            continue
        assertions.append(exported)

    for pair in record.get("pairs", []) or []:
        skipped.append({"kind": "val_had_request", "target": pair.get("response_prefix"), "reason": "requires_auxiliary_counter_state"})
    for pair in record.get("deferred_pairs", []) or []:
        skipped.append({"kind": "deferred_val_had_request", "target": pair.get("response_prefix"), "reason": "requires_auxiliary_counter_state"})

    export_report = {
        "schema_version": "1.0",
        "sable_root": str(sable_root),
        "primary_rtl": str(normalized["primary_rtl"]),
        "top_module": normalized["top_module"],
        "clock": clock,
        "reset": reset_name,
        "active_low_reset": active_low,
        "depth": depth,
        "sv_frontend": sv_frontend,
        "analysis_status": record.get("status_typed"),
        "analysis_reason": record.get("reason"),
        "exported_assertion_count": len(assertions),
        "skipped": skipped,
        "blackboxed_modules": sorted(set(blackboxed)),
    }
    _write_json(outdir / "sable_export_report.json", export_report)

    response = {
        "generator_version": "sable-assertneuro-adapter-v1",
        "model": "assertneuro/sable",
        "assertions": assertions,
        "engine": {
            "sable_root": str(sable_root),
            "status_typed": record.get("status_typed"),
            "reason": record.get("reason"),
            "clock": clock,
            "reset": reset_name,
            "sv_frontend": sv_frontend,
            "depth": depth,
            "workdir_copied_to": copied_workdir,
            "export_status": "ok" if assertions else "no_assertions",
            "export_report": export_report,
        },
    }
    _write_json(outdir / "contract_response.json", response)
    return response
