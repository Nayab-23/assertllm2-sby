from __future__ import annotations

import shutil
from pathlib import Path

from .formal_types import SourcePlan
from .models import DesignRecord, ValidationError


def build_source_plan(design: DesignRecord, *, name: str | None = None) -> SourcePlan:
    if not design.top_module:
        raise ValidationError(f"design has no top module: {design.key}")
    if not design.rtl_files:
        raise ValidationError(f"design has no RTL files: {design.key}")
    return SourcePlan(
        name=name or design.key.replace("/", "__"),
        top_module=design.top_module,
        rtl_files=tuple(path.resolve() for path in design.rtl_files),
        include_dirs=tuple(path.resolve() for path in design.include_dirs),
    )


def materialize_source_plan(plan: SourcePlan, workdir: Path) -> SourcePlan:
    """Copy RTL/include files into an isolated workdir without modifying originals."""
    src_dir = workdir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    seen_names: set[str] = set()
    for idx, rtl in enumerate(plan.rtl_files):
        name = rtl.name
        if name in seen_names:
            name = f"{idx:04d}_{rtl.name}"
        seen_names.add(name)
        dst = src_dir / name
        shutil.copy2(rtl, dst)
        copied.append(dst.resolve())
    return SourcePlan(
        name=plan.name,
        top_module=plan.top_module,
        rtl_files=tuple(copied),
        include_dirs=(src_dir.resolve(),),
        defines=plan.defines,
    )
