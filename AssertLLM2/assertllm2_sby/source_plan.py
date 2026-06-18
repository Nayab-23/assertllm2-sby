from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .formal_types import SourcePlan
from .models import DesignRecord, ValidationError


@dataclass(frozen=True)
class Port:
    name: str
    direction: str
    width: str


@dataclass(frozen=True)
class Parameter:
    name: str
    value: str


def build_source_plan(design: DesignRecord, *, name: str | None = None) -> SourcePlan:
    if not design.top_module:
        raise ValidationError(f"design has no top module: {design.key}")
    if not design.rtl_files:
        raise ValidationError(f"design has no RTL files: {design.key}")
    return SourcePlan(
        name=name or design.key.replace("/", "__"),
        top_module=design.top_module,
        rtl_files=tuple(path.resolve() for path in design.rtl_files),
        include_dirs=tuple(path.resolve() for path in design.include_dirs),
        defines=design.defines,
        parameters=design.parameters,
        blackbox_modules=design.blackbox_modules,
    )


def _clean_verilog_text(plan: SourcePlan) -> str:
    text = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in plan.rtl_files)
    decl_text = re.sub(r"//.*", "", text)
    return re.sub(r"/\*.*?\*/", "", decl_text, flags=re.DOTALL)


def _top_module_header(plan: SourcePlan) -> tuple[str, str]:
    decl_text = _clean_verilog_text(plan)
    module = re.search(
        rf"\bmodule\s+{re.escape(plan.top_module)}\s*(?:#\s*\((?P<params>.*?)\)\s*)?\((?P<ports>.*?)\)\s*;",
        decl_text,
        re.DOTALL,
    )
    if not module:
        raise ValidationError(f"could not parse top module ports: {plan.top_module}")
    return module.group("params") or "", module.group("ports")


def _top_module_text(plan: SourcePlan) -> str:
    decl_text = _clean_verilog_text(plan)
    module = re.search(
        rf"\bmodule\s+{re.escape(plan.top_module)}\b(?P<body>.*?)\bendmodule\b",
        decl_text,
        re.DOTALL,
    )
    if not module:
        raise ValidationError(f"could not parse top module body: {plan.top_module}")
    return module.group(0)


def parse_parameters(plan: SourcePlan) -> tuple[Parameter, ...]:
    params, _ = _top_module_header(plan)
    out: list[Parameter] = []
    param_re = re.compile(
        r"\bparameter\s+(?:\w+\s+)?(?:\[[^\]]+\]\s+)?(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<value>[^,]+)"
    )
    for match in param_re.finditer(params):
        configured = plan.parameters.get(match.group("name"), match.group("value").strip())
        out.append(Parameter(name=match.group("name"), value=str(configured).strip()))
    for name, value in plan.parameters.items():
        if all(item.name != name for item in out):
            out.append(Parameter(name=str(name), value=str(value).strip()))
    return tuple(out)


def parse_ports(plan: SourcePlan) -> tuple[Port, ...]:
    module_text = _top_module_text(plan)
    _, port_text = _top_module_header(plan)
    port_names = []
    for raw_port in port_text.replace("\n", " ").split(","):
        item = raw_port.strip().lstrip(".").split("(")[0].strip()
        item = re.sub(r"\[[^\]]+\]", " ", item)
        item = item.replace("reg ", " ").replace("wire ", " ").replace("logic ", " ")
        parts = item.split()
        name = parts[-1] if parts else ""
        if re.match(r"^[A-Za-z_]\w*$", name):
            port_names.append(name)
    ports: dict[str, Port] = {}

    decl_re = re.compile(r"\b(input|output|inout)\s+(?:reg\s+|wire\s+|logic\s+)?(\[[^;\]]+:[^;\]]+\]\s+)?([^;]+);")
    for match in decl_re.finditer(module_text):
        direction = match.group(1)
        width = (match.group(2) or "").strip()
        for raw in match.group(3).split(","):
            name = raw.strip().split("=")[0].strip()
            name = re.sub(r"\s+", " ", name).split(" ")[-1]
            if name in port_names:
                ports[name] = Port(name=name, direction=direction, width=width)

    ansi_re = re.compile(r"\b(input|output|inout)\s+(?:reg\s+|wire\s+|logic\s+)?(\[[^\]]+\]\s+)?([A-Za-z_]\w*)")
    for match in ansi_re.finditer(port_text):
        name = match.group(3)
        if name in port_names:
            ports[name] = Port(name=name, direction=match.group(1), width=(match.group(2) or "").strip())

    missing = [name for name in port_names if name not in ports]
    if missing:
        raise ValidationError(f"could not parse directions for top ports: {', '.join(missing)}")
    return tuple(ports[name] for name in port_names)


def defined_modules(plan: SourcePlan) -> set[str]:
    modules: set[str] = set()
    module_re = re.compile(r"\bmodule\s+([A-Za-z_]\w*)\b")
    for path in plan.rtl_files:
        if path.suffix.lower() not in {".v", ".sv", ".vh", ".svh"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        modules.update(module_re.findall(text))
    return modules


def _split_instance_ports(port_text: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    for idx, char in enumerate(port_text):
        if char in "([{":
            depth += 1
        elif char in ")]}" and depth:
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(port_text[start:idx].strip())
            start = idx + 1
    tail = port_text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def inferred_blackbox_ports(plan: SourcePlan, module_name: str) -> tuple[str, ...]:
    text = _clean_verilog_text(plan)
    ports: list[str] = []
    inst_re = re.compile(
        rf"\b{re.escape(module_name)}\s*(?:#\s*\(.*?\)\s*)?(?P<inst>[A-Za-z_]\w*)\s*\((?P<ports>.*?)\)\s*;",
        re.DOTALL,
    )
    for match in inst_re.finditer(text):
        raw_ports = _split_instance_ports(match.group("ports"))
        named = [re.match(r"\.\s*([A-Za-z_]\w*)\s*\(", item) for item in raw_ports]
        if any(named):
            for named_match in named:
                if named_match and named_match.group(1) not in ports:
                    ports.append(named_match.group(1))
        else:
            for idx in range(len(raw_ports)):
                name = f"bb_port_{idx}"
                if name not in ports:
                    ports.append(name)
    return tuple(ports)


def blackbox_stub_text(plan: SourcePlan) -> str:
    missing = [name for name in plan.blackbox_modules if name not in defined_modules(plan)]
    if not missing:
        return ""
    blocks = [
        "// Generated by AssertLLM2-SBY for modules marked as JasperGold blackboxes.",
        "// Port directions and widths are intentionally conservative.",
        "",
    ]
    for module_name in missing:
        ports = inferred_blackbox_ports(plan, module_name)
        port_list = ", ".join(ports)
        blocks.append(f"(* blackbox *) module {module_name}({port_list});")
        for port in ports:
            blocks.append(f"  inout {port};")
        blocks.append("endmodule")
        blocks.append("")
    return "\n".join(blocks).rstrip() + "\n"


def write_blackbox_stubs(plan: SourcePlan, workdir: Path) -> Path | None:
    text = blackbox_stub_text(plan)
    if not text:
        return None
    path = workdir / "generated_blackboxes.sv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def source_plan_artifact(plan: SourcePlan, *, generated_files: tuple[Path, ...] = ()) -> dict[str, Any]:
    payload = plan.to_json()
    payload.update({
        "rtl_file_order_preserved": True,
        "generated_files": [str(path) for path in generated_files],
        "blackbox_modules_defined_in_rtl": sorted(defined_modules(plan) & set(plan.blackbox_modules)),
        "blackbox_modules_stubbed": sorted(set(plan.blackbox_modules) - defined_modules(plan)),
    })
    return payload


def materialize_source_plan(plan: SourcePlan, workdir: Path) -> SourcePlan:
    """Copy RTL/include files into an isolated workdir without modifying originals."""
    src_dir = workdir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    seen_names: set[str] = set()
    for idx, rtl in enumerate(plan.rtl_files):
        name = rtl.name
        if name in seen_names:
            name = f"{idx:04d}_{rtl.name}"
        seen_names.add(name)
        dst = src_dir / name
        shutil.copy2(rtl, dst)
        copied.append(dst.resolve())
    return SourcePlan(
        name=plan.name,
        top_module=plan.top_module,
        rtl_files=tuple(copied),
        include_dirs=(src_dir.resolve(),),
        defines=plan.defines,
        parameters=plan.parameters,
        blackbox_modules=plan.blackbox_modules,
    )
