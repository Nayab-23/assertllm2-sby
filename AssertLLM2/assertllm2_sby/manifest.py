from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SECRET_KEY_RE = re.compile(r"(api[_-]?key|token|secret|password|credential)", re.IGNORECASE)
NON_SECRET_KEYS = {"max_tokens", "input_tokens", "output_tokens", "total_tokens"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def redacted_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key, value in mapping.items():
        key_text = str(key)
        if key_text.lower() not in NON_SECRET_KEYS and SECRET_KEY_RE.search(key_text):
            out[key] = "<redacted>"
        elif isinstance(value, dict):
            out[key] = redacted_mapping(value)
        else:
            out[key] = value
    return out


def git_capture(args: list[str], cwd: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def repo_state(path: Path) -> dict[str, Any]:
    return {
        "path": str(path.resolve()),
        "branch": git_capture(["branch", "--show-current"], path),
        "commit": git_capture(["rev-parse", "HEAD"], path),
        "status_short": (git_capture(["status", "--short"], path) or "").splitlines(),
    }


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes", "on"}
