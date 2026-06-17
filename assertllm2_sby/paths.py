from __future__ import annotations

from pathlib import Path

# assertllm2-sby/ (package distribution root)
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def resolve_assertllm2_checkout() -> Path:
    """Return the read-only AssertLLM2 benchmark checkout."""
    candidates = (
        PACKAGE_ROOT / "third_party" / "AssertLLM2",
        PACKAGE_ROOT.parent / "third_party" / "AssertLLM2",
    )
    for path in candidates:
        if path.is_dir():
            return path.resolve()
    return candidates[0].resolve()


def config_path() -> Path:
    return PACKAGE_ROOT / "config" / "assertllm2_sby.yaml"


def results_root() -> Path:
    return PACKAGE_ROOT / "results"


def runs_root() -> Path:
    return PACKAGE_ROOT / "runs" / "assertllm2-sby"


def dotenv_path() -> Path:
    for path in (PACKAGE_ROOT / ".env", PACKAGE_ROOT.parent / ".env"):
        if path.is_file():
            return path
    return PACKAGE_ROOT / ".env"
