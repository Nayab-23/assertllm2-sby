from __future__ import annotations

import json
from pathlib import Path

import pytest

from assertllm2_sby.assertion_parser import extract_assertions
from assertllm2_sby.formal_types import LoweredAssertion, SourcePlan
from assertllm2_sby.models import DesignRecord
from assertllm2_sby.real_design import build_property_harness, mutant_source_plan, parse_ports, run_design
from assertllm2_sby.runtime_config import generator_defaults


def make_design(tmp_path: Path) -> DesignRecord:
    d = tmp_path / "design"
    m = d / "mutations" / "mutants" / "M_0000"
    m.mkdir(parents=True)
    rtl = d / "tiny.v"
    rtl.write_text(
        "module tiny(input clk, input req, output ack);\n"
        "  assign ack = req;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    mutant = m / "tiny.v"
    mutant.write_text(
        "module tiny(input clk, input req, output ack);\n"
        "  assign ack = ~req;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    spec = d / "spec.md"
    spec.write_text("# Tiny\nWhen req is high, ack is high in the same cycle.\n", encoding="utf-8")
    return DesignRecord(
        key="assertllm2/test/tiny",
        category="TEST",
        design_name="tiny",
        design_dir=d,
        spec_md=spec,
        raw_specs=(),
        rtl_files=(rtl,),
        include_dirs=(),
        support_files=(),
        mutation_files=(mutant,),
        top_module="tiny",
        clocks=("clk",),
        reset=None,
        source_language="verilog",
        identity={},
    )


def fake_transport(system: str, user: str, config: dict):
    assert "module tiny" not in user
    return {
        "provider": "fake",
        "model": "fake-model",
        "temperature": 0.0,
        "max_tokens": 1000,
        "raw_http_body": "{}",
        "text": json.dumps({
            "assertions": [
                {
                    "label": "req_ack_same_cycle",
                    "sva": "req_ack_same_cycle: assert property (@(posedge clk) req |-> ack);",
                    "citation": "spec.md",
                }
            ]
        }),
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def test_parse_ports_ansi_header(tmp_path: Path):
    design = make_design(tmp_path)
    ports = parse_ports(SourcePlan("tiny", "tiny", design.rtl_files))
    assert [(p.name, p.direction) for p in ports] == [("clk", "input"), ("req", "input"), ("ack", "output")]


def test_parse_ports_non_ansi_ignores_comment_words(tmp_path: Path):
    rtl = tmp_path / "legacy.v"
    rtl.write_text(
        "module legacy(clk, req, ack, data);\n"
        "// input interface\n"
        "input clk;\n"
        "input req; // input valid flag\n"
        "output ack;\n"
        "// output data path\n"
        "output [7:0] data;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    ports = parse_ports(SourcePlan("legacy", "legacy", (rtl,)))
    assert [(p.name, p.direction, p.width) for p in ports] == [
        ("clk", "input", ""),
        ("req", "input", ""),
        ("ack", "output", ""),
        ("data", "output", "[7:0]"),
    ]


def test_parse_ports_parameterized_ansi_header(tmp_path: Path):
    rtl = tmp_path / "param_top.v"
    rtl.write_text(
        "module param_top #(parameter WIDTH = 8,\n"
        "                   parameter RESET_VALUE = 1'b0)\n"
        "  (\n"
        "  output [WIDTH-1:0] data_o,\n"
        "  input clk_i,\n"
        "  input [WIDTH-1:0] data_i\n"
        "  );\n"
        "endmodule\n",
        encoding="utf-8",
    )
    ports = parse_ports(SourcePlan("param_top", "param_top", (rtl,)))
    assert [(p.name, p.direction, p.width) for p in ports] == [
        ("data_o", "output", "[WIDTH-1:0]"),
        ("clk_i", "input", ""),
        ("data_i", "input", "[WIDTH-1:0]"),
    ]
    lowered = LoweredAssertion("a", "assert", "assert(1'b1);", "assert(1'b1);", True)
    harness = build_property_harness(SourcePlan("param_top", "param_top", (rtl,)), lowered, "clk_i")
    assert "localparam WIDTH = 8;" in harness
    assert "wire [WIDTH-1:0] data_o;" in harness
    assert "(* anyseq *) reg [WIDTH-1:0] data_i;" in harness


def test_extract_property_definition_from_partial_fenced_json():
    raw = '```json\n{"assertions":[{"sva":"property p_req;\\n @(posedge clk) req |-> ack;\\nendproperty\\nassert property (p_req);"}'
    items = extract_assertions(raw)
    assert len(items) == 1
    assert items[0].label == "p_req"
    assert "@(posedge clk)" in items[0].text
    assert "req |-> ack" in items[0].text


def test_mutant_source_replacement_does_not_compile_golden_and_mutant(tmp_path: Path):
    design = make_design(tmp_path)
    golden = SourcePlan("tiny", "tiny", design.rtl_files)
    mutant = mutant_source_plan(golden, design.mutation_files[0].parent)
    assert mutant.rtl_files != golden.rtl_files
    assert len(mutant.rtl_files) == 1
    assert mutant.rtl_files[0].name == "tiny.v"
    assert mutant.rtl_files[0].parent.name == "M_0000"


def test_run_design_artifacts_stage_order_and_mutant_kill(tmp_path: Path):
    design = make_design(tmp_path)
    result = run_design(design, output_root=tmp_path / "results", transport=fake_transport)
    out = Path(result["result_path"])
    summary = json.loads((out / "summary.json").read_text())
    manifest = json.loads((out / "manifest.json").read_text())
    model_config = json.loads((out / "model_configuration.json").read_text())
    assert summary["stages"] == [
        "dataset_lookup",
        "create_isolated_workspace",
        "anthropic_generation",
        "source_planning",
        "yosys_parse_elaboration",
        "golden_sby_evaluation",
        "cached_mutant_evaluation",
    ]
    assert (out / "manifest.json").exists()
    assert (out / "generation_artifacts" / "raw_model_response.json").exists()
    assert summary["generation"]["supported_count"] == 1
    assert summary["golden_outcomes"]["GOLDEN_BOUNDED_CLEAN"] == 1
    assert summary["mutant_outcomes"]["BOUNDED_ONLY_KILLED"] == 1
    assert summary["model_configuration"]["model"] == "fake-model"
    assert model_config["max_tokens"] == generator_defaults()["max_tokens"]
    assert manifest["model_configuration"]["api_key_value_logged"] is False


def test_adapter_default_max_tokens_is_4096():
    assert generator_defaults()["max_tokens"] == 4096


def test_run_design_no_assertion_output_is_reported(tmp_path: Path):
    design = make_design(tmp_path)

    def no_assertions(system: str, user: str, config: dict):
        return {
            "provider": "fake",
            "model": "fake-model",
            "temperature": 0.0,
            "max_tokens": 1000,
            "raw_http_body": "{}",
            "text": '{"assertions":[]}',
            "usage": {},
        }

    result = run_design(design, output_root=tmp_path / "results", transport=no_assertions)
    summary = result["summary"]
    assert summary["generation"]["extracted_count"] == 1
    assert summary["generation"]["unsupported_count"] == 1
    assert summary["mutant_outcomes"]["NOT_RUN"] == 1


def test_run_design_failure_propagates_generation_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    design = make_design(tmp_path)
    monkeypatch.delenv("SABLE_ENABLE_CLOUD_LLM", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = run_design(design, output_root=tmp_path / "results")
    assert result["status"] == "PARTIAL"
    assert result["summary"]["generation"]["succeeded"] is False
    assert "generation failed" in "\n".join(result["summary"]["failures"])
