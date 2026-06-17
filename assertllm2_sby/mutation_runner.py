from __future__ import annotations

from .formal_types import FormalResult, FormalStatus, MutantEvaluation


def classify_mutant(
    *,
    mutant_id: str,
    golden_result: FormalResult,
    mutant_result: FormalResult,
    responsible_assertion: str | None = None,
) -> MutantEvaluation:
    golden_ok = golden_result.status in {FormalStatus.PROVEN, FormalStatus.BOUNDED_CLEAN}
    killed = golden_ok and mutant_result.status == FormalStatus.COUNTEREXAMPLE
    return MutantEvaluation(
        mutant_id=mutant_id,
        killed=killed,
        responsible_assertion=responsible_assertion if killed else None,
        golden_status=golden_result.status,
        mutant_status=mutant_result.status,
        mutant_result=mutant_result,
    )
