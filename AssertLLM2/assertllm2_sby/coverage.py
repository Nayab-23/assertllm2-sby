from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .formal_types import SourcePlan
from .source_plan import parse_ports

IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_$]*\b")
KEYWORDS = {
    "always", "and", "assert", "assume", "begin", "cover", "disable", "else",
    "end", "endproperty", "if", "iff", "not", "or", "posedge", "property",
    "negedge", "wire", "reg", "logic", "input", "output", "inout",
}
SYSTEM_FUNCTIONS = {"past", "rose", "fell", "changed", "stable", "onehot", "onehot0"}


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//.*", "", text)


def assertion_identifiers(text: str) -> set[str]:
    cleaned = _strip_comments(text)
    out = set()
    for ident in IDENT_RE.findall(cleaned):
        if ident in KEYWORDS:
            continue
        if ident.startswith("$") or ident in SYSTEM_FUNCTIONS:
            continue
        if re.fullmatch(r"[0-9]+", ident):
            continue
        out.add(ident)
    return out


def _assignment_edges(plan: SourcePlan) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    assign_re = re.compile(r"\bassign\s+(?P<lhs>[A-Za-z_][A-Za-z0-9_$]*)\s*=\s*(?P<rhs>[^;]+);")
    nb_re = re.compile(r"(?P<lhs>[A-Za-z_][A-Za-z0-9_$]*)\s*(?:<=|=)\s*(?P<rhs>[^;]+);")
    for path in plan.rtl_files:
        if path.suffix.lower() not in {".v", ".sv", ".vh", ".svh"}:
            continue
        text = _strip_comments(path.read_text(encoding="utf-8", errors="ignore"))
        for match in assign_re.finditer(text):
            lhs = match.group("lhs")
            graph.setdefault(lhs, set()).update(assertion_identifiers(match.group("rhs")))
        for match in nb_re.finditer(text):
            lhs = match.group("lhs")
            rhs_ids = assertion_identifiers(match.group("rhs"))
            if lhs not in {"if", "for", "while", "case"} and rhs_ids:
                graph.setdefault(lhs, set()).update(rhs_ids)
    return graph


def _reachable(seed: set[str], graph: dict[str, set[str]], limit: int = 64) -> set[str]:
    seen = set(seed)
    frontier = list(seed)
    steps = 0
    while frontier and steps < limit:
        current = frontier.pop()
        steps += 1
        for nxt in graph.get(current, set()):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    return seen


def checker_coi_summary(plan: SourcePlan, fpv_rows: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        ports = {port.name: port.direction for port in parse_ports(plan)}
    except Exception:
        ports = {}
    graph = _assignment_edges(plan)
    rows = []
    for row in fpv_rows:
        seed = assertion_identifiers(str(row.get("original_text") or row.get("assertion_text") or ""))
        coi = _reachable(seed, graph)
        interface = sorted(name for name in coi if name in ports)
        rows.append({
            "assertion_id": row.get("assertion_id"),
            "status": row.get("status"),
            "seed_signals": sorted(seed),
            "coi_signals": sorted(coi),
            "interface_signals": interface,
            "interface_signal_count": len(interface),
        })
    total_interface = len(ports)
    touched = sorted({sig for row in rows for sig in row["interface_signals"]})
    return {
        "method": "static_textual_rtl_coi",
        "toolchain": "AssertLLM2-SBY Python static analysis",
        "is_jaspergold_coverage": False,
        "official_jaspergold_result": False,
        "covered": len(touched),
        "total": total_interface,
        "percentage": (len(touched) / total_interface * 100.0) if total_interface else None,
        "covered_interface_signals": touched,
        "total_interface_signals": sorted(ports),
        "assertions": rows,
        "limitations": [
            "textual assign/dependency approximation",
            "does not compute JasperGold proof cores",
            "does not account for all procedural control dependencies",
        ],
    }


def interface_activity_summary(plan: SourcePlan, fpv_rows: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    try:
        ports = parse_ports(plan)
    except Exception:
        ports = ()
    input_ports = [port.name for port in ports if port.direction == "input"]
    touched = sorted({
        signal
        for row in fpv_rows
        for signal in assertion_identifiers(str(row.get("original_text") or row.get("assertion_text") or ""))
        if signal in input_ports
    })
    return {
        "method": "static_interface_activity_from_assertions",
        "toolchain": "AssertLLM2-SBY Python static analysis",
        "is_jaspergold_coverage": False,
        "official_jaspergold_result": False,
        "depth_limits": {
            "cover_depth": config.get("cover_depth"),
            "timeout_seconds": config.get("timeout_seconds"),
        },
        "covered": len(touched),
        "total": len(input_ports),
        "percentage": (len(touched) / len(input_ports) * 100.0) if input_ports else None,
        "covered_input_signals": touched,
        "total_input_signals": sorted(input_ports),
        "unsupported_reason": None,
    }


def formal_coverage_summary(fpv_rows: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    meaningful = [row for row in fpv_rows if row.get("status") in {"proven", "cex", "undetermined"}]
    completed = [row for row in meaningful if row.get("status") in {"proven", "cex"}]
    return {
        "method": "fpv_status_completion_summary",
        "toolchain": "Yosys/SymbiYosys status aggregation",
        "is_jaspergold_coverage": False,
        "official_jaspergold_result": False,
        "depth_limits": {
            "bmc_depth": config.get("bmc_depth"),
            "prove_depth": config.get("prove_depth"),
            "timeout_seconds": config.get("timeout_seconds"),
        },
        "covered": len(completed),
        "total": len(meaningful),
        "percentage": (len(completed) / len(meaningful) * 100.0) if meaningful else None,
        "unsupported_reason": "status summary only; not JasperGold formal coverage",
    }


def checker_proof_summary(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "method": "unsupported_open_source_proof_core",
        "toolchain": "Yosys/SymbiYosys",
        "is_jaspergold_coverage": False,
        "official_jaspergold_result": False,
        "depth_limits": {
            "prove_depth": config.get("prove_depth"),
            "timeout_seconds": config.get("timeout_seconds"),
        },
        "covered": None,
        "total": None,
        "percentage": None,
        "unsupported_reason": "JasperGold proof-core coverage has no current open-source substitute",
    }


def coverage_summary(plan: SourcePlan | None, fpv_rows: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    if plan is None:
        checker_coi = {
            "method": "static_textual_rtl_coi",
            "toolchain": "AssertLLM2-SBY Python static analysis",
            "is_jaspergold_coverage": False,
            "official_jaspergold_result": False,
            "covered": None,
            "total": None,
            "percentage": None,
            "unsupported_reason": "source plan unavailable",
            "assertions": [],
        }
        stimuli = {
            "method": "static_interface_activity_from_assertions",
            "toolchain": "AssertLLM2-SBY Python static analysis",
            "is_jaspergold_coverage": False,
            "official_jaspergold_result": False,
            "covered": None,
            "total": None,
            "percentage": None,
            "unsupported_reason": "source plan unavailable",
        }
    else:
        checker_coi = checker_coi_summary(plan, fpv_rows)
        stimuli = interface_activity_summary(plan, fpv_rows, config)
    return {
        "formal": formal_coverage_summary(fpv_rows, config),
        "stimuli": stimuli,
        "checker_coi": checker_coi,
        "checker_proof": checker_proof_summary(config),
    }


def write_coverage_report(path: Path, title: str, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# {title}",
        "adapter: AssertLLM2-SBY",
        "official_jaspergold_result: false",
        f"is_jaspergold_coverage: {str(payload.get('is_jaspergold_coverage')).lower()}",
        f"method: {payload.get('method')}",
        f"toolchain: {payload.get('toolchain')}",
        f"covered: {payload.get('covered')}",
        f"total: {payload.get('total')}",
        f"percentage: {payload.get('percentage')}",
    ]
    if payload.get("unsupported_reason"):
        lines.append(f"unsupported_reason: {payload['unsupported_reason']}")
    if payload.get("covered_interface_signals"):
        lines.append("covered_interface_signals: " + ", ".join(payload["covered_interface_signals"]))
    if payload.get("covered_input_signals"):
        lines.append("covered_input_signals: " + ", ".join(payload["covered_input_signals"]))
    if payload.get("assertions"):
        lines.append("")
        lines.append("assertions:")
        for row in payload["assertions"]:
            lines.append(
                f"- {row.get('assertion_id')}: interface={','.join(row.get('interface_signals') or [])} "
                f"coi={','.join(row.get('coi_signals') or [])}"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_coverage_reports(outdir: Path, coverage: dict[str, Any]) -> dict[str, str]:
    report_dir = outdir / "report"
    mapping = {
        "formal": report_dir / "formal_coverage.rpt",
        "stimuli": report_dir / "stimuli_coverage.rpt",
        "checker_coi": report_dir / "checker_coi.rpt",
        "checker_proof": report_dir / "checker_proof.rpt",
    }
    titles = {
        "formal": "Formal Coverage Approximation",
        "stimuli": "Stimuli Coverage Approximation",
        "checker_coi": "Checker COI Approximation",
        "checker_proof": "Checker Proof Coverage",
    }
    for key, path in mapping.items():
        write_coverage_report(path, titles[key], coverage[key])
    return {key: str(path) for key, path in mapping.items()}
