from __future__ import annotations

from pathlib import Path

from .formal_types import FormalTask, SourcePlan


def _read_verilog_args(plan: SourcePlan) -> str:
    parts = ["read_verilog -formal -sv"]
    for incdir in plan.include_dirs:
        parts.append(f"-I{incdir}")
    for define in plan.defines:
        parts.append(f"-D{define}")
    parts.extend(str(path) for path in plan.rtl_files)
    return " ".join(parts)


def write_harness(task: FormalTask, harness_body: str) -> Path:
    path = task.workdir / "harness.sv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(harness_body.rstrip() + "\n", encoding="utf-8")
    return path


def write_sby_file(task: FormalTask, *, harness_path: Path, solver: str, trace: bool) -> Path:
    task.workdir.mkdir(parents=True, exist_ok=True)
    trace_opts = ""
    if trace:
        trace_opts = "append 0\n"
    sby = f"""[options]
mode {task.mode}
depth {task.depth}
{trace_opts}
[engines]
smtbmc {solver}

[script]
{_read_verilog_args(task.source_plan)}
read_verilog -formal -sv {harness_path}
prep -top {task.top_module}

[files]
{harness_path}
"""
    for rtl in task.source_plan.rtl_files:
        sby += f"{rtl}\n"
    path = task.workdir / f"{task.task_id}.sby"
    path.write_text(sby, encoding="utf-8")
    return path
