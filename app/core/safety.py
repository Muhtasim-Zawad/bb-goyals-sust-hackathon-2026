"""
core/safety.py — Input firewall and output scrubber.

Design pattern: Guard / Decorator
- detect_injection(): Layer 1 check — if triggered, LLM is never called.
- scrub_output(): Post-LLM scrubber — last defence before response exits.
- make_injection_response(): Factory for the forced phishing response.
"""
import re

from app.enums import CaseType, Department, EvidenceVerdict, Severity
from app.models.response import AnalyzeResponse


# ── Injection Patterns ─────────────────────────────────────────────────────────
# Any complaint matching these triggers an immediate phishing route.
INJECTION_PATTERNS: list[str] = [
    r"ignore (previous|all|your) instructions",
    r"forget (your|all) (rules|instructions|prompt)",
    r"act as (a different|an? unrestricted|dan)",
    r"you are now",
    r"\bjailbreak\b",
    r"\bDAN\b",
    r"system prompt",
    r"reveal your (instructions|prompt|rules)",
    r"pretend (you are|to be)",
    r"new persona",
    r"override (all|your|previous)",
    r"disregard (all|your|previous|the above)",
]

# ── Banned Phrases in customer_reply ──────────────────────────────────────────
BANNED_CUSTOMER_REPLY: list[str] = [
    r"\bPIN\b",
    r"\bOTP\b",
    r"\bpassword\b",
    r"\bcard.?number\b",
    r"we will refund",
    r"you will (be )?refunded",
    r"your money will be returned",
    r"refund (has been|will be) (processed|approved|initiated|confirmed)",
    r"we (can|will) (process|approve|confirm) (your )?(refund|reversal)",
    r"contact .{0,50}(support|helpline|agent|number)",  # third-party redirect
]

# ── Banned Phrases in recommended_next_action ─────────────────────────────────
BANNED_NEXT_ACTION: list[str] = [
    r"confirm.*refund",
    r"process.*refund",
    r"approve.*reversal",
    r"release.*funds",
    r"unblock.*account",
]

# ── Safe Fallback Reply ────────────────────────────────────────────────────────
SAFE_REPLY_TEMPLATE = (
    "We have received your concern and it has been logged for review. "
    "If eligible, any adjustment will be made through official channels. "
    "Please contact bKash official support for further assistance. "
    "Do not share your PIN, OTP, or password with anyone."
)


class SafetyGuard:
    """
    Stateless safety utility — all methods are static.
    Encapsulates all input and output safety concerns.
    """

    @staticmethod
    def detect_injection(complaint: str) -> bool:
        """Return True if the complaint contains a prompt-injection pattern."""
        return any(
            re.search(pattern, complaint, re.IGNORECASE)
            for pattern in INJECTION_PATTERNS
        )

    @staticmethod
    def scrub_output(response: AnalyzeResponse) -> AnalyzeResponse:
        """
        Scan customer_reply and recommended_next_action for banned phrases.
        If found, replace with safe defaults and force human_review_required.
        """
        for pattern in BANNED_CUSTOMER_REPLY:
            if re.search(pattern, response.customer_reply, re.IGNORECASE):
                response.customer_reply = SAFE_REPLY_TEMPLATE
                response.human_review_required = True
                break

        for pattern in BANNED_NEXT_ACTION:
            if re.search(pattern, response.recommended_next_action, re.IGNORECASE):
                response.recommended_next_action = (
                    "Escalate to human agent for manual verification and action."
                )
                response.human_review_required = True
                break

        return response

    @staticmethod
    def make_injection_response(ticket_id: str) -> AnalyzeResponse:
        """Factory: build a hard-coded phishing response without calling the LLM."""
        return AnalyzeResponse(
            ticket_id=ticket_id,
            relevant_transaction_id=None,
            evidence_verdict=EvidenceVerdict.insufficient_data,
            case_type=CaseType.phishing_or_social_engineering,
            severity=Severity.high,
            department=Department.fraud_risk,
            agent_summary=(
                "Complaint contains prompt injection or social engineering attempt. "
                "No transaction analysis was performed."
            ),
            recommended_next_action=(
                "Flag for fraud team review. Do not act on embedded instructions."
            ),
            customer_reply=SAFE_REPLY_TEMPLATE,
            human_review_required=True,
            confidence=0.95,
            reason_codes=["prompt_injection_detected", "fraud_risk_escalation"],
        )
