from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class FormalStatus(str, Enum):
    PROVEN = "PROVEN"
    BOUNDED_CLEAN = "BOUNDED_CLEAN"
    COUNTEREXAMPLE = "COUNTEREXAMPLE"
    COVER_REACHED = "COVER_REACHED"
    COVER_UNREACHED_AT_DEPTH = "COVER_UNREACHED_AT_DEPTH"
    UNKNOWN = "UNKNOWN"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class FormalConfig:
    bmc_depth: int = 8
    prove_depth: int = 8
    cover_depth: int = 6
    timeout_seconds: int = 30
    solver: str = "z3"
    jobs: int = 1
    trace: bool = True

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SourcePlan:
    name: str
    top_module: str
    rtl_files: tuple[Path, ...]
    include_dirs: tuple[Path, ...] = ()
    defines: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "top_module": self.top_module,
            "rtl_files": [str(p) for p in self.rtl_files],
            "include_dirs": [str(p) for p in self.include_dirs],
            "defines": list(self.defines),
        }


@dataclass(frozen=True)
class LoweredAssertion:
    assertion_id: str
    kind: str
    original_text: str
    lowered_text: str
    supported: bool
    reasons: tuple[str, ...] = ()
    transformation_rule: str | None = None
    equivalence_assumptions: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FormalTask:
    task_id: str
    mode: str
    depth: int
    source_plan: SourcePlan
    assertions: tuple[LoweredAssertion, ...]
    workdir: Path
    top_module: str = "sby_harness"

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "mode": self.mode,
            "depth": self.depth,
            "source_plan": self.source_plan.to_json(),
            "assertions": [a.to_json() for a in self.assertions],
            "workdir": str(self.workdir),
            "top_module": self.top_module,
        }


@dataclass(frozen=True)
class FormalResult:
    task_id: str
    mode: str
    status: FormalStatus
    returncode: int | None
    runtime_s: float
    workdir: Path
    sby_file: Path | None = None
    log_file: Path | None = None
    trace_files: tuple[Path, ...] = ()
    assertion_ids: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "mode": self.mode,
            "status": self.status.value,
            "returncode": self.returncode,
            "runtime_s": self.runtime_s,
            "workdir": str(self.workdir),
            "sby_file": str(self.sby_file) if self.sby_file else None,
            "log_file": str(self.log_file) if self.log_file else None,
            "trace_files": [str(p) for p in self.trace_files],
            "assertion_ids": list(self.assertion_ids),
            "details": self.details,
        }


@dataclass(frozen=True)
class MutantEvaluation:
    mutant_id: str
    killed: bool
    responsible_assertion: str | None
    golden_status: FormalStatus
    mutant_status: FormalStatus
    mutant_result: FormalResult

    def to_json(self) -> dict[str, Any]:
        return {
            "mutant_id": self.mutant_id,
            "killed": self.killed,
            "responsible_assertion": self.responsible_assertion,
            "golden_status": self.golden_status.value,
            "mutant_status": self.mutant_status.value,
            "mutant_result": self.mutant_result.to_json(),
        }
