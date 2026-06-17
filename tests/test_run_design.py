from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from assertllm2_sby.assertion_parser import extract_assertions
from assertllm2_sby.compat import parse_design_fpv_report, write_compatibility_artifacts
from assertllm2_sby.coverage import checker_coi_summary
from assertllm2_sby.formal_types import LoweredAssertion, SourcePlan
from assertllm2_sby.models import DesignRecord, GenerationMode
from assertllm2_sby.mutation_runner import load_mutation_cache, mutant_source_plan as cache_mutant_source_plan
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


def write_bug_hunting_adapter(tmp_path: Path) -> str:
    module = tmp_path / "fake_bug_hunting_contract.py"
    module.write_text(
        "def infer(request, output_dir):\n"
        "    assert request['mode'] == 'bug-hunting'\n"
        "    assert request['buggy_rtl_files']\n"
        "    assert request['clean_rtl_visible_to_generator'] is False\n"
        "    assert request['rtl_files'] == request['buggy_rtl_files']\n"
        "    return {\n"
        "        'generator_version': 'bug-hunting-test',\n"
        "        'assertions': [{\n"
        "            'label': 'req_ack_same_cycle',\n"
        "            'sva': 'req_ack_same_cycle: assert property (@(posedge clk) req |-> ack);',\n"
        "        }],\n"
        "    }\n",
        encoding="utf-8",
    )
    if str(tmp_path) not in sys.path:
        sys.path.insert(0, str(tmp_path))
    return "fake_bug_hunting_contract:infer"


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


def test_static_checker_coi_follows_simple_assign(tmp_path: Path):
    rtl = tmp_path / "tiny.v"
    rtl.write_text(
        "module tiny(input clk, input req, output ack);\n"
        "  assign ack = req;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    plan = SourcePlan("tiny", "tiny", (rtl,))
    summary = checker_coi_summary(plan, [{
        "assertion_id": "a_req_ack",
        "status": "undetermined",
        "original_text": "a_req_ack: assert property (@(posedge clk) ack);",
    }])

    assert summary["is_jaspergold_coverage"] is False
    assert summary["covered"] == 3
    assert summary["total"] == 3
    row = summary["assertions"][0]
    assert "ack" in row["seed_signals"]
    assert "req" in row["coi_signals"]
    assert row["interface_signals"] == ["ack", "clk", "req"]


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
        "jasper_compatible_baseline_artifacts",
    ]
    assert (out / "manifest.json").exists()
    assert (out / "assertions.sv").exists()
    assert (out / "assertions_meta.json").exists()
    assert (out / "baseline_eval.json").exists()
    assert (out / "mutation_results.json").exists()
    assert (out / "metrics.json").exists()
    assert (out / "scorecard.json").exists()
    assert (out / "report" / "design.fpv.rpt").exists()
    assert (out / "report" / "formal_coverage.rpt").exists()
    assert (out / "report" / "stimuli_coverage.rpt").exists()
    assert (out / "report" / "checker_coi.rpt").exists()
    assert (out / "report" / "checker_proof.rpt").exists()
    assert (out / "generation_artifacts" / "raw_model_response.json").exists()
    assert summary["generation"]["supported_count"] == 1
    assert summary["golden_outcomes"]["GOLDEN_BOUNDED_CLEAN"] == 1
    assert summary["mutant_outcomes"]["BOUNDED_ONLY_KILLED"] == 1
    baseline = json.loads((out / "baseline_eval.json").read_text())
    assert baseline["official_jaspergold_result"] is False
    assert baseline["syntax_correctness"] == 1.0
    assert baseline["total_assertions"] == 1
    assert baseline["scoreable_assertions"] == 1
    assert baseline["fpv_rows"][0]["status"] == "undetermined"
    assert baseline["coverage"]["formal"]["is_jaspergold_coverage"] is False
    assert baseline["coverage"]["checker_coi"]["method"] == "static_textual_rtl_coi"
    assert baseline["coverage"]["checker_coi"]["is_jaspergold_coverage"] is False
    assert "req" in baseline["coverage"]["checker_coi"]["covered_interface_signals"]
    assert "ack" in baseline["coverage"]["checker_coi"]["covered_interface_signals"]
    assert baseline["coverage"]["stimuli"]["is_jaspergold_coverage"] is False
    assert "req" in baseline["coverage"]["stimuli"]["covered_input_signals"]
    assert baseline["coverage"]["checker_proof"]["unsupported_reason"]
    assert baseline["coverage_report_paths"]["checker_coi"].endswith("checker_coi.rpt")
    mutation_results = json.loads((out / "mutation_results.json").read_text())
    metrics = json.loads((out / "metrics.json").read_text())
    assert mutation_results["official_jaspergold_result"] is False
    assert mutation_results["mutation_cache"]["scoreable_mutant_count"] == 1
    assert mutation_results["mutants"][0]["status"] == "BOUNDED_ONLY_KILLED"
    assert mutation_results["mutants"][0]["killed_by"] == baseline["fpv_rows"][0]["assertion_id"]
    assert metrics["bounded_only_killed"] == 1
    assert metrics["strict_killed"] == 0
    assert metrics["bounded_inclusive_mutation_score"] == 1.0
    parsed = parse_design_fpv_report(out / "report" / "design.fpv.rpt")
    assert len(parsed) == 1
    assert parsed[0]["index"] == 1
    assert parsed[0]["qualified_name"] == baseline["fpv_rows"][0]["qualified_name"]
    assert parsed[0]["assertion_name"] == baseline["fpv_rows"][0]["assertion_name"]
    assert parsed[0]["signal_name"] == "req_ack_same_cycle"
    assert parsed[0]["status"] == "undetermined"
    assert parsed[0]["trace_path"] is None
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


def test_run_design_artifacts_do_not_leak_secret_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    secret = "sk-ant-test-secret-value-should-not-appear"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    monkeypatch.setenv("SABLE_ENABLE_CLOUD_LLM", "1")
    design = make_design(tmp_path)

    result = run_design(design, output_root=tmp_path / "results", transport=fake_transport)
    out = Path(result["result_path"])

    scanned_suffixes = {".json", ".txt", ".md", ".rpt", ".csv", ".sv", ".yaml"}
    leaks = []
    for path in out.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in scanned_suffixes:
            continue
        if secret in path.read_text(encoding="utf-8", errors="ignore"):
            leaks.append(str(path.relative_to(out)))
    assert leaks == []


def test_compat_artifacts_remove_golden_cex_from_scoreable_set(tmp_path: Path):
    source_assertions = tmp_path / "generated.sv"
    source_assertions.write_text("a1: assert property (@(posedge clk) req |-> ack);\n", encoding="utf-8")
    out = tmp_path / "compat"

    baseline = write_compatibility_artifacts(
        out,
        design_key="assertllm2/test/tiny",
        top_module="tiny",
        generation={"syntax_cleanup": {"initial_blocks": 1, "valid_blocks": 1}},
        config={"bmc_depth": 8, "prove_depth": 8, "cover_depth": 6, "timeout_seconds": 30},
        source_assertions_path=source_assertions,
        assertion_rows=[{
            "assertion_id": "a1",
            "label": "req_ack_same_cycle",
            "original_text": source_assertions.read_text(encoding="utf-8"),
            "classification": "NEEDS_FORMAL_VALIDATION",
            "lowered": {"supported": True, "kind": "assert", "reasons": []},
            "golden_outcome": "GOLDEN_COUNTEREXAMPLE",
            "golden_result": {
                "details": {"compatibility_status": "cex"},
                "trace_files": [str(tmp_path / "trace.vcd")],
            },
        }],
    )

    assert baseline["scoreable_assertions"] == 0
    assert baseline["removed_or_unsupported_assertions"][0]["reason"] == "golden_counterexample"
    meta = json.loads((out / "assertions_meta.json").read_text())
    assert meta["assertions"][0]["scoreable"] is False
    assert meta["assertions"][0]["removed_from_scoreable"] is True
    parsed = parse_design_fpv_report(out / "report" / "design.fpv.rpt")
    assert parsed[0]["status"] == "cex"
    assert parsed[0]["trace_path"].endswith("trace.vcd")


def test_mutation_cache_parses_summary_and_missing_rtl(tmp_path: Path):
    design = make_design(tmp_path)
    summary = design.design_dir / "mutations" / "mutation_summary.json"
    (design.design_dir / "mutations" / "mutants" / "M_0001").mkdir()
    summary.write_text(
        json.dumps({
            "cache_schema": "assertbench_mutations_v1",
            "mutants": [
                {
                    "mutant_id": "M_0000",
                    "applied_to_source_files": ["tiny.v"],
                    "log": "M_0000: applied",
                },
                {
                    "mutant_id": "M_0001",
                    "applied_to_source_files": ["missing.v"],
                    "log": "M_0001: applied",
                },
            ],
        }),
        encoding="utf-8",
    )

    cache = load_mutation_cache(design)

    by_id = {mutant.mutant_id: mutant for mutant in cache.mutants}
    assert by_id["M_0000"].scoreable is True
    assert by_id["M_0001"].scoreable is False
    assert by_id["M_0001"].non_scoreable_reason == "missing_mutant_rtl"
    source_plan = cache_mutant_source_plan(
        SourcePlan("tiny", "tiny", design.rtl_files),
        design,
        by_id["M_0000"],
    )
    assert source_plan.rtl_files[0].parent.name == "M_0000"


def test_run_design_bug_hunting_metrics_for_merged_buggy_rtl(tmp_path: Path):
    design = make_design(tmp_path)
    merged = design.design_dir / "buggy_artifacts" / "merged_buggy_rtl"
    merged.mkdir(parents=True)
    (merged / "tiny.v").write_text(
        "module tiny(input clk, input req, output ack);\n"
        "  assign ack = ~req;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    entrypoint = write_bug_hunting_adapter(tmp_path)

    result = run_design(
        design,
        mode=GenerationMode.BUG_HUNTING,
        output_root=tmp_path / "results",
        method="contract-inference",
        contract_config={"python_entrypoint": entrypoint},
        max_mutants=0,
    )
    out = Path(result["result_path"])
    summary = json.loads((out / "summary.json").read_text())
    metrics = json.loads((out / "bug_hunting_metrics.json").read_text())
    visibility = json.loads((out / "generation_artifacts" / "source_visibility_manifest.json").read_text())

    assert result["status"] == "COMPLETED"
    assert summary["bug_hunting"]["enabled"] is True
    assert summary["bug_hunting"]["merged_buggy_results"][0]["status"] == "BOUNDED_ONLY_KILLED"
    assert metrics["clean_design"]["cex_count"] == 0
    assert metrics["clean_design"]["clean_design_cex_ratio"] == 0.0
    assert metrics["merged_buggy_targets"]["detected_count"] == 1
    assert metrics["merged_buggy_targets"]["detection_rate"] == 1.0
    assert metrics["merged_buggy_targets"]["miss_rate"] == 0.0
    assert metrics["merged_buggy_targets"]["error_rate"] == 0.0
    assert visibility["clean_rtl_visible_to_generator"] is False
    assert [Path(row["path"]).name for row in visibility["visible_buggy_rtl_files"]] == ["tiny.v"]
