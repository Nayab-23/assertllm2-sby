from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from .manifest import redacted_mapping, sha256_file, utc_now_iso, write_json
from .models import (
    DesignRecord,
    ExposedFile,
    GenerationMode,
    IsolatedWorkspace,
    SpecSource,
    ValidationError,
)

FORBIDDEN_SUFFIXES = {
    ".v", ".sv", ".vh", ".svh", ".vhd", ".vhdl", ".sby", ".smt2", ".vcd", ".fst",
    ".jsonl",
}
FORBIDDEN_NAMES = {
    ".env", ".git", "mutations", "mutation_summary.json", "mutants_index.json",
    "baseline_eval.json", "metrics.json", "mutation_results.json", "assertions.sv",
}


def _copy_exposed(src: Path, workspace_root: Path, role: str) -> ExposedFile:
    dst = workspace_root / "input" / src.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return ExposedFile(
        original_path=src.resolve(),
        workspace_path=dst.resolve(),
        relpath=str(dst.relative_to(workspace_root)),
        sha256=sha256_file(dst),
        size=dst.stat().st_size,
        role=role,
    )


def validate_workspace_isolation(workspace_root: Path) -> None:
    root = workspace_root.resolve()
    problems = []
    for path in root.rglob("*"):
        rel_parts = set(path.relative_to(root).parts)
        if path.name in FORBIDDEN_NAMES or rel_parts & FORBIDDEN_NAMES:
            problems.append(f"forbidden name: {path.relative_to(root)}")
        if path.is_file() and path.suffix.lower() in FORBIDDEN_SUFFIXES:
            if path.name != "manifest.json":
                problems.append(f"forbidden suffix: {path.relative_to(root)}")
    if problems:
        raise ValidationError("isolated workspace contains forbidden files: " + "; ".join(problems))


def create_isolated_workspace(
    design: DesignRecord,
    *,
    mode: GenerationMode = GenerationMode.BUG_PREVENTION,
    spec_source: SpecSource = SpecSource.SPEC_MD,
    include_raw: bool = False,
    output_root: Path | None = None,
    generator_config: dict[str, Any] | None = None,
) -> IsolatedWorkspace:
    if mode != GenerationMode.BUG_PREVENTION:
        raise ValidationError(f"unsupported generation mode for this stage: {mode.value}")
    if spec_source == SpecSource.RAW and not include_raw:
        raise ValidationError("raw spec source requires include_raw=True")

    parent = output_root.resolve() if output_root else Path(tempfile.mkdtemp(prefix="assertllm2_sby_parent_"))
    parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="gen_", dir=parent)).resolve()

    exposed: list[ExposedFile] = []
    if spec_source == SpecSource.SPEC_MD:
        exposed.append(_copy_exposed(design.spec_md, root, "spec_md"))
    else:
        if not design.raw_specs:
            raise ValidationError(f"design has no raw specification documents: {design.key}")
        for raw in design.raw_specs:
            exposed.append(_copy_exposed(raw, root, "raw_spec"))

    validate_workspace_isolation(root)
    cfg = redacted_mapping(generator_config or {})
    manifest = {
        "schema_version": "1.0",
        "adapter": "AssertLLM2-SBY",
        "created_at": utc_now_iso(),
        "design_key": design.key,
        "mode": mode.value,
        "specification_source": spec_source.value,
        "exposed_files": [f.to_json() for f in exposed],
        "forbidden_inputs_excluded": [
            "golden_rtl",
            "support_rtl",
            "mutated_rtl",
            "mutation_metadata",
            "golden_assertions",
            "official_formal_results",
            "prior_generation_outputs",
            ".env",
            ".git",
        ],
        "generator_config": cfg,
    }
    manifest_path = root / "manifest.json"
    write_json(manifest_path, manifest)
    return IsolatedWorkspace(
        root=root,
        manifest_path=manifest_path,
        design_key=design.key,
        mode=mode,
        spec_source=spec_source,
        exposed_files=tuple(exposed),
        generator_config=cfg,
    )
