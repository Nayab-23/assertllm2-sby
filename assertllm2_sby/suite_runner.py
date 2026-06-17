from __future__ import annotations

import csv
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .dataset import capability_matrix, discover_designs
from .manifest import read_json, write_json
from .models import DesignRecord, GenerationMode, ValidationError
from .real_design import run_design


DesignRunner = Callable[..., dict[str, Any]]


def suite_run_id(prefix: str = "suite") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}_{stamp}"


def safe_design_key(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "__", key).strip("_")


@dataclass(frozen=True)
class SuiteConfig:
    mode: GenerationMode
    method: str = "llm-spec"
    max_mutants: int | None = 1
    jobs: int = 1
    limit: int | None = None
    design_keys: tuple[str, ...] = ()
    suite_id: str | None = None
    resume: Path | None = None
    contract_config: dict[str, Any] | None = None


def _suite_dir(output_root: Path, config: SuiteConfig) -> Path:
    if config.resume:
        resume = config.resume.resolve()
        if resume.is_file():
            return resume.parent
        return resume
    return output_root / (config.suite_id or suite_run_id())


def _load_previous_rows(suite_dir: Path) -> dict[str, dict[str, Any]]:
    for path in (suite_dir / "suite_summary.json", suite_dir / "manifest.json"):
        if not path.is_file():
            continue
        payload = read_json(path)
        rows = payload.get("results") if isinstance(payload, dict) else None
        if isinstance(rows, list):
            return {
                str(row.get("design_key")): dict(row)
                for row in rows
                if isinstance(row, dict) and row.get("design_key") and row.get("result_path")
            }
    return {}


def _select_designs(checkout: Path | None, config: SuiteConfig) -> list[DesignRecord]:
    designs = discover_designs(checkout)
    if config.design_keys:
        by_key = {design.key: design for design in designs}
        selected = []
        for key in config.design_keys:
            if key not in by_key:
                raise ValidationError(f"unknown design key in suite selection: {key}")
            selected.append(by_key[key])
    else:
        selected = designs
    if config.limit is not None:
        selected = selected[: max(0, config.limit)]
    return selected


def _result_row(design: DesignRecord, result: dict[str, Any], status: str = "RAN") -> dict[str, Any]:
    summary = result.get("summary") or {}
    return {
        "design_key": design.key,
        "status": result.get("status"),
        "suite_status": status,
        "result_path": result.get("result_path"),
        "run_id": result.get("run_id"),
        "completed_end_to_end": bool(summary.get("design_completed_end_to_end")),
        "generation_succeeded": bool((summary.get("generation") or {}).get("succeeded")),
        "scoreable_assertions": summary.get("scoreable_assertions"),
        "mutant_outcomes": summary.get("mutant_outcomes") or {},
        "failures": summary.get("failures") or [],
    }


def _skip_row(design: DesignRecord, previous: dict[str, Any]) -> dict[str, Any]:
    row = dict(previous)
    row["suite_status"] = "SKIPPED_RESUME"
    row.setdefault("design_key", design.key)
    return row


def _run_one(
    design: DesignRecord,
    suite_dir: Path,
    config: SuiteConfig,
    runner: DesignRunner,
) -> dict[str, Any]:
    design_output = suite_dir / "design_runs" / safe_design_key(design.key)
    design_output.mkdir(parents=True, exist_ok=True)
    try:
        result = runner(
            design,
            mode=config.mode,
            output_root=design_output,
            max_mutants=config.max_mutants,
            method=config.method,
            contract_config=config.contract_config,
        )
        return _result_row(design, result)
    except Exception as exc:  # noqa: BLE001 - suite must preserve per-design failure
        return {
            "design_key": design.key,
            "status": "ERROR",
            "suite_status": "ERROR",
            "result_path": None,
            "run_id": None,
            "completed_end_to_end": False,
            "generation_succeeded": False,
            "scoreable_assertions": None,
            "mutant_outcomes": {},
            "failures": [f"{type(exc).__name__}: {exc}"],
        }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "design_key",
        "suite_status",
        "status",
        "completed_end_to_end",
        "generation_succeeded",
        "scoreable_assertions",
        "result_path",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column) for column in columns})


def _write_markdown(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# AssertLLM2-SBY Suite Summary",
        "",
        "This is an AssertLLM2-SBY open-source backend suite run, not an official JasperGold result.",
        "",
        f"- Suite ID: `{payload['suite_id']}`",
        f"- Mode: `{payload['mode']}`",
        f"- Method: `{payload['method']}`",
        f"- Designs selected: `{payload['selected_count']}`",
        f"- Completed end to end: `{payload['completed_count']}`",
        f"- Errors: `{payload['error_count']}`",
        f"- Resumed/skipped: `{payload['skipped_resume_count']}`",
        "",
        "| Design | Suite Status | Run Status | Completed | Result |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in payload["results"]:
        result = row.get("result_path") or ""
        lines.append(
            f"| `{row.get('design_key')}` | `{row.get('suite_status')}` | `{row.get('status')}` | "
            f"`{row.get('completed_end_to_end')}` | `{result}` |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _suite_payload(
    *,
    suite_id: str,
    suite_dir: Path,
    config: SuiteConfig,
    rows: list[dict[str, Any]],
    checkout: Path | None,
) -> dict[str, Any]:
    completed = sum(1 for row in rows if row.get("completed_end_to_end"))
    errors = sum(1 for row in rows if row.get("suite_status") == "ERROR")
    skipped = sum(1 for row in rows if row.get("suite_status") == "SKIPPED_RESUME")
    return {
        "schema_version": "1.0",
        "adapter": "AssertLLM2-SBY",
        "suite_id": suite_id,
        "suite_dir": str(suite_dir),
        "mode": config.mode.value,
        "method": config.method,
        "jobs": config.jobs,
        "max_mutants": config.max_mutants,
        "selected_count": len(rows),
        "completed_count": completed,
        "error_count": errors,
        "skipped_resume_count": skipped,
        "results": rows,
        "capability_matrix": capability_matrix(checkout),
        "official_jaspergold_result": False,
    }


def run_suite(
    *,
    output_root: Path,
    config: SuiteConfig,
    checkout: Path | None = None,
    runner: DesignRunner = run_design,
) -> dict[str, Any]:
    suite_dir = _suite_dir(output_root.resolve(), config)
    if suite_dir.exists() and not config.resume:
        raise ValidationError(f"suite output directory already exists; refusing to overwrite: {suite_dir}")
    suite_dir.mkdir(parents=True, exist_ok=True)
    suite_id = suite_dir.name
    designs = _select_designs(checkout, config)
    previous = _load_previous_rows(suite_dir) if config.resume else {}
    rows_by_key: dict[str, dict[str, Any]] = {}
    to_run: list[DesignRecord] = []
    for design in designs:
        previous_row = previous.get(design.key)
        if previous_row and previous_row.get("result_path") and Path(str(previous_row["result_path"])).exists():
            rows_by_key[design.key] = _skip_row(design, previous_row)
        else:
            to_run.append(design)

    if to_run:
        if config.jobs <= 1:
            for design in to_run:
                rows_by_key[design.key] = _run_one(design, suite_dir, config, runner)
        else:
            with ThreadPoolExecutor(max_workers=config.jobs) as pool:
                futures = {
                    pool.submit(_run_one, design, suite_dir, config, runner): design
                    for design in to_run
                }
                for future in as_completed(futures):
                    design = futures[future]
                    rows_by_key[design.key] = future.result()

    rows = [rows_by_key[design.key] for design in designs]
    payload = _suite_payload(
        suite_id=suite_id,
        suite_dir=suite_dir,
        config=config,
        rows=rows,
        checkout=checkout,
    )
    write_json(suite_dir / "suite_summary.json", payload)
    write_json(suite_dir / "manifest.json", payload)
    _write_csv(suite_dir / "suite_summary.csv", rows)
    _write_markdown(suite_dir / "suite_summary.md", payload)
    return payload
