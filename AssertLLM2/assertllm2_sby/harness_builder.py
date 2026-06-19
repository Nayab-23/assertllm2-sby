from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .formal_types import FormalTask, SourcePlan
from .source_plan import parse_parameters, parse_ports, write_blackbox_stubs


@dataclass(frozen=True)
class FormalArtifacts:
    strategy: str
    primary_file: Path
    top_module: str
    source_plan: SourcePlan | None = None
    blackbox_stub_file: Path | None = None
    bind_file: Path | None = None
    checker_file: Path | None = None
    generated_files: tuple[Path, ...] = ()
    details: dict[str, object] | None = None


def _shell_join(items: list[str]) -> str:
    return " ".join(items)


def _read_verilog_args(plan: SourcePlan, *, extra_files: tuple[Path, ...] = (), frontend: str = "yosys") -> str:
    if frontend not in {"yosys", "slang"}:
        raise ValueError(f"unsupported formal frontend: {frontend}")
    command = "read_slang --formal" if frontend == "slang" else "read_verilog -formal -sv"
    parts = [command]
    for incdir in plan.include_dirs:
        parts.append(f"-I{incdir}")
    for define in plan.defines:
        parts.append(f"-D{define}")
    parts.extend(str(path) for path in plan.rtl_files)
    parts.extend(str(path) for path in extra_files)
    return _shell_join(parts)


def _parameter_commands(plan: SourcePlan, top_module: str) -> list[str]:
    commands = []
    for name, value in plan.parameters.items():
        commands.append(f"chparam -set {name} {value} {top_module}")
    return commands


def yosys_script_lines(
    plan: SourcePlan,
    *,
    extra_files: tuple[Path, ...] = (),
    top_module: str,
    frontend: str = "yosys",
) -> list[str]:
    lines = [_read_verilog_args(plan, extra_files=extra_files, frontend=frontend)]
    lines.extend(_parameter_commands(plan, top_module))
    lines.append(f"prep -top {top_module}")
    return lines


def _sanitize_identifier(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_$]", "_", value)
    if not clean or clean[0].isdigit():
        clean = f"sby_{clean}"
    return clean


def write_harness(task: FormalTask, harness_body: str) -> Path:
    path = task.workdir / "harness.sv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(harness_body.rstrip() + "\n", encoding="utf-8")
    return path


def _wrapper_harness_text(plan: SourcePlan, *, clock: str) -> str:
    ports = parse_ports(plan)
    parameters = parse_parameters(plan)
    lines = [f"module sby_harness(input wire {clock});"]
    for parameter in parameters:
        lines.append(f"  localparam {parameter.name} = {parameter.value};")
    if parameters:
        lines.append("")
    for port in ports:
        if port.name == clock:
            continue
        width = f" {port.width}" if port.width else ""
        if port.direction == "input":
            lines.append(f"  (* anyseq *) reg{width} {port.name};")
        elif port.direction == "inout":
            lines.append(f"  tri{width} {port.name};")
        else:
            lines.append(f"  wire{width} {port.name};")
    lines.append("")
    lines.append(f"  {plan.top_module} dut (")
    lines.append(",\n".join(f"    .{port.name}({port.name})" for port in ports))
    lines.append("  );")
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


def _find_top_module_file(plan: SourcePlan) -> Path:
    pattern = re.compile(rf"\bmodule\s+{re.escape(plan.top_module)}\b")
    for path in plan.rtl_files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if pattern.search(text):
            return path
    raise ValueError(f"could not locate top module source file: {plan.top_module}")


def _inject_assertions_into_module(source: str, module_name: str, block: str) -> str:
    module_match = re.search(rf"\bmodule\s+{re.escape(module_name)}\b", source)
    if not module_match:
        raise ValueError(f"top module declaration not found: {module_name}")
    end_match = re.search(r"\bendmodule\b", source[module_match.end():])
    if not end_match:
        raise ValueError(f"endmodule not found for top module: {module_name}")
    insert_at = module_match.end() + end_match.start()
    return source[:insert_at] + block + "\n" + source[insert_at:]


def write_bind_checker(task: FormalTask, *, clock: str) -> FormalArtifacts:
    ports = parse_ports(task.source_plan)
    parameters = parse_parameters(task.source_plan)
    checker_name = _sanitize_identifier(f"{task.task_id}_checker")
    checker_path = task.workdir / "checker.sv"
    bind_path = task.workdir / f"{task.source_plan.top_module}_bind.sv"
    checker_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [f"module {checker_name} #("]
    if parameters:
        lines.append(",\n".join(f"  parameter {p.name} = {p.value}" for p in parameters))
        lines.append(") (")
    else:
        lines = [f"module {checker_name} ("]
    port_lines = []
    for port in ports:
        width = f" {port.width}" if port.width else ""
        port_lines.append(f"  input wire{width} {port.name}")
    lines.append(",\n".join(port_lines))
    lines.append(");")
    lines.append("  reg past_valid = 1'b0;")
    lines.append(f"  always @(posedge {clock}) begin")
    lines.append("    past_valid <= 1'b1;")
    for assertion in task.assertions:
        body = assertion.lowered_text.rstrip().rstrip(";") + ";"
        if "$past" in body and "past_valid" not in body:
            body = f"if (past_valid) begin\n      {body}\n    end"
        lines.append(f"    // {assertion.assertion_id}")
        lines.extend("    " + line if line.strip() else "" for line in body.splitlines())
    lines.append("  end")
    lines.append("endmodule")
    checker_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    param_bind = ""
    if parameters:
        param_bind = " #(" + ", ".join(f".{p.name}({p.name})" for p in parameters) + ")"
    bind_path.write_text(
        f"bind {task.source_plan.top_module} {checker_name}{param_bind} "
        f"{checker_name}_inst (.*);\n",
        encoding="utf-8",
    )

    blackbox_stub = write_blackbox_stubs(task.source_plan, task.workdir)
    generated = tuple(path for path in (checker_path, bind_path, blackbox_stub) if path is not None)
    return FormalArtifacts(
        strategy="bind",
        primary_file=bind_path,
        top_module=task.source_plan.top_module,
        blackbox_stub_file=blackbox_stub,
        bind_file=bind_path,
        checker_file=checker_path,
        generated_files=generated,
        details={"assertion_count": len(task.assertions), "clock": clock, "checker_module": checker_name},
    )


def write_internal_injected_checker(task: FormalTask, *, clock: str) -> FormalArtifacts:
    top_rtl = _find_top_module_file(task.source_plan)
    source_text = top_rtl.read_text(encoding="utf-8", errors="ignore")

    assertion_lines = [
        "",
        "// Internal-signal formal assertions injected by assertllm2-sby.",
        "reg _sby_past_valid = 1'b0;",
        f"always @(posedge {clock}) begin",
        "  _sby_past_valid <= 1'b1;",
    ]
    for assertion in task.assertions:
        body = assertion.lowered_text.rstrip().rstrip(";") + ";"
        body = re.sub(r"\bpast_valid\b", "_sby_past_valid", body)
        if "$past" in body and "_sby_past_valid" not in body:
            body = f"if (_sby_past_valid) begin\n  {body}\nend"
        assertion_lines.append(f"  // {assertion.assertion_id}")
        assertion_lines.extend("  " + line if line.strip() else "" for line in body.splitlines())
    assertion_lines.append("end")

    injected_text = _inject_assertions_into_module(
        source_text,
        task.source_plan.top_module,
        "\n".join(assertion_lines),
    )
    injected_top = task.workdir / top_rtl.name
    injected_top.parent.mkdir(parents=True, exist_ok=True)
    injected_top.write_text(injected_text, encoding="utf-8")

    replaced_rtl = tuple(injected_top if path == top_rtl else path for path in task.source_plan.rtl_files)
    injected_plan = SourcePlan(
        name=task.source_plan.name,
        top_module=task.source_plan.top_module,
        rtl_files=replaced_rtl,
        include_dirs=task.source_plan.include_dirs,
        defines=task.source_plan.defines,
        parameters=task.source_plan.parameters,
        blackbox_modules=task.source_plan.blackbox_modules,
    )
    blackbox_stub = write_blackbox_stubs(injected_plan, task.workdir)
    generated = tuple(path for path in (blackbox_stub,) if path is not None)
    return FormalArtifacts(
        strategy="internal_injected",
        primary_file=injected_top,
        top_module=injected_plan.top_module,
        source_plan=injected_plan,
        blackbox_stub_file=blackbox_stub,
        generated_files=generated,
        details={
            "assertion_count": len(task.assertions),
            "clock": clock,
            "internal_signals": list(task.referenced_internal_signals),
            "injected_top_file": str(injected_top),
        },
    )


def write_wrapper_artifacts(task: FormalTask, harness_body: str) -> FormalArtifacts:
    harness_path = write_harness(task, harness_body)
    blackbox_stub = write_blackbox_stubs(task.source_plan, task.workdir)
    generated = tuple(path for path in (harness_path, blackbox_stub) if path is not None)
    return FormalArtifacts(
        strategy="wrapper",
        primary_file=harness_path,
        top_module=task.top_module,
        blackbox_stub_file=blackbox_stub,
        generated_files=generated,
        details={"assertion_count": len(task.assertions), "bind_emulation": True},
    )


def write_sby_file(
    task: FormalTask,
    *,
    artifacts: FormalArtifacts,
    solver: str,
    trace: bool,
    frontend: str = "yosys",
) -> Path:
    task.workdir.mkdir(parents=True, exist_ok=True)
    plan = artifacts.source_plan or task.source_plan
    trace_opts = ""
    if trace:
        trace_opts = "append 0\n"
    extra_files = artifacts.generated_files
    sby = f"""[options]
mode {task.mode}
depth {task.depth}
{trace_opts}
[engines]
smtbmc {solver}

[script]
{chr(10).join(yosys_script_lines(plan, extra_files=extra_files, top_module=artifacts.top_module, frontend=frontend))}

[files]
"""
    for rtl in plan.rtl_files:
        sby += f"{rtl}\n"
    for extra in extra_files:
        sby += f"{extra}\n"
    path = task.workdir / f"{task.task_id}.sby"
    path.write_text(sby, encoding="utf-8")
    return path
