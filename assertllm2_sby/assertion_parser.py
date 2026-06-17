from __future__ import annotations

import hashlib
import json
import re

from .models import AssertionCandidate, AssertionClassification

FENCE_RE = re.compile(r"```(?:systemverilog|verilog|sv)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
ASSERT_HEAD_RE = re.compile(
    r"(?:(?P<label>[A-Za-z_]\w*)\s*:\s*)?"
    r"(?P<body>(?:assert|assume|cover)\s+(?:property\s*)?\(.*?;)",
    re.IGNORECASE | re.DOTALL,
)
ALWAYS_COMB_RE = re.compile(r"always_comb\s+begin.*?end", re.IGNORECASE | re.DOTALL)
SVA_JSON_RE = re.compile(r'"sva"\s*:\s*"((?:\\.|[^"\\])*)"', re.DOTALL)
PROPERTY_BLOCK_RE = re.compile(
    r"property\s+(?P<name>[A-Za-z_]\w*)\s*;\s*(?P<body>.*?)\s*endproperty\s*"
    r"(?:(?P<label>[A-Za-z_]\w*)\s*:\s*)?"
    r"(?P<kind>assert|cover)\s+property\s*\(\s*(?P=name)\s*\)\s*;",
    re.IGNORECASE | re.DOTALL,
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
            text.lower().find("[signal]"),
        ] if idx >= 0
    ]
    if candidates:
        text = text[min(candidates):]
    return text.strip()


def _decode_json_string(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except Exception:
        return value.replace(r"\n", "\n").replace(r"\"", '"').replace(r"\\", "\\")


def _sva_strings_from_json_like(text: str) -> list[str]:
    return [_decode_json_string(match.group(1)) for match in SVA_JSON_RE.finditer(text or "")]


def _property_block_snippets(text: str) -> list[tuple[str | None, str]]:
    snippets: list[tuple[str | None, str]] = []
    for match in PROPERTY_BLOCK_RE.finditer(text):
        label = match.group("label") or match.group("name")
        kind = match.group("kind").lower()
        body = match.group("body").strip()
        snippets.append((label, f"{label}: {kind} property ({body});"))
    return snippets


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
    if re.search(r"\bassert\s+property\b", stripped, re.IGNORECASE):
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

    snippets: list[tuple[str | None, str]] = []
    snippets.extend(_property_block_snippets(normalized))
    for block in ALWAYS_COMB_RE.findall(normalized):
        snippets.append((None, block.strip()))
    if not snippets:
        for match in ASSERT_HEAD_RE.finditer(normalized):
            snippets.append((match.group("label"), match.group("body").strip()))

    # Deduplicate while preserving order. If no parser hit, classify the whole normalized output.
    seen = set()
    ordered = []
    for label, snippet in snippets or [(None, normalized)]:
        key = re.sub(r"\s+", " ", snippet)
        if key in seen:
            continue
        seen.add(key)
        ordered.append((label, snippet))

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
