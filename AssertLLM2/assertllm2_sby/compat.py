from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from .coverage import coverage_summary, write_coverage_reports
from .formal_types import SourcePlan
from .manifest import read_json, write_json

COMPAT_STATUSES = {"proven", "cex", "undetermined", "unprocessed", "error"}


def compatibility_status_from_golden(golden_outcome: str, result: dict[str, Any] | None) -> str:
    if result and isinstance(result.get("details"), dict):
        status = result["details"].get("compatibility_status")
        if status in COMPAT_STATUSES:
            return str(status)
    if golden_outcome == "GOLDEN_PROVEN":
        return "proven"
    if golden_outcome == "GOLDEN_COUNTEREXAMPLE":
        return "cex"
    if golden_outcome in {"GOLDEN_UNSUPPORTED"}:
        return "unprocessed"
    if golden_outcome in {
        "GOLDEN_ERROR",
        "GOLDEN_ELABORATION_ERROR",
        "GOLDEN_INFRASTRUCTURE_ERROR",
    }:
        return "error"
    return "undetermined"


def _trace_files(result: dict[str, Any] | None) -> list[str]:
    if not result:
        return []
    return [str(path) for path in result.get("trace_files") or []]


def _signal_name(row: dict[str, Any]) -> str | None:
    label = row.get("label")
    if isinstance(label, str) and label.strip():
        return label.strip()
    text = str(row.get("original_text") or "")
    match = re.search(r"\b(?:assert|assume|cover)\s+property\s*\([^)]*?([A-Za-z_]\w*)\s*(?:\)|[|&=!<>])", text)
    return match.group(1) if match else None


def fpv_rows(assertion_rows: list[dict[str, Any]], *, top_module: str | None) -> list[dict[str, Any]]:
    hierarchy = top_module or "top"
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(assertion_rows, start=1):
        result = row.get("golden_result")
        status = compatibility_status_from_golden(str(row.get("golden_outcome") or ""), result)
        assertion_id = str(row.get("assertion_id") or f"assertion_{index}")
        traces = _trace_files(result)
        removed_from_scoreable = status == "cex"
        unsupported = status in {"unprocessed", "error"} or not row.get("lowered", {}).get("supported", False)
        rows.append({
            "index": index,
            "hierarchy": hierarchy,
            "assertion_id": assertion_id,
            "assertion_name": assertion_id,
            "qualified_name": f"{hierarchy}.{assertion_id}",
            "signal_name": _signal_name(row),
            "status": status,
            "golden_outcome": row.get("golden_outcome"),
            "classification": row.get("classification"),
            "supported": bool(row.get("lowered", {}).get("supported", False)),
            "lowered_kind": row.get("lowered", {}).get("kind"),
            "trace_files": traces,
            "trace_path": traces[0] if traces else None,
            "scoreable": not removed_from_scoreable and not unsupported and row.get("lowered", {}).get("kind") == "assert",
            "removed_from_scoreable": removed_from_scoreable,
            "removal_reason": "golden_counterexample" if removed_from_scoreable else None,
            "unsupported_reasons": row.get("lowered", {}).get("reasons") or [],
        })
    return rows


def coverage_placeholder(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config or {}
    common = {
        "toolchain": "Yosys/SymbiYosys",
        "is_jaspergold_coverage": False,
        "official_jaspergold_result": False,
        "depth_limits": {
            "bmc_depth": cfg.get("bmc_depth"),
            "prove_depth": cfg.get("prove_depth"),
            "cover_depth": cfg.get("cover_depth"),
            "timeout_seconds": cfg.get("timeout_seconds"),
        },
    }
    return {
        "formal": {
            **common,
            "method": "bounded_open_source_formal_summary",
            "covered": None,
            "total": None,
            "percentage": None,
            "unsupported_reason": "JasperGold formal coverage is not reproduced by AssertLLM2-SBY",
        },
        "stimuli": {
            **common,
            "method": "bounded_interface_activity_placeholder",
            "covered": None,
            "total": None,
            "percentage": None,
            "unsupported_reason": "stimuli coverage approximation is not implemented yet",
        },
        "checker_coi": {
            **common,
            "method": "static_coi_placeholder",
            "covered": None,
            "total": None,
            "percentage": None,
            "unsupported_reason": "checker COI extraction is scheduled for Phase 8",
        },
        "checker_proof": {
            **common,
            "method": "unsupported_open_source_proof_core",
            "covered": None,
            "total": None,
            "percentage": None,
            "unsupported_reason": "JasperGold proof-core coverage has no current open-source substitute",
        },
    }


def write_design_fpv_report(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# AssertLLM2-SBY FPV compatibility report",
        "# official_jaspergold_result: false",
    ]
    for row in rows:
        lines.append(f"[{row['index']}] {row['qualified_name']} {row['status']}")
        if row.get("signal_name"):
            lines.append(f"# signal: {row['signal_name']}")
        if row.get("trace_path"):
            lines.append(f"# trace: {row['trace_path']}")
        if row.get("removed_from_scoreable"):
            lines.append(f"# removed_from_scoreable: {row['removal_reason']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_design_fpv_report(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    line_re = re.compile(r"^\[(?P<index>\d+)\]\s+(?P<qualified>\S+)\s+(?P<status>\w+)\s*$")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        match = line_re.match(line)
        if match:
            qualified = match.group("qualified")
            assertion_name = qualified.rsplit(".", 1)[-1]
            rows.append({
                "index": int(match.group("index")),
                "qualified_name": qualified,
                "assertion_name": assertion_name,
                "signal_name": assertion_name,
                "status": match.group("status"),
                "trace_path": None,
            })
            continue
        if not rows or not line.startswith("#"):
            continue
        key, sep, value = line[1:].strip().partition(":")
        if sep and key == "signal":
            rows[-1]["signal_name"] = value.strip() or rows[-1]["signal_name"]
        elif sep and key == "trace":
            rows[-1]["trace_path"] = value.strip() or None
    return rows


def _load_existing_meta(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    payload = read_json(path)
    rows = payload.get("assertions") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}
    return {str(row.get("assertion_id")): dict(row) for row in rows if isinstance(row, dict)}


def write_compatibility_artifacts(
    outdir: Path,
    *,
    design_key: str,
    top_module: str | None,
    generation: dict[str, Any],
    assertion_rows: list[dict[str, Any]],
    config: dict[str, Any],
    source_assertions_path: Path | None,
    source_meta_path: Path | None = None,
    source_plan: SourcePlan | None = None,
) -> dict[str, Any]:
    outdir.mkdir(parents=True, exist_ok=True)
    if source_assertions_path and source_assertions_path.is_file():
        shutil.copy2(source_assertions_path, outdir / "assertions.sv")
    else:
        (outdir / "assertions.sv").write_text(
            "\n\n".join(str(row.get("original_text") or "") for row in assertion_rows).strip() + "\n",
            encoding="utf-8",
        )

    rows = fpv_rows(assertion_rows, top_module=top_module)
    report_path = outdir / "report" / "design.fpv.rpt"
    write_design_fpv_report(report_path, rows)

    cleanup = generation.get("syntax_cleanup", {})
    initial_raw = cleanup.get("initial_blocks") if isinstance(cleanup, dict) else None
    valid_raw = cleanup.get("valid_blocks") if isinstance(cleanup, dict) else None
    initial_blocks = int(initial_raw) if initial_raw is not None else len(assertion_rows)
    valid_blocks = int(valid_raw) if valid_raw is not None else len(assertion_rows)
    supported_count = sum(1 for row in rows if row["supported"])
    unsupported_rows = [row for row in rows if row["status"] in {"unprocessed", "error"} or not row["supported"]]
    removed_rows = [row for row in rows if row["removed_from_scoreable"]]
    coverage_input_rows = [
        {
            **row,
            "original_text": assertion_rows[idx].get("original_text") if idx < len(assertion_rows) else "",
        }
        for idx, row in enumerate(rows)
    ]
    coverage = coverage_summary(source_plan, coverage_input_rows, config)
    coverage_report_paths = write_coverage_reports(outdir, coverage)
    baseline = {
        "schema_version": "1.0",
        "adapter": "AssertLLM2-SBY",
        "design_key": design_key,
        "official_jaspergold_result": False,
        "syntax_correctness": (valid_blocks / initial_blocks) if initial_blocks else 0.0,
        "initial_assertion_blocks": initial_blocks,
        "total_assertions": len(rows),
        "valid_assertions": valid_blocks,
        "supported_assertions": supported_count,
        "removed_assertions": len(removed_rows),
        "unsupported_assertions": len(unsupported_rows),
        "scoreable_assertions": sum(1 for row in rows if row["scoreable"]),
        "removed_or_unsupported_assertions": [
            {
                "assertion_id": row["assertion_id"],
                "status": row["status"],
                "removed_from_scoreable": row["removed_from_scoreable"],
                "reason": row["removal_reason"] or ",".join(row["unsupported_reasons"]) or row["status"],
            }
            for row in rows
            if row in removed_rows or row in unsupported_rows
        ],
        "fpv_rows": rows,
        "coverage": coverage,
        "coverage_report_paths": coverage_report_paths,
        "trace_links": [
            {"assertion_id": row["assertion_id"], "trace_path": trace}
            for row in rows
            for trace in row["trace_files"]
        ],
        "report_path": str(report_path),
    }
    write_json(outdir / "baseline_eval.json", baseline)

    existing_meta = _load_existing_meta(source_meta_path) if source_meta_path else {}
    meta_rows = []
    for row in rows:
        meta = dict(existing_meta.get(row["assertion_id"], {}))
        meta.update({
            "assertion_id": row["assertion_id"],
            "label": meta.get("label") or row["signal_name"],
            "compatibility_status": row["status"],
            "golden_outcome": row["golden_outcome"],
            "scoreable": row["scoreable"],
            "removed_from_scoreable": row["removed_from_scoreable"],
            "removal_reason": row["removal_reason"],
            "trace_files": row["trace_files"],
            "official_jaspergold_result": False,
        })
        meta_rows.append(meta)
    write_json(outdir / "assertions_meta.json", {
        "schema_version": "1.0",
        "adapter": "AssertLLM2-SBY",
        "design_key": design_key,
        "assertions": meta_rows,
        "official_jaspergold_result": False,
    })
    return baseline
