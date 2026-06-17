from __future__ import annotations

import subprocess
import time
from pathlib import Path

from .formal_types import FormalConfig, FormalResult, FormalStatus, FormalTask
from .harness_builder import write_harness, write_sby_file
from .manifest import write_json
from .result_parser import collect_trace_files, parse_sby_status


def run_sby_task(task: FormalTask, *, config: FormalConfig, harness_body: str) -> FormalResult:
    if any(not item.supported for item in task.assertions):
        result = FormalResult(
            task_id=task.task_id,
            mode=task.mode,
            status=FormalStatus.UNSUPPORTED,
            returncode=None,
            runtime_s=0.0,
            workdir=task.workdir,
            assertion_ids=tuple(item.assertion_id for item in task.assertions),
            details={"unsupported": [a.to_json() for a in task.assertions if not a.supported]},
        )
        write_json(task.workdir / "result.json", result.to_json())
        return result

    task.workdir.mkdir(parents=True, exist_ok=True)
    harness_path = write_harness(task, harness_body)
    sby_path = write_sby_file(task, harness_path=harness_path, solver=config.solver, trace=config.trace)
    start = time.time()
    timed_out = False
    returncode: int | None
    stdout = ""
    try:
        proc = subprocess.run(
            ["sby", "-f", str(sby_path)],
            cwd=task.workdir,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=config.timeout_seconds,
            check=False,
        )
        returncode = proc.returncode
        stdout = proc.stdout
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = None
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
    runtime = round(time.time() - start, 3)
    log_path = task.workdir / "sby_stdout.log"
    log_path.write_text(stdout, encoding="utf-8")
    status = parse_sby_status(stdout, mode=task.mode, returncode=returncode, timed_out=timed_out)
    result = FormalResult(
        task_id=task.task_id,
        mode=task.mode,
        status=status,
        returncode=returncode,
        runtime_s=runtime,
        workdir=task.workdir,
        sby_file=sby_path,
        log_file=log_path,
        trace_files=collect_trace_files(task.workdir),
        assertion_ids=tuple(item.assertion_id for item in task.assertions),
        details={"timed_out": timed_out},
    )
    write_json(task.workdir / "result.json", result.to_json())
    return result
