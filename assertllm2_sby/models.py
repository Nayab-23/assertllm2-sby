from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class AdapterError(RuntimeError):
    pass


class ValidationError(AdapterError):
    pass


class GenerationBlocked(AdapterError):
    pass


class GenerationMode(str, Enum):
    BUG_PREVENTION = "bug-prevention"


class SpecSource(str, Enum):
    SPEC_MD = "spec_md"
    RAW = "raw"


class AssertionClassification(str, Enum):
    SUPPORTED_CANDIDATE = "SUPPORTED_CANDIDATE"
    REQUIRES_EXACT_LOWERING = "REQUIRES_EXACT_LOWERING"
    UNSUPPORTED_SVA = "UNSUPPORTED_SVA"
    TRUNCATED_OR_INVALID_OUTPUT = "TRUNCATED_OR_INVALID_OUTPUT"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    EMPTY_OUTPUT = "EMPTY_OUTPUT"
    NEEDS_FORMAL_VALIDATION = "NEEDS_FORMAL_VALIDATION"


@dataclass(frozen=True)
class FileRecord:
    path: Path
    relpath: str
    sha256: str
    size: int

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["path"] = str(self.path)
        return data


@dataclass(frozen=True)
class DesignRecord:
    key: str
    category: str
    design_name: str
    design_dir: Path
    spec_md: Path
    raw_specs: tuple[Path, ...]
    rtl_files: tuple[Path, ...]
    include_dirs: tuple[Path, ...]
    support_files: tuple[Path, ...]
    mutation_files: tuple[Path, ...]
    top_module: str | None
    clocks: tuple[str, ...]
    reset: str | None
    source_language: str
    upstream_config: dict[str, Any] = field(default_factory=dict)
    identity: dict[str, Any] = field(default_factory=dict)

    def to_json(self, include_upstream: bool = True) -> dict[str, Any]:
        data = asdict(self)
        for field_name in (
            "design_dir",
            "spec_md",
        ):
            data[field_name] = str(getattr(self, field_name))
        for field_name in (
            "raw_specs",
            "rtl_files",
            "include_dirs",
            "support_files",
            "mutation_files",
            "clocks",
        ):
            data[field_name] = [str(x) for x in getattr(self, field_name)]
        if not include_upstream:
            data.pop("upstream_config", None)
        return data


@dataclass(frozen=True)
class ExposedFile:
    original_path: Path
    workspace_path: Path
    relpath: str
    sha256: str
    size: int
    role: str

    def to_json(self) -> dict[str, Any]:
        return {
            "original_path": str(self.original_path),
            "workspace_path": str(self.workspace_path),
            "relpath": self.relpath,
            "sha256": self.sha256,
            "size": self.size,
            "role": self.role,
        }


@dataclass(frozen=True)
class IsolatedWorkspace:
    root: Path
    manifest_path: Path
    design_key: str
    mode: GenerationMode
    spec_source: SpecSource
    exposed_files: tuple[ExposedFile, ...]
    generator_config: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "manifest_path": str(self.manifest_path),
            "design_key": self.design_key,
            "mode": self.mode.value,
            "spec_source": self.spec_source.value,
            "exposed_files": [f.to_json() for f in self.exposed_files],
            "generator_config": self.generator_config,
        }


@dataclass(frozen=True)
class AssertionCandidate:
    assertion_id: str
    text: str
    classification: AssertionClassification
    reasons: tuple[str, ...] = ()
    label: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "assertion_id": self.assertion_id,
            "text": self.text,
            "classification": self.classification.value,
            "reasons": list(self.reasons),
            "label": self.label,
        }


@dataclass(frozen=True)
class GenerationResult:
    design_key: str
    workspace: Path
    output_dir: Path
    succeeded: bool
    blocked_reason: str | None
    raw_response_path: Path | None
    assertions_path: Path | None
    candidates: tuple[AssertionCandidate, ...]
    metadata: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "design_key": self.design_key,
            "workspace": str(self.workspace),
            "output_dir": str(self.output_dir),
            "succeeded": self.succeeded,
            "blocked_reason": self.blocked_reason,
            "raw_response_path": str(self.raw_response_path) if self.raw_response_path else None,
            "assertions_path": str(self.assertions_path) if self.assertions_path else None,
            "candidates": [c.to_json() for c in self.candidates],
            "metadata": self.metadata,
        }
