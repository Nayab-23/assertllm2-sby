from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from .formal_types import FormalResult, FormalStatus, MutantEvaluation
from .manifest import write_json


def write_formal_summary(
    outdir: Path,
    *,
    run_id: str,
    results: list[FormalResult],
    mutants: list[MutantEvaluation] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    mutants = mutants or []
    counts = {status.value: 0 for status in FormalStatus}
    for result in results:
        counts[result.status.value] += 1
    payload = {
        "run_id": run_id,
        "status_counts": counts,
        "results": [r.to_json() for r in results],
        "mutants": [m.to_json() for m in mutants],
        "extra": extra or {},
    }
    write_json(outdir / "summary.json", payload)
    with (outdir / "summary.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["task_id", "mode", "status", "runtime_s", "trace_count"])
        for result in results:
            writer.writerow([result.task_id, result.mode, result.status.value, result.runtime_s, len(result.trace_files)])
    return payload
