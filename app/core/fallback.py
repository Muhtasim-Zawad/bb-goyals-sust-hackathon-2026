"""
core/fallback.py — Pure rule-based fallback response.

Fires when ALL Groq API keys are exhausted or the 20-second budget is exceeded.
Guarantees a schema-valid response with no LLM dependency.

This protects the Performance & Reliability score even under sustained load.
"""
import logging
from typing import Optional

from app.enums import CaseType, Department, EvidenceVerdict, Severity
from app.models.request import TicketRequest
from app.models.response import AnalyzeResponse
from app.core.evidence import EvidenceEngine, extract_amounts
from app.core.routing import RoutingEngine
from app.core.safety import SAFE_REPLY_TEMPLATE

logger = logging.getLogger(__name__)

_evidence_engine = EvidenceEngine()
_routing_engine = RoutingEngine()


class FallbackHandler:
    """
    Builds a schema-valid response using only deterministic rules.
    No LLM is called. Fields are intentionally conservative.
    """

    def build_response(self, req: TicketRequest) -> AnalyzeResponse:
        logger.warning("Fallback activated for ticket %s", req.ticket_id)

        history = req.transaction_history or []
        result = _evidence_engine.match(req.complaint, history)

        dept_hint: Optional[Department] = _routing_engine.routing_hint(
            req.channel, req.user_type
        )

        # Extract amount for severity
        amounts = extract_amounts(req.complaint)
        best_txn = next(
            (t for t in history if t.transaction_id == result.transaction_id),
            None,
        )
        top_amount = best_txn.amount if best_txn else (max(amounts) if amounts else None)

        sev_str = _routing_engine.severity_pre_score(
            amount=top_amount,
            txn_status=best_txn.status if best_txn else None,
            campaign_context=req.campaign_context,
            case_type_hint=None,
        )

        return AnalyzeResponse(
            ticket_id=req.ticket_id,
            relevant_transaction_id=result.transaction_id,
            evidence_verdict=EvidenceVerdict.insufficient_data,
            case_type=CaseType.other,
            severity=Severity(sev_str),
            department=dept_hint or Department.customer_support,
            agent_summary=(
                "Automated analysis unavailable due to service limitation. "
                "Manual review required."
            ),
            recommended_next_action=(
                "Assign to a human agent for full investigation."
            ),
            customer_reply=SAFE_REPLY_TEMPLATE,
            human_review_required=True,
            confidence=0.1,
            reason_codes=["llm_unavailable", "fallback_response"],
        )
