"""Final enforcement layer — runs on EVERY response (LLM or deterministic).

Two independent passes:
  enforce_safety  — rewrites unsafe prose (credential asks, refund promises),
                    forces review on prompt injection, manages the PIN/OTP
                    reminder per user_type.
  enforce_schema  — snaps enums to the allowlist, clamps confidence, guarantees
                    reason_codes is a list, and blocks invented transaction IDs.

These gates are the hard guarantee: even if a model ignores instructions, the
output that leaves the service is safe and schema-valid.
"""
from __future__ import annotations

import re
from typing import Optional

from .normalizer import contains_injection, detect_language

SAFE_RETURN_PHRASE = "any eligible amount will be returned through official channels"
PIN_REMINDER_EN = "Please do not share your PIN or OTP with anyone."
PIN_REMINDER_BN = "অনুগ্রহ করে আপনার পিন বা ওটিপি কারও সাথে শেয়ার করবেন না।"

_CREDENTIAL_NOUNS = r"(?:pin|otp|password|pass code|passcode|cvv|card number|ওটিপি|পিন|পাসওয়ার্ড)"
_REQUEST_VERBS = r"(?:share|provide|enter|send|give|tell|type|confirm|verify|submit|input|দিন|দিবেন|বলুন|পাঠান|শেয়ার\s+কর)"
_NEGATION = r"(?:do not|don't|dont|never|do\s*n't|না|করবেন\s*না)"

# A credential REQUEST = a request verb near a credential noun WITHOUT a negation.
# (A "do not share your PIN" reminder must be allowed.)
_CRED_REQUEST_RE = re.compile(
    rf"{_REQUEST_VERBS}[^.।!?]{{0,40}}{_CREDENTIAL_NOUNS}|{_CREDENTIAL_NOUNS}[^.।!?]{{0,40}}{_REQUEST_VERBS}",
    re.IGNORECASE,
)
_NEG_NEAR_CRED_RE = re.compile(
    rf"{_NEGATION}[^.।!?]{{0,40}}{_CREDENTIAL_NOUNS}|{_CREDENTIAL_NOUNS}[^.।!?]{{0,40}}{_NEGATION}",
    re.IGNORECASE,
)

# Unauthorized refund / reversal / unblock PROMISES -> rewrite to the safe phrase.
_PROMISE_PATTERNS = [
    r"\bwe(?:'ll| will)\s+(?:refund|reverse|return|unblock|reinstate|recover|credit)\b[^.।!?]*",
    r"\byou(?:'ll| will)\s+(?:be\s+refunded|get\s+(?:a\s+)?refund|receive\s+(?:a\s+)?refund)\b[^.।!?]*",
    r"\b(?:the\s+)?(?:refund|reversal|amount|money)\s+will\s+be\s+(?:processed|initiated|issued|returned|reversed|credited)\b[^.।!?]*",
    r"\b(?:the\s+)?(?:refund|reversal)\s+(?:process\s+)?(?:will\s+be|is\s+being)\s+initiated\b[^.।!?]*",
    r"\bwe\s+(?:are\s+)?(?:initiat\w+|process\w+)\s+(?:your\s+)?(?:refund|reversal)\b[^.।!?]*",
    r"\bwe\s+guarantee\b[^.।!?]*",
]
_PROMISE_RE = re.compile("|".join(_PROMISE_PATTERNS), re.IGNORECASE)


def _split_sentences(text: str) -> list[str]:
    """Split on ./!/?/। keeping it simple; preserves delimiters loosely."""
    parts = re.split(r"(?<=[.!?।])\s+", text.strip())
    return [p for p in parts if p.strip()]


def _is_credential_request(sentence: str) -> bool:
    if not _CRED_REQUEST_RE.search(sentence):
        return False
    # Allow if the credential mention is negated ("do not share your PIN").
    if _NEG_NEAR_CRED_RE.search(sentence):
        return False
    return True


def _strip_credential_requests(text: str) -> str:
    """Drop any sentence that asks the customer for a credential."""
    kept = [s for s in _split_sentences(text) if not _is_credential_request(s)]
    return " ".join(kept).strip()


_SAFE_PLACEHOLDER = "SAFE_RETURN"


def _rewrite_promises(text: str) -> str:
    """Replace refund/reversal promises with the safe official-channels phrase."""
    # Protect any already-safe phrasing so the promise patterns (which include
    # "amount will be returned") never re-match and duplicate it.
    text = re.sub(re.escape(SAFE_RETURN_PHRASE), _SAFE_PLACEHOLDER, text, flags=re.IGNORECASE)
    if not _PROMISE_RE.search(text):
        return text.replace(_SAFE_PLACEHOLDER, SAFE_RETURN_PHRASE)
    out = _PROMISE_RE.sub(SAFE_RETURN_PHRASE, text)
    out = out.replace(_SAFE_PLACEHOLDER, SAFE_RETURN_PHRASE)
    # Collapse a doubled safe phrase if two promises were adjacent.
    out = re.sub(
        re.escape(SAFE_RETURN_PHRASE) + r"[ ,.;]+" + re.escape(SAFE_RETURN_PHRASE),
        SAFE_RETURN_PHRASE,
        out,
        flags=re.IGNORECASE,
    )
    # Capitalize the phrase when it now starts a sentence (drops a leftover article).
    out = re.sub(
        r"(^|[.!?।]\s+)(?:[Tt]he\s+)?" + re.escape(SAFE_RETURN_PHRASE),
        lambda m: m.group(1) + "Any eligible amount will be returned through official channels",
        out,
    )
    return re.sub(r"\s{2,}", " ", out).strip()


def _has_reminder(text: str) -> bool:
    low = text.lower()
    has_negated_share = (
        "do not share" in low
        or "don't share" in low
        or "শেয়ার করবেন না" in text
        or "প্রদান করবেন না" in text  # "do not provide"
        or "দেবেন না" in text          # "do not give"
        or "বলবেন না" in text          # "do not tell"
        or "করবেন না" in text          # generic "do not ..." (Bangla negation suffix)
    )
    has_cred = "pin" in low or "otp" in low or "পিন" in text or "ওটিপি" in text
    return has_negated_share and has_cred


def _reminder_for(language: str) -> str:
    return PIN_REMINDER_BN if language == "bn" else PIN_REMINDER_EN


def enforce_safety(
    response: dict,
    complaint: str,
    user_type: Optional[str],
    language: Optional[str] = None,
) -> dict:
    """Rewrite unsafe prose and enforce escalation on injection. Mutates a copy."""
    out = dict(response)
    reply = str(out.get("customer_reply", "") or "")
    action = str(out.get("recommended_next_action", "") or "")

    # 1. Strip any credential requests from BOTH customer-facing fields.
    reply = _strip_credential_requests(reply)
    action = _strip_credential_requests(action)

    # 2. Rewrite unauthorized refund/reversal promises in BOTH fields.
    reply = _rewrite_promises(reply)
    action = _rewrite_promises(action)

    # 3. Manage the PIN/OTP reminder by user_type.
    lang = language or detect_language(reply or complaint)
    customer_facing = (user_type or "unknown") in ("customer", "unknown")
    is_phishing = out.get("case_type") == "phishing_or_social_engineering"
    if customer_facing:
        # Phishing replies already carry strong credential-safety language.
        if not _has_reminder(reply) and not is_phishing:
            reply = (reply + " " + _reminder_for(lang)).strip()
    else:
        # Merchant/agent replies are business-formal: drop a reminder if present.
        if _has_reminder(reply):
            reply = " ".join(
                s for s in _split_sentences(reply) if not _has_reminder(s)
            ).strip()

    out["customer_reply"] = re.sub(r"\s{2,}", " ", reply).strip()
    out["recommended_next_action"] = re.sub(r"\s{2,}", " ", action).strip()

    # 4. Prompt injection -> force human review + reason code (never let the
    #    complaint text lower the escalation).
    if contains_injection(complaint):
        out["human_review_required"] = True
        codes = list(out.get("reason_codes") or [])
        if "prompt_injection_detected" not in codes:
            codes.append("prompt_injection_detected")
        out["reason_codes"] = codes

    return out


# ----------------------------------------------------------------- schema gate
_VERDICTS = {"consistent", "inconsistent", "insufficient_data"}
_CASE_TYPES = {
    "wrong_transfer", "payment_failed", "refund_request", "duplicate_payment",
    "merchant_settlement_delay", "agent_cash_in_issue",
    "phishing_or_social_engineering", "other",
}
_SEVERITIES = {"low", "medium", "high", "critical"}
_DEPARTMENTS = {
    "customer_support", "dispute_resolution", "payments_ops",
    "merchant_operations", "agent_operations", "fraud_risk",
}


def _snap(value, allowed: set, default: str) -> str:
    return value if isinstance(value, str) and value in allowed else default


def enforce_schema(response: dict, valid_txn_ids: set[str]) -> dict:
    """Snap enums, clamp confidence, validate IDs. Guarantees a valid response."""
    out = dict(response)

    out["evidence_verdict"] = _snap(out.get("evidence_verdict"), _VERDICTS, "insufficient_data")
    out["case_type"] = _snap(out.get("case_type"), _CASE_TYPES, "other")
    out["severity"] = _snap(out.get("severity"), _SEVERITIES, "low")
    out["department"] = _snap(out.get("department"), _DEPARTMENTS, "customer_support")

    # relevant_transaction_id must be null or a real ID from the request history.
    rid = out.get("relevant_transaction_id")
    if rid is not None and rid not in valid_txn_ids:
        rid = None
    out["relevant_transaction_id"] = rid

    # human_review_required must be a real bool.
    out["human_review_required"] = bool(out.get("human_review_required"))

    # confidence: clamp to [0, 1] if present; drop if non-numeric.
    conf = out.get("confidence")
    if isinstance(conf, (int, float)) and not isinstance(conf, bool):
        out["confidence"] = max(0.0, min(1.0, float(conf)))
    else:
        out["confidence"] = None

    # reason_codes must be a list of strings.
    codes = out.get("reason_codes")
    if not isinstance(codes, list):
        codes = []
    out["reason_codes"] = [str(c) for c in codes if isinstance(c, (str, int, float))]

    # ticket_id must be a string.
    out["ticket_id"] = str(out.get("ticket_id", ""))

    # Ensure required prose fields are non-empty strings.
    for field in ("agent_summary", "recommended_next_action", "customer_reply"):
        val = out.get(field)
        out[field] = val.strip() if isinstance(val, str) and val.strip() else "Under review."

    return out
