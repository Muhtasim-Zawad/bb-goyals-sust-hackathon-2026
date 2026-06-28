from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

from langchain_groq import ChatGroq
from langchain_core.exceptions import OutputParserException

from models import TicketRequest, TicketResponse
from prompts import ANALYZE_PROMPT

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LLM setup
# ---------------------------------------------------------------------------

PRIMARY_MODEL  = "llama-3.3-70b-versatile"
FALLBACK_MODEL = "gemma2-9b-it"

def _build_llm(model: str) -> ChatGroq:
    return ChatGroq(
        model=model,
        temperature=0,
        max_tokens=1024,
        timeout=25,          # leaves headroom before the 30s hard limit
        api_key=os.environ["GROQ_API_KEY"],
    )


# ---------------------------------------------------------------------------
# Safety scrubber (post-processing belt-and-suspenders)
# ---------------------------------------------------------------------------

# Phrases that must never appear in customer_reply or recommended_next_action.
# Keyed by (pattern, replacement, field_hint) for targeted fixes.
_SAFETY_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Credential requests
    (re.compile(r"\b(share|provide|send|give|enter|type)\b.{0,40}\b(pin|otp|password|passcode|card number)\b", re.I),
     "Please do not share your PIN, OTP, or password with anyone."),

    # Unconditional refund promises
    (re.compile(r"\bwe (will|shall|are going to) (refund|return|reverse|reimburse|credit)\b", re.I),
     "Any eligible amount will be returned through official channels."),
    (re.compile(r"\byou (will|shall) (get|receive) your money back\b", re.I),
     "Any eligible amount will be returned through official channels."),
    (re.compile(r"\byour (money|amount|funds?) (will|shall) be (returned|refunded|credited)\b", re.I),
     "Any eligible amount will be returned through official channels."),
    (re.compile(r"\bwe (guarantee|promise|confirm) (a |the )?(refund|reversal|return)\b", re.I),
     "Any eligible amount will be returned through official channels."),

    # Third-party referrals (heuristic: external phone numbers or non-bKash URLs)
    (re.compile(r"\b(contact|call|visit|reach out to)\b.{0,60}(0\d{9,}|https?://(?!bkash))", re.I),
     "Please contact official bKash support through the app or our helpline."),
]

_OTP_PIN_ANYWHERE = re.compile(
    r"\b(your |the )?(pin|otp|password|passcode)\b.{0,30}\b(is|:|=|number)\b", re.I
)

SAFE_REFUND_LANGUAGE = "Any eligible amount will be returned through official channels."
SAFE_CREDENTIAL_REMINDER = "Please do not share your PIN, OTP, or password with anyone."


def _scrub_field(text: str, field_name: str) -> str:
    """Apply safety patterns to a single text field. Logs any replacements."""
    for pattern, replacement in _SAFETY_PATTERNS:
        if pattern.search(text):
            logger.warning("Safety scrubber triggered on '%s': pattern=%s", field_name, pattern.pattern)
            text = pattern.sub(replacement, text)

    # Catch any remaining OTP/PIN leakage
    if _OTP_PIN_ANYWHERE.search(text):
        logger.warning("Safety scrubber: OTP/PIN value detected in '%s', sanitizing.", field_name)
        text = _OTP_PIN_ANYWHERE.sub(SAFE_CREDENTIAL_REMINDER, text)

    return text


def _apply_safety_scrub(response: TicketResponse) -> TicketResponse:
    """Scrub safety-sensitive fields and enforce minimum safe wording."""
    cleaned_reply  = _scrub_field(response.customer_reply,          "customer_reply")
    cleaned_action = _scrub_field(response.recommended_next_action,  "recommended_next_action")

    # Ensure the credential reminder is always present in customer_reply
    reminder_present = any(kw in cleaned_reply.lower() for kw in ["pin", "otp", "password"])
    if not reminder_present:
        cleaned_reply = cleaned_reply.rstrip() + " " + SAFE_CREDENTIAL_REMINDER

    return response.model_copy(update={
        "customer_reply":           cleaned_reply,
        "recommended_next_action":  cleaned_action,
    })


# ---------------------------------------------------------------------------
# Input formatter
# ---------------------------------------------------------------------------

def _format_transactions(request: TicketRequest) -> str:
    """Serialise transaction history into a readable block for the prompt."""
    history = request.transaction_history or []
    if not history:
        return "(no transaction history provided)"

    lines: list[str] = []
    for i, txn in enumerate(history, start=1):
        lines.append(
            f"  [{i}] id={txn.transaction_id} | "
            f"type={txn.type} | "
            f"amount={txn.amount} BDT | "
            f"status={txn.status} | "
            f"counterparty={txn.counterparty} | "
            f"time={txn.timestamp}"
        )
    return "\n".join(lines)


def _build_prompt_vars(request: TicketRequest) -> dict:
    return {
        "ticket_id":           request.ticket_id,
        "complaint":           request.complaint,
        "language":            request.language          or "not specified",
        "channel":             request.channel           or "not specified",
        "user_type":           request.user_type         or "not specified",
        "campaign_context":    request.campaign_context  or "none",
        "txn_count":           len(request.transaction_history or []),
        "transaction_history": _format_transactions(request),
    }


# ---------------------------------------------------------------------------
# Chain builder
# ---------------------------------------------------------------------------

def _build_chain(model: str):
    llm = _build_llm(model)
    structured_llm = llm.with_structured_output(TicketResponse)
    return ANALYZE_PROMPT | structured_llm


# Instantiate once at module load to avoid cold-start latency per request
try:
    _primary_chain  = _build_chain(PRIMARY_MODEL)
    _fallback_chain = _build_chain(FALLBACK_MODEL)
except Exception as exc:                          # pragma: no cover
    logger.critical("Failed to build LLM chains: %s", exc)
    raise


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

async def analyze_ticket(request: TicketRequest) -> TicketResponse:
    """
    Run the investigation chain and return a validated, safety-scrubbed response.

    Tries the primary model first. Falls back to the secondary model on any
    LLM-level or parsing failure. Raises RuntimeError if both fail so the
    caller can return a clean 500.
    """
    prompt_vars = _build_prompt_vars(request)

    for chain_name, chain in [("primary", _primary_chain), ("fallback", _fallback_chain)]:
        try:
            logger.info("Running %s chain for ticket_id=%s", chain_name, request.ticket_id)
            response: TicketResponse = await chain.ainvoke(prompt_vars)

            # Guarantee ticket_id echo (model occasionally drifts)
            if response.ticket_id != request.ticket_id:
                logger.warning(
                    "Model returned wrong ticket_id '%s', overriding with '%s'.",
                    response.ticket_id, request.ticket_id,
                )
                response = response.model_copy(update={"ticket_id": request.ticket_id})

            response = _apply_safety_scrub(response)
            logger.info("ticket_id=%s analysed successfully via %s chain.", request.ticket_id, chain_name)
            return response

        except OutputParserException as exc:
            logger.warning("%s chain parser error for ticket_id=%s: %s", chain_name, request.ticket_id, exc)

        except Exception as exc:
            logger.warning("%s chain failed for ticket_id=%s: %s", chain_name, request.ticket_id, exc)

    raise RuntimeError(f"Both LLM chains failed for ticket_id={request.ticket_id}.")
