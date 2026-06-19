from __future__ import annotations

from pathlib import Path

from .models import ValidationError

# assertllm2-sby/ (repository root)
PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def resolve_assertllm2_checkout() -> Path:
    """Return the read-only AssertLLM2 benchmark checkout."""
    candidates = (
        PACKAGE_ROOT / "third_party" / "AssertLLM2",
        PACKAGE_ROOT.parent / "third_party" / "AssertLLM2",
        PACKAGE_ROOT / "assertllm2-sby" / "third_party" / "AssertLLM2",
    )
    for path in candidates:
        if path.is_symlink() and not path.exists():
            raise ValidationError(
                f"AssertLLM2 checkout path is a broken symlink: {path} -> {path.readlink()}"
            )
        if not path.exists():
            continue
        if not path.is_dir():
            raise ValidationError(f"AssertLLM2 checkout path is not a directory: {path}")
        resolved = path.resolve()
        direct_config = resolved / "configs" / "assertllm2_design_configs.json"
        nested_config = resolved / "AssertLLM2" / "configs" / "assertllm2_design_configs.json"
        if direct_config.is_file() or nested_config.is_file():
            return resolved
        continue
    searched = ", ".join(str(path) for path in candidates)
    raise ValidationError(f"AssertLLM2 checkout not found; searched: {searched}")


def config_path() -> Path:
    return PACKAGE_ROOT / "AssertLLM2" / "configs" / "assertllm2_sby.yaml"


def results_root() -> Path:
    return PACKAGE_ROOT / "results"


def runs_root() -> Path:
    return PACKAGE_ROOT / "runs" / "assertllm2-sby"


def dotenv_path() -> Path:
    for path in (PACKAGE_ROOT / ".env", PACKAGE_ROOT.parent / ".env"):
        if path.is_file():
            return path
    return PACKAGE_ROOT / ".env"
