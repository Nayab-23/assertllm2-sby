from __future__ import annotations

import re

from .assertion_parser import UNSUPPORTED_PATTERNS
from .formal_types import LoweredAssertion


KIND_RE = re.compile(r"\b(?P<kind>assert|assume|cover)\b", re.IGNORECASE)
CONCURRENT_RE = re.compile(
    r"^(?:(?P<label>[A-Za-z_]\w*)\s*:\s*)?"
    r"(?P<kind>assert|assume|cover)\s+property\s*"
    r"\(\s*@\s*\(\s*posedge\s+(?P<clock>[A-Za-z_]\w*)\s*\)\s*"
    r"(?:(?:disable\s+iff)\s*\(\s*(?P<disable>[^()]*)\s*\)\s*)?"
    r"(?P<expr>.*?)\s*\)\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)
IMMEDIATE_RE = re.compile(
    r"^(?:(?P<label>[A-Za-z_]\w*)\s*:\s*)?"
    r"(?P<kind>assert|assume|cover)\s*\(\s*(?P<expr>.*?)\s*\)\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)
IF_IMMEDIATE_RE = re.compile(
    r"^if\s*\(\s*(?P<guard>.*?)\s*\)\s*"
    r"(?P<kind>assert|assume|cover)\s*\(\s*(?P<expr>.*?)\s*\)\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)
FIXED_DELAY_PREFIX_RE = re.compile(r"^##\s*(?P<delay>\d+)\s+(?P<expr>.+)$", re.DOTALL)
ANY_DELAY_RE = re.compile(r"##\s*(?P<token>\[[^\]]+\]|[A-Za-z_$]\w*|\d+)")
SEQUENCE_OPERATOR_RE = re.compile(
    r"(##\s*(?:\[[^\]]+\]|[A-Za-z_$]\w*|\d+)|"
    r"\[\*\s*[^\]]+\]|\[->\s*[^\]]+\]|\[=\s*[^\]]+\]|"
    r"\bthroughout\b|\bwithin\b|\bfirst_match\s*\()",
    re.IGNORECASE,
)
IMPLICATION_OPERATOR_RE = re.compile(r"\|->|\|=>")

FIXED_DELAY_ASSUMPTIONS = (
    "single_clock_posedge_monitor",
    "fixed_finite_delay_only",
    "disable_iff_is_sampled_on_monitor_clock_and_aborts_active_delay_obligations",
    "consequent_is_a_boolean_expression_without_sequence_operators",
)


def _clean_expr(expr: str) -> str:
    expr = re.sub(r"//.*", "", expr)
    expr = re.sub(r"/\*.*?\*/", "", expr, flags=re.DOTALL)
    return re.sub(r"\s+", " ", expr.strip()).rstrip(";").strip()


def _split_top_level_implication(expr: str) -> tuple[str, str, str] | None:
    depth = 0
    idx = 0
    while idx < len(expr):
        char = expr[idx]
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            if expr.startswith("|->", idx):
                return expr[:idx], "|->", expr[idx + 3:]
            if expr.startswith("|=>", idx):
                return expr[:idx], "|=>", expr[idx + 3:]
        idx += 1
    return None


def _valid_history_guard(delay: int) -> str:
    if delay <= 0:
        return ""
    if delay == 1:
        return "past_valid"
    return f"past_valid && $past(past_valid, {delay - 1})"


def _disable_window_guard(disable: str, delay: int) -> str:
    if not disable:
        return ""
    guards = [f"!({disable})"]
    guards.extend(f"!$past({disable}, {idx})" for idx in range(1, delay + 1))
    return " && ".join(guards)


def _combine_guards(*guards: str) -> str:
    active = [guard for guard in guards if guard]
    return " && ".join(f"({guard})" for guard in active)


def _parse_consequent_delay(raw_consequent: str) -> tuple[int, str, tuple[str, ...]]:
    consequent = _clean_expr(raw_consequent)
    if not consequent:
        return 0, "", ("missing_consequent",)
    fixed = FIXED_DELAY_PREFIX_RE.match(consequent)
    if fixed:
        delay = int(fixed.group("delay"))
        body = _clean_expr(fixed.group("expr"))
    else:
        delay = 0
        body = consequent
    if not body:
        return 0, "", ("missing_consequent",)
    bad_delay = ANY_DELAY_RE.search(body)
    if bad_delay:
        token = bad_delay.group("token")
        if token.isdigit():
            return 0, "", ("multiple_or_nested_delays_not_supported",)
        if token.startswith("["):
            return 0, "", ("ranged_delay",)
        return 0, "", ("variable_delay",)
    if IMPLICATION_OPERATOR_RE.search(body):
        return 0, "", ("nested_implication_not_supported",)
    if SEQUENCE_OPERATOR_RE.search(body):
        return 0, "", ("unsupported_sequence_operator_in_consequent",)
    return delay, body, ()


def _lower_concurrent(match: re.Match[str]) -> tuple[str, tuple[str, ...], str | None, tuple[str, ...]]:
    kind = match.group("kind").lower()
    disable = _clean_expr(match.group("disable") or "")
    expr = _clean_expr(match.group("expr"))
    implication = _split_top_level_implication(expr)
    if implication:
        antecedent_raw, operator, consequent_raw = implication
        antecedent = _clean_expr(antecedent_raw)
        consequent_delay, consequent, reasons = _parse_consequent_delay(consequent_raw)
        if reasons:
            return "", reasons, None, ()
        if not antecedent or not consequent:
            return "", ("invalid_implication",), None, ()
        if ANY_DELAY_RE.search(antecedent) or SEQUENCE_OPERATOR_RE.search(antecedent):
            return "", ("sequence_operator_in_antecedent_not_supported",), None, ()
        delay = consequent_delay + (1 if operator == "|=>" else 0)
        if delay == 0:
            body = f"{kind}((!({antecedent})) || ({consequent}));"
            if disable:
                body = f"if (!({disable})) begin\n      {body}\n    end"
            return body, (), "same_cycle_overlapping_implication", (
                "single_clock_posedge_monitor",
                "consequent_is_a_boolean_expression_without_sequence_operators",
            )
        guard = _combine_guards(
            _valid_history_guard(delay),
            _disable_window_guard(disable, delay),
            f"$past({antecedent}, {delay})",
        )
        body = f"if ({guard}) begin\n      {kind}({consequent});\n    end"
        return body, (), f"fixed_delay_{operator}_delay_{delay}", FIXED_DELAY_ASSUMPTIONS
    delay = ANY_DELAY_RE.search(expr)
    if delay:
        token = delay.group("token")
        if token.isdigit():
            return "", ("fixed_delay_without_supported_implication",), None, ()
        if token.startswith("["):
            return "", ("ranged_delay",), None, ()
        return "", ("variable_delay",), None, ()
    if SEQUENCE_OPERATOR_RE.search(expr):
        return "", ("unsupported_sequence_operator",), None, ()
    body = f"{kind}({expr});"
    if disable:
        body = f"if (!({disable})) begin\n      {body}\n    end"
    return body, (), "same_cycle_property_expression", (
        "single_clock_posedge_monitor",
        "expression_contains_no_sequence_operators",
    )


def _lower_text(text: str) -> tuple[str, str, bool, tuple[str, ...], str | None, tuple[str, ...]]:
    stripped = text.strip()
    concurrent = CONCURRENT_RE.match(stripped)
    if concurrent:
        kind = concurrent.group("kind").lower()
        body, reasons, rule, assumptions = _lower_concurrent(concurrent)
        return kind, body, not reasons, reasons, rule, assumptions
    immediate = IMMEDIATE_RE.match(stripped)
    if immediate:
        kind = immediate.group("kind").lower()
        expr = _clean_expr(immediate.group("expr"))
        return kind, f"{kind}({expr});", True, (), "immediate_assert_assume_cover", ()
    guarded = IF_IMMEDIATE_RE.match(stripped)
    if guarded:
        kind = guarded.group("kind").lower()
        guard = _clean_expr(guarded.group("guard"))
        expr = _clean_expr(guarded.group("expr"))
        body = f"if ({guard}) begin\n      {kind}({expr});\n    end"
        return kind, body, True, (), "guarded_immediate_assert_assume_cover", ()
    kind_match = KIND_RE.search(stripped)
    return (
        kind_match.group("kind").lower() if kind_match else "unknown",
        "",
        False,
        ("unsupported_syntax_shape",),
        None,
        (),
    )


def classify_and_lower_assertion(assertion_id: str, text: str) -> LoweredAssertion:
    stripped = text.strip()
    reasons = [name for name, pattern in UNSUPPORTED_PATTERNS if pattern.search(stripped)]
    kind_match = KIND_RE.search(stripped)
    if not kind_match:
        return LoweredAssertion(
            assertion_id=assertion_id,
            kind="unknown",
            original_text=text,
            lowered_text="",
            supported=False,
            reasons=("no_assert_assume_or_cover_statement",),
        )
    if reasons:
        return LoweredAssertion(
            assertion_id=assertion_id,
            kind=kind_match.group("kind").lower(),
            original_text=text,
            lowered_text="",
            supported=False,
            reasons=tuple(reasons),
        )
    kind, lowered, supported, lowering_reasons, rule, assumptions = _lower_text(stripped)
    if not supported:
        return LoweredAssertion(
            assertion_id=assertion_id,
            kind=kind,
            original_text=text,
            lowered_text="",
            supported=False,
            reasons=lowering_reasons,
        )
    return LoweredAssertion(
        assertion_id=assertion_id,
        kind=kind,
        original_text=text,
        lowered_text=lowered,
        supported=True,
        transformation_rule=rule,
        equivalence_assumptions=assumptions,
    )


def lower_assertions(items: list[tuple[str, str]] | tuple[tuple[str, str], ...]) -> tuple[LoweredAssertion, ...]:
    return tuple(classify_and_lower_assertion(assertion_id, text) for assertion_id, text in items)
