from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .assertion_lowering import classify_and_lower_assertion
from .compat import write_compatibility_artifacts
from .contract_adapter import generate_contract_assertions
from .formal_types import FormalConfig, FormalResult, FormalStatus, FormalTask, LoweredAssertion, SourcePlan
from .generator import generate_assertions, load_generation_result_from_artifacts
from .isolation import create_isolated_workspace, validate_workspace_isolation
from .manifest import read_json, repo_state, sha256_file, write_json
from .models import AssertionCandidate, DesignRecord, GenerationMode, GenerationResult, SpecSource, ValidationError
from .mutation_runner import (
    buggy_rtl_files,
    bug_hunting_metrics,
    load_mutation_cache,
    merged_buggy_source_plan,
    mutation_counts,
    mutation_metrics,
    mutation_results_payload,
    mutant_source_plan as build_mutant_source_plan,
)
from .runtime_config import generator_defaults
from .sby_backend import run_sby_task
from .harness_builder import yosys_script_lines
from .source_plan import build_source_plan, source_plan_artifact, write_blackbox_stubs

from .paths import PACKAGE_ROOT, config_path, resolve_assertllm2_checkout, results_root


GOLDEN_STATUS_MAP = {
    FormalStatus.PROVEN: "GOLDEN_PROVEN",
    FormalStatus.BOUNDED_CLEAN: "GOLDEN_BOUNDED_CLEAN",
    FormalStatus.COUNTEREXAMPLE: "GOLDEN_COUNTEREXAMPLE",
    FormalStatus.UNSUPPORTED: "GOLDEN_UNSUPPORTED",
    FormalStatus.TIMEOUT: "GOLDEN_TIMEOUT",
    FormalStatus.UNKNOWN: "GOLDEN_UNKNOWN",
    FormalStatus.ERROR: "GOLDEN_ERROR",
    FormalStatus.ELABORATION_ERROR: "GOLDEN_ELABORATION_ERROR",
    FormalStatus.INFRASTRUCTURE_ERROR: "GOLDEN_INFRASTRUCTURE_ERROR",
}


@dataclass(frozen=True)
class Port:
    name: str
    direction: str
    width: str


@dataclass(frozen=True)
class Parameter:
    name: str
    value: str


def utc_run_id(design: DesignRecord) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", design.design_name).strip("_")
    return f"one_design_{safe}_{stamp}"


def tool_line(cmd: list[str]) -> str:
    if not shutil.which(cmd[0]):
        return "not found"
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    return proc.stdout.splitlines()[0] if proc.stdout else f"exit {proc.returncode}"


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


def parse_parameters(plan: SourcePlan) -> tuple[Parameter, ...]:
    params, _ = _top_module_header(plan)
    out: list[Parameter] = []
    param_re = re.compile(
        r"\bparameter\s+(?:\w+\s+)?(?:\[[^\]]+\]\s+)?(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<value>[^,]+)"
    )
    for match in param_re.finditer(params):
        out.append(Parameter(name=match.group("name"), value=match.group("value").strip()))
    return tuple(out)


def parse_ports(plan: SourcePlan) -> tuple[Port, ...]:
    decl_text = _clean_verilog_text(plan)
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
    for match in decl_re.finditer(decl_text):
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


def build_property_harness(plan: SourcePlan, lowered: LoweredAssertion, clock: str | None) -> str:
    ports = parse_ports(plan)
    clock_name = clock or next((p.name for p in ports if p.name.lower() in {"clk", "clock", "i_clk"}), None)
    if not clock_name:
        raise ValidationError(f"no clock available for harness: {plan.name}")
    lines = [f"module sby_harness(input wire {clock_name});"]
    for parameter in parse_parameters(plan):
        lines.append(f"  localparam {parameter.name} = {parameter.value};")
    if len(lines) > 1:
        lines.append("")
    for port in ports:
        if port.name == clock_name:
            continue
        width = f" {port.width}" if port.width else ""
        if port.direction == "input":
            lines.append(f"  (* anyseq *) reg{width} {port.name};")
        else:
            lines.append(f"  wire{width} {port.name};")
    lines.append("")
    lines.append(f"  {plan.top_module} dut (")
    conn = [f"    .{port.name}({port.name})" for port in ports]
    lines.append(",\n".join(conn))
    lines.append("  );")
    lines.append("")
    lines.append("  reg past_valid = 1'b0;")
    body = lowered.lowered_text.rstrip().rstrip(";") + ";"
    if "$past" in body and "past_valid" not in body:
        body = f"if (past_valid) begin\n      {body}\n    end"
    indented = "\n".join("    " + line if line.strip() else "" for line in body.splitlines())
    lines.append(f"  always @(posedge {clock_name}) begin")
    lines.append("    past_valid <= 1'b1;")
    lines.append(indented)
    lines.append("  end")
    lines.append("endmodule")
    return "\n".join(lines)


def run_yosys_elaboration(plan: SourcePlan, outdir: Path) -> dict[str, Any]:
    outdir.mkdir(parents=True, exist_ok=True)
    ports = parse_ports(plan)
    clock = next((p.name for p in ports if p.name.lower() in {"clk", "clock", "i_clk"}), ports[0].name)
    dummy = LoweredAssertion("parse_elab", "assert", "assert(1'b1);", "assert(1'b1);", True)
    harness = outdir / "parse_harness.sv"
    harness.write_text(build_property_harness(plan, dummy, clock), encoding="utf-8")
    blackbox_stub = write_blackbox_stubs(plan, outdir)
    extra_files = tuple(path for path in (harness, blackbox_stub) if path is not None)
    log = outdir / "yosys_parse_elab.log"
    cmd = [
        "yosys",
        "-p",
        "; ".join(yosys_script_lines(plan, extra_files=extra_files, top_module="sby_harness")),
    ]
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    log.write_text(proc.stdout, encoding="utf-8")
    return {
        "command": " ".join(cmd),
        "returncode": proc.returncode,
        "passed": proc.returncode == 0,
        "log": str(log),
        "blackbox_stub_file": str(blackbox_stub) if blackbox_stub else None,
        "generated_files": [str(path) for path in extra_files],
    }


def mutation_dirs(design: DesignRecord) -> list[Path]:
    dirs = set()
    for path in design.mutation_files:
        for parent in path.parents:
            if parent.name.startswith("M_"):
                dirs.add(parent)
                break
    return sorted(dirs, key=lambda p: p.name)


def mutant_source_plan(golden: SourcePlan, mutant_dir: Path) -> SourcePlan:
    replacements: list[Path] = []
    for rtl in golden.rtl_files:
        candidate = mutant_dir / rtl.name
        replacements.append(candidate.resolve() if candidate.is_file() else rtl)
    if tuple(replacements) == golden.rtl_files:
        raise ValidationError(f"mutant has no applicable RTL replacements: {mutant_dir}")
    return SourcePlan(
        name=f"{golden.name}__{mutant_dir.name}",
        top_module=golden.top_module,
        rtl_files=tuple(replacements),
        include_dirs=golden.include_dirs,
        defines=golden.defines,
        parameters=golden.parameters,
        blackbox_modules=golden.blackbox_modules,
    )


def _golden_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    keys = [
        "GOLDEN_PROVEN",
        "GOLDEN_BOUNDED_CLEAN",
        "GOLDEN_COUNTEREXAMPLE",
        "GOLDEN_UNSUPPORTED",
        "GOLDEN_TIMEOUT",
        "GOLDEN_UNKNOWN",
        "GOLDEN_ERROR",
        "GOLDEN_ELABORATION_ERROR",
        "GOLDEN_INFRASTRUCTURE_ERROR",
    ]
    counts = {key: 0 for key in keys}
    for row in rows:
        counts[row["golden_outcome"]] = counts.get(row["golden_outcome"], 0) + 1
    return counts


def _mutant_status(golden: str, result: FormalResult) -> str:
    if result.status == FormalStatus.COUNTEREXAMPLE:
        return "STRICT_KILLED" if golden == "GOLDEN_PROVEN" else "BOUNDED_ONLY_KILLED"
    if result.status in {FormalStatus.PROVEN, FormalStatus.BOUNDED_CLEAN}:
        return "SURVIVED"
    if result.status == FormalStatus.TIMEOUT:
        return "TIMEOUT"
    if result.status == FormalStatus.UNKNOWN:
        return "UNKNOWN"
    if result.status == FormalStatus.UNSUPPORTED:
        return "UNSUPPORTED"
    if result.status == FormalStatus.INFRASTRUCTURE_ERROR:
        return "INFRASTRUCTURE_ERROR"
    if result.status == FormalStatus.ELABORATION_ERROR:
        return "ELABORATION_ERROR"
    return "ELABORATION_ERROR"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def model_configuration(gen: GenerationResult) -> dict[str, Any]:
    defaults = generator_defaults()
    if gen.metadata.get("adapter_generator") == "contract_inference":
        return {
            "provider": "contract-inference",
            "model": gen.metadata.get("model"),
            "temperature": None,
            "max_tokens": None,
            "configured_max_output_tokens": None,
            "timeout_seconds": None,
            "api_url_env": None,
            "api_version": None,
            "prompt_template": None,
            "user_prompt_builder": None,
            "attempts_per_design": 1,
            "retry_count": 0,
            "thinking": "none",
            "cloud_gate_env": None,
            "api_key_env": None,
            "api_key_value_logged": False,
        }
    return {
        "provider": gen.metadata.get("provider") or "anthropic",
        "model": gen.metadata.get("model") or os.environ.get("ASSERTLLM2_SBY_LLM_MODEL") or defaults["model"],
        "temperature": gen.metadata.get("temperature")
        if gen.metadata.get("temperature") is not None
        else _float_env("ASSERTLLM2_SBY_LLM_TEMPERATURE", float(defaults["temperature"])),
        "max_tokens": gen.metadata.get("configured_max_output_tokens")
        or gen.metadata.get("max_tokens")
        or _int_env("ASSERTLLM2_SBY_LLM_MAX_TOKENS", int(defaults["max_tokens"])),
        "configured_max_output_tokens": gen.metadata.get("configured_max_output_tokens")
        or gen.metadata.get("max_tokens")
        or _int_env("ASSERTLLM2_SBY_LLM_MAX_TOKENS", int(defaults["max_tokens"])),
        "timeout_seconds": _float_env("ASSERTLLM2_SBY_LLM_TIMEOUT", 30.0),
        "api_url_env": "ANTHROPIC_API_URL",
        "api_version": gen.metadata.get("api_version") or "2023-06-01",
        "prompt_template": gen.metadata.get("prompt_template")
        or "AssertLLM2/assertllm2_sby/generator.py::SPEC_ONLY_SYSTEM_PROMPT",
        "user_prompt_builder": gen.metadata.get("user_prompt_builder")
        or "AssertLLM2/assertllm2_sby/generator.py::_build_user_prompt",
        "attempts_per_design": gen.metadata.get("attempts_per_design") or 1,
        "retry_count": gen.metadata.get("retry_count") or 0,
        "thinking": gen.metadata.get("thinking") or "none",
        "cloud_gate_env": "ASSERTLLM2_SBY_ENABLE_CLOUD_LLM",
        "api_key_env": "ANTHROPIC_API_KEY",
        "api_key_value_logged": False,
    }


def run_design(
    design: DesignRecord,
    *,
    mode: GenerationMode = GenerationMode.BUG_PREVENTION,
    output_root: Path | None = None,
    config: FormalConfig | None = None,
    transport: Callable[[str, str, dict[str, Any]], dict[str, Any]] | None = None,
    reuse_generation_artifacts: Path | None = None,
    max_mutants: int | None = 1,
    method: str = "llm-spec",
    contract_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = config or FormalConfig()
    resolved_output_root = output_root or results_root()
    run_id = utc_run_id(design)
    outdir = resolved_output_root / run_id
    outdir.mkdir(parents=True, exist_ok=False)
    shutil.copy2(config_path(), outdir / "config.snapshot.yaml")
    (outdir / "generation_artifacts").mkdir()
    (outdir / "formal").mkdir()

    selection_reason = (
        "Selected as a relatively small single-clock Verilog design with spec.md, "
        "a clear top module, complete single-file RTL, and cached mutants."
    )
    stages: list[str] = []
    failures: list[str] = []

    stages.append("dataset_lookup")
    gen: GenerationResult
    if method == "contract-inference":
        stages.append("contract_inference_generation")
        gen = generate_contract_assertions(
            design,
            mode=mode,
            output_dir=outdir / "generation_artifacts",
            config=contract_config,
        )
    elif method == "llm-spec":
        stages.append("create_isolated_workspace")
        workspace = create_isolated_workspace(
            design,
            mode=mode,
            spec_source=SpecSource.SPEC_MD,
            output_root=outdir / "isolated_input",
            generator_config={"spec_source": "spec_md"},
        )
        validate_workspace_isolation(workspace.root)
        shutil.copy2(workspace.manifest_path, outdir / "isolated_input_manifest.json")

        stages.append("anthropic_generation")
        if reuse_generation_artifacts is not None:
            gen = load_generation_result_from_artifacts(
                reuse_generation_artifacts,
                output_dir=outdir / "generation_artifacts",
            )
        else:
            gen = generate_assertions(
                workspace,
                output_dir=outdir / "generation_artifacts",
                config=generator_defaults(),
                transport=transport,
            )
    else:
        raise ValidationError(f"unsupported generation method: {method}")
    if not gen.succeeded:
        failures.append(f"generation failed: {gen.blocked_reason}")
    model_cfg = model_configuration(gen)

    stages.append("source_planning")
    golden_plan = build_source_plan(design, name=design.key.replace("/", "__"))
    write_json(outdir / "formal" / "golden_source_plan.json", golden_plan.to_json())
    write_json(outdir / "formal" / "golden_source_plan_artifact.json", source_plan_artifact(golden_plan))

    stages.append("yosys_parse_elaboration")
    parse_elab = run_yosys_elaboration(golden_plan, outdir / "formal" / "parse_elaboration")
    if not parse_elab["passed"]:
        failures.append("golden Yosys parse/elaboration failed")

    assertion_rows: list[dict[str, Any]] = []
    golden_results: list[FormalResult] = []
    clock = design.clocks[0] if design.clocks else None
    stages.append("golden_sby_evaluation")
    candidates = list(gen.candidates)
    for candidate in candidates:
        lowered = classify_and_lower_assertion(candidate.assertion_id, candidate.text)
        if candidate.classification.value in {"EMPTY_OUTPUT", "INVALID_OUTPUT", "TRUNCATED_OR_INVALID_OUTPUT"}:
            lowered = LoweredAssertion(
                candidate.assertion_id,
                "unknown",
                candidate.text,
                "",
                False,
                (candidate.classification.value,),
            )
        if lowered.kind not in {"assert", "cover"}:
            lowered = LoweredAssertion(
                lowered.assertion_id,
                lowered.kind,
                lowered.original_text,
                "",
                False,
                (*lowered.reasons, "only_assert_and_cover_are_runnable"),
            )
        result: FormalResult | None = None
        if lowered.supported and parse_elab["passed"] and gen.succeeded:
            task = FormalTask(
                task_id=f"golden_{candidate.assertion_id}",
                mode="cover" if lowered.kind == "cover" else "bmc",
                depth=config.cover_depth if lowered.kind == "cover" else config.bmc_depth,
                source_plan=golden_plan,
                assertions=(lowered,),
                workdir=(outdir / "formal" / "golden" / candidate.assertion_id).resolve(),
            )
            if clock and config.prefer_bind:
                result = run_sby_task(task, config=config, clock=clock)
                if result.status == FormalStatus.ELABORATION_ERROR and (
                    result.details.get("artifact_generation_error") or result.details.get("bind_checker_removed")
                ):
                    wrapper_task = FormalTask(
                        task_id=f"{task.task_id}_wrapper",
                        mode=task.mode,
                        depth=task.depth,
                        source_plan=task.source_plan,
                        assertions=task.assertions,
                        workdir=(task.workdir / "wrapper_fallback").resolve(),
                    )
                    result = run_sby_task(
                        wrapper_task,
                        config=config,
                        harness_body=build_property_harness(golden_plan, lowered, clock),
                        prefer_bind=False,
                    )
                    result.details["bind_fallback_from"] = str(task.workdir)
            else:
                result = run_sby_task(task, config=config, harness_body=build_property_harness(golden_plan, lowered, clock))
            golden_results.append(result)
            golden_outcome = GOLDEN_STATUS_MAP.get(result.status, "GOLDEN_UNKNOWN")
        else:
            golden_outcome = "GOLDEN_UNSUPPORTED"
        assertion_rows.append({
            "assertion_id": candidate.assertion_id,
            "label": candidate.label,
            "original_text": candidate.text,
            "classification": candidate.classification.value,
            "lowered": lowered.to_json(),
            "golden_outcome": golden_outcome,
            "golden_result": result.to_json() if result else None,
        })

    stages.append("cached_mutant_evaluation")
    mutant_rows: list[dict[str, Any]] = []
    eligible = [
        row for row in assertion_rows
        if row["lowered"]["supported"] and row["lowered"]["kind"] == "assert"
        and row["golden_outcome"] in {"GOLDEN_PROVEN", "GOLDEN_BOUNDED_CLEAN"}
    ]
    mutation_cache = load_mutation_cache(design)
    write_json(outdir / "formal" / "mutation_cache.json", mutation_cache.to_json())
    mutants = list(mutation_cache.mutants)
    selected_mutants = mutants if max_mutants is None else mutants[:max(0, max_mutants)]
    if not selected_mutants:
        mutant_rows.append({"mutant_id": None, "status": "NOT_RUN", "reason": "no cached mutants"})
    elif not eligible:
        for selected_mutant in selected_mutants:
            mutant_rows.append({
                "mutant_id": selected_mutant.mutant_id,
                "status": "NOT_RUN",
                "reason": "no golden-accepted assertions",
                "mutant": selected_mutant.to_json(),
            })
    else:
        for selected_mutant in selected_mutants:
            try:
                if not selected_mutant.scoreable:
                    mutant_rows.append({
                        "mutant_id": selected_mutant.mutant_id,
                        "status": "NON_SCOREABLE",
                        "reason": selected_mutant.non_scoreable_reason,
                        "mutant": selected_mutant.to_json(),
                    })
                    continue
                m_plan = build_mutant_source_plan(golden_plan, design, selected_mutant)
                write_json(outdir / "formal" / "mutants" / selected_mutant.mutant_id / "source_plan.json", m_plan.to_json())
                killed = None
                attempts: list[dict[str, Any]] = []
                final_status = "SURVIVED"
                final_result: FormalResult | None = None
                final_responsible: str | None = None
                non_survivor_status: str | None = None
                non_survivor_result: FormalResult | None = None
                for row in eligible:
                    lowered_payload = row["lowered"]
                    lowered = LoweredAssertion(
                        assertion_id=lowered_payload["assertion_id"],
                        kind=lowered_payload["kind"],
                        original_text=lowered_payload["original_text"],
                        lowered_text=lowered_payload["lowered_text"],
                        supported=lowered_payload["supported"],
                        reasons=tuple(lowered_payload["reasons"]),
                        transformation_rule=lowered_payload.get("transformation_rule"),
                        equivalence_assumptions=tuple(lowered_payload.get("equivalence_assumptions") or ()),
                    )
                    task = FormalTask(
                        task_id=f"mutant_{selected_mutant.mutant_id}_{row['assertion_id']}",
                        mode="bmc",
                        depth=config.bmc_depth,
                        source_plan=m_plan,
                        assertions=(lowered,),
                        workdir=(outdir / "formal" / "mutants" / selected_mutant.mutant_id / row["assertion_id"]).resolve(),
                    )
                    if clock and config.prefer_bind:
                        result = run_sby_task(task, config=config, clock=clock)
                        if result.status == FormalStatus.ELABORATION_ERROR and (
                            result.details.get("artifact_generation_error") or result.details.get("bind_checker_removed")
                        ):
                            wrapper_task = FormalTask(
                                task_id=f"{task.task_id}_wrapper",
                                mode=task.mode,
                                depth=task.depth,
                                source_plan=task.source_plan,
                                assertions=task.assertions,
                                workdir=(task.workdir / "wrapper_fallback").resolve(),
                            )
                            result = run_sby_task(
                                wrapper_task,
                                config=config,
                                harness_body=build_property_harness(m_plan, lowered, clock),
                                prefer_bind=False,
                            )
                            result.details["bind_fallback_from"] = str(task.workdir)
                    else:
                        result = run_sby_task(task, config=config, harness_body=build_property_harness(m_plan, lowered, clock))
                    status = _mutant_status(row["golden_outcome"], result)
                    attempt = {
                        "assertion_id": row["assertion_id"],
                        "status": status,
                        "result": result.to_json(),
                    }
                    attempts.append(attempt)
                    final_result = result
                    if status in {"STRICT_KILLED", "BOUNDED_ONLY_KILLED"}:
                        final_status = status
                        final_responsible = row["assertion_id"]
                        killed = attempt
                        break
                    if status != "SURVIVED" and non_survivor_status is None:
                        non_survivor_status = status
                        non_survivor_result = result
                if killed is None and non_survivor_status is not None:
                    final_status = non_survivor_status
                    final_result = non_survivor_result
                final_result_json = final_result.to_json() if final_result else None
                mutant_rows.append({
                    "mutant_id": selected_mutant.mutant_id,
                    "assertion_id": final_responsible,
                    "status": final_status,
                    "result": final_result_json,
                    "responsible_assertion": final_responsible,
                    "killed_by": final_responsible,
                    "trace_files": final_result_json.get("trace_files", []) if final_result_json else [],
                    "mutant": selected_mutant.to_json(),
                    "assertion_results": attempts,
                })
            except Exception as exc:  # noqa: BLE001 - preserve exact infrastructure issue
                mutant_rows.append({
                    "mutant_id": selected_mutant.mutant_id,
                    "status": "ELABORATION_ERROR",
                    "reason": str(exc),
                    "mutant": selected_mutant.to_json(),
                })
                failures.append(f"mutant evaluation failed for {selected_mutant.mutant_id}: {exc}")

    raw_count = 0 if not gen.assertions_path or not gen.assertions_path.exists() else int(bool(gen.assertions_path.read_text(encoding="utf-8").strip()))
    supported_count = sum(1 for row in assertion_rows if row["lowered"]["supported"])
    unsupported_count = len(assertion_rows) - supported_count
    golden_counts = _golden_counts(assertion_rows)
    mutant_counts = mutation_counts(mutant_rows)
    mutation_results = mutation_results_payload(
        design_key=design.key,
        mutation_cache=mutation_cache,
        mutant_rows=mutant_rows,
        eligible_assertions=eligible,
    )
    metrics = mutation_metrics(
        design_key=design.key,
        mutation_cache=mutation_cache,
        mutant_rows=mutant_rows,
        eligible_assertions=eligible,
    )
    bug_hunting_rows: list[dict[str, Any]] = []
    bug_hunting_metrics_payload: dict[str, Any] | None = None
    if mode == GenerationMode.BUG_HUNTING:
        stages.append("bug_hunting_merged_buggy_evaluation")
        if not eligible:
            for merged_dir in mutation_cache.merged_bug_hunting_dirs:
                bug_hunting_rows.append({
                    "target_id": merged_dir.name,
                    "target_path": str(merged_dir),
                    "status": "NOT_RUN",
                    "reason": "no golden-accepted assertions",
                })
        elif not mutation_cache.merged_bug_hunting_dirs:
            bug_hunting_rows.append({
                "target_id": None,
                "target_path": None,
                "status": "NOT_RUN",
                "reason": "no merged buggy RTL target",
            })
        else:
            for merged_dir in mutation_cache.merged_bug_hunting_dirs:
                try:
                    buggy_plan = merged_buggy_source_plan(golden_plan, design, merged_dir)
                    write_json(outdir / "formal" / "bug_hunting" / merged_dir.name / "source_plan.json", buggy_plan.to_json())
                    attempts: list[dict[str, Any]] = []
                    final_status = "SURVIVED"
                    final_result: FormalResult | None = None
                    final_responsible: str | None = None
                    non_survivor_status: str | None = None
                    non_survivor_result: FormalResult | None = None
                    for row in eligible:
                        lowered_payload = row["lowered"]
                        lowered = LoweredAssertion(
                            assertion_id=lowered_payload["assertion_id"],
                            kind=lowered_payload["kind"],
                            original_text=lowered_payload["original_text"],
                            lowered_text=lowered_payload["lowered_text"],
                            supported=lowered_payload["supported"],
                            reasons=tuple(lowered_payload["reasons"]),
                            transformation_rule=lowered_payload.get("transformation_rule"),
                            equivalence_assumptions=tuple(lowered_payload.get("equivalence_assumptions") or ()),
                        )
                        task = FormalTask(
                            task_id=f"bug_hunting_{merged_dir.name}_{row['assertion_id']}",
                            mode="bmc",
                            depth=config.bmc_depth,
                            source_plan=buggy_plan,
                            assertions=(lowered,),
                            workdir=(outdir / "formal" / "bug_hunting" / merged_dir.name / row["assertion_id"]).resolve(),
                        )
                        if clock and config.prefer_bind:
                            result = run_sby_task(task, config=config, clock=clock)
                            if result.status == FormalStatus.ELABORATION_ERROR and (
                                result.details.get("artifact_generation_error") or result.details.get("bind_checker_removed")
                            ):
                                wrapper_task = FormalTask(
                                    task_id=f"{task.task_id}_wrapper",
                                    mode=task.mode,
                                    depth=task.depth,
                                    source_plan=task.source_plan,
                                    assertions=task.assertions,
                                    workdir=(task.workdir / "wrapper_fallback").resolve(),
                                )
                                result = run_sby_task(
                                    wrapper_task,
                                    config=config,
                                    harness_body=build_property_harness(buggy_plan, lowered, clock),
                                    prefer_bind=False,
                                )
                                result.details["bind_fallback_from"] = str(task.workdir)
                        else:
                            result = run_sby_task(
                                task,
                                config=config,
                                harness_body=build_property_harness(buggy_plan, lowered, clock),
                            )
                        status = _mutant_status(row["golden_outcome"], result)
                        attempts.append({
                            "assertion_id": row["assertion_id"],
                            "status": status,
                            "result": result.to_json(),
                        })
                        final_result = result
                        if status in {"STRICT_KILLED", "BOUNDED_ONLY_KILLED"}:
                            final_status = status
                            final_responsible = row["assertion_id"]
                            break
                        if status != "SURVIVED" and non_survivor_status is None:
                            non_survivor_status = status
                            non_survivor_result = result
                    if final_responsible is None and non_survivor_status is not None:
                        final_status = non_survivor_status
                        final_result = non_survivor_result
                    final_result_json = final_result.to_json() if final_result else None
                    bug_hunting_rows.append({
                        "target_id": merged_dir.name,
                        "target_path": str(merged_dir),
                        "assertion_id": final_responsible,
                        "status": final_status,
                        "result": final_result_json,
                        "responsible_assertion": final_responsible,
                        "detected_by": final_responsible,
                        "trace_files": final_result_json.get("trace_files", []) if final_result_json else [],
                        "assertion_results": attempts,
                    })
                except Exception as exc:  # noqa: BLE001 - preserve buggy target issue
                    bug_hunting_rows.append({
                        "target_id": merged_dir.name,
                        "target_path": str(merged_dir),
                        "status": "ELABORATION_ERROR",
                        "reason": str(exc),
                    })
        bug_hunting_metrics_payload = bug_hunting_metrics(
            design_key=design.key,
            clean_rows=assertion_rows,
            merged_buggy_rows=bug_hunting_rows,
            mutant_metrics=metrics,
            visible_buggy_rtl_files=[str(path) for path in buggy_rtl_files(design)],
        )
        write_json(outdir / "bug_hunting_metrics.json", bug_hunting_metrics_payload)
        metrics["bug_hunting"] = {
            "metrics_path": str(outdir / "bug_hunting_metrics.json"),
            "clean_design_cex_ratio": bug_hunting_metrics_payload["clean_design"]["clean_design_cex_ratio"],
            "detection_rate": bug_hunting_metrics_payload["merged_buggy_targets"]["detection_rate"],
            "miss_rate": bug_hunting_metrics_payload["merged_buggy_targets"]["miss_rate"],
            "error_rate": bug_hunting_metrics_payload["merged_buggy_targets"]["error_rate"],
        }
    write_json(outdir / "mutation_results.json", mutation_results)
    write_json(outdir / "metrics.json", metrics)
    write_json(outdir / "scorecard.json", {
        "schema_version": "1.0",
        "adapter": "AssertLLM2-SBY",
        "design_key": design.key,
        "official_jaspergold_result": False,
        "baseline_eval": str(outdir / "baseline_eval.json"),
        "mutation_results": str(outdir / "mutation_results.json"),
        "metrics": str(outdir / "metrics.json"),
        "strict_mutation_score": metrics["strict_mutation_score"],
        "bounded_inclusive_mutation_score": metrics["bounded_inclusive_mutation_score"],
        "counts": metrics["counts"],
    })
    mutation_evaluated = bool(mutant_rows and any(row["status"] not in {"NOT_RUN", "NON_SCOREABLE"} for row in mutant_rows))
    bug_hunting_evaluated = bool(
        mode == GenerationMode.BUG_HUNTING
        and bug_hunting_rows
        and any(row["status"] not in {"NOT_RUN", "NON_SCOREABLE"} for row in bug_hunting_rows)
    )
    completed = bool(
        gen.succeeded
        and parse_elab["passed"]
        and assertion_rows
        and (mutation_evaluated or bug_hunting_evaluated)
    )
    if not completed:
        failures.append("design did not complete end to end")

    environment = {
        "python": tool_line(["python", "--version"]),
        "yosys": tool_line(["yosys", "-V"]),
        "sby": tool_line(["sby", "--version"]),
        "z3": tool_line(["z3", "--version"]),
        "assertllm2_sby": repo_state(PACKAGE_ROOT),
        "assertllm2": repo_state(resolve_assertllm2_checkout()),
        "cloud_llm_gate_detected": bool(os.environ.get("ASSERTLLM2_SBY_ENABLE_CLOUD_LLM")),
        "anthropic_api_key_detected": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "secret_values_logged": False,
        "model_configuration": model_cfg,
    }
    write_json(outdir / "environment.json", environment)
    write_json(outdir / "model_configuration.json", model_cfg)
    summary = {
        "run_id": run_id,
        "design_key": design.key,
        "design_completed_end_to_end": completed,
        "selection_reason": selection_reason,
        "model_configuration": model_cfg,
        "stages": stages,
        "generation": {
            "succeeded": gen.succeeded,
            "blocked_reason": gen.blocked_reason,
            "raw_response_path": str(gen.raw_response_path) if gen.raw_response_path else None,
            "assertions_path": str(gen.assertions_path) if gen.assertions_path else None,
            "raw_output_count": raw_count,
            "extracted_count": len(assertion_rows),
            "supported_count": supported_count,
            "unsupported_count": unsupported_count,
            "metadata": gen.metadata,
        },
        "parse_elaboration": parse_elab,
        "assertions": assertion_rows,
        "golden_outcomes": golden_counts,
        "mutants": mutant_rows,
        "mutant_outcomes": mutant_counts,
        "bug_hunting": {
            "enabled": mode == GenerationMode.BUG_HUNTING,
            "metrics_path": str(outdir / "bug_hunting_metrics.json") if bug_hunting_metrics_payload else None,
            "merged_buggy_results": bug_hunting_rows,
        },
        "failures": failures,
    }
    stages.append("jasper_compatible_baseline_artifacts")
    baseline_eval = write_compatibility_artifacts(
        outdir,
        design_key=design.key,
        top_module=design.top_module,
        generation={
            "syntax_cleanup": gen.metadata.get("syntax_cleanup") or {},
        },
        assertion_rows=assertion_rows,
        config=config.to_json(),
        source_assertions_path=gen.assertions_path,
        source_meta_path=Path(str(gen.metadata["assertions_meta_path"]))
        if gen.metadata.get("assertions_meta_path")
        else None,
        source_plan=golden_plan,
    )
    summary["baseline_eval_path"] = str(outdir / "baseline_eval.json")
    summary["design_fpv_report_path"] = str(outdir / "report" / "design.fpv.rpt")
    summary["scoreable_assertions"] = baseline_eval["scoreable_assertions"]
    write_json(outdir / "summary.json", summary)
    write_json(outdir / "manifest.json", {
        "adapter": "AssertLLM2-SBY",
        "run_id": run_id,
        "status": "COMPLETED" if completed else "PARTIAL",
        "mode": mode.value,
        "method": method,
        "design": design.to_json(include_upstream=False),
        "model_configuration": model_cfg,
        "result_path": str(outdir),
        "official_jaspergold_result": False,
        "statement": "This is an AssertLLM2-SBY open-source backend evaluation. It replaces the official JasperGold judge with Yosys/SymbiYosys. Results are backend-specific and are not directly comparable to the published JasperGold-based AssertLLM2 scores.",
    })
    with (outdir / "summary.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["assertion_id", "classification", "supported", "golden_outcome"])
        for row in assertion_rows:
            writer.writerow([row["assertion_id"], row["classification"], row["lowered"]["supported"], row["golden_outcome"]])
    (outdir / "failures.md").write_text(
        "# Failures\n\n" + ("\n".join(f"- {item}" for item in failures) if failures else "No failures.\n"),
        encoding="utf-8",
    )
    (outdir / "report.md").write_text(_report(summary, outdir), encoding="utf-8")
    return {"run_id": run_id, "status": "COMPLETED" if completed else "PARTIAL", "result_path": str(outdir), "summary": summary}


def _report(summary: dict[str, Any], outdir: Path) -> str:
    traces = []
    for row in summary["assertions"]:
        result = row.get("golden_result")
        if result:
            for trace in result.get("trace_files", []):
                traces.append(f"- golden `{row['assertion_id']}`: `{trace}`")
    for row in summary["mutants"]:
        result = row.get("result") or {}
        for trace in result.get("trace_files", []):
            traces.append(f"- mutant `{row.get('mutant_id')}` / `{row.get('assertion_id')}`: `{trace}`")
    return f"""# AssertLLM2-SBY One-Design Report

This is an AssertLLM2-SBY open-source backend evaluation. It replaces the official JasperGold judge with Yosys/SymbiYosys. Results are backend-specific and are not directly comparable to the published JasperGold-based AssertLLM2 scores.

## Design

- Design: `{summary['design_key']}`
- Selection reason: {summary['selection_reason']}
- Completed end to end: `{summary['design_completed_end_to_end']}`

## Generation

- Anthropic call succeeded: `{summary['generation']['succeeded']}`
- Provider: `{summary['model_configuration']['provider']}`
- Model: `{summary['model_configuration']['model']}`
- Temperature: `{summary['model_configuration']['temperature']}`
- Max tokens: `{summary['model_configuration']['max_tokens']}`
- Attempts/retries: `{summary['model_configuration']['attempts_per_design']}` / `{summary['model_configuration']['retry_count']}`
- Thinking: `{summary['model_configuration']['thinking']}`
- Raw output count: `{summary['generation']['raw_output_count']}`
- Extracted assertions: `{summary['generation']['extracted_count']}`
- Supported assertions: `{summary['generation']['supported_count']}`
- Unsupported assertions: `{summary['generation']['unsupported_count']}`

## Golden Outcomes

```json
{json.dumps(summary['golden_outcomes'], indent=2, sort_keys=True)}
```

## Mutant Outcomes

```json
{json.dumps(summary['mutant_outcomes'], indent=2, sort_keys=True)}
```

## Traces

{chr(10).join(traces) if traces else "No counterexample or cover traces were produced."}

## Reproduction

```bash
source .venv-assertllm2-sby/bin/activate
python -m assertllm2_sby.cli run-design --mode bug-prevention --design {summary['design_key']}
```

Result path: `{outdir}`
"""
