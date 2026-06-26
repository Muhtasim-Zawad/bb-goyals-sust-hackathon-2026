"""
services/analyzer.py — Ticket analysis orchestrator.

Design pattern: Template Method (three-layer pipeline)
  Layer 1 → _layer1_safety()    : Injection check — short-circuits if triggered
  Layer 2 → _layer2_evidence()  : Deterministic pre-analysis
  Layer 3 → _layer3_llm()       : LLM reasoning + post-parse validation

Each layer can return early or enrich the context for the next layer.
The final AnalyzeResponse always passes through the output scrubber.
"""
import json
import logging
from typing import Optional

from app.enums import CaseType, Department, EvidenceVerdict, Severity
from app.models.request import TicketRequest, TransactionEntry
from app.models.response import AnalyzeResponse
from app.core.evidence import EvidenceEngine, EvidenceResult, extract_amounts
from app.core.routing import RoutingEngine
from app.core.safety import SafetyGuard, SAFE_REPLY_TEMPLATE
from app.core.groq_client import call_groq
from app.core.fallback import FallbackHandler
from app.core.prompts import SYSTEM_PROMPT, FEW_SHOT_BLOCK

logger = logging.getLogger(__name__)

# ── Shared instances (reused across requests) ─────────────────────────────────
_evidence_engine = EvidenceEngine()
_routing_engine = RoutingEngine()
_fallback_handler = FallbackHandler()


class TicketAnalyzer:
    """
    Main orchestrator for ticket analysis.
    Implements a three-layer Chain of Responsibility pipeline.
    """

    async def analyze(self, req: TicketRequest) -> AnalyzeResponse:
        """Entry point. Executes all three layers in sequence."""

        # ── Layer 1: Safety / Injection Check ────────────────────────────────
        early_exit = self._layer1_safety(req)
        if early_exit is not None:
            return early_exit

        # ── Layer 2: Evidence + Routing Pre-analysis ─────────────────────────
        history = req.transaction_history or []
        evidence_result, pre_block, sev_hint, dept_hint = self._layer2_evidence(req, history)

        # ── Layer 3: LLM Reasoning + Response Assembly ───────────────────────
        response = self._layer3_llm(req, history, evidence_result, pre_block, sev_hint, dept_hint)

        return SafetyGuard.scrub_output(response)

    # ── Layer 1 ────────────────────────────────────────────────────────────────

    def _layer1_safety(self, req: TicketRequest) -> Optional[AnalyzeResponse]:
        """
        Check complaint for injection patterns.
        Returns a hard-coded phishing response immediately if triggered.
        The LLM is NEVER called in this case.
        """
        if SafetyGuard.detect_injection(req.complaint):
            logger.warning(
                "Injection detected for ticket %s — skipping LLM.", req.ticket_id
            )
            return SafetyGuard.make_injection_response(req.ticket_id)
        return None

    # ── Layer 2 ────────────────────────────────────────────────────────────────

    def _layer2_evidence(
        self,
        req: TicketRequest,
        history: list[TransactionEntry],
    ) -> tuple[EvidenceResult, str, str, Optional[Department]]:
        """
        Run the evidence engine and routing engine.
        Returns:
          - evidence_result: scored match result
          - pre_block: structured context string for the LLM prompt
          - sev_hint: severity pre-score string
          - dept_hint: routing department hint or None
        """
        evidence_result = _evidence_engine.match(req.complaint, history)

        # Find best-matched transaction object
        best_txn = next(
            (t for t in history if t.transaction_id == evidence_result.transaction_id),
            None,
        )

        # Routing hints
        dept_hint = _routing_engine.routing_hint(req.channel, req.user_type)

        # Severity pre-score
        amounts = extract_amounts(req.complaint)
        top_amount = best_txn.amount if best_txn else (max(amounts) if amounts else None)
        sev_hint = _routing_engine.severity_pre_score(
            amount=top_amount,
            txn_status=best_txn.status if best_txn else None,
            campaign_context=req.campaign_context,
        )

        # Build the pre-analysis context block
        parts: list[str] = []
        if best_txn:
            parts.append(
                f"MATCHED TRANSACTION: {evidence_result.transaction_id} "
                f"(amount={best_txn.amount} BDT, type={best_txn.type}, "
                f"status={best_txn.status}, time={best_txn.timestamp}). "
                f"Signal: {evidence_result.pre_verdict_signal}. "
                f"Match score: {evidence_result.match_score}/14."
            )
        else:
            parts.append(
                "No transaction in history matched the complaint with high confidence."
            )

        if evidence_result.duplicate_id:
            parts.append(
                f"DUPLICATE PAYMENT PATTERN detected: {evidence_result.duplicate_id} "
                f"is likely the duplicate transaction."
            )

        if dept_hint:
            parts.append(f"Routing hint from channel/user_type: {dept_hint.value}.")

        parts.append(f"Severity pre-score: {sev_hint}.")
        parts.append(f"Complaint language: {req.language or 'unknown'}.")

        pre_block = " ".join(parts)

        return evidence_result, pre_block, sev_hint, dept_hint

    # ── Layer 3 ────────────────────────────────────────────────────────────────

    def _layer3_llm(
        self,
        req: TicketRequest,
        history: list[TransactionEntry],
        evidence_result: EvidenceResult,
        pre_block: str,
        sev_hint: str,
        dept_hint: Optional[Department],
    ) -> AnalyzeResponse:
        """
        Call the LLM with the enriched context block.
        Falls back to FallbackHandler if the LLM fails.
        """
        all_txn_text = (
            json.dumps([t.model_dump() for t in history], ensure_ascii=False)
            if history
            else "[]"
        )

        user_message = f"""{FEW_SHOT_BLOCK}
---
NOW ANALYZE THIS TICKET:
Ticket ID: {req.ticket_id}
Complaint: {req.complaint}
Language: {req.language}
Channel: {req.channel}
User type: {req.user_type}
Campaign: {req.campaign_context}
Transaction history: {all_txn_text}

PRE-ANALYSIS (computed deterministically before you):
{pre_block}

Your task: confirm or override the pre-analysis with your reasoning.
Return only JSON matching the schema above. No extra text.
"""

        try:
            parsed = call_groq(SYSTEM_PROMPT, user_message)
        except RuntimeError as e:
            logger.error("LLM call failed: %s — using fallback.", e)
            return _fallback_handler.build_response(req)

        # Blend confidence: formula (60%) + LLM self-report (40%)
        verdict = parsed.get("evidence_verdict", "insufficient_data")
        formula_conf = _routing_engine.compute_confidence(
            pre_score=evidence_result.match_score,
            evidence_verdict=verdict,
            has_history=bool(history),
        )
        llm_conf = float(parsed.get("confidence", 0.5))
        blended_conf = round((formula_conf * 0.6) + (llm_conf * 0.4), 2)

        try:
            return AnalyzeResponse(
                ticket_id=req.ticket_id,                # always echo, never trust LLM
                relevant_transaction_id=parsed.get("relevant_transaction_id"),
                evidence_verdict=EvidenceVerdict(verdict),
                case_type=CaseType(parsed.get("case_type", "other")),
                severity=Severity(parsed.get("severity", "medium")),
                department=Department(parsed.get("department", "customer_support")),
                agent_summary=parsed.get("agent_summary", ""),
                recommended_next_action=parsed.get("recommended_next_action", ""),
                customer_reply=parsed.get("customer_reply", SAFE_REPLY_TEMPLATE),
                human_review_required=bool(parsed.get("human_review_required", True)),
                confidence=blended_conf,
                reason_codes=parsed.get("reason_codes", []),
            )
        except Exception as e:
            logger.error("Response assembly failed: %s — using fallback.", e)
            return _fallback_handler.build_response(req)
