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


def _maybe_dir(base: Path, value: str | Path, containment_root: Path | None = None) -> Path:
    path = _resolve_inside(base, value, containment_root)
    if not path.is_dir():
        raise ValidationError(f"missing include directory: {path}")
    return path


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


def load_design_index(checkout_root: Path | None = None) -> dict[str, Any]:
    checkout = (checkout_root or default_checkout_root()).resolve()
    index = checkout / "AssertLLM2" / "configs" / "assertllm2_design_configs.json"
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
        config_base = checkout / "AssertLLM2"
        spec_md = _require_file(config_base, spec_files[0], f"spec.md for {key}", checkout)
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
        rtl_files = [
            _require_file(config_base, item, f"RTL file for {key}", checkout)
            for item in (rtl_cfg.get("filelist") or [])
        ]
        include_dirs = [
            _maybe_dir(config_base, item, checkout)
            for item in (rtl_cfg.get("incdir") or [])
            if (config_base / item).resolve().exists()
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
            top_module=rtl_cfg.get("top_module") or None,
            clocks=clocks,
            reset=cr.get("reset") or None,
            source_language=_source_language(rtl_files),
            upstream_config=cfg,
            identity=_identity(all_identity_paths, checkout),
        ))
    return designs


def get_design(key: str, checkout_root: Path | None = None) -> DesignRecord:
    designs = discover_designs(checkout_root)
    by_key = {d.key: d for d in designs}
    try:
        return by_key[key]
    except KeyError as exc:
        raise ValidationError(f"unknown design key: {key}") from exc
