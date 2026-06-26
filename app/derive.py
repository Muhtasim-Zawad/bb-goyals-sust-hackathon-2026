"""Pure derivation tables for the hybrid pipeline.

severity, department, human_review_required and confidence are NOT asked of the
LLM — they are pure functions of (case_type, evidence_verdict, user_type, amount).
Computing them in code keeps these fields deterministic, reproducible, and
sample-calibrated regardless of LLM variance.

Reused by BOTH the LLM post-processor (apply_derivations) and the deterministic
fallback engine.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .models import TicketRequest

# High-value guardrail (spec text; not exercised by the public samples).
HIGH_VALUE_THRESHOLD = 50000.0

_SEVERITY_ORDER = ["low", "medium", "high", "critical"]

# Base severity assuming `consistent` evidence (CLAUDE.md §9).
_BASE_SEVERITY = {
    "phishing_or_social_engineering": "critical",
    "wrong_transfer": "high",
    "payment_failed": "high",
    "duplicate_payment": "high",
    "agent_cash_in_issue": "high",
    "merchant_settlement_delay": "medium",
    "refund_request": "low",
    "other": "low",
}

# Department routing by case_type (CLAUDE.md §11).
_DEPARTMENT = {
    "wrong_transfer": "dispute_resolution",
    "payment_failed": "payments_ops",
    "duplicate_payment": "payments_ops",
    "agent_cash_in_issue": "agent_operations",
    "merchant_settlement_delay": "merchant_operations",
    "phishing_or_social_engineering": "fraud_risk",
    "refund_request": "customer_support",
    "other": "customer_support",
}

# Case types that warrant human review when evidence is consistent (CLAUDE.md §10).
_REVIEW_WHEN_CONSISTENT = {
    "wrong_transfer",
    "duplicate_payment",
    "agent_cash_in_issue",
}


def _step_down(severity: str) -> str:
    """Drop severity one level, floored at 'low'."""
    idx = _SEVERITY_ORDER.index(severity)
    return _SEVERITY_ORDER[max(0, idx - 1)]


def derive_severity(case_type: str, verdict: str, amount: Optional[float] = None) -> str:
    """Base severity + evidence step-down + high-value bump.

    Phishing is always critical and exempt from step-down. For everything else,
    weak evidence (verdict != consistent) lowers urgency by one level.
    """
    if case_type == "phishing_or_social_engineering":
        return "critical"
    severity = _BASE_SEVERITY.get(case_type, "low")
    if verdict != "consistent":
        severity = _step_down(severity)
    # High-value guardrail may bump severity up one level (heuristic, tunable).
    if amount is not None and amount >= HIGH_VALUE_THRESHOLD:
        idx = _SEVERITY_ORDER.index(severity)
        severity = _SEVERITY_ORDER[min(len(_SEVERITY_ORDER) - 1, idx + 1)]
    return severity


def derive_department(case_type: str, user_type: Optional[str] = None) -> str:
    """Route by case_type, with merchant/agent user_type as a tie-break signal."""
    dept = _DEPARTMENT.get(case_type, "customer_support")
    # user_type bias only nudges the generic 'other'/customer_support bucket so it
    # never fights a case-type-specific routing the samples depend on.
    if case_type == "other":
        if user_type == "merchant":
            return "merchant_operations"
        if user_type == "agent":
            return "agent_operations"
    return dept


def derive_human_review(
    case_type: str, verdict: str, amount: Optional[float] = None
) -> bool:
    """Boolean escalation rule (NOT driven by severity).

    TRUE if: phishing, inconsistent evidence, a consistent dispute-class case
    (wrong_transfer / duplicate_payment / agent_cash_in_issue), or a high-value
    amount. Notably FALSE for payment_failed, merchant_settlement_delay, and
    every insufficient_data case.
    """
    if case_type == "phishing_or_social_engineering":
        return True
    if verdict == "inconsistent":
        return True
    if amount is not None and amount >= HIGH_VALUE_THRESHOLD:
        return True
    if verdict == "consistent" and case_type in _REVIEW_WHEN_CONSISTENT:
        return True
    return False


def derive_confidence(verdict: str, case_type: str) -> float:
    """Deterministic confidence map (avoid a flat 0.9)."""
    if case_type == "phishing_or_social_engineering":
        return 0.95
    if verdict == "consistent":
        return 0.9
    if verdict == "inconsistent":
        return 0.75
    # insufficient_data / ambiguous / vague
    return 0.62


def _relevant_amount(request: "TicketRequest", relevant_txn_id: Optional[str]) -> Optional[float]:
    """Amount of the matched transaction, used by severity/review guardrails."""
    if not relevant_txn_id or not request.transaction_history:
        return None
    for t in request.transaction_history:
        if t.transaction_id == relevant_txn_id:
            return t.amount
    return None


def apply_derivations(judgment: dict, request: "TicketRequest") -> dict:
    """Combine the LLM/rule judgment with code-derived structural fields.

    `judgment` carries: relevant_transaction_id, evidence_verdict, case_type,
    agent_summary, recommended_next_action, customer_reply, reason_codes.
    Returns a complete response dict (still subject to the safety + schema gates).
    """
    case_type = judgment["case_type"]
    verdict = judgment["evidence_verdict"]
    user_type = request.user_type.value if request.user_type else None
    amount = _relevant_amount(request, judgment.get("relevant_transaction_id"))

    return {
        "ticket_id": request.ticket_id,
        "relevant_transaction_id": judgment.get("relevant_transaction_id"),
        "evidence_verdict": verdict,
        "case_type": case_type,
        "severity": derive_severity(case_type, verdict, amount),
        "department": derive_department(case_type, user_type),
        "agent_summary": judgment["agent_summary"],
        "recommended_next_action": judgment["recommended_next_action"],
        "customer_reply": judgment["customer_reply"],
        "human_review_required": derive_human_review(case_type, verdict, amount),
        "confidence": derive_confidence(verdict, case_type),
        "reason_codes": judgment.get("reason_codes") or [],
    }
