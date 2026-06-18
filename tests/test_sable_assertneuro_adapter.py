from __future__ import annotations

from assertllm2_sby.assertion_lowering import classify_and_lower_assertion
import assertllm2_sby.sable_assertneuro_adapter as sable_adapter
from assertllm2_sby.sable_assertneuro_adapter import (
    _access_hit_assertion,
    _memory_request_assertion,
    _stable_assertion,
    infer,
)


def test_stable_assertion_exports_supported_concurrent_sva():
    row = _stable_assertion(
        {
            "prefix": "p_",
            "valid": "p_srdy",
            "ready": "p_drdy",
            "payload": ["p_data", "p_txid"],
        },
        clock="clk",
        reset_name="reset",
        active_low=False,
        signal_map={},
    )

    lowered = classify_and_lower_assertion(row["assertion_id"], row["sva"])
    assert lowered.supported is True
    assert lowered.kind == "assert"


def test_access_hit_assertion_exports_supported_concurrent_sva():
    row = _access_hit_assertion(
        {
            "response_prefix": "rsp_",
            "request_valid": "req_valid_i",
            "response_valid": "hit_o",
        },
        clock="clk",
        reset_name="rst_n",
        active_low=True,
        signal_map={},
    )

    lowered = classify_and_lower_assertion(row["assertion_id"], row["sva"])
    assert lowered.supported is True
    assert lowered.kind == "assert"


def test_memory_request_assertion_skips_without_stall_signal():
    row = _memory_request_assertion(
        {
            "prefix": "mem_",
            "valid": "mem_valid_o",
            "stall": None,
        },
        clock="clk",
        reset_name="reset",
        active_low=False,
        signal_map={},
    )

    assert row is None


def test_infer_returns_empty_response_when_sable_has_no_supported_channels(tmp_path, monkeypatch):
    rtl = tmp_path / "tiny.v"
    rtl.write_text("module tiny(input clk, input rst); endmodule\n", encoding="utf-8")

    class FakeProjectContext:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeSable:
        def project_context(self, config):
            return FakeProjectContext()

        def probe_ports(self, primary_rtl, top_module, probe_dir, *, sv_frontend, blackbox_sink):
            return {"clk": {}, "rst": {}}, "probe ok"

        def build_signal_emit_map(self, ports):
            return {}

        def analyze_file(self, primary_rtl, top_module, depth, preserve_modules, *, preserve_on_failure, sv_frontend, project_config):
            return {
                "status_typed": "SKIPPED",
                "reason": "no_supported_channels",
                "channels": [],
                "access_hit_pairs": [],
                "memory_request_interfaces": [],
                "pairs": [],
                "deferred_pairs": [],
            }

        def is_active_low_reset(self, reset_name):
            return False

    monkeypatch.setattr(sable_adapter, "_load_sable_modules", lambda: (FakeSable(), tmp_path))

    response = infer(
        {
            "design_key": "assertllm2/test/tiny",
            "design_name": "tiny",
            "mode": "rtl-contract",
            "top_module": "tiny",
            "rtl_files": [str(rtl)],
            "include_dirs": [],
            "clocks": ["clk"],
            "reset": "rst",
            "defines": [],
            "parameters": {},
            "blackbox_modules": [],
        },
        tmp_path / "out",
    )

    assert response["assertions"] == []
    assert response["engine"]["export_status"] == "no_assertions"
    assert response["engine"]["export_report"]["analysis_reason"] == "no_supported_channels"
