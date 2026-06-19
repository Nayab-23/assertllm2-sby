from __future__ import annotations

import json
import os
import importlib.util
from pathlib import Path
from typing import Any

from .manifest import utc_now_iso, write_json

ADAPTER_VERSION = "assertneuro-contract-internal-adapter-1"
DEFAULT_DEPTH = 12
CONTRACT_ROOT_ENV = "ASSERTNEURO_CONTRACT_ROOT"


class AdapterFailure(RuntimeError):
    pass


def infer(request: dict[str, Any], outdir: str | Path) -> dict[str, Any]:
    output_dir = Path(outdir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = utc_now_iso()

    normalized = _normalize_request(request)
    write_json(output_dir / "contract_adapter_request.normalized.json", normalized)

    contract_root = _discover_contract_root()
    oc = _load_oracle_contracts(contract_root)

    primary_rtl = Path(normalized["primary_rtl"])
    if not primary_rtl.is_file():
        raise AdapterFailure(f"primary RTL file not found: {primary_rtl}")

    record = oc.analyze_file(primary_rtl, normalized["top_module"], DEFAULT_DEPTH, False)
    write_json(output_dir / "contract_analysis_record.json", _jsonable(record))

    probe_output = record.get("_full_probe_output") or record.get("probe_output")
    if isinstance(probe_output, str) and probe_output:
        (output_dir / "contract_probe_output.txt").write_text(probe_output, encoding="utf-8")

    assertions, export_report = _export_assertions(oc, normalized, record)
    export_report.update(
        {
            "schema_version": "1.0",
            "contract_root": str(contract_root),
            "primary_rtl": normalized["primary_rtl"],
            "top_module": normalized["top_module"],
            "clock": normalized["clock"],
            "reset": normalized["reset"],
            "depth": DEFAULT_DEPTH,
            "analysis_status": record.get("status_typed") or record.get("status"),
            "analysis_reason": record.get("reason"),
            "exported_assertion_count": len(assertions),
        }
    )
    write_json(output_dir / "contract_export_report.json", export_report)
    write_json(
        output_dir / "contract_signal_emit_map.json",
        {"signals": {}, "blackboxed_modules": record.get("blackboxed_modules", [])},
    )

    return {
        "generator_version": ADAPTER_VERSION,
        "model": "AssertNeuro contract backend",
        "started_at": started_at,
        "completed_at": utc_now_iso(),
        "assertions": assertions,
        "stats": {
            "assertion_count": len(assertions),
            "analysis_status": record.get("status_typed") or record.get("status"),
            "analysis_reason": record.get("reason"),
        },
    }


def generate_assertions(request: dict[str, Any], outdir: str | Path) -> dict[str, Any]:
    return infer(request, outdir)


def _normalize_request(request: dict[str, Any]) -> dict[str, Any]:
    rtl_files = [str(Path(p).resolve()) for p in request.get("rtl_files") or []]
    if not rtl_files:
        raise AdapterFailure("request must include rtl_files")
    clocks = list(request.get("clocks") or [])
    return {
        "design_key": str(request.get("design_key") or ""),
        "design_name": str(request.get("design_name") or ""),
        "mode": str(request.get("mode") or ""),
        "top_module": str(request.get("top_module") or ""),
        "rtl_files": rtl_files,
        "include_dirs": [str(Path(p).resolve()) for p in request.get("include_dirs") or []],
        "clocks": clocks,
        "clock": clocks[0] if clocks else None,
        "reset": request.get("reset"),
        "defines": list(request.get("defines") or []),
        "parameters": dict(request.get("parameters") or {}),
        "blackbox_modules": list(request.get("blackbox_modules") or []),
        "primary_rtl": rtl_files[0],
    }


def _discover_contract_root() -> Path:
    env = Path(os.environ.get(CONTRACT_ROOT_ENV, "")).expanduser()
    candidates = []
    if str(env) not in {"", "."}:
        candidates.append(env)
    here = Path(__file__).resolve()
    candidates.append(here.parents[4] / "AssertNeuro" / "contract-engine")
    candidates.append(Path.cwd() / "AssertNeuro" / "contract-engine")
    for candidate in candidates:
        if candidate.is_file() and candidate.name == "oracle_contracts.py":
            return candidate.parent.resolve()
        if candidate.is_dir():
            for path in candidate.rglob("oracle_contracts.py"):
                return path.parent.resolve()
    searched = ", ".join(str(path) for path in candidates)
    raise AdapterFailure(f"could not locate contract engine root; searched: {searched}")


def _load_oracle_contracts(contract_root: Path):
    module_path = contract_root / "oracle_contracts.py"
    if not module_path.is_file():
        raise AdapterFailure(f"oracle_contracts.py not found under {contract_root}")
    module_name = f"oracle_contracts_{abs(hash(str(module_path)))}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise AdapterFailure(f"failed to load oracle_contracts from {module_path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001
        raise AdapterFailure(f"failed to import oracle_contracts from {module_path}: {exc}") from exc
    return module


def _export_assertions(
    oc,
    normalized: dict[str, Any],
    record: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    assertions: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    clock = normalized.get("clock")
    reset = normalized.get("reset")
    active_low = bool(reset and oc.is_active_low_reset(reset))
    disable_reset = ""
    if reset:
        disable_reset = f"disable iff ({'!' if active_low else ''}{reset}) "

    arithmetic = record.get("arithmetic") or {}
    arithmetic_props = arithmetic.get("properties") or {}
    if arithmetic_props.get("accumulator_integrity", {}).get("confirmed"):
        source_text = Path(normalized["primary_rtl"]).read_text(errors="ignore")
        match = oc._ACC_RE.search(oc._module_body(source_text, normalized["top_module"]) or source_text)
        if match and clock:
            acc, inc = match.group(1), match.group(2)
            assertions.append(
                _assertion_row(
                    "accumulator_integrity",
                    "arithmetic",
                    acc,
                    f"assert property (@(posedge {clock}) {disable_reset}$past({inc}) != 0 |-> {acc} >= $past({acc}));",
                )
            )
        else:
            skipped.append({"property_class": "accumulator_integrity", "reason": "pattern_not_recovered"})

    fsm = record.get("fsm") or {}
    interface = fsm.get("interface") or {}
    fsm_props = fsm.get("properties") or {}
    reg = interface.get("reg")
    encodings = interface.get("encodings") or {}
    init_state = interface.get("init_state", 0)
    if reg and clock and encodings:
        enc_ints = {int(k): v for k, v in encodings.items()}
        enc_expr = oc._fsm_encoding_set_expr(reg, enc_ints)
        past_enc = oc._fsm_encoding_set_expr(f"$past({reg})", enc_ints)

        if fsm_props.get("legal_transition", {}).get("confirmed"):
            assertions.append(
                _assertion_row(
                    "legal_transition",
                    "fsm",
                    reg,
                    f"assert property (@(posedge {clock}) {disable_reset}{past_enc} |-> {enc_expr});",
                )
            )

        if fsm_props.get("no_illegal_state", {}).get("confirmed"):
            assertions.append(
                _assertion_row(
                    "no_illegal_state",
                    "fsm",
                    reg,
                    f"assert property (@(posedge {clock}) {disable_reset}{enc_expr});",
                )
            )

        if fsm_props.get("reset_correctness", {}).get("confirmed") and reset:
            reset_release = f"{reset} && $past(!{reset})" if active_low else f"!{reset} && $past({reset})"
            assertions.append(
                _assertion_row(
                    "reset_correctness",
                    "fsm",
                    reg,
                    f"assert property (@(posedge {clock}) {reset_release} |-> {reg} == {init_state});",
                )
            )

        if fsm_props.get("output_determinism", {}).get("confirmed"):
            skipped.append({"property_class": "output_determinism", "reason": "requires_two_copy_miter"})
    elif fsm_props:
        skipped.append({"property_class": "fsm", "reason": "missing_clock_or_state_encoding"})

    seen = set()
    unique_assertions: list[dict[str, Any]] = []
    for row in assertions:
        key = row["sva"]
        if key in seen:
            continue
        seen.add(key)
        unique_assertions.append(row)

    return unique_assertions, {"skipped": skipped, "blackboxed_modules": record.get("blackboxed_modules", [])}


def _assertion_row(label: str, family: str, target: str, sva: str) -> dict[str, Any]:
    return {
        "id": label,
        "label": label,
        "contract_family": family,
        "target": target,
        "sva": sva,
        "source_locations": [],
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "name") and hasattr(value, "value"):
        return getattr(value, "value")
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)
