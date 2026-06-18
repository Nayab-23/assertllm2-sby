from __future__ import annotations

from pathlib import Path

from assertllm2_sby.formal_types import SourcePlan
from assertllm2_sby.real_design import parse_ports
from assertllm2_sby.source_plan import parse_ports as parse_source_plan_ports


def test_parse_ports_uses_top_module_declarations_only(tmp_path: Path):
    top = tmp_path / "top.v"
    child = tmp_path / "child.v"
    top.write_text(
        "module top(input clk, output [15:0] int_address);\n"
        "  child u_child(.int_address(int_address));\n"
        "endmodule\n",
        encoding="utf-8",
    )
    child.write_text(
        "module child(output [AW-1:0] int_address);\n"
        "  parameter AW = 8;\n"
        "endmodule\n",
        encoding="utf-8",
    )

    plan = SourcePlan(name="top", top_module="top", rtl_files=(top, child))
    ports = {port.name: port for port in parse_ports(plan)}
    harness_ports = {port.name: port for port in parse_source_plan_ports(plan)}

    assert ports["int_address"].width == "[15:0]"
    assert harness_ports["int_address"].width == "[15:0]"
