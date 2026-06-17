from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assertllm2_sby.assertion_parser import cleanup_assertion_file, collect_assertion_blocks, extract_assertions, syntax_correctness
from assertllm2_sby import paths as package_paths
from assertllm2_sby.cli import main as cli_main
from assertllm2_sby.dataset import discover_designs, get_design
from assertllm2_sby.generator import generate_assertions
from assertllm2_sby.isolation import create_isolated_workspace, validate_workspace_isolation
from assertllm2_sby.manifest import env_flag, redacted_mapping, sha256_file
from assertllm2_sby.models import (
    AssertionClassification,
    GenerationMode,
    SpecSource,
    ValidationError,
)


def make_checkout(tmp_path: Path, *, duplicate: bool = False) -> Path:
    root = tmp_path / "AssertLLM2"
    cfg_dir = root / "AssertLLM2" / "configs"
    cfg_dir.mkdir(parents=True)
    design = root / "designs" / "CAT" / "tiny"
    (design / "include").mkdir(parents=True)
    (design / "spec.md").write_text("# Tiny\nWhen req is high, ack follows.\n", encoding="utf-8")
    (design / "raw.pdf").write_text("raw spec text", encoding="utf-8")
    (design / "tiny.v").write_text(
        "module tiny #(parameter WIDTH = 4)(input clk, input rst, input req, output ack); endmodule\n",
        encoding="utf-8",
    )
    (design / "include" / "helper.v").write_text("module helper; endmodule\n", encoding="utf-8")
    (design / "jg_bbox.json").write_text('{"bbox_modules":["vendor_ip"]}\n', encoding="utf-8")
    (design / "mutations").mkdir()
    (design / "mutations" / "mutation_summary.json").write_text('{"mutants":[]}\n', encoding="utf-8")
    config = {
        "assertllm2/cat/tiny": {
            "spec_file": ["../designs/CAT/tiny/spec.md"],
            "rtl": {
                "filelist": [
                    "../designs/CAT/tiny/tiny.v",
                    "../designs/CAT/tiny/include/helper.v",
                ],
                "incdir": ["../designs/CAT/tiny/include"],
                "top_module": "tiny",
                "defines": ["ASSERTLLM2_SBY_TEST"],
                "parameters": {"DEPTH": 2},
            },
            "clock_reset": {"clocks": [{"signal": "clk"}], "reset": "rst"},
            "output_dir": "out",
        }
    }
    if duplicate:
        config["assertllm2/cat/tiny_dup"] = dict(config["assertllm2/cat/tiny"])
    (cfg_dir / "assertllm2_design_configs.json").write_text(json.dumps(config), encoding="utf-8")
    return root


def make_direct_checkout(tmp_path: Path) -> Path:
    root = tmp_path / "direct"
    cfg_dir = root / "configs"
    cfg_dir.mkdir(parents=True)
    design = root / "designs" / "CAT" / "direct"
    design.mkdir(parents=True)
    (design / "spec.md").write_text("# Direct\n", encoding="utf-8")
    (design / "direct.v").write_text("module direct(input clk); endmodule\n", encoding="utf-8")
    config = {
        "assertllm2/cat/direct": {
            "spec_file": ["../designs/CAT/direct/spec.md"],
            "rtl": {
                "filelist": ["../designs/CAT/direct/direct.v"],
                "top_module": "direct",
            },
            "clock_reset": {"clocks": [{"signal": "clk"}]},
        }
    }
    (cfg_dir / "assertllm2_design_configs.json").write_text(json.dumps(config), encoding="utf-8")
    return root


def test_dataset_discovery_and_ordering(tmp_path: Path):
    root = make_checkout(tmp_path)
    designs = discover_designs(root)
    assert [d.key for d in designs] == ["assertllm2/cat/tiny"]
    d = designs[0]
    assert d.category == "CAT"
    assert d.design_name == "tiny"
    assert d.top_module == "tiny"
    assert d.clocks == ("clk",)
    assert d.reset == "rst"
    assert d.source_language == "verilog"
    assert d.defines == ("ASSERTLLM2_SBY_TEST",)
    assert d.parameters == {"WIDTH": "4", "DEPTH": 2}
    assert d.blackbox_modules == ("vendor_ip",)
    assert d.mutation_metadata["has_mutation_cache"] is True
    assert d.capability["multi_file"] is True
    assert d.capability["single_clock"] is True
    assert d.capability["parameterized"] is True
    assert d.capability["blackbox_required"] is True
    assert "requires_blackbox_stubs" in d.capability["unsupported_reasons"]
    assert d.identity["dataset_identity_sha256"]


def test_stable_design_key_lookup(tmp_path: Path):
    root = make_checkout(tmp_path)
    assert get_design("assertllm2/cat/tiny", root).key == "assertllm2/cat/tiny"


def test_direct_submodule_layout_discovery(tmp_path: Path):
    root = make_direct_checkout(tmp_path)
    designs = discover_designs(root)
    assert [d.key for d in designs] == ["assertllm2/cat/direct"]
    assert designs[0].rtl_files[0] == (root / "designs" / "CAT" / "direct" / "direct.v").resolve()


def test_checkout_resolver_rejects_broken_primary_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo"
    third_party = repo / "third_party"
    third_party.mkdir(parents=True)
    broken_target = tmp_path / "missing" / "AssertLLM2"
    (third_party / "AssertLLM2").symlink_to(broken_target)
    fallback = tmp_path / "third_party" / "AssertLLM2" / "configs"
    fallback.mkdir(parents=True)
    (fallback / "assertllm2_design_configs.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(package_paths, "PACKAGE_ROOT", repo)

    with pytest.raises(ValidationError, match="broken symlink"):
        package_paths.resolve_assertllm2_checkout()


def test_checkout_resolver_accepts_nested_assertllm2_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo"
    config_dir = repo / "third_party" / "AssertLLM2" / "AssertLLM2" / "configs"
    config_dir.mkdir(parents=True)
    (config_dir / "assertllm2_design_configs.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(package_paths, "PACKAGE_ROOT", repo)

    assert package_paths.resolve_assertllm2_checkout() == (repo / "third_party" / "AssertLLM2").resolve()


def test_path_containment_rejects_escape(tmp_path: Path):
    root = make_checkout(tmp_path)
    cfg = root / "AssertLLM2" / "configs" / "assertllm2_design_configs.json"
    payload = json.loads(cfg.read_text())
    payload["assertllm2/cat/tiny"]["spec_file"] = ["../../outside/spec.md"]
    cfg.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValidationError):
        discover_designs(root)


def test_duplicate_detection(tmp_path: Path):
    root = make_checkout(tmp_path, duplicate=True)
    with pytest.raises(ValidationError, match="duplicate design directory"):
        discover_designs(root)


def test_workspace_isolation_and_hashing(tmp_path: Path):
    root = make_checkout(tmp_path)
    design = get_design("assertllm2/cat/tiny", root)
    ws = create_isolated_workspace(
        design,
        mode=GenerationMode.BUG_PREVENTION,
        spec_source=SpecSource.SPEC_MD,
        output_root=tmp_path / "runs",
        generator_config={"api_key": "secret", "model": "m"},
    )
    exposed = ws.exposed_files[0]
    assert exposed.workspace_path.name == "spec.md"
    assert exposed.sha256 == sha256_file(exposed.workspace_path)
    assert not list(ws.root.rglob("*.v"))
    manifest = json.loads(ws.manifest_path.read_text())
    assert manifest["generator_config"]["api_key"] == "<redacted>"


def test_forbidden_rtl_detection(tmp_path: Path):
    root = make_checkout(tmp_path)
    design = get_design("assertllm2/cat/tiny", root)
    ws = create_isolated_workspace(design, output_root=tmp_path / "runs")
    bad = ws.root / "input" / "leak.sv"
    bad.write_text("module leak; endmodule\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="forbidden suffix"):
        validate_workspace_isolation(ws.root)


def test_assertion_extraction_and_unsupported_classification():
    raw = """
    Here is the SVA:
    ```systemverilog
    a_ok: assert property (@(posedge clk) disable iff (rst) req |-> ack);
    a_bad: assert property (@(posedge clk) req ##[1:3] ack);
    ```
    """
    items = extract_assertions(raw)
    classes = [i.classification for i in items]
    assert AssertionClassification.NEEDS_FORMAL_VALIDATION in classes
    assert AssertionClassification.UNSUPPORTED_SVA in classes
    assert any("ranged_delay" in i.reasons for i in items)


def test_phase3_assertion_block_collector_supported_forms(tmp_path: Path):
    raw = """
    // [signal] req maps to request
    p_req_ack: property p_req_ack_body;
      @(posedge clk) disable iff (rst) req |-> ack;
    endproperty
    a_prop: assert property (p_req_ack_body);

    a_immediate: assert (ready);
    c_immediate: cover (done);

    comb_check: always_comb begin
      if (valid) begin
        a_comb: assert (ready);
      end
    end
    """
    blocks = collect_assertion_blocks(raw)
    assert [b.label for b in blocks] == ["a_prop", "a_immediate", "c_immediate", "comb_check"]
    assert "property p_req_ack_body" in blocks[0].text
    assert "assert property (p_req_ack_body)" in blocks[0].text
    assert "always_comb" in blocks[-1].text

    items = extract_assertions(raw)
    assert [item.label for item in items] == ["a_prop", "a_immediate", "c_immediate", "comb_check"]
    assert items[0].classification == AssertionClassification.NEEDS_FORMAL_VALIDATION
    assert items[1].classification == AssertionClassification.SUPPORTED_CANDIDATE
    assert items[2].classification == AssertionClassification.SUPPORTED_CANDIDATE
    assert items[3].classification == AssertionClassification.SUPPORTED_CANDIDATE

    stats = syntax_correctness(items)
    assert stats["initial_blocks"] == 4
    assert stats["valid_blocks"] == 4
    assert stats["syntax_correctness"] == 1.0


def test_phase3_cleanup_writes_run_local_copy_only(tmp_path: Path):
    source = tmp_path / "assertions.sv"
    source.write_text(
        "a_ok: assert (req);\n\n"
        "this is not valid sva\n",
        encoding="utf-8",
    )
    original = source.read_text(encoding="utf-8")
    cleaned = tmp_path / "run_local_assertions.sv"

    report = cleanup_assertion_file(source, cleaned)

    assert source.read_text(encoding="utf-8") == original
    assert cleaned.read_text(encoding="utf-8").strip() == "a_ok: assert (req);"
    assert report["initial_blocks"] == 1
    assert report["valid_blocks"] == 1


def test_empty_and_invalid_output_classification():
    assert extract_assertions("")[0].classification == AssertionClassification.EMPTY_OUTPUT
    assert extract_assertions("no assertions here")[0].classification == AssertionClassification.INVALID_OUTPUT


def test_secret_redaction():
    redacted = redacted_mapping({"api_key": "abc", "nested": {"token": "def"}, "model": "m"})
    assert redacted["api_key"] == "<redacted>"
    assert redacted["nested"]["token"] == "<redacted>"
    assert redacted["model"] == "m"


@pytest.mark.parametrize("value", [None, "", "0"])
def test_cloud_llm_gate_blocks_when_not_explicitly_enabled(monkeypatch: pytest.MonkeyPatch, value: str | None):
    if value is None:
        monkeypatch.delenv("SABLE_ENABLE_CLOUD_LLM", raising=False)
    else:
        monkeypatch.setenv("SABLE_ENABLE_CLOUD_LLM", value)
    assert env_flag("SABLE_ENABLE_CLOUD_LLM") is False


def test_cloud_llm_gate_permits_explicit_opt_in(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SABLE_ENABLE_CLOUD_LLM", "1")
    assert env_flag("SABLE_ENABLE_CLOUD_LLM") is True


def test_generate_fails_closed_before_api_key_or_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = make_checkout(tmp_path)
    design = get_design("assertllm2/cat/tiny", root)
    ws = create_isolated_workspace(design, output_root=tmp_path / "runs")
    monkeypatch.setenv("SABLE_ENABLE_CLOUD_LLM", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    result = generate_assertions(ws, output_dir=tmp_path / "blocked_gen")

    assert not result.succeeded
    assert result.blocked_reason == "ANTHROPIC_API_KEY is not set"
    assert (tmp_path / "blocked_gen" / "generation_result.json").exists()


def test_raw_output_preservation_with_fake_transport(tmp_path: Path):
    root = make_checkout(tmp_path)
    design = get_design("assertllm2/cat/tiny", root)
    ws = create_isolated_workspace(design, output_root=tmp_path / "runs")

    def fake_transport(system: str, user: str, config: dict):
        assert "module tiny" not in user
        return {
            "provider": "fake",
            "model": "fake-model",
            "temperature": 0.0,
            "raw_http_body": '{"content":[{"text":"ok"}]}',
            "text": '{"assertions":[{"label":"a_req_ack","sva":"a_req_ack: assert property (@(posedge clk) req |-> ack);","citation":"spec.md"}]}',
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    result = generate_assertions(ws, output_dir=tmp_path / "gen", transport=fake_transport)
    assert result.succeeded
    assert result.raw_response_path and result.raw_response_path.exists()
    assert result.assertions_path and result.assertions_path.exists()
    assert result.candidates
    payload = json.loads((tmp_path / "gen" / "generation_result.json").read_text())
    assert payload["succeeded"] is True
    assert (tmp_path / "gen" / "assertions.cleaned.sv").exists()
    assert payload["metadata"]["syntax_cleanup"]["initial_blocks"] == 1


def test_cli_dry_run_behavior(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    root = make_checkout(tmp_path)
    rc = cli_main([
        "prepare-input",
        "--checkout", str(root),
        "--mode", "bug-prevention",
        "--design", "assertllm2/cat/tiny",
        "--dry-run",
        "--output-root", str(tmp_path / "runs"),
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["model_invoked"] is False
    assert out["exposed_files"][0]["filename"] == "spec.md"
    assert "When req" not in json.dumps(out)


def test_cli_capability_matrix(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    root = make_checkout(tmp_path)
    rc = cli_main(["capability-matrix", "--checkout", str(root), "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 1
    row = out["capability_matrix"][0]
    assert row["key"] == "assertllm2/cat/tiny"
    assert row["rtl_file_count"] == 2
    assert row["parameterized"] is True
    assert row["blackbox_modules"] == ["vendor_ip"]
