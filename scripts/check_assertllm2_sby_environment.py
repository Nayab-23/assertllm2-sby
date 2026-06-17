#!/usr/bin/env python3
from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assertllm2_sby.paths import PACKAGE_ROOT, resolve_assertllm2_checkout

EXPECTED_CHECKOUT_COMMIT = "f66fd20679dfff1de2f6d6e90bc4922d04e6ff62"
EXPECTED_VENV = PACKAGE_ROOT / ".venv-assertllm2-sby"


def run_capture(cmd: list[str], cwd: Path = ROOT) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        return 1, str(exc)
    return proc.returncode, proc.stdout.strip()


def add_check(checks: list[dict[str, Any]], name: str, status: str, detail: str) -> None:
    checks.append({"name": name, "status": status, "detail": detail})


def tool_version(tool: str, args: list[str]) -> tuple[str, str]:
    if not shutil.which(tool):
        return "WARN" if tool == "boolector" else "FAIL", "not found"
    code, out = run_capture([tool, *args])
    if code != 0:
        detail = out.splitlines()[0] if out else f"{tool} exited {code}"
        return "FAIL", detail
    return "PASS", out.splitlines()[0] if out else f"{tool} exited 0"


def config_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip().strip('"')
    return values


def main() -> int:
    checks: list[dict[str, Any]] = []

    py_version = ".".join(str(x) for x in sys.version_info[:3])
    add_check(
        checks,
        "python",
        "PASS" if sys.version_info[:2] == (3, 13) else "FAIL",
        f"{sys.executable} ({py_version})",
    )
    add_check(
        checks,
        "virtualenv",
        "PASS" if Path(sys.prefix).resolve() == EXPECTED_VENV.resolve() else "FAIL",
        sys.prefix,
    )

    checkout = resolve_assertllm2_checkout()
    code, commit = run_capture(["git", "rev-parse", "HEAD"], checkout)
    add_check(
        checks,
        "assertllm2_commit",
        "PASS" if code == 0 and commit == EXPECTED_CHECKOUT_COMMIT else "FAIL",
        commit or "unavailable",
    )
    code, status = run_capture(["git", "status", "--short"], checkout)
    add_check(
        checks,
        "assertllm2_clean",
        "PASS" if code == 0 and not status else "FAIL",
        status or "clean",
    )

    cfg_path = PACKAGE_ROOT / "config" / "assertllm2_sby.yaml"
    cfg = config_values(cfg_path)
    secret_keys = [k for k in cfg if "api_key" in k.lower() and not k.endswith("_env")]
    add_check(checks, "config_present", "PASS" if cfg_path.is_file() else "FAIL", str(cfg_path))
    add_check(
        checks,
        "config_no_secrets",
        "PASS" if not secret_keys and "ANTHROPIC_API_KEY" in cfg.values() else "FAIL",
        "no secret values stored",
    )

    try:
        importlib.import_module("dotenv")
        add_check(checks, "python_dotenv", "PASS", "python-dotenv available")
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        add_check(checks, "python_dotenv", "FAIL", str(exc))

    try:
        importlib.import_module("requests")
        add_check(checks, "python_requests", "PASS", "requests available")
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        add_check(checks, "python_requests", "FAIL", str(exc))

    try:
        importlib.import_module("assertllm2_sby.generator")
        importlib.import_module("assertllm2_sby.sby_backend")
        from assertllm2_sby.dataset import discover_designs
        from assertllm2_sby.manifest import env_flag

        add_check(checks, "adapter_import", "PASS", "assertllm2_sby")
        designs = discover_designs(checkout)
        add_check(
            checks,
            "design_discovery",
            "PASS" if len(designs) == 83 else "FAIL",
            f"{len(designs)} designs",
        )
        old = os.environ.get("SABLE_ENABLE_CLOUD_LLM")
        try:
            gate_results = {}
            for value in (None, "", "0", "1"):
                if value is None:
                    os.environ.pop("SABLE_ENABLE_CLOUD_LLM", None)
                    label = "missing"
                else:
                    os.environ["SABLE_ENABLE_CLOUD_LLM"] = value
                    label = value or "empty"
                gate_results[label] = env_flag("SABLE_ENABLE_CLOUD_LLM")
        finally:
            if old is None:
                os.environ.pop("SABLE_ENABLE_CLOUD_LLM", None)
            else:
                os.environ["SABLE_ENABLE_CLOUD_LLM"] = old
        expected_gate = {
            "missing": False,
            "empty": False,
            "0": False,
            "1": True,
        }
        add_check(
            checks,
            "cloud_llm_gate",
            "PASS" if gate_results == expected_gate else "FAIL",
            json.dumps(gate_results, sort_keys=True),
        )
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        add_check(checks, "adapter_import", "FAIL", str(exc))

    for name, args in (
        ("yosys", ["-V"]),
        ("sby", ["--version"]),
        ("z3", ["--version"]),
        ("boolector", ["--version"]),
    ):
        status, detail = tool_version(name, args)
        add_check(checks, name, status, detail)

    add_check(
        checks,
        "formal_backend",
        "PASS" if cfg.get("implemented") == "true" else "FAIL",
        f"formal.implemented={cfg.get('implemented', 'missing')}",
    )

    failures = [c for c in checks if c["status"] == "FAIL"]
    print(json.dumps({"status": "PASS" if not failures else "FAIL", "checks": checks}, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
