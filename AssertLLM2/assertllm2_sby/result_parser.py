from __future__ import annotations

import re
from pathlib import Path

from .formal_types import FormalStatus


def collect_trace_files(workdir: Path) -> tuple[Path, ...]:
    suffixes = {".vcd", ".yw", ".smtc", ".v"}
    traces = []
    for path in workdir.rglob("*"):
        if not path.is_file():
            continue
        if "trace" in path.name and path.suffix in suffixes:
            traces.append(path.resolve())
    return tuple(sorted(traces, key=str))


def parse_sby_status(log_text: str, *, mode: str, returncode: int | None, timed_out: bool) -> FormalStatus:
    text = log_text.lower()
    if timed_out:
        return FormalStatus.TIMEOUT
    if "infrastructure error:" in text or "command not found" in text or "no such file or directory" in text:
        return FormalStatus.INFRASTRUCTURE_ERROR
    if "unsupported" in text:
        return FormalStatus.UNSUPPORTED
    if any(
        marker in text
        for marker in (
            "can't resolve module",
            "cannot resolve module",
            "module not found",
            "unknown module",
            "not part of the design",
            "hierarchy command failed",
            "re-definition of module",
            "duplicate module",
            "can't find gold module",
        )
    ):
        return FormalStatus.ELABORATION_ERROR
    if "syntax error" in text or "error:" in text or "failed to" in text:
        if "status: failed" not in text and "returned fail" not in text:
            return FormalStatus.ERROR
    if "status: failed" in text or "returned fail" in text or "assert failed" in text:
        if mode == "cover":
            return FormalStatus.COVER_UNREACHED_AT_DEPTH
        return FormalStatus.COUNTEREXAMPLE
    if "status: passed" in text or "returned pass" in text or "done (pass" in text:
        if mode == "prove":
            return FormalStatus.PROVEN
        if mode == "cover":
            return FormalStatus.COVER_REACHED
        return FormalStatus.BOUNDED_CLEAN
    if returncode not in (0, None):
        return FormalStatus.ERROR
    return FormalStatus.UNKNOWN


def extract_counterexample_assertion(log_text: str) -> str | None:
    patterns = [
        re.compile(r"assert failed[^\\n]*?([^/\\s]+:[0-9]+)", re.IGNORECASE),
        re.compile(r"assertion failed[^\\n]*?([^/\\s]+:[0-9]+)", re.IGNORECASE),
    ]
    for pattern in patterns:
        match = pattern.search(log_text)
        if match:
            return match.group(1)
    return None
