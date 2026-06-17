from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import AssertionCandidate, AssertionClassification

FENCE_RE = re.compile(r"```(?:systemverilog|verilog|sv)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
STATEMENT_HEAD_RE = re.compile(
    r"(?:(?P<label>[A-Za-z_]\w*)\s*:\s*)?(?P<kind>assert|assume|cover)\b",
    re.IGNORECASE,
)
ALWAYS_COMB_RE = re.compile(r"\balways_comb\b", re.IGNORECASE)
SVA_JSON_RE = re.compile(r'"sva"\s*:\s*"((?:\\.|[^"\\])*)"', re.DOTALL)
PROPERTY_BLOCK_RE = re.compile(
    r"\bproperty\s+(?P<name>[A-Za-z_]\w*)\b(?P<body>.*?)\bendproperty\b",
    re.IGNORECASE | re.DOTALL,
)
PROPERTY_REF_RE = re.compile(
    r"\b(?P<kind>assert|assume|cover)\s+property\s*\(\s*(?P<name>[A-Za-z_]\w*)\s*\)",
    re.IGNORECASE,
)

UNSUPPORTED_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ranged_delay", re.compile(r"##\s*\[[^\]]+\]")),
    ("variable_delay", re.compile(r"##\s*[A-Za-z_$]")),
    ("consecutive_repetition", re.compile(r"\[\*\s*[^\]]+\]")),
    ("goto_repetition", re.compile(r"\[->\s*[^\]]+\]")),
    ("nonconsecutive_repetition", re.compile(r"\[=\s*[^\]]+\]")),
    ("throughout", re.compile(r"\bthroughout\b")),
    ("within", re.compile(r"\bwithin\b")),
    ("first_match", re.compile(r"\bfirst_match\s*\(")),
    ("sequence_local_variable_or_declaration", re.compile(r"\b(sequence|endsequence|local\s+\w+)\b")),
    ("clocking_block", re.compile(r"\b(clocking|endclocking)\b")),
    ("liveness_operator", re.compile(r"\b(s_eventually|eventually|until|until_with)\b")),
    ("strong_weak", re.compile(r"\b(strong|weak)\s*\(")),
    ("edge_sample_function", re.compile(r"\$(?:rose|fell|changed)\s*\(", re.IGNORECASE)),
]


@dataclass(frozen=True)
class AssertionBlock:
    text: str
    label: str | None
    kind: str
    start: int
    end: int
    source: str = "generated"

    def to_json(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "label": self.label,
            "kind": self.kind,
            "start": self.start,
            "end": self.end,
            "source": self.source,
        }


def normalize_output(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    fences = FENCE_RE.findall(text)
    if fences:
        return "\n\n".join(block.strip() for block in fences if block.strip()).strip()
    # Trim common prose before first assertion-like construct.
    candidates = [
        idx for idx in [
            text.lower().find("assert"),
            text.lower().find("assume"),
            text.lower().find("cover"),
            text.lower().find("always_comb"),
            text.lower().find("property"),
            text.lower().find("[signal]"),
        ] if idx >= 0
    ]
    if candidates:
        first = min(candidates)
        text = text[text.rfind("\n", 0, first) + 1:]
    return text.strip()


def _mask_comments_and_strings(text: str) -> str:
    chars = list(text)
    i = 0
    state = "code"
    while i < len(chars):
        c = chars[i]
        n = chars[i + 1] if i + 1 < len(chars) else ""
        if state == "code":
            if c == "/" and n == "/":
                chars[i] = chars[i + 1] = " "
                i += 2
                state = "line_comment"
                continue
            if c == "/" and n == "*":
                chars[i] = chars[i + 1] = " "
                i += 2
                state = "block_comment"
                continue
            if c == '"':
                chars[i] = " "
                i += 1
                state = "string"
                continue
        elif state == "line_comment":
            if c == "\n":
                state = "code"
            else:
                chars[i] = " "
        elif state == "block_comment":
            if c == "*" and n == "/":
                chars[i] = chars[i + 1] = " "
                i += 2
                state = "code"
                continue
            chars[i] = " " if c != "\n" else "\n"
        elif state == "string":
            if c == "\\" and i + 1 < len(chars):
                chars[i] = chars[i + 1] = " "
                i += 2
                continue
            if c == '"':
                state = "code"
            chars[i] = " " if c != "\n" else "\n"
        i += 1
    return "".join(chars)


def _statement_end(masked: str, start: int) -> int:
    depth = 0
    i = start
    while i < len(masked):
        c = masked[i]
        if c in "([{":
            depth += 1
        elif c in ")]}" and depth:
            depth -= 1
        elif c == ";" and depth == 0:
            return i + 1
        i += 1
    return len(masked)


def _block_end(masked: str, start: int) -> int:
    token_re = re.compile(r"\b(begin|end)\b|;", re.IGNORECASE)
    first = token_re.search(masked, start)
    if not first:
        return _statement_end(masked, start)
    if first.group(0) == ";":
        return first.end()
    depth = 0
    for match in token_re.finditer(masked, first.start()):
        token = match.group(0).lower()
        if token == "begin":
            depth += 1
        elif token == "end":
            depth -= 1
            if depth <= 0:
                return match.end()
    return len(masked)


def _line_label_before(masked: str, start: int) -> tuple[int, str | None]:
    line_start = masked.rfind("\n", 0, start) + 1
    prefix = masked[line_start:start]
    match = re.match(r"\s*(?P<label>[A-Za-z_]\w*)\s*:\s*$", prefix)
    if not match:
        return start, None
    return line_start, match.group("label")


def _kind_from_statement(text: str) -> str:
    match = re.search(r"\b(assert|assume|cover)\b", text, re.IGNORECASE)
    return match.group(1).lower() if match else "unknown"


def _collect_property_definitions(normalized: str) -> dict[str, str]:
    return {match.group("name"): match.group(0).strip() for match in PROPERTY_BLOCK_RE.finditer(normalized)}


def collect_assertion_blocks(raw_output: str) -> list[AssertionBlock]:
    json_svas = _sva_strings_from_json_like(raw_output)
    normalized = "\n\n".join(json_svas) if json_svas else normalize_output(raw_output)
    if not normalized:
        return []
    masked = _mask_comments_and_strings(normalized)
    blocks: list[AssertionBlock] = []
    consumed: list[tuple[int, int]] = []

    def is_consumed(start: int, end: int) -> bool:
        return any(start < old_end and end > old_start for old_start, old_end in consumed)

    for match in ALWAYS_COMB_RE.finditer(masked):
        start, label = _line_label_before(masked, match.start())
        end = _block_end(masked, match.start())
        text = normalized[start:end].strip()
        if re.search(r"\b(assert|assume|cover)\b", text, re.IGNORECASE):
            blocks.append(AssertionBlock(text=text, label=label, kind="always_comb", start=start, end=end))
            consumed.append((start, end))

    properties = _collect_property_definitions(normalized)
    for match in STATEMENT_HEAD_RE.finditer(masked):
        start = match.start()
        if is_consumed(start, match.end()):
            continue
        end = _statement_end(masked, start)
        block_start, line_label = _line_label_before(masked, start)
        label = match.group("label") or line_label
        text = normalized[block_start:end].strip()
        prop_ref = PROPERTY_REF_RE.search(text)
        if prop_ref and prop_ref.group("name") in properties:
            prop_text = properties[prop_ref.group("name")]
            text = f"{prop_text}\n{text}"
            label = label or prop_ref.group("name")
        blocks.append(AssertionBlock(
            text=text,
            label=label,
            kind=(match.group("kind") or _kind_from_statement(text)).lower(),
            start=block_start,
            end=end,
        ))
        consumed.append((block_start, end))

    seen = set()
    ordered: list[AssertionBlock] = []
    for block in sorted(blocks, key=lambda b: (b.start, b.end)):
        key = re.sub(r"\s+", " ", block.text)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(block)
    return ordered


def _decode_json_string(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except Exception:
        return value.replace(r"\n", "\n").replace(r"\"", '"').replace(r"\\", "\\")


def _sva_strings_from_json_like(text: str) -> list[str]:
    return [_decode_json_string(match.group(1)) for match in SVA_JSON_RE.finditer(text or "")]


def _stable_id(text: str, ordinal: int) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"sby_assert_{ordinal:04d}_{digest}"


def _classify(text: str) -> tuple[AssertionClassification, tuple[str, ...]]:
    stripped = text.strip()
    if not stripped:
        return AssertionClassification.EMPTY_OUTPUT, ("empty",)
    if not re.search(r"\b(assert|assume|cover)\b", stripped, re.IGNORECASE):
        return AssertionClassification.INVALID_OUTPUT, ("no_assert_assume_or_cover_statement",)
    reasons = [name for name, pattern in UNSUPPORTED_PATTERNS if pattern.search(stripped)]
    if reasons:
        return AssertionClassification.UNSUPPORTED_SVA, tuple(reasons)
    if re.search(r"\b(assert|assume|cover)\s+property\b", stripped, re.IGNORECASE):
        return AssertionClassification.NEEDS_FORMAL_VALIDATION, ("concurrent_assertion_requires_frontend_validation",)
    if ALWAYS_COMB_RE.search(stripped):
        return AssertionClassification.SUPPORTED_CANDIDATE, ()
    if re.search(r"\b(assert|assume|cover)\s*\(", stripped, re.IGNORECASE):
        return AssertionClassification.SUPPORTED_CANDIDATE, ()
    return AssertionClassification.REQUIRES_EXACT_LOWERING, ("syntax_shape_not_directly_supported",)


def extract_assertions(raw_output: str) -> list[AssertionCandidate]:
    json_svas = _sva_strings_from_json_like(raw_output)
    normalized = "\n\n".join(json_svas) if json_svas else normalize_output(raw_output)
    if not normalized:
        return [AssertionCandidate(
            assertion_id="sby_assert_0000_empty",
            text="",
            classification=AssertionClassification.EMPTY_OUTPUT,
            reasons=("empty_model_output",),
        )]

    blocks = collect_assertion_blocks(normalized)
    ordered = [(block.label, block.text) for block in blocks] or [(None, normalized)]

    out: list[AssertionCandidate] = []
    for idx, (label, snippet) in enumerate(ordered, start=1):
        classification, reasons = _classify(snippet)
        out.append(AssertionCandidate(
            assertion_id=_stable_id(snippet, idx),
            text=snippet,
            classification=classification,
            reasons=reasons,
            label=label,
        ))
    return out


def syntax_correctness(candidates: list[AssertionCandidate]) -> dict[str, Any]:
    total = len(candidates)
    valid = sum(
        1 for item in candidates
        if item.classification not in {
            AssertionClassification.EMPTY_OUTPUT,
            AssertionClassification.INVALID_OUTPUT,
            AssertionClassification.TRUNCATED_OR_INVALID_OUTPUT,
        }
    )
    return {
        "initial_blocks": total,
        "valid_blocks": valid,
        "removed_or_invalid_blocks": total - valid,
        "syntax_correctness": (valid / total) if total else 0.0,
    }


def cleanup_assertion_file(input_path: Path, output_path: Path | None = None) -> dict[str, Any]:
    source = input_path.read_text(encoding="utf-8")
    candidates = extract_assertions(source)
    kept = [
        item for item in candidates
        if item.classification not in {
            AssertionClassification.EMPTY_OUTPUT,
            AssertionClassification.INVALID_OUTPUT,
            AssertionClassification.TRUNCATED_OR_INVALID_OUTPUT,
        }
    ]
    target = output_path or input_path.with_name(f"{input_path.stem}.cleaned{input_path.suffix}")
    target.write_text("\n\n".join(item.text for item in kept), encoding="utf-8")
    payload = syntax_correctness(candidates)
    payload.update({
        "input_path": str(input_path),
        "output_path": str(target),
        "removed_assertion_ids": [item.assertion_id for item in candidates if item not in kept],
        "kept_assertion_ids": [item.assertion_id for item in kept],
    })
    return payload
