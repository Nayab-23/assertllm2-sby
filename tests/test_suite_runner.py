from __future__ import annotations

import json
from pathlib import Path

import pytest

from assertllm2_sby.cli import main as cli_main
from assertllm2_sby.models import GenerationMode
from assertllm2_sby.suite_runner import SuiteConfig, run_suite


def make_suite_checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    cfg = root / "configs"
    cfg.mkdir(parents=True)
    designs = {}
    for name in ("a", "b"):
        design = root / "designs" / "CAT" / name
        design.mkdir(parents=True)
        (design / "spec.md").write_text(f"# {name}\n", encoding="utf-8")
        (design / f"{name}.v").write_text(f"module {name}(input clk); endmodule\n", encoding="utf-8")
        designs[f"assertllm2/cat/{name}"] = {
            "spec_file": [f"../designs/CAT/{name}/spec.md"],
            "rtl": {
                "filelist": [f"../designs/CAT/{name}/{name}.v"],
                "top_module": name,
            },
            "clock_reset": {"clocks": [{"signal": "clk"}]},
        }
    (cfg / "assertllm2_design_configs.json").write_text(json.dumps(designs), encoding="utf-8")
    return root


def fake_runner(design, **kwargs):
    out = Path(kwargs["output_root"]) / f"run_{design.design_name}"
    out.mkdir(parents=True)
    manifest = {
        "adapter": "AssertLLM2-SBY",
        "design": {"key": design.key},
        "official_jaspergold_result": False,
    }
    (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return {
        "run_id": f"run_{design.design_name}",
        "status": "COMPLETED",
        "result_path": str(out),
        "summary": {
            "design_completed_end_to_end": True,
            "generation": {"succeeded": True},
            "scoreable_assertions": 1,
            "mutant_outcomes": {"SURVIVED": 1},
            "failures": [],
        },
    }


def test_run_suite_writes_summaries_and_refuses_overwrite(tmp_path: Path):
    checkout = make_suite_checkout(tmp_path)
    output = tmp_path / "suites"
    config = SuiteConfig(
        mode=GenerationMode.BUG_PREVENTION,
        method="llm-spec",
        max_mutants=0,
        jobs=1,
        limit=1,
        suite_id="smoke",
    )

    payload = run_suite(output_root=output, checkout=checkout, config=config, runner=fake_runner)
    suite_dir = output / "smoke"

    assert payload["selected_count"] == 1
    assert payload["completed_count"] == 1
    assert payload["official_jaspergold_result"] is False
    assert (suite_dir / "suite_summary.json").exists()
    assert (suite_dir / "suite_summary.csv").exists()
    assert (suite_dir / "suite_summary.md").exists()
    assert (suite_dir / "manifest.json").exists()
    assert len(payload["capability_matrix"]) == 2
    with pytest.raises(Exception, match="refusing to overwrite"):
        run_suite(output_root=output, checkout=checkout, config=config, runner=fake_runner)


def test_run_suite_resume_skips_existing_manifest(tmp_path: Path):
    checkout = make_suite_checkout(tmp_path)
    output = tmp_path / "suites"
    config = SuiteConfig(
        mode=GenerationMode.BUG_PREVENTION,
        method="llm-spec",
        max_mutants=0,
        jobs=1,
        suite_id="resume-smoke",
    )
    first = run_suite(output_root=output, checkout=checkout, config=config, runner=fake_runner)
    suite_dir = Path(first["suite_dir"])

    calls = []

    def counting_runner(design, **kwargs):
        calls.append(design.key)
        return fake_runner(design, **kwargs)

    resumed = run_suite(
        output_root=output,
        checkout=checkout,
        config=SuiteConfig(
            mode=GenerationMode.BUG_PREVENTION,
            method="llm-spec",
            max_mutants=0,
            jobs=2,
            resume=suite_dir,
        ),
        runner=counting_runner,
    )

    assert calls == []
    assert resumed["skipped_resume_count"] == 2
    assert [row["suite_status"] for row in resumed["results"]] == ["SKIPPED_RESUME", "SKIPPED_RESUME"]


def test_cli_run_suite_with_limit_and_json(tmp_path: Path, capsys, monkeypatch):
    checkout = make_suite_checkout(tmp_path)

    def fake_cli_run_suite(*, output_root, config, checkout=None):
        return {
            "suite_id": "cli-smoke",
            "suite_dir": str(output_root / "cli-smoke"),
            "selected_count": config.limit,
            "completed_count": config.limit,
            "error_count": 0,
            "skipped_resume_count": 0,
            "official_jaspergold_result": False,
        }

    import assertllm2_sby.cli as cli_mod

    monkeypatch.setattr(cli_mod, "run_suite", fake_cli_run_suite)
    rc = cli_main([
        "run-suite",
        "--checkout", str(checkout),
        "--mode", "bug-prevention",
        "--limit", "1",
        "--jobs", "2",
        "--output-root", str(tmp_path / "out"),
    ])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["suite_id"] == "cli-smoke"
    assert payload["selected_count"] == 1
    assert payload["official_jaspergold_result"] is False
