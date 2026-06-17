from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .manifest import sha256_file
from .models import DesignRecord, ValidationError

RAW_SPEC_SUFFIXES = {".pdf", ".docx", ".odt", ".txt", ".md", ".ppt", ".pptx"}
RTL_SUFFIXES = {".v", ".sv", ".vh", ".svh", ".vhd", ".vhdl"}
VERILOG_SUFFIXES = {".v", ".sv", ".vh", ".svh"}
VHDL_SUFFIXES = {".vhd", ".vhdl"}
MUTATION_METADATA_NAMES = {
    "mutation_summary.json",
    "mutation_metadata.json",
    "mutants.json",
    "merged_mutants.json",
}


def default_checkout_root() -> Path:
    from .paths import resolve_assertllm2_checkout

    return resolve_assertllm2_checkout()


def _resolve_inside(base: Path, value: str | Path, containment_root: Path | None = None) -> Path:
    path = (base / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
    root = (containment_root or base).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValidationError(f"path escapes AssertLLM2 checkout: {path}") from exc
    return path


def _require_file(base: Path, value: str | Path, desc: str, containment_root: Path | None = None) -> Path:
    path = _resolve_inside(base, value, containment_root)
    if not path.is_file():
        raise ValidationError(f"missing {desc}: {path}")
    return path


def _require_file_from_bases(
    bases: tuple[Path, ...],
    value: str | Path,
    desc: str,
    containment_root: Path | None = None,
) -> Path:
    errors: list[str] = []
    for base in bases:
        try:
            return _require_file(base, value, desc, containment_root)
        except ValidationError as exc:
            errors.append(str(exc))
    raise ValidationError(errors[0] if errors else f"missing {desc}: {value}")


def _maybe_dir(base: Path, value: str | Path, containment_root: Path | None = None) -> Path:
    path = _resolve_inside(base, value, containment_root)
    if not path.is_dir():
        raise ValidationError(f"missing include directory: {path}")
    return path


def _maybe_dir_from_bases(
    bases: tuple[Path, ...],
    value: str | Path,
    containment_root: Path | None = None,
) -> Path:
    errors: list[str] = []
    for base in bases:
        try:
            return _maybe_dir(base, value, containment_root)
        except ValidationError as exc:
            errors.append(str(exc))
    raise ValidationError(errors[0] if errors else f"missing include directory: {value}")


def _design_parts_from_key(key: str, spec_md: Path, checkout: Path) -> tuple[str, str, Path]:
    parts = key.split("/")
    if len(parts) >= 3:
        category = parts[1].upper()
        design_name = parts[-1]
    else:
        try:
            rel = spec_md.parent.relative_to(checkout / "designs")
            category = rel.parts[0]
            design_name = rel.parts[1]
        except Exception:
            category = "UNKNOWN"
            design_name = spec_md.parent.name
    return category, design_name, spec_md.parent


def _raw_specs_for_design(design_dir: Path) -> tuple[Path, ...]:
    out = []
    for path in design_dir.iterdir():
        if not path.is_file():
            continue
        if path.name == "spec.md":
            continue
        if path.suffix.lower() in RAW_SPEC_SUFFIXES:
            out.append(path.resolve())
    return tuple(sorted(out, key=lambda p: p.name.lower()))


def _mutation_files(design_dir: Path) -> tuple[Path, ...]:
    mut_dir = design_dir / "mutations"
    if not mut_dir.is_dir():
        return ()
    return tuple(sorted((p.resolve() for p in mut_dir.rglob("*") if p.is_file()), key=str))


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _string_list(value: Any) -> tuple[str, ...]:
    out: list[str] = []
    for item in _as_list(value):
        if item is None:
            continue
        text = str(item).strip()
        if text:
            out.append(text)
    return tuple(dict.fromkeys(out))


def _normalize_parameters(*values: Any) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for value in values:
        if not value:
            continue
        if isinstance(value, dict):
            for key, item in value.items():
                params[str(key)] = item
            continue
        for item in _as_list(value):
            if isinstance(item, dict):
                name = item.get("name") or item.get("parameter") or item.get("key")
                if name:
                    params[str(name)] = item.get("value", item.get("default"))
                else:
                    for key, val in item.items():
                        params[str(key)] = val
            elif isinstance(item, str) and "=" in item:
                key, val = item.split("=", 1)
                params[key.strip()] = val.strip()
    return params


def _strip_comments(text: str) -> str:
    import re

    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//.*", "", text)


def _split_top_level_commas(text: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    for idx, char in enumerate(text):
        if char in "([{":
            depth += 1
        elif char in ")]}" and depth:
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:idx].strip())
            start = idx + 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _top_module_parameter_defaults(rtl_files: list[Path], top_module: str | None) -> dict[str, str]:
    if not top_module:
        return {}
    import re

    pattern = re.compile(
        rf"\bmodule\s+{re.escape(top_module)}\s*#\s*\((?P<params>.*?)\)\s*\(",
        re.S,
    )
    defaults: dict[str, str] = {}
    for path in rtl_files:
        if path.suffix.lower() not in VERILOG_SUFFIXES:
            continue
        try:
            text = _strip_comments(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        match = pattern.search(text)
        if not match:
            continue
        for part in _split_top_level_commas(match.group("params")):
            param_match = re.search(
                r"\b(?:parameter|localparam)\b(?:\s+\w+)?\s+(?P<name>[A-Za-z_][A-Za-z0-9_$]*)\s*=\s*(?P<value>.+)$",
                part,
                re.S,
            )
            if param_match:
                defaults[param_match.group("name")] = param_match.group("value").strip()
        break
    return defaults


def _json_file(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _nested_get(mapping: dict[str, Any], *keys: str) -> Any:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _collect_module_names(value: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            names.add(stripped)
    elif isinstance(value, list):
        for item in value:
            names.update(_collect_module_names(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            key_l = str(key).lower()
            if key_l in {
                "module",
                "modules",
                "module_name",
                "bbox",
                "bbox_m",
                "bbox_module",
                "bbox_modules",
                "blackbox",
                "blackboxes",
                "blackbox_module",
                "blackbox_modules",
            }:
                names.update(_collect_module_names(item))
            elif isinstance(item, dict):
                names.update(_collect_module_names(item))
    return names


def _blackbox_modules(design_dir: Path, cfg: dict[str, Any]) -> tuple[str, ...]:
    names = set()
    names.update(_collect_module_names(_nested_get(cfg, "formal", "bbox_modules")))
    names.update(_collect_module_names(_nested_get(cfg, "formal", "blackbox_modules")))
    names.update(_collect_module_names(cfg.get("bbox_modules")))
    names.update(_collect_module_names(cfg.get("blackbox_modules")))
    for path in sorted(design_dir.rglob("jg_bbox.json")):
        payload = _json_file(path)
        if payload is not None:
            names.update(_collect_module_names(payload))
    return tuple(sorted(names))


def _mutation_metadata(design_dir: Path, checkout: Path) -> dict[str, Any]:
    mut_dir = design_dir / "mutations"
    if not mut_dir.is_dir():
        return {
            "has_mutation_cache": False,
            "cache_root": None,
            "metadata_files": [],
            "mutant_directories": [],
            "file_count": 0,
        }
    files = sorted((p.resolve() for p in mut_dir.rglob("*") if p.is_file()), key=str)
    dirs = sorted((p.resolve() for p in mut_dir.rglob("*") if p.is_dir()), key=str)
    metadata_files = [p for p in files if p.name in MUTATION_METADATA_NAMES or p.name.endswith("_summary.json")]
    summaries: dict[str, Any] = {}
    for path in metadata_files:
        payload = _json_file(path)
        if payload is not None:
            summaries[str(path.relative_to(checkout))] = payload
    return {
        "has_mutation_cache": True,
        "cache_root": str(mut_dir.relative_to(checkout)),
        "metadata_files": [str(p.relative_to(checkout)) for p in metadata_files],
        "mutant_directories": [str(p.relative_to(checkout)) for p in dirs],
        "file_count": len(files),
        "summaries": summaries,
    }


def _source_language(paths: list[Path]) -> str:
    suffixes = {p.suffix.lower() for p in paths}
    has_v = bool(suffixes & VERILOG_SUFFIXES)
    has_vhdl = bool(suffixes & VHDL_SUFFIXES)
    if has_v and has_vhdl:
        return "mixed"
    if has_vhdl:
        return "vhdl"
    if has_v:
        return "verilog"
    return "unknown"


def _capability(
    *,
    source_language: str,
    rtl_files: list[Path],
    clocks: tuple[str, ...],
    parameters: dict[str, Any],
    blackbox_modules: tuple[str, ...],
    mutation_metadata: dict[str, Any],
    top_module: str | None,
) -> dict[str, Any]:
    language_supported = source_language in {"verilog", "unknown"}
    unsupported_reasons: list[str] = []
    if source_language in {"vhdl", "mixed"}:
        unsupported_reasons.append("vhdl_or_mixed_language")
    if not rtl_files:
        unsupported_reasons.append("missing_rtl_filelist")
    if not top_module:
        unsupported_reasons.append("missing_top_module")
    if blackbox_modules:
        unsupported_reasons.append("requires_blackbox_stubs")
    if not mutation_metadata.get("has_mutation_cache"):
        unsupported_reasons.append("missing_mutation_cache")
    return {
        "source_language": source_language,
        "verilog_systemverilog": source_language == "verilog",
        "vhdl": source_language == "vhdl",
        "mixed_language": source_language == "mixed",
        "rtl_file_count": len(rtl_files),
        "multi_file": len(rtl_files) > 1,
        "single_clock": len(clocks) == 1,
        "multi_clock": len(clocks) > 1,
        "combinational": len(clocks) == 0,
        "parameterized": bool(parameters),
        "blackbox_required": bool(blackbox_modules),
        "blackbox_modules": list(blackbox_modules),
        "has_complete_mutants": bool(mutation_metadata.get("has_mutation_cache")),
        "mutation_file_count": int(mutation_metadata.get("file_count") or 0),
        "expected_frontend_support": "yosys_read_verilog_sv" if language_supported else "unsupported_language",
        "scoreable": not unsupported_reasons,
        "unsupported_reasons": unsupported_reasons,
    }


def _identity(paths: list[Path], checkout: Path) -> dict[str, Any]:
    entries = []
    for path in sorted(paths, key=str):
        if not path.is_file():
            continue
        entries.append({
            "relpath": str(path.relative_to(checkout)),
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        })
    joined = "\n".join(f"{e['relpath']} {e['sha256']}" for e in entries)
    import hashlib

    return {
        "file_count": len(entries),
        "files": entries,
        "dataset_identity_sha256": hashlib.sha256(joined.encode("utf-8")).hexdigest(),
    }


def _checkout_config_base(checkout: Path) -> Path:
    direct = checkout / "configs" / "assertllm2_design_configs.json"
    nested = checkout / "AssertLLM2" / "configs" / "assertllm2_design_configs.json"
    if direct.is_file():
        return checkout
    if nested.is_file():
        return checkout / "AssertLLM2"
    return checkout


def _config_path_bases(config_base: Path) -> tuple[Path, ...]:
    config_dir = config_base / "configs"
    if config_dir.is_dir():
        return (config_base, config_dir)
    return (config_base,)


def load_design_index(checkout_root: Path | None = None) -> dict[str, Any]:
    checkout = (checkout_root or default_checkout_root()).resolve()
    config_base = _checkout_config_base(checkout)
    index = config_base / "configs" / "assertllm2_design_configs.json"
    if not index.is_file():
        raise ValidationError(f"AssertLLM2 design config not found: {index}")
    payload = json.loads(index.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValidationError(f"design config must be a JSON object: {index}")
    return payload


def discover_designs(checkout_root: Path | None = None) -> list[DesignRecord]:
    checkout = (checkout_root or default_checkout_root()).resolve()
    payload = load_design_index(checkout)
    seen_dirs: dict[Path, str] = {}
    designs: list[DesignRecord] = []
    for key in sorted(payload):
        cfg = payload[key]
        if not isinstance(cfg, dict):
            raise ValidationError(f"design config for {key!r} must be an object")
        spec_files = cfg.get("spec_file") or []
        if not spec_files:
            raise ValidationError(f"design {key}: missing spec_file")
        config_base = _checkout_config_base(checkout)
        path_bases = _config_path_bases(config_base)
        spec_md = _require_file_from_bases(path_bases, spec_files[0], f"spec.md for {key}", checkout)
        category, design_name, design_dir = _design_parts_from_key(key, spec_md, checkout)
        design_dir = design_dir.resolve()
        try:
            design_dir.relative_to(checkout)
        except ValueError as exc:
            raise ValidationError(f"design directory escapes checkout: {design_dir}") from exc
        if design_dir in seen_dirs:
            raise ValidationError(
                f"duplicate design directory {design_dir} for keys {seen_dirs[design_dir]} and {key}"
            )
        seen_dirs[design_dir] = key

        rtl_cfg = cfg.get("rtl") or {}
        if not isinstance(rtl_cfg, dict):
            raise ValidationError(f"design {key}: rtl config must be an object")
        rtl_files = [
            _require_file_from_bases(path_bases, item, f"RTL file for {key}", checkout)
            for item in (rtl_cfg.get("filelist") or [])
        ]
        include_dirs = [
            _maybe_dir_from_bases(path_bases, item, checkout)
            for item in (rtl_cfg.get("incdir") or [])
            if any(((base / item).resolve().exists() if not Path(item).is_absolute() else Path(item).resolve().exists()) for base in path_bases)
        ]
        support_files = tuple(
            p for p in rtl_files
            if p.parent != design_dir or p.name != Path(rtl_files[0]).name
        )
        cr = cfg.get("clock_reset") or {}
        clocks = tuple(
            c.get("signal")
            for c in (cr.get("clocks") or [])
            if isinstance(c, dict) and c.get("signal")
        )
        defines = _string_list(
            rtl_cfg.get("defines")
            or rtl_cfg.get("define")
            or cfg.get("defines")
            or cfg.get("define")
        )
        top_module = rtl_cfg.get("top_module") or cfg.get("top_module") or None
        parameters = _normalize_parameters(
            _top_module_parameter_defaults(rtl_files, top_module),
            rtl_cfg.get("parameters"),
            rtl_cfg.get("parameter"),
            rtl_cfg.get("params"),
            cfg.get("parameters"),
            cfg.get("parameter"),
            cfg.get("params"),
        )
        blackboxes = _blackbox_modules(design_dir, cfg)
        mutation_metadata = _mutation_metadata(design_dir, checkout)
        source_language = _source_language(rtl_files)
        capability = _capability(
            source_language=source_language,
            rtl_files=rtl_files,
            clocks=clocks,
            parameters=parameters,
            blackbox_modules=blackboxes,
            mutation_metadata=mutation_metadata,
            top_module=top_module,
        )
        all_identity_paths = [spec_md, *rtl_files, *_raw_specs_for_design(design_dir)]
        designs.append(DesignRecord(
            key=key,
            category=category,
            design_name=design_name,
            design_dir=design_dir,
            spec_md=spec_md,
            raw_specs=_raw_specs_for_design(design_dir),
            rtl_files=tuple(rtl_files),
            include_dirs=tuple(include_dirs),
            support_files=support_files,
            mutation_files=_mutation_files(design_dir),
            top_module=top_module,
            clocks=clocks,
            reset=cr.get("reset") or None,
            source_language=source_language,
            defines=defines,
            parameters=parameters,
            blackbox_modules=blackboxes,
            mutation_metadata=mutation_metadata,
            capability=capability,
            upstream_config=cfg,
            identity=_identity(all_identity_paths, checkout),
        ))
    return designs


def capability_matrix(checkout_root: Path | None = None) -> list[dict[str, Any]]:
    rows = []
    for design in discover_designs(checkout_root):
        rows.append({
            "key": design.key,
            "category": design.category,
            "design_name": design.design_name,
            "top_module": design.top_module,
            **design.capability,
        })
    return rows


def get_design(key: str, checkout_root: Path | None = None) -> DesignRecord:
    designs = discover_designs(checkout_root)
    by_key = {d.key: d for d in designs}
    try:
        return by_key[key]
    except KeyError as exc:
        raise ValidationError(f"unknown design key: {key}") from exc
