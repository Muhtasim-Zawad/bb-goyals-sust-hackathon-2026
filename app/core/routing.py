"""
core/routing.py — Department routing + severity + confidence scoring.

Design pattern: Table-driven dispatch
- Lookup tables replace branching logic — easy to extend without code changes.
- compute_confidence() provides a deterministic formula-based confidence score
  that is blended with the LLM's self-reported confidence in the analyzer.
"""
from typing import Optional

from app.enums import CaseType, Department, Severity


# ── Routing Lookup Tables ──────────────────────────────────────────────────────

# Explicit channel → department hint
CHANNEL_HINTS: dict[str, Department] = {
    "merchant_portal": Department.merchant_operations,
    "field_agent":     Department.agent_operations,
}

# user_type → department hint (lower priority than channel)
USER_TYPE_HINTS: dict[str, Department] = {
    "merchant": Department.merchant_operations,
    "agent":    Department.agent_operations,
}

# case_type → default department (used if LLM doesn't override)
CASE_DEPT_MAP: dict[CaseType, Department] = {
    CaseType.wrong_transfer:                Department.dispute_resolution,
    CaseType.payment_failed:                Department.payments_ops,
    CaseType.duplicate_payment:             Department.payments_ops,
    CaseType.refund_request:                Department.dispute_resolution,
    CaseType.merchant_settlement_delay:     Department.merchant_operations,
    CaseType.agent_cash_in_issue:           Department.agent_operations,
    CaseType.phishing_or_social_engineering: Department.fraud_risk,
    CaseType.other:                         Department.customer_support,
}

# Severity bump for active campaign context
_SEVERITY_BUMP: dict[str, str] = {
    "low": "medium",
    "medium": "high",
    "high": "critical",
    "critical": "critical",
}


class RoutingEngine:
    """
    Stateless routing and scoring engine.
    All methods are static — no instance state needed.
    """

    @staticmethod
    def routing_hint(
        channel: Optional[str],
        user_type: Optional[str],
    ) -> Optional[Department]:
        """
        Return the most specific department hint given channel and user_type.
        Channel takes priority over user_type.
        Returns None if no hint can be determined.
        """
        if channel and channel in CHANNEL_HINTS:
            return CHANNEL_HINTS[channel]
        if user_type and user_type in USER_TYPE_HINTS:
            return USER_TYPE_HINTS[user_type]
        return None

    @staticmethod
    def severity_pre_score(
        amount: Optional[float],
        txn_status: Optional[str],
        campaign_context: Optional[str],
        case_type_hint: Optional[str] = None,
    ) -> str:
        """
        Compute a severity string hint passed to the LLM context block.
        The LLM may override this, but it constrains the search space.

        Scale:
          amount >= 50,000 BDT  → critical
          amount >= 10,000 BDT  → high
          amount >= 1,000  BDT  → medium
          amount <  1,000  BDT  → low
          active campaign       → bump one level
          phishing case         → always high
        """
        if case_type_hint == "phishing_or_social_engineering":
            return "high"

        if amount and amount >= 50_000:
            base = "critical"
        elif amount and amount >= 10_000:
            base = "high"
        elif amount and amount >= 1_000:
            base = "medium"
        else:
            base = "low"

        # Bump if active campaign
        if campaign_context and campaign_context not in ("none", "", None):
            base = _SEVERITY_BUMP.get(base, base)

        return base

    @staticmethod
    def compute_confidence(
        pre_score: int,
        evidence_verdict: str,
        has_history: bool,
    ) -> float:
        """
        Deterministic confidence formula:
          base = 0.5
          + min(pre_score × 0.05, 0.30)   → up to +0.30 for transaction match quality
          + 0.15  if verdict == consistent
          + 0.10  if verdict == inconsistent (confident it IS wrong)
          - 0.15  if verdict == insufficient_data
          - 0.10  if no transaction history at all
          → clamped [0.05, 1.00]
        """
        base = 0.5
        base += min(pre_score * 0.05, 0.30)

        if evidence_verdict == "consistent":
            base += 0.15
        elif evidence_verdict == "inconsistent":
            base += 0.10
        elif evidence_verdict == "insufficient_data":
            base -= 0.15

        if not has_history:
            base -= 0.10

        return round(max(0.05, min(1.0, base)), 2)
