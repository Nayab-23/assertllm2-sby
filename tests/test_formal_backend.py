from __future__ import annotations

from pathlib import Path

from assertllm2_sby.assertion_lowering import classify_and_lower_assertion
from assertllm2_sby.formal_types import FormalConfig, FormalResult, FormalStatus, FormalTask, SourcePlan
from assertllm2_sby.harness_builder import write_bind_checker, write_sby_file
from assertllm2_sby.sby_backend import run_sby_task
from assertllm2_sby.mutation_runner import classify_mutant
from assertllm2_sby.result_parser import parse_sby_status
from assertllm2_sby.source_plan import blackbox_stub_text, source_plan_artifact


def result(task_id: str, status: FormalStatus) -> FormalResult:
    return FormalResult(
        task_id=task_id,
        mode="bmc",
        status=status,
        returncode=0,
        runtime_s=0.0,
        workdir=Path("."),
    )


def test_parse_sby_status_distinguishes_bounded_and_proven():
    assert parse_sby_status("Status: passed", mode="bmc", returncode=0, timed_out=False) == FormalStatus.BOUNDED_CLEAN
    assert parse_sby_status("Status: passed", mode="prove", returncode=0, timed_out=False) == FormalStatus.PROVEN


def test_parse_sby_status_cover_and_failures():
    assert parse_sby_status("Status: passed", mode="cover", returncode=0, timed_out=False) == FormalStatus.COVER_REACHED
    assert parse_sby_status("Status: failed", mode="cover", returncode=2, timed_out=False) == FormalStatus.COVER_UNREACHED_AT_DEPTH
    assert parse_sby_status("Status: failed", mode="bmc", returncode=1, timed_out=False) == FormalStatus.COUNTEREXAMPLE
    assert parse_sby_status("", mode="bmc", returncode=None, timed_out=True) == FormalStatus.TIMEOUT


def test_parse_sby_status_separates_backend_failures():
    assert (
        parse_sby_status("ERROR: Module `vendor_ip' referenced in module `top' is not part of the design.", mode="bmc", returncode=1, timed_out=False)
        == FormalStatus.ELABORATION_ERROR
    )
    assert (
        parse_sby_status("infrastructure error: [Errno 2] No such file or directory: 'sby'", mode="bmc", returncode=None, timed_out=False)
        == FormalStatus.INFRASTRUCTURE_ERROR
    )


def test_mutant_kill_requires_golden_acceptance_and_mutant_counterexample():
    killed = classify_mutant(
        mutant_id="m",
        golden_result=result("golden", FormalStatus.PROVEN),
        mutant_result=result("mutant", FormalStatus.COUNTEREXAMPLE),
        responsible_assertion="a",
    )
    assert killed.killed is True
    assert killed.responsible_assertion == "a"

    not_killed = classify_mutant(
        mutant_id="m",
        golden_result=result("golden", FormalStatus.ERROR),
        mutant_result=result("mutant", FormalStatus.COUNTEREXAMPLE),
        responsible_assertion="a",
    )
    assert not_killed.killed is False
    assert not_killed.responsible_assertion is None


def test_phase4_blackbox_stub_and_source_plan_artifact(tmp_path: Path):
    rtl = tmp_path / "top.sv"
    rtl.write_text(
        "module top(input clk, input a, output y);\n"
        "  vendor_ip u_ip(.clk(clk), .a(a), .y(y));\n"
        "endmodule\n",
        encoding="utf-8",
    )
    plan = SourcePlan("top", "top", (rtl,), blackbox_modules=("vendor_ip",))
    stub = blackbox_stub_text(plan)
    assert "module vendor_ip(clk, a, y);" in stub
    assert "inout clk;" in stub
    artifact = source_plan_artifact(plan)
    assert artifact["rtl_file_order_preserved"] is True
    assert artifact["blackbox_modules_stubbed"] == ["vendor_ip"]


def test_phase4_bind_artifacts_and_sby_script_preserve_sources(tmp_path: Path):
    rtl = tmp_path / "top.sv"
    rtl.write_text(
        "module top #(parameter WIDTH = 2)(input clk, input [WIDTH-1:0] a, output y);\n"
        "  assign y = a[0];\n"
        "endmodule\n",
        encoding="utf-8",
    )
    lowered = classify_and_lower_assertion("a_y", "a_y: assert property (@(posedge clk) a[0] |-> y);")
    assert lowered.supported
    task = FormalTask(
        task_id="bind_task",
        mode="bmc",
        depth=4,
        source_plan=SourcePlan("top", "top", (rtl,), parameters={"WIDTH": 4}),
        assertions=(lowered,),
        workdir=tmp_path / "work",
    )
    artifacts = write_bind_checker(task, clock="clk")
    sby = write_sby_file(task, artifacts=artifacts, solver="z3", trace=True)
    checker_text = artifacts.checker_file.read_text(encoding="utf-8") if artifacts.checker_file else ""
    sby_text = sby.read_text(encoding="utf-8")
    assert artifacts.strategy == "bind"
    assert artifacts.bind_file and "bind top" in artifacts.bind_file.read_text(encoding="utf-8")
    assert "parameter WIDTH = 4" in checker_text
    assert str(rtl) in sby_text
    assert str(artifacts.bind_file) in sby_text
    assert "chparam -set WIDTH 4 top" in sby_text


def test_concurrent_property_lowering_strips_trailing_semicolon():
    lowered = classify_and_lower_assertion(
        "a",
        "p: assert property (@(posedge clk) req |-> ack;);",
    )
    assert lowered.supported
    assert "ack;" not in lowered.lowered_text
    assert "assert((!(req)) || (ack));" == lowered.lowered_text


def test_fixed_delay_lowering_shapes_and_metadata():
    delay1 = classify_and_lower_assertion(
        "d1",
        "p: assert property (@(posedge clk) req |-> ##1 ack);",
    )
    assert delay1.supported
    assert delay1.transformation_rule == "fixed_delay_|->_delay_1"
    assert "past_valid" in delay1.lowered_text
    assert "$past(req, 1)" in delay1.lowered_text
    assert "assert(ack);" in delay1.lowered_text

    delay2 = classify_and_lower_assertion(
        "d2",
        "p: assert property (@(posedge clk) req |-> ##2 ack);",
    )
    assert delay2.supported
    assert delay2.transformation_rule == "fixed_delay_|->_delay_2"
    assert "$past(past_valid, 1)" in delay2.lowered_text
    assert "$past(req, 2)" in delay2.lowered_text

    nonoverlap = classify_and_lower_assertion(
        "n",
        "p: assert property (@(posedge clk) req |=> ack);",
    )
    assert nonoverlap.supported
    assert nonoverlap.transformation_rule == "fixed_delay_|=>_delay_1"
    assert "$past(req, 1)" in nonoverlap.lowered_text


def test_fixed_delay_disable_guard_covers_delay_window():
    lowered = classify_and_lower_assertion(
        "rst",
        "p: assert property (@(posedge clk) disable iff (rst) req |-> ##2 ack);",
    )
    assert lowered.supported
    assert "!(rst)" in lowered.lowered_text
    assert "!$past(rst, 1)" in lowered.lowered_text
    assert "!$past(rst, 2)" in lowered.lowered_text


def test_unsupported_variable_and_ranged_delays_stay_unsupported():
    variable = classify_and_lower_assertion(
        "v",
        "p: assert property (@(posedge clk) req |-> ##delay ack);",
    )
    assert not variable.supported
    assert "variable_delay" in variable.reasons

    ranged = classify_and_lower_assertion(
        "r",
        "p: assert property (@(posedge clk) req |-> ##[1:2] ack);",
    )
    assert not ranged.supported
    assert "ranged_delay" in ranged.reasons


def test_unsupported_edge_sample_functions_stay_unsupported():
    rose = classify_and_lower_assertion(
        "rose",
        "p: assert property (@(posedge clk) $rose(reset) |=> inReady);",
    )
    assert not rose.supported
    assert "edge_sample_function" in rose.reasons

    stable = classify_and_lower_assertion(
        "stable",
        "p: assert property (@(posedge clk) !outValid |=> $stable(outSamp));",
    )
    assert stable.supported


def test_nested_implication_and_antecedent_sequence_stay_unsupported():
    nested = classify_and_lower_assertion(
        "nested",
        "p: assert property (@(posedge clk) !rst_n |-> ##1 ((cyc && stb) |-> ack));",
    )
    assert not nested.supported
    assert "nested_implication_not_supported" in nested.reasons

    antecedent_sequence = classify_and_lower_assertion(
        "ante_seq",
        "p: assert property (@(posedge clk) (wr) ##1 (rd) |-> dat_o[4]);",
    )
    assert not antecedent_sequence.supported
    assert "sequence_operator_in_antecedent_not_supported" in antecedent_sequence.reasons

    comment = classify_and_lower_assertion(
        "comment",
        "p: assert property (@(posedge clk) disable iff (rst)\n"
        "  // generated note\n"
        "  req |=> ack;);",
    )
    assert comment.supported
    assert "//" not in comment.lowered_text


def _trace_module(*, ack_cycle: int, rst_cycle: int | None = None) -> str:
    rst_expr = "1'b0" if rst_cycle is None else f"(cycle == 4'd{rst_cycle})"
    return f"""module trace_delay(
  input wire clk,
  output wire req,
  output wire ack,
  output wire rst
);
  reg [3:0] cycle = 4'd0;
  always @(posedge clk) begin
    cycle <= cycle + 4'd1;
  end
  assign req = (cycle == 4'd1);
  assign ack = (cycle == 4'd{ack_cycle});
  assign rst = {rst_expr};
endmodule
"""


def _delay_harness(property_body: str) -> str:
    return f"""module sby_harness(input wire clk);
  wire req;
  wire ack;
  wire rst;
  trace_delay dut(.clk(clk), .req(req), .ack(ack), .rst(rst));
  reg past_valid = 1'b0;
  always @(posedge clk) begin
    past_valid <= 1'b1;
{property_body.rstrip()}
  end
endmodule
"""


def _run_delay_trace(tmp_path: Path, *, text: str, ack_cycle: int, rst_cycle: int | None = None) -> FormalStatus:
    tmp_path.mkdir(parents=True, exist_ok=True)
    rtl = tmp_path / "trace_delay.sv"
    rtl.write_text(_trace_module(ack_cycle=ack_cycle, rst_cycle=rst_cycle), encoding="utf-8")
    lowered = classify_and_lower_assertion("delay", text)
    assert lowered.supported
    task = FormalTask(
        task_id="delay_trace",
        mode="bmc",
        depth=6,
        source_plan=SourcePlan("trace_delay", "trace_delay", (rtl.resolve(),)),
        assertions=(lowered,),
        workdir=(tmp_path / "sby").resolve(),
    )
    result = run_sby_task(task, config=FormalConfig(), harness_body=_delay_harness(lowered.lowered_text))
    return result.status


def test_fixed_delay_passing_and_failing_traces(tmp_path: Path):
    prop = "p: assert property (@(posedge clk) req |-> ##2 ack);"
    assert _run_delay_trace(tmp_path / "pass", text=prop, ack_cycle=3) == FormalStatus.BOUNDED_CLEAN
    assert _run_delay_trace(tmp_path / "fail", text=prop, ack_cycle=4) == FormalStatus.COUNTEREXAMPLE


def test_nonoverlap_and_reset_abort_traces(tmp_path: Path):
    nonoverlap = "p: assert property (@(posedge clk) req |=> ack);"
    assert _run_delay_trace(tmp_path / "nonoverlap_pass", text=nonoverlap, ack_cycle=2) == FormalStatus.BOUNDED_CLEAN
    assert _run_delay_trace(tmp_path / "nonoverlap_fail", text=nonoverlap, ack_cycle=3) == FormalStatus.COUNTEREXAMPLE

    reset_abort = "p: assert property (@(posedge clk) disable iff (rst) req |-> ##2 ack);"
    assert _run_delay_trace(tmp_path / "reset_abort", text=reset_abort, ack_cycle=4, rst_cycle=2) == FormalStatus.BOUNDED_CLEAN


def test_fixed_delay_insufficient_history_and_vacuous_antecedent():
    delayed = classify_and_lower_assertion(
        "early",
        "p: assert property (@(posedge clk) req |-> ##2 ack);",
    )
    assert delayed.supported
    assert "past_valid && $past(past_valid, 1)" in delayed.lowered_text

    vacuous = classify_and_lower_assertion(
        "vac",
        "p: assert property (@(posedge clk) req |-> ##1 ack);",
    )
    assert vacuous.supported
    assert "$past(req, 1)" in vacuous.lowered_text
