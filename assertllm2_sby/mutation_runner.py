from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .formal_types import FormalResult, FormalStatus, MutantEvaluation
from .formal_types import SourcePlan
from .models import DesignRecord, ValidationError


@dataclass(frozen=True)
class MutantRecord:
    mutant_id: str
    directory: Path | None
    applied_to_source_files: tuple[str, ...] = ()
    log: str | None = None
    source_kind: str = "mutation_cache"
    present_files: tuple[Path, ...] = ()
    missing_mutant_rtl: tuple[str, ...] = ()

    @property
    def scoreable(self) -> bool:
        return self.directory is not None and not self.missing_mutant_rtl

    @property
    def non_scoreable_reason(self) -> str | None:
        if self.directory is None:
            return "missing_mutant_directory"
        if self.missing_mutant_rtl:
            return "missing_mutant_rtl"
        return None

    def to_json(self) -> dict[str, Any]:
        return {
            "mutant_id": self.mutant_id,
            "directory": str(self.directory) if self.directory else None,
            "applied_to_source_files": list(self.applied_to_source_files),
            "log": self.log,
            "source_kind": self.source_kind,
            "present_files": [str(path) for path in self.present_files],
            "missing_mutant_rtl": list(self.missing_mutant_rtl),
            "scoreable": self.scoreable,
            "non_scoreable_reason": self.non_scoreable_reason,
        }


@dataclass(frozen=True)
class MutationCache:
    design_key: str
    cache_root: Path | None
    summary_files: tuple[Path, ...]
    mutants: tuple[MutantRecord, ...]
    merged_bug_hunting_dirs: tuple[Path, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "design_key": self.design_key,
            "cache_root": str(self.cache_root) if self.cache_root else None,
            "summary_files": [str(path) for path in self.summary_files],
            "mutant_count": len(self.mutants),
            "scoreable_mutant_count": sum(1 for item in self.mutants if item.scoreable),
            "non_scoreable_mutant_count": sum(1 for item in self.mutants if not item.scoreable),
            "merged_bug_hunting_dirs": [str(path) for path in self.merged_bug_hunting_dirs],
            "mutants": [item.to_json() for item in self.mutants],
            "official_jaspergold_result": False,
        }


def _json_file(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _summary_files(design: DesignRecord) -> tuple[Path, ...]:
    paths = []
    mut_dir = design.design_dir / "mutations"
    if mut_dir.is_dir():
        for path in mut_dir.rglob("*.json"):
            if path.name in {"mutation_summary.json", "mutation_metadata.json", "mutants.json", "merged_mutants.json"} or path.name.endswith("_summary.json"):
                paths.append(path.resolve())
    return tuple(sorted(set(paths), key=str))


def _summary_rows(summary_files: tuple[Path, ...]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for path in summary_files:
        payload = _json_file(path)
        if not isinstance(payload, dict):
            continue
        mutants = payload.get("mutants")
        if not isinstance(mutants, list):
            continue
        for item in mutants:
            if not isinstance(item, dict):
                continue
            mutant_id = item.get("mutant_id") or item.get("id") or item.get("name")
            if mutant_id:
                rows[str(mutant_id)] = dict(item)
    return rows


def _mutant_dirs(design: DesignRecord) -> dict[str, Path]:
    dirs: dict[str, Path] = {}
    for root in (
        design.design_dir / "mutations" / "mutants",
        design.design_dir / "buggy_artifacts" / "single_bug_mutants",
    ):
        if root.is_dir():
            for path in root.iterdir():
                if path.is_dir() and path.name.startswith("M_"):
                    dirs.setdefault(path.name, path.resolve())
    for path in design.mutation_files:
        for parent in path.parents:
            if parent.name.startswith("M_"):
                dirs.setdefault(parent.name, parent.resolve())
                break
    return dirs


def _merged_bug_hunting_dirs(design: DesignRecord) -> tuple[Path, ...]:
    root = design.design_dir / "buggy_artifacts"
    if not root.is_dir():
        return ()
    return tuple(sorted((p.resolve() for p in root.rglob("merged_buggy_rtl") if p.is_dir()), key=str))


def buggy_rtl_files(design: DesignRecord) -> tuple[Path, ...]:
    files: list[Path] = []
    root = design.design_dir / "buggy_artifacts"
    if root.is_dir():
        for dirname in ("merged_buggy_rtl", "single_bug_mutants"):
            for base in root.rglob(dirname):
                if base.is_dir():
                    files.extend(path.resolve() for path in base.rglob("*") if path.is_file() and path.suffix.lower() in {".v", ".sv", ".vh", ".svh", ".vhd", ".vhdl"})
    return tuple(sorted(set(files), key=str))


def _present_files(directory: Path | None) -> tuple[Path, ...]:
    if directory is None or not directory.is_dir():
        return ()
    return tuple(sorted((path.resolve() for path in directory.rglob("*") if path.is_file()), key=str))


def _candidate_for_source(mutant_dir: Path, golden_path: Path, design_dir: Path) -> Path | None:
    rel: Path | None = None
    try:
        rel = golden_path.resolve().relative_to(design_dir.resolve())
    except ValueError:
        rel = None
    candidates = []
    if rel is not None:
        candidates.append(mutant_dir / rel)
    candidates.append(mutant_dir / golden_path.name)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def _matches_applied_source(golden_path: Path, design_dir: Path, applied: tuple[str, ...]) -> bool:
    if not applied:
        return False
    try:
        rel = str(golden_path.resolve().relative_to(design_dir.resolve()))
    except ValueError:
        rel = golden_path.name
    normalized = {item.replace("\\", "/").lstrip("./") for item in applied}
    return rel.replace("\\", "/") in normalized or golden_path.name in {Path(item).name for item in normalized}


def _missing_mutant_rtl(mutant_dir: Path | None, design: DesignRecord, applied: tuple[str, ...]) -> tuple[str, ...]:
    if mutant_dir is None:
        return applied or tuple(str(path.name) for path in design.rtl_files)
    missing: list[str] = []
    if applied:
        for item in applied:
            if not any(_candidate_for_source(mutant_dir, rtl, design.design_dir) for rtl in design.rtl_files if _matches_applied_source(rtl, design.design_dir, (item,))):
                missing.append(item)
    elif not any(_candidate_for_source(mutant_dir, rtl, design.design_dir) for rtl in design.rtl_files):
        missing.append("no_mutant_rtl_matching_golden_filelist")
    return tuple(missing)


def load_mutation_cache(design: DesignRecord) -> MutationCache:
    summary_files = _summary_files(design)
    summary_rows = _summary_rows(summary_files)
    dirs = _mutant_dirs(design)
    records: list[MutantRecord] = []
    for mutant_id in sorted(set(summary_rows) | set(dirs)):
        summary = summary_rows.get(mutant_id, {})
        applied = tuple(str(item) for item in summary.get("applied_to_source_files") or ())
        directory = dirs.get(mutant_id)
        records.append(MutantRecord(
            mutant_id=mutant_id,
            directory=directory,
            applied_to_source_files=applied,
            log=str(summary.get("log")) if summary.get("log") is not None else None,
            source_kind="mutation_cache" if directory and "mutations" in directory.parts else "bug_hunting_single",
            present_files=_present_files(directory),
            missing_mutant_rtl=_missing_mutant_rtl(directory, design, applied),
        ))
    cache_root = design.design_dir / "mutations"
    return MutationCache(
        design_key=design.key,
        cache_root=cache_root.resolve() if cache_root.is_dir() else None,
        summary_files=summary_files,
        mutants=tuple(records),
        merged_bug_hunting_dirs=_merged_bug_hunting_dirs(design),
    )


def mutant_source_plan(golden: SourcePlan, design: DesignRecord, mutant: MutantRecord) -> SourcePlan:
    if not mutant.scoreable or mutant.directory is None:
        raise ValidationError(f"mutant is not scoreable: {mutant.mutant_id}: {mutant.non_scoreable_reason}")
    replacements: list[Path] = []
    changed = False
    for rtl in golden.rtl_files:
        should_replace = _matches_applied_source(rtl, design.design_dir, mutant.applied_to_source_files)
        if not mutant.applied_to_source_files:
            should_replace = True
        candidate = _candidate_for_source(mutant.directory, rtl, design.design_dir) if should_replace else None
        if candidate is not None:
            replacements.append(candidate)
            changed = True
        else:
            replacements.append(rtl)
    if not changed:
        raise ValidationError(f"mutant has no applicable RTL replacements: {mutant.mutant_id}")
    return SourcePlan(
        name=f"{golden.name}__{mutant.mutant_id}",
        top_module=golden.top_module,
        rtl_files=tuple(replacements),
        include_dirs=golden.include_dirs,
        defines=golden.defines,
        parameters=golden.parameters,
        blackbox_modules=golden.blackbox_modules,
    )


def merged_buggy_source_plan(golden: SourcePlan, design: DesignRecord, merged_dir: Path) -> SourcePlan:
    if not merged_dir.is_dir():
        raise ValidationError(f"missing merged buggy RTL directory: {merged_dir}")
    replacements: list[Path] = []
    changed = False
    for rtl in golden.rtl_files:
        candidate = _candidate_for_source(merged_dir, rtl, design.design_dir)
        if candidate is not None:
            replacements.append(candidate)
            changed = True
        else:
            replacements.append(rtl)
    if not changed:
        raise ValidationError(f"merged buggy RTL has no applicable replacements: {merged_dir}")
    return SourcePlan(
        name=f"{golden.name}__merged_buggy_rtl",
        top_module=golden.top_module,
        rtl_files=tuple(replacements),
        include_dirs=golden.include_dirs,
        defines=golden.defines,
        parameters=golden.parameters,
        blackbox_modules=golden.blackbox_modules,
    )


def bug_hunting_metrics(
    *,
    design_key: str,
    clean_rows: list[dict[str, Any]],
    merged_buggy_rows: list[dict[str, Any]],
    mutant_metrics: dict[str, Any],
    visible_buggy_rtl_files: list[str],
) -> dict[str, Any]:
    clean_cex = [row for row in clean_rows if row.get("golden_outcome") == "GOLDEN_COUNTEREXAMPLE"]
    clean_assert_count = len(clean_rows)
    detected = sum(1 for row in merged_buggy_rows if row.get("status") in {"STRICT_KILLED", "BOUNDED_ONLY_KILLED"})
    survived = sum(1 for row in merged_buggy_rows if row.get("status") == "SURVIVED")
    error = sum(
        1
        for row in merged_buggy_rows
        if row.get("status") in {"TIMEOUT", "UNKNOWN", "ELABORATION_ERROR", "INFRASTRUCTURE_ERROR", "UNSUPPORTED", "NON_SCOREABLE"}
    )
    evaluated = detected + survived + error
    return {
        "schema_version": "1.0",
        "adapter": "AssertLLM2-SBY",
        "design_key": design_key,
        "mode": "bug-hunting",
        "official_jaspergold_result": False,
        "visible_buggy_rtl_files": visible_buggy_rtl_files,
        "clean_design": {
            "assertion_count": clean_assert_count,
            "cex_count": len(clean_cex),
            "cex_assertions": [row.get("assertion_id") for row in clean_cex],
            "clean_design_cex_ratio": (len(clean_cex) / clean_assert_count) if clean_assert_count else None,
        },
        "merged_buggy_targets": {
            "evaluated_count": evaluated,
            "detected_count": detected,
            "missed_count": survived,
            "error_count": error,
            "detection_rate": (detected / evaluated) if evaluated else None,
            "miss_rate": (survived / evaluated) if evaluated else None,
            "error_rate": (error / evaluated) if evaluated else None,
            "results": merged_buggy_rows,
        },
        "mutation_metrics": mutant_metrics,
    }


def mutation_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    keys = [
        "STRICT_KILLED",
        "BOUNDED_ONLY_KILLED",
        "SURVIVED",
        "TIMEOUT",
        "UNKNOWN",
        "ELABORATION_ERROR",
        "INFRASTRUCTURE_ERROR",
        "UNSUPPORTED",
        "NOT_RUN",
        "NON_SCOREABLE",
    ]
    counts = {key: 0 for key in keys}
    for row in rows:
        status = str(row.get("status") or "UNKNOWN")
        counts[status] = counts.get(status, 0) + 1
    return counts


def mutation_metrics(
    *,
    design_key: str,
    mutation_cache: MutationCache,
    mutant_rows: list[dict[str, Any]],
    eligible_assertions: list[dict[str, Any]],
) -> dict[str, Any]:
    counts = mutation_counts(mutant_rows)
    strict_killed = counts.get("STRICT_KILLED", 0)
    bounded_killed = counts.get("BOUNDED_ONLY_KILLED", 0)
    survived = counts.get("SURVIVED", 0)
    scoreable_evaluated = strict_killed + bounded_killed + survived
    strict_denominator = strict_killed + survived
    return {
        "schema_version": "1.0",
        "adapter": "AssertLLM2-SBY",
        "design_key": design_key,
        "official_jaspergold_result": False,
        "eligible_assertion_count": len(eligible_assertions),
        "eligible_assertions": [row.get("assertion_id") for row in eligible_assertions],
        "mutant_count": len(mutation_cache.mutants),
        "scoreable_mutant_count": sum(1 for item in mutation_cache.mutants if item.scoreable),
        "non_scoreable_mutant_count": sum(1 for item in mutation_cache.mutants if not item.scoreable),
        "selected_mutant_count": len(mutant_rows),
        "counts": counts,
        "strict_killed": strict_killed,
        "bounded_only_killed": bounded_killed,
        "survived": survived,
        "scoreable_evaluated": scoreable_evaluated,
        "strict_mutation_score": (strict_killed / strict_denominator) if strict_denominator else None,
        "bounded_inclusive_mutation_score": ((strict_killed + bounded_killed) / scoreable_evaluated) if scoreable_evaluated else None,
        "non_kill_statuses": {
            "timeout": counts.get("TIMEOUT", 0),
            "unknown": counts.get("UNKNOWN", 0),
            "unsupported": counts.get("UNSUPPORTED", 0),
            "elaboration_error": counts.get("ELABORATION_ERROR", 0),
            "infrastructure_error": counts.get("INFRASTRUCTURE_ERROR", 0),
            "not_run": counts.get("NOT_RUN", 0),
            "non_scoreable": counts.get("NON_SCOREABLE", 0),
        },
    }


def mutation_results_payload(
    *,
    design_key: str,
    mutation_cache: MutationCache,
    mutant_rows: list[dict[str, Any]],
    eligible_assertions: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "adapter": "AssertLLM2-SBY",
        "design_key": design_key,
        "official_jaspergold_result": False,
        "kill_semantics": "A mutant is killed only when a golden-accepted assertion produces a counterexample on the mutant; elaboration, infrastructure, timeout, unknown, and unsupported statuses are not kills.",
        "mutation_cache": mutation_cache.to_json(),
        "eligible_assertions": [
            {
                "assertion_id": row.get("assertion_id"),
                "golden_outcome": row.get("golden_outcome"),
            }
            for row in eligible_assertions
        ],
        "mutants": mutant_rows,
    }


def classify_mutant(
    *,
    mutant_id: str,
    golden_result: FormalResult,
    mutant_result: FormalResult,
    responsible_assertion: str | None = None,
) -> MutantEvaluation:
    golden_ok = golden_result.status in {FormalStatus.PROVEN, FormalStatus.BOUNDED_CLEAN}
    killed = golden_ok and mutant_result.status == FormalStatus.COUNTEREXAMPLE
    return MutantEvaluation(
        mutant_id=mutant_id,
        killed=killed,
        responsible_assertion=responsible_assertion if killed else None,
        golden_status=golden_result.status,
        mutant_status=mutant_result.status,
        mutant_result=mutant_result,
    )
