from __future__ import annotations

import subprocess
import time
from pathlib import Path

from .formal_types import FormalConfig, FormalResult, FormalStatus, FormalTask
from .harness_builder import write_bind_checker, write_internal_injected_checker, write_sby_file, write_wrapper_artifacts
from .manifest import write_json
from .result_parser import collect_trace_files, parse_sby_status


def _generated_log_text(workdir: Path) -> str:
    chunks = []
    for path in sorted(workdir.rglob("*.log"), key=str):
        if path.name == "sby_stdout.log":
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return "\n".join(chunks)


def _compatibility_status(status: FormalStatus) -> str:
    if status == FormalStatus.PROVEN:
        return "proven"
    if status == FormalStatus.COUNTEREXAMPLE:
        return "cex"
    if status in {
        FormalStatus.BOUNDED_CLEAN,
        FormalStatus.COVER_REACHED,
        FormalStatus.COVER_UNREACHED_AT_DEPTH,
        FormalStatus.UNKNOWN,
        FormalStatus.TIMEOUT,
    }:
        return "undetermined"
    if status == FormalStatus.UNSUPPORTED:
        return "unprocessed"
    return "error"


def run_sby_task(
    task: FormalTask,
    *,
    config: FormalConfig,
    harness_body: str | None = None,
    clock: str | None = None,
    prefer_bind: bool | None = None,
) -> FormalResult:
    if any(not item.supported for item in task.assertions):
        result = FormalResult(
            task_id=task.task_id,
            mode=task.mode,
            status=FormalStatus.UNSUPPORTED,
            returncode=None,
            runtime_s=0.0,
            workdir=task.workdir,
            assertion_ids=tuple(item.assertion_id for item in task.assertions),
            details={
                "unsupported": [a.to_json() for a in task.assertions if not a.supported],
                "compatibility_status": _compatibility_status(FormalStatus.UNSUPPORTED),
            },
        )
        write_json(task.workdir / "result.json", result.to_json())
        return result

    task.workdir.mkdir(parents=True, exist_ok=True)
    requested_bind = config.prefer_bind if prefer_bind is None else prefer_bind
    try:
        if task.signal_scope == "internal":
            if not clock:
                raise ValueError("clock is required for internal-signal assertions")
            artifacts = write_internal_injected_checker(task, clock=clock)
        elif harness_body is None and requested_bind and clock:
            artifacts = write_bind_checker(task, clock=clock)
        elif harness_body is not None:
            artifacts = write_wrapper_artifacts(task, harness_body)
        else:
            raise ValueError("wrapper harness body is required when bind mode is unavailable")
        sby_path = write_sby_file(
            task,
            artifacts=artifacts,
            solver=config.solver,
            trace=config.trace,
            frontend=config.frontend,
        )
    except Exception as exc:  # noqa: BLE001 - artifact generation must be reported as backend result
        result = FormalResult(
            task_id=task.task_id,
            mode=task.mode,
            status=FormalStatus.ELABORATION_ERROR,
            returncode=None,
            runtime_s=0.0,
            workdir=task.workdir,
            assertion_ids=tuple(item.assertion_id for item in task.assertions),
            details={
                "artifact_generation_error": str(exc),
                "signal_scope": task.signal_scope,
                "referenced_internal_signals": list(task.referenced_internal_signals),
                "compatibility_status": _compatibility_status(FormalStatus.ELABORATION_ERROR),
            },
        )
        write_json(task.workdir / "result.json", result.to_json())
        return result
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
    except FileNotFoundError as exc:
        runtime = round(time.time() - start, 3)
        log_path = task.workdir / "sby_stdout.log"
        stdout = f"infrastructure error: {exc}\n"
        log_path.write_text(stdout, encoding="utf-8")
        result = FormalResult(
            task_id=task.task_id,
            mode=task.mode,
            status=FormalStatus.INFRASTRUCTURE_ERROR,
            returncode=None,
            runtime_s=runtime,
            workdir=task.workdir,
            sby_file=sby_path,
            log_file=log_path,
            assertion_ids=tuple(item.assertion_id for item in task.assertions),
            details={
                "timed_out": False,
                "artifact_strategy": artifacts.strategy,
                "signal_scope": task.signal_scope,
                "referenced_internal_signals": list(task.referenced_internal_signals),
                "generated_files": [str(path) for path in artifacts.generated_files],
                "compatibility_status": _compatibility_status(FormalStatus.INFRASTRUCTURE_ERROR),
            },
        )
        write_json(task.workdir / "result.json", result.to_json())
        return result
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = None
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
    runtime = round(time.time() - start, 3)
    log_path = task.workdir / "sby_stdout.log"
    log_path.write_text(stdout, encoding="utf-8")
    status = parse_sby_status(stdout, mode=task.mode, returncode=returncode, timed_out=timed_out)
    artifact_details = artifacts.details or {}
    bind_checker_removed = False
    checker_module = artifact_details.get("checker_module")
    if artifacts.strategy == "bind" and checker_module:
        backend_logs = _generated_log_text(task.workdir)
        bind_checker_removed = f"Removing unused module `\\{checker_module}'" in (stdout + "\n" + backend_logs)
        if bind_checker_removed:
            status = FormalStatus.ELABORATION_ERROR
    traces = collect_trace_files(task.workdir)
    result = FormalResult(
        task_id=task.task_id,
        mode=task.mode,
        status=status,
        returncode=returncode,
        runtime_s=runtime,
        workdir=task.workdir,
        sby_file=sby_path,
        log_file=log_path,
        trace_files=traces,
        assertion_ids=tuple(item.assertion_id for item in task.assertions),
        details={
            "timed_out": timed_out,
            "artifact_strategy": artifacts.strategy,
            "generated_files": [str(path) for path in artifacts.generated_files],
            "bind_file": str(artifacts.bind_file) if artifacts.bind_file else None,
            "checker_file": str(artifacts.checker_file) if artifacts.checker_file else None,
            "blackbox_stub_file": str(artifacts.blackbox_stub_file) if artifacts.blackbox_stub_file else None,
            "trace_count": len(traces),
            "signal_scope": task.signal_scope,
            "referenced_internal_signals": list(task.referenced_internal_signals),
            "compatibility_status": _compatibility_status(status),
            "bind_checker_removed": bind_checker_removed,
            **artifact_details,
        },
    )
    write_json(task.workdir / "result.json", result.to_json())
    return result
