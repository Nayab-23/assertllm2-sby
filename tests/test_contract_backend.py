from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

from assertllm2_sby.contract_adapter import generate_contract_assertions
from assertllm2_sby.models import DesignRecord, GenerationMode


def make_request(tmp_path: Path) -> dict:
    rtl = tmp_path / "tiny.v"
    rtl.write_text("module tiny(input clk, input req, output ack); assign ack = req; endmodule\n", encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text("# Tiny\n", encoding="utf-8")
    return {
        "schema_version": "1.0",
        "adapter": "AssertLLM2-SBY",
        "design_key": "assertllm2/test/tiny",
        "category": "TEST",
        "design_name": "tiny",
        "top_module": "tiny",
        "design_dir": str(tmp_path),
        "working_directory": str(tmp_path),
        "spec_files": [str(spec)],
        "raw_spec_files": [],
        "rtl_files": [str(rtl)],
        "include_dirs": [str(tmp_path / "include")],
        "support_files": [],
        "defines": ["FORMAL"],
        "parameters": {"WIDTH": "8"},
        "clocks": ["clk"],
        "reset": "rst_n",
        "blackbox_modules": ["vendor_ip"],
        "mode": "rtl-contract",
        "buggy_rtl_files": [],
        "clean_rtl_visible_to_generator": True,
        "merged_buggy_rtl_dirs": [],
    }


def make_design(tmp_path: Path) -> DesignRecord:
    request = make_request(tmp_path)
    return DesignRecord(
        key=request["design_key"],
        category=request["category"],
        design_name=request["design_name"],
        design_dir=tmp_path,
        spec_md=Path(request["spec_files"][0]),
        raw_specs=(),
        rtl_files=(Path(request["rtl_files"][0]),),
        include_dirs=(Path(request["include_dirs"][0]),),
        support_files=(),
        mutation_files=(),
        top_module=request["top_module"],
        clocks=("clk",),
        reset="rst_n",
        source_language="verilog",
        defines=("FORMAL",),
        parameters={"WIDTH": "8"},
        blackbox_modules=("vendor_ip",),
        identity={},
    )


def write_engine(tmp_path: Path, module_name: str, body: str) -> str:
    module_path = tmp_path / f"{module_name}.py"
    module_path.write_text(body, encoding="utf-8")
    if str(tmp_path) not in sys.path:
        sys.path.insert(0, str(tmp_path))
    sys.modules.pop(module_name, None)
    importlib.invalidate_caches()
    return f"{module_name}:run"


def test_contract_backend_imports():
    module = importlib.import_module("assertllm2_sby.contract_backend")

    assert callable(module.infer)
    assert callable(module.generate_assertions)


def test_contract_backend_successful_inference_and_request_parsing(tmp_path: Path, monkeypatch):
    entrypoint = write_engine(
        tmp_path,
        "contract_success_engine",
        "def run(request, output_dir):\n"
        "    assert request['top_module'] == 'tiny'\n"
        "    assert request['include_dirs']\n"
        "    assert request['defines'] == ['FORMAL']\n"
        "    assert request['parameters'] == {'WIDTH': '8'}\n"
        "    assert request['clocks'] == ['clk']\n"
        "    assert request['reset'] == 'rst_n'\n"
        "    assert request['spec_files']\n"
        "    assert request['working_directory']\n"
        "    return {\n"
        "        'version': 'contract-test-1',\n"
        "        'model': 'local contract test engine',\n"
        "        'stats': {'signals_seen': 3},\n"
        "        'assertions': [{\n"
        "            'id': 'ack_tracks_req',\n"
        "            'label': 'ack_tracks_req',\n"
        "            'sva': 'ack_tracks_req: assert property (@(posedge clk) req |-> ack);',\n"
        "            'family': 'handshake',\n"
        "            'target': 'req/ack',\n"
        "            'source_locations': [{'file': request['rtl_files'][0], 'line': 1}],\n"
        "        }],\n"
        "    }\n",
    )
    monkeypatch.setenv("ASSERTLLM2_SBY_CONTRACT_ENGINE_ENTRYPOINT", entrypoint)

    from assertllm2_sby.contract_backend import infer

    response = infer(make_request(tmp_path), tmp_path / "out")

    assert response["model"] == "local contract test engine"
    assert response["generator_version"] == "contract-test-1"
    assert response["statistics"]["signals_seen"] == 3
    assert response["assertions"][0]["sva"].startswith("ack_tracks_req: assert property")
    assert (tmp_path / "out" / "contract_request.json").exists()
    assert (tmp_path / "out" / "contract_assertions.json").exists()
    assert (tmp_path / "out" / "contract_stats.json").exists()


def test_contract_backend_empty_assertions(tmp_path: Path, monkeypatch):
    entrypoint = write_engine(
        tmp_path,
        "contract_empty_engine",
        "def run(request, output_dir):\n"
        "    return {'version': 'contract-empty', 'assertions': [], 'stats': {'reason': 'no_contracts'}}\n",
    )
    monkeypatch.setenv("ASSERTLLM2_SBY_CONTRACT_ENGINE_ENTRYPOINT", entrypoint)

    from assertllm2_sby.contract_backend import infer

    response = infer(make_request(tmp_path), tmp_path / "out")

    assert response["assertions"] == []
    assert response["statistics"]["assertion_count"] == 0
    assert response["statistics"]["reason"] == "no_contracts"


def test_contract_backend_exception_handling(tmp_path: Path, monkeypatch):
    entrypoint = write_engine(
        tmp_path,
        "contract_exception_engine",
        "def run(request, output_dir):\n"
        "    raise RuntimeError('contract inference failed')\n",
    )
    monkeypatch.setenv("ASSERTLLM2_SBY_CONTRACT_ENGINE_ENTRYPOINT", entrypoint)

    from assertllm2_sby.contract_backend import infer

    response = infer(make_request(tmp_path), tmp_path / "out")

    assert response["assertions"] == []
    assert response["error"]["type"] == "RuntimeError"
    assert "contract inference failed" in response["error"]["message"]
    assert (tmp_path / "out" / "contract_error.json").exists()


def test_contract_backend_malformed_request(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ASSERTLLM2_SBY_CONTRACT_ENGINE_ENTRYPOINT", "missing.module:run")

    from assertllm2_sby.contract_backend import infer

    response = infer({"design_key": "missing_rtl"}, tmp_path / "out")

    assert response["assertions"] == []
    assert response["error"]["type"] == "ContractBackendError"
    assert "missing required fields" in response["error"]["message"]


def test_contract_backend_backend_integration(tmp_path: Path, monkeypatch):
    entrypoint = write_engine(
        tmp_path,
        "contract_backend_engine",
        "def run(request, output_dir):\n"
        "    return {\n"
        "        'version': 'contract-backend',\n"
        "        'assertions': ['ack_tracks_req: assert property (@(posedge clk) req |-> ack);'],\n"
        "    }\n",
    )
    monkeypatch.setenv("ASSERTLLM2_SBY_CONTRACT_ENGINE_ENTRYPOINT", entrypoint)

    result = generate_contract_assertions(
        make_design(tmp_path),
        mode=GenerationMode.RTL_CONTRACT,
        output_dir=tmp_path / "gen",
        config={"python_entrypoint": "assertllm2_sby.contract_backend:infer"},
    )

    assert result.succeeded
    assert result.candidates
    assert (tmp_path / "gen" / "contract_request.json").exists()
    raw = json.loads((tmp_path / "gen" / "contract_response.json").read_text(encoding="utf-8"))
    assert raw["adapter"] == "AssertLLM2-SBY Contract Backend"
