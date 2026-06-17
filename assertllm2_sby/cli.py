from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .contract_adapter import generate_contract_assertions
from .dataset import capability_matrix, discover_designs, get_design
from .generator import generate_assertions
from .isolation import create_isolated_workspace
from .manifest import env_flag
from .models import GenerationMode, SpecSource, ValidationError
from .real_design import run_design
from .paths import dotenv_path, results_root, runs_root
from .self_test import run_formal_self_test
from .suite_runner import SuiteConfig, run_suite

GENERATION_METHODS = ("llm-spec", "contract-inference")
MODE_CHOICES = [mode.value for mode in GenerationMode]


def load_repo_dotenv() -> None:
    env_path = dotenv_path()
    if not env_path.is_file():
        return
    try:
        from dotenv import load_dotenv
    except Exception:
        for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("\"'")
            if key and key not in os.environ:
                os.environ[key] = value
        return
    load_dotenv(env_path, override=False)


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _checkout_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--checkout", type=Path, default=Path("third_party/AssertLLM2"))


def cmd_list_designs(args: argparse.Namespace) -> int:
    designs = discover_designs(args.checkout)
    if args.json:
        _print_json({"count": len(designs), "designs": [d.to_json(False) for d in designs]})
    else:
        for d in designs:
            print(f"{d.key}\t{d.source_language}\t{d.top_module or ''}")
        print(f"count: {len(designs)}")
    return 0


def cmd_capability_matrix(args: argparse.Namespace) -> int:
    rows = capability_matrix(args.checkout)
    if args.json:
        _print_json({"count": len(rows), "capability_matrix": rows})
    else:
        columns = [
            "key",
            "source_language",
            "rtl_file_count",
            "single_clock",
            "multi_clock",
            "combinational",
            "parameterized",
            "blackbox_required",
            "has_complete_mutants",
            "scoreable",
            "unsupported_reasons",
        ]
        print("\t".join(columns))
        for row in rows:
            values = []
            for column in columns:
                value = row[column]
                if isinstance(value, list):
                    value = ",".join(str(v) for v in value)
                values.append(str(value))
            print("\t".join(values))
        print(f"count: {len(rows)}")
    return 0


def cmd_inspect_design(args: argparse.Namespace) -> int:
    d = get_design(args.design, args.checkout)
    _print_json(d.to_json(include_upstream=True))
    return 0


def _spec_source(args: argparse.Namespace) -> SpecSource:
    return SpecSource.RAW if args.spec_source == "raw" else SpecSource.SPEC_MD


def cmd_prepare_input(args: argparse.Namespace) -> int:
    d = get_design(args.design, args.checkout)
    ws = create_isolated_workspace(
        d,
        mode=GenerationMode(args.mode),
        spec_source=_spec_source(args),
        include_raw=args.spec_source == "raw",
        output_root=args.output_root,
        generator_config={"dry_run": bool(args.dry_run), "spec_source": args.spec_source},
    )
    payload = {
        "dry_run": bool(args.dry_run),
        "workspace": str(ws.root),
        "manifest": str(ws.manifest_path),
        "design": d.key,
        "mode": ws.mode.value,
        "spec_source": ws.spec_source.value,
        "exposed_files": [
            {
                "filename": f.workspace_path.name,
                "role": f.role,
                "sha256": f.sha256,
                "size": f.size,
            }
            for f in ws.exposed_files
        ],
        "model_invoked": False,
    }
    _print_json(payload)
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    d = get_design(args.design, args.checkout)
    if args.method == "contract-inference":
        result = generate_contract_assertions(
            d,
            mode=GenerationMode(args.mode),
            output_dir=args.output_root,
            config={
                "python_entrypoint": args.contract_python_entrypoint,
                "executable": args.contract_executable,
                "tool_root": args.contract_tool_root,
            },
        )
        _print_json(result.to_json())
        return 0 if result.succeeded else 2
    ws = create_isolated_workspace(
        d,
        mode=GenerationMode(args.mode),
        spec_source=_spec_source(args),
        include_raw=args.spec_source == "raw",
        output_root=args.output_root,
        generator_config={
            "spec_source": args.spec_source,
            "model": args.model,
            "temperature": args.temperature,
            "max_tokens": args.max_tokens,
        },
    )
    result = generate_assertions(
        ws,
        config={
            "model": args.model,
            "temperature": args.temperature,
            "max_tokens": args.max_tokens,
        },
    )
    _print_json(result.to_json())
    return 0 if result.succeeded else 2


def cmd_env_status(args: argparse.Namespace) -> int:
    _print_json({
        "cloud_llm_gate_detected": "yes" if env_flag("SABLE_ENABLE_CLOUD_LLM") else "no",
        "anthropic_api_key_detected": "yes" if bool(os.environ.get("ANTHROPIC_API_KEY")) else "no",
    })
    return 0


def cmd_formal_self_test(args: argparse.Namespace) -> int:
    result = run_formal_self_test(args.output_root or results_root())
    _print_json({
        "run_id": result["run_id"],
        "status": result["status"],
        "result_path": result["result_path"],
        "cloud_llm_gate_detected": "yes" if result["summary"]["extra"]["env_detection"]["cloud_llm_gate_detected"] else "no",
        "anthropic_api_key_detected": "yes" if result["summary"]["extra"]["env_detection"]["anthropic_api_key_detected"] else "no",
    })
    return 0 if result["status"] == "PASS" else 1


def cmd_run_design(args: argparse.Namespace) -> int:
    design = get_design(args.design, args.checkout)
    result = run_design(
        design,
        mode=GenerationMode(args.mode),
        output_root=args.output_root or results_root(),
        reuse_generation_artifacts=args.reuse_generation_artifacts,
        max_mutants=args.max_mutants,
        method=args.method,
        contract_config={
            "python_entrypoint": args.contract_python_entrypoint,
            "executable": args.contract_executable,
            "tool_root": args.contract_tool_root,
        },
    )
    _print_json({
        "run_id": result["run_id"],
        "status": result["status"],
        "result_path": result["result_path"],
        "design": design.key,
        "completed_end_to_end": result["summary"]["design_completed_end_to_end"],
    })
    return 0 if result["summary"]["generation"]["succeeded"] else 2


def cmd_run_suite(args: argparse.Namespace) -> int:
    config = SuiteConfig(
        mode=GenerationMode(args.mode),
        method=args.method,
        max_mutants=args.max_mutants,
        jobs=args.jobs,
        limit=args.limit,
        design_keys=tuple(args.design or ()),
        suite_id=args.suite_id,
        resume=args.resume,
        contract_config={
            "python_entrypoint": args.contract_python_entrypoint,
            "executable": args.contract_executable,
            "tool_root": args.contract_tool_root,
        },
    )
    payload = run_suite(
        output_root=args.output_root or (results_root() / "suites"),
        config=config,
        checkout=args.checkout,
    )
    _print_json({
        "suite_id": payload["suite_id"],
        "suite_dir": payload["suite_dir"],
        "selected_count": payload["selected_count"],
        "completed_count": payload["completed_count"],
        "error_count": payload["error_count"],
        "skipped_resume_count": payload["skipped_resume_count"],
        "official_jaspergold_result": False,
    })
    return 0 if payload["error_count"] == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AssertLLM2-SBY adapter")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list-designs")
    _checkout_arg(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list_designs)

    p = sub.add_parser("capability-matrix")
    _checkout_arg(p)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_capability_matrix)

    p = sub.add_parser("inspect-design")
    _checkout_arg(p)
    p.add_argument("--design", required=True)
    p.set_defaults(func=cmd_inspect_design)

    p = sub.add_parser("env-status")
    p.set_defaults(func=cmd_env_status)

    p = sub.add_parser("formal-self-test")
    p.add_argument("--output-root", type=Path, default=None)
    p.set_defaults(func=cmd_formal_self_test)

    p = sub.add_parser("run-design")
    _checkout_arg(p)
    p.add_argument("--method", choices=GENERATION_METHODS, default="llm-spec")
    p.add_argument("--mode", choices=MODE_CHOICES, required=True)
    p.add_argument("--design", required=True)
    p.add_argument("--output-root", type=Path, default=None)
    p.add_argument("--reuse-generation-artifacts", type=Path, default=None)
    p.add_argument("--max-mutants", type=int, default=1)
    p.add_argument("--contract-python-entrypoint", default=None)
    p.add_argument("--contract-executable", default=None)
    p.add_argument("--contract-tool-root", default=None)
    p.set_defaults(func=cmd_run_design)

    p = sub.add_parser("run-suite")
    _checkout_arg(p)
    p.add_argument("--method", choices=GENERATION_METHODS, default="llm-spec")
    p.add_argument("--mode", choices=MODE_CHOICES, required=True)
    p.add_argument("--design", action="append", default=[])
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--jobs", type=int, default=1)
    p.add_argument("--output-root", type=Path, default=None)
    p.add_argument("--suite-id", default=None)
    p.add_argument("--resume", type=Path, default=None)
    p.add_argument("--max-mutants", type=int, default=1)
    p.add_argument("--contract-python-entrypoint", default=None)
    p.add_argument("--contract-executable", default=None)
    p.add_argument("--contract-tool-root", default=None)
    p.set_defaults(func=cmd_run_suite)

    for name, func in (("prepare-input", cmd_prepare_input), ("generate", cmd_generate)):
        p = sub.add_parser(name)
        _checkout_arg(p)
        if name == "generate":
            p.add_argument("--method", choices=GENERATION_METHODS, default="llm-spec")
        p.add_argument("--mode", choices=MODE_CHOICES, required=True)
        p.add_argument("--design", required=True)
        p.add_argument("--spec-source", choices=["spec_md", "raw"], default="spec_md")
        p.add_argument("--output-root", type=Path, default=runs_root())
        if name == "prepare-input":
            p.add_argument("--dry-run", action="store_true")
        else:
            p.add_argument("--model", default=None)
            p.add_argument("--temperature", type=float, default=None)
            p.add_argument("--max-tokens", type=int, default=None)
            p.add_argument("--contract-python-entrypoint", default=None)
            p.add_argument("--contract-executable", default=None)
            p.add_argument("--contract-tool-root", default=None)
        p.set_defaults(func=func)
    return parser


def main(argv: list[str] | None = None) -> int:
    load_repo_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValidationError as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
