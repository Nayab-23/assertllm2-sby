from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .assertion_lowering import lower_assertions
from .formal_types import FormalConfig, FormalResult, FormalStatus, FormalTask, SourcePlan
from .manifest import env_flag, repo_state, write_json
from .mutation_runner import classify_mutant
from .reporting import write_formal_summary
from .sby_backend import run_sby_task

from .paths import PACKAGE_ROOT, resolve_assertllm2_checkout, results_root


GOLDEN_RTL = """module synthetic_counter(
  input wire clk,
  input wire rst,
  input wire en,
  output reg [3:0] count
);
  initial count = 4'd0;
  always @(posedge clk) begin
    if (rst) begin
      count <= 4'd0;
    end else if (en) begin
      count <= count + 4'd1;
    end
  end
endmodule
"""

MUTANT_RTL = """module synthetic_counter(
  input wire clk,
  input wire rst,
  input wire en,
  output reg [3:0] count
);
  initial count = 4'd0;
  always @(posedge clk) begin
    if (rst) begin
      count <= 4'd0;
    end else if (en) begin
      count <= count + 4'd2;
    end
  end
endmodule
"""


def _utc_run_id() -> str:
    return "formal_self_test_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _tool_line(cmd: list[str]) -> str:
    if not shutil.which(cmd[0]):
        return "not found"
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    return proc.stdout.splitlines()[0] if proc.stdout else f"exit {proc.returncode}"


def _write_fixture(outdir: Path) -> tuple[SourcePlan, SourcePlan]:
    fixture = outdir / "fixture"
    fixture.mkdir(parents=True, exist_ok=True)
    golden = fixture / "synthetic_counter_golden.sv"
    mutant = fixture / "synthetic_counter_mutant_faulty.sv"
    golden.write_text(GOLDEN_RTL, encoding="utf-8")
    mutant.write_text(MUTANT_RTL, encoding="utf-8")
    return (
        SourcePlan(name="synthetic_golden", top_module="synthetic_counter", rtl_files=(golden.resolve(),)),
        SourcePlan(name="synthetic_mutant", top_module="synthetic_counter", rtl_files=(mutant.resolve(),)),
    )


def _harness(property_body: str) -> str:
    return f"""module sby_harness(input wire clk);
  wire [3:0] count;
  synthetic_counter dut(
    .clk(clk),
    .rst(1'b0),
    .en(1'b1),
    .count(count)
  );
  reg past_valid = 1'b0;
  always @(posedge clk) begin
    past_valid <= 1'b1;
{property_body.rstrip()}
  end
endmodule
"""


PROPERTIES: dict[str, dict[str, Any]] = {
    "parse_elab": {
        "mode": "bmc",
        "depth_attr": "bmc_depth",
        "text": "assert(1'b1);",
        "body": "    assert(1'b1);",
        "expected": FormalStatus.BOUNDED_CLEAN,
    },
    "bmc_bounded_clean": {
        "mode": "bmc",
        "depth_attr": "bmc_depth",
        "text": "assert(1'b1);",
        "body": "    assert(1'b1);",
        "expected": FormalStatus.BOUNDED_CLEAN,
    },
    "prove_provable": {
        "mode": "prove",
        "depth_attr": "prove_depth",
        "text": "assert(1'b1);",
        "body": "    assert(1'b1);",
        "expected": FormalStatus.PROVEN,
    },
    "bmc_failing": {
        "mode": "bmc",
        "depth_attr": "bmc_depth",
        "text": "assert(count < 4'd3);",
        "body": "    assert(count < 4'd3);",
        "expected": FormalStatus.COUNTEREXAMPLE,
    },
    "cover_reached": {
        "mode": "cover",
        "depth_attr": "cover_depth",
        "text": "cover(count == 4'd3);",
        "body": "    cover(count == 4'd3);",
        "expected": FormalStatus.COVER_REACHED,
    },
    "cover_unreached": {
        "mode": "cover",
        "depth_attr": "cover_depth",
        "text": "cover(count == 4'd9);",
        "body": "    cover(count == 4'd9);",
        "expected": FormalStatus.COVER_UNREACHED_AT_DEPTH,
    },
    "past_guarded": {
        "mode": "prove",
        "depth_attr": "prove_depth",
        "text": "if (past_valid) assert(count == $past(count) + 4'd1);",
        "body": "    if (past_valid) begin\n      assert(count == $past(count) + 4'd1);\n    end",
        "expected": FormalStatus.PROVEN,
    },
}


def _make_task(
    *,
    name: str,
    spec: dict[str, Any],
    source_plan: SourcePlan,
    outdir: Path,
    config: FormalConfig,
) -> tuple[FormalTask, str]:
    assertion = lower_assertions(((name, spec["text"]),))[0]
    depth = int(getattr(config, spec["depth_attr"]))
    task = FormalTask(
        task_id=name,
        mode=spec["mode"],
        depth=depth,
        source_plan=source_plan,
        assertions=(assertion,),
        workdir=(outdir / "tasks" / name).resolve(),
    )
    return task, _harness(spec["body"])


def _run_yosys_parse(task: FormalTask, harness_body: str, outdir: Path) -> dict[str, Any]:
    harness = outdir / "parse_elab_harness.sv"
    harness.write_text(harness_body, encoding="utf-8")
    log = outdir / "yosys_parse_elab.log"
    cmd = [
        "yosys",
        "-p",
        "read_verilog -formal -sv "
        + " ".join(str(p) for p in task.source_plan.rtl_files)
        + f" {harness}; prep -top {task.top_module}",
    ]
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    log.write_text(proc.stdout, encoding="utf-8")
    return {"command": " ".join(cmd), "returncode": proc.returncode, "log": str(log), "passed": proc.returncode == 0}


def run_formal_self_test(output_root: Path | None = None) -> dict[str, Any]:
    run_id = _utc_run_id()
    outdir = (output_root or results_root()) / run_id
    outdir.mkdir(parents=True, exist_ok=False)
    config = FormalConfig()
    golden_plan, mutant_plan = _write_fixture(outdir)

    env_detected = {
        "cloud_llm_gate_detected": env_flag("ASSERTLLM2_SBY_ENABLE_CLOUD_LLM"),
        "anthropic_api_key_detected": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "secret_values_logged": False,
    }
    environment = {
        "run_id": run_id,
        "python": _tool_line(["python", "--version"]),
        "yosys": _tool_line(["yosys", "-V"]),
        "sby": _tool_line(["sby", "--version"]),
        "z3": _tool_line(["z3", "--version"]),
        "formal_config": config.to_json(),
        "env_detection": env_detected,
        "assertllm2_sby": repo_state(PACKAGE_ROOT),
        "assertllm2": repo_state(resolve_assertllm2_checkout()),
    }
    write_json(outdir / "environment.json", environment)

    results: list[FormalResult] = []
    expected: dict[str, str] = {}
    parse_task, parse_harness = _make_task(
        name="parse_elab", spec=PROPERTIES["parse_elab"], source_plan=golden_plan, outdir=outdir, config=config
    )
    parse_elab = _run_yosys_parse(parse_task, parse_harness, outdir)

    for name, spec in PROPERTIES.items():
        task, harness = _make_task(name=name, spec=spec, source_plan=golden_plan, outdir=outdir, config=config)
        result = run_sby_task(task, config=config, harness_body=harness)
        results.append(result)
        expected[name] = spec["expected"].value

    past_spec = PROPERTIES["past_guarded"]
    mutant_task, mutant_harness = _make_task(
        name="mutant_faulty_increment",
        spec={**past_spec, "mode": "bmc", "depth_attr": "bmc_depth", "expected": FormalStatus.COUNTEREXAMPLE},
        source_plan=mutant_plan,
        outdir=outdir,
        config=config,
    )
    mutant_result = run_sby_task(mutant_task, config=config, harness_body=mutant_harness)
    results.append(mutant_result)
    expected["mutant_faulty_increment"] = FormalStatus.COUNTEREXAMPLE.value

    timeout_task, timeout_harness = _make_task(
        name="timeout_classification",
        spec=PROPERTIES["bmc_bounded_clean"],
        source_plan=golden_plan,
        outdir=outdir,
        config=config,
    )
    timeout_result = run_sby_task(timeout_task, config=FormalConfig(timeout_seconds=0), harness_body=timeout_harness)
    results.append(timeout_result)
    expected["timeout_classification"] = FormalStatus.TIMEOUT.value

    error_task = FormalTask(
        task_id="error_classification",
        mode="bmc",
        depth=config.bmc_depth,
        source_plan=golden_plan,
        assertions=lower_assertions((("error_classification", "assert(1'b1);"),)),
        workdir=(outdir / "tasks" / "error_classification").resolve(),
    )
    error_result = run_sby_task(error_task, config=config, harness_body="module sby_harness(")
    results.append(error_result)
    expected["error_classification"] = FormalStatus.ERROR.value

    golden_past = next(r for r in results if r.task_id == "past_guarded")
    mutant_eval = classify_mutant(
        mutant_id="faulty_increment_by_two",
        golden_result=golden_past,
        mutant_result=mutant_result,
        responsible_assertion="past_guarded",
    )

    observed = {r.task_id: r.status.value for r in results}
    expected_matches = {task_id: observed.get(task_id) == status for task_id, status in expected.items()}
    passed = parse_elab["passed"] and all(expected_matches.values()) and mutant_eval.killed
    summary = write_formal_summary(
        outdir,
        run_id=run_id,
        results=results,
        mutants=[mutant_eval],
        extra={
            "passed": passed,
            "expected": expected,
            "observed": observed,
            "expected_matches": expected_matches,
            "parse_elaboration": parse_elab,
            "env_detection": env_detected,
        },
    )
    manifest = {
        "adapter": "AssertLLM2-SBY",
        "run_id": run_id,
        "status": "PASS" if passed else "FAIL",
        "scope": "synthetic formal self-test only",
        "anthropic_invoked": False,
        "real_assertllm2_design_invoked": False,
        "result_path": str(outdir),
        "formal_config": config.to_json(),
        "summary": summary,
    }
    write_json(outdir / "manifest.json", manifest)
    failures = []
    if not parse_elab["passed"]:
        failures.append("Yosys parse/elaboration failed")
    failures.extend(task_id for task_id, ok in expected_matches.items() if not ok)
    if not mutant_eval.killed:
        failures.append("faulty mutant was not killed by the accepted golden assertion")
    (outdir / "failures.md").write_text(
        "# Formal Self-Test Failures\n\n"
        + ("\n".join(f"- {item}" for item in failures) if failures else "No failures.\n"),
        encoding="utf-8",
    )
    (outdir / "report.md").write_text(_report_text(run_id, outdir, results, mutant_eval, parse_elab, passed), encoding="utf-8")
    return manifest


def _report_text(
    run_id: str,
    outdir: Path,
    results: list[FormalResult],
    mutant_eval: Any,
    parse_elab: dict[str, Any],
    passed: bool,
) -> str:
    by_id = {r.task_id: r for r in results}
    traces = []
    for result in results:
        for trace in result.trace_files:
            traces.append(f"- `{result.task_id}`: `{trace}`")
    return f"""# AssertLLM2-SBY Formal Self-Test

Status: {"PASS" if passed else "FAIL"}

This run uses synthetic fixtures only. It does not invoke Anthropic and does not run a real AssertLLM2 design.

## Parse And Elaboration

- Yosys parse/elaboration: {"PASS" if parse_elab["passed"] else "FAIL"}
- Log: `{parse_elab["log"]}`

## Assertion Outcomes

- Provable assertion: `{by_id["prove_provable"].status.value}`
- BMC bounded-clean assertion: `{by_id["bmc_bounded_clean"].status.value}`
- Failing assertion: `{by_id["bmc_failing"].status.value}`
- `$past` guarded assertion on golden RTL: `{by_id["past_guarded"].status.value}`
- Timeout classification: `{by_id["timeout_classification"].status.value}`
- Error classification: `{by_id["error_classification"].status.value}`

## Cover Outcomes

- Reachable cover: `{by_id["cover_reached"].status.value}`
- Unreached cover at selected depth: `{by_id["cover_unreached"].status.value}`

## Mutant Outcome

- Mutant: `faulty_increment_by_two`
- Killed: `{mutant_eval.killed}`
- Responsible assertion: `{mutant_eval.responsible_assertion}`
- Mutant status: `{mutant_eval.mutant_status.value}`

## Traces

{chr(10).join(traces) if traces else "No traces were produced."}

Result path: `{outdir}`
Run ID: `{run_id}`
"""
