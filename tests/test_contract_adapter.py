from __future__ import annotations

import json
import sys
from pathlib import Path

from assertllm2_sby.cli import main as cli_main
from assertllm2_sby.contract_adapter import contract_request, generate_contract_assertions
from assertllm2_sby.models import DesignRecord, GenerationMode


def make_design(tmp_path: Path) -> DesignRecord:
    d = tmp_path / "design"
    d.mkdir()
    rtl = d / "tiny.v"
    rtl.write_text(
        "module tiny #(parameter WIDTH = 8)(input clk, input req, output ack);\n"
        "  assign ack = req;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    spec = d / "spec.md"
    spec.write_text("# Tiny\n", encoding="utf-8")
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
        mutation_files=(),
        top_module="tiny",
        clocks=("clk",),
        reset=None,
        source_language="verilog",
        defines=("FORMAL",),
        parameters={"WIDTH": "8"},
        blackbox_modules=("vendor_ip",),
        identity={},
    )


def write_fake_adapter(tmp_path: Path) -> str:
    module = tmp_path / "fake_contract_tool.py"
    module.write_text(
        "def infer(request, output_dir):\n"
        "    assert request['design_key'] == 'assertllm2/test/tiny'\n"
        "    assert request['top_module'] == 'tiny'\n"
        "    assert request['rtl_files']\n"
        "    assert request['defines'] == ['FORMAL']\n"
        "    assert request['parameters'] == {'WIDTH': '8'}\n"
        "    assert request['blackbox_modules'] == ['vendor_ip']\n"
        "    return {\n"
        "        'generator_version': 'fake-contract-1',\n"
        "        'assertions': [{\n"
        "            'label': 'req_ack_same_cycle',\n"
        "            'sva': 'req_ack_same_cycle: assert property (@(posedge clk) req |-> ack);',\n"
        "            'contract_family': 'handshake',\n"
        "            'target': 'req/ack',\n"
        "            'source_locations': [{'file': request['rtl_files'][0], 'line': 1}],\n"
        "        }],\n"
        "    }\n",
        encoding="utf-8",
    )
    if str(tmp_path) not in sys.path:
        sys.path.insert(0, str(tmp_path))
    return "fake_contract_tool:infer"


def make_checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    cfg_dir = root / "configs"
    design = root / "designs" / "TEST" / "tiny"
    cfg_dir.mkdir(parents=True)
    design.mkdir(parents=True)
    (design / "spec.md").write_text("# Tiny\n", encoding="utf-8")
    (design / "tiny.v").write_text("module tiny(input clk, input req, output ack); endmodule\n", encoding="utf-8")
    payload = {
        "assertllm2/test/tiny": {
            "spec_file": ["../designs/TEST/tiny/spec.md"],
            "rtl": {
                "filelist": ["../designs/TEST/tiny/tiny.v"],
                "top_module": "tiny",
                "defines": ["FORMAL"],
            },
            "parameters": {"WIDTH": "8"},
            "blackbox_modules": ["vendor_ip"],
            "clock_reset": {"clocks": [{"signal": "clk"}]},
        }
    }
    (cfg_dir / "assertllm2_design_configs.json").write_text(json.dumps(payload), encoding="utf-8")
    return root


def test_contract_inference_adapter_outputs_assertions_and_metadata(tmp_path: Path):
    design = make_design(tmp_path)
    entrypoint = write_fake_adapter(tmp_path)

    result = generate_contract_assertions(
        design,
        mode=GenerationMode.RTL_CONTRACT,
        output_dir=tmp_path / "out",
        config={"python_entrypoint": entrypoint},
    )

    assert result.succeeded
    assert result.assertions_path and result.assertions_path.name == "assertions.sv"
    assert (tmp_path / "out" / "assertions_meta.json").exists()
    assert (tmp_path / "out" / "assertions.cleaned.sv").exists()
    assert (tmp_path / "out" / "source_visibility_manifest.json").exists()
    assert result.metadata["syntax_cleanup"]["initial_blocks"] == 1
    meta = json.loads((tmp_path / "out" / "assertions_meta.json").read_text())
    assert meta["assertions"][0]["visibility_mode"] == "rtl-contract"
    assert meta["assertions"][0]["contract_family"] == "handshake"
    assert meta["assertions"][0]["assertion_id"] == result.candidates[0].assertion_id
    visibility = json.loads((tmp_path / "out" / "source_visibility_manifest.json").read_text())
    assert visibility["rtl_visible_to_generator"] is True
    assert any(row["role"] == "rtl" for row in visibility["visible_files"])


def test_bug_hunting_request_exposes_buggy_rtl_not_clean_rtl(tmp_path: Path):
    design = make_design(tmp_path)
    buggy = design.design_dir / "buggy_artifacts" / "merged_buggy_rtl"
    buggy.mkdir(parents=True)
    buggy_rtl = buggy / "tiny.v"
    buggy_rtl.write_text(
        "module tiny #(parameter WIDTH = 8)(input clk, input req, output ack);\n"
        "  assign ack = ~req;\n"
        "endmodule\n",
        encoding="utf-8",
    )

    request = contract_request(design, GenerationMode.BUG_HUNTING)
    result = generate_contract_assertions(
        design,
        mode=GenerationMode.BUG_HUNTING,
        output_dir=tmp_path / "bug_hunting",
        config={"python_entrypoint": write_fake_adapter(tmp_path)},
    )
    visibility = json.loads((tmp_path / "bug_hunting" / "source_visibility_manifest.json").read_text())

    assert result.succeeded
    assert request["clean_rtl_visible_to_generator"] is False
    assert request["rtl_files"] == [str(buggy_rtl)]
    assert request["buggy_rtl_files"] == [str(buggy_rtl)]
    assert visibility["clean_rtl_visible_to_generator"] is False
    assert visibility["buggy_rtl_visible_to_generator"] is True
    assert not any(row["role"] == "rtl" for row in visibility["visible_files"])
    assert [row["path"] for row in visibility["visible_buggy_rtl_files"]] == [str(buggy_rtl.resolve())]


def test_cli_generate_contract_inference(tmp_path: Path, capsys):
    checkout = make_checkout(tmp_path)
    entrypoint = write_fake_adapter(tmp_path)

    rc = cli_main([
        "generate",
        "--checkout", str(checkout),
        "--method", "contract-inference",
        "--mode", "rtl-contract",
        "--design", "assertllm2/test/tiny",
        "--output-root", str(tmp_path / "gen"),
        "--contract-python-entrypoint", entrypoint,
    ])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["succeeded"] is True
    assert Path(out["assertions_path"]).name == "assertions.sv"


def test_contract_internal_export_keeps_internal_signal_assertions():
    from assertllm2_sby import contract_internal_adapter as adapter

    class FakeOracle:
        _ACC_RE = __import__("re").compile(r"([A-Za-z_]\w*)\s*<=\s*\1\s*\+\s*([A-Za-z_0-9']+)\s*;")

        @staticmethod
        def is_active_low_reset(_name: str | None) -> bool:
            return False

        @staticmethod
        def _module_body(text: str, _module_name: str) -> str:
            return text

        @staticmethod
        def _fsm_encoding_set_expr(reg: str, encodings: dict[int, str]) -> str:
            return "(" + " || ".join(f"{reg}=={value}" for value in sorted(encodings)) + ")"

    normalized = {
        "primary_rtl": "/tmp/fake_ft816.v",
        "top_module": "FT816Float",
        "clock": "clk",
        "reset": "rst",
    }
    record = {
        "arithmetic": {
            "properties": {
                "accumulator_integrity": {"confirmed": True},
            }
        },
        "fsm": {
            "interface": {"reg": "state", "encodings": {"0": "RESET", "1": "RUN"}, "init_state": 0},
            "properties": {
                "legal_transition": {"confirmed": True},
                "reset_correctness": {"confirmed": True},
            },
        },
        "blackboxed_modules": [],
    }
    source = Path(normalized["primary_rtl"])
    source.write_text(
        "module FT816Float(input clk, input rst);\n"
        "  reg [7:0] state;\n"
        "  reg [7:0] cyccnt;\n"
        "  always @(posedge clk) cyccnt <= cyccnt + 1;\n"
        "endmodule\n",
        encoding="utf-8",
    )
    try:
        assertions, report = adapter._export_assertions(FakeOracle, normalized, record)
    finally:
        source.unlink(missing_ok=True)

    assert [row["label"] for row in assertions] == [
        "accumulator_integrity",
        "legal_transition",
        "reset_correctness",
    ]
    assert report["skipped"] == []
    assert "$fell" not in assertions[2]["sva"]
