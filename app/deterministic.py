"""Deterministic fallback investigator.

Runs ONLY when the Groq LLM path is unavailable (no keys, total failure, or
timeout). It reproduces the LLM's judgment with pure rules, then reuses the
shared derivation tables (app/derive.py) for severity/department/etc.

Design intent: capture the ESSENCE of each rule (general signals), not memorize
the 10 public samples. No transaction IDs, amounts, or ticket IDs are hardcoded.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from .derive import apply_derivations
from .normalizer import (
    bangla_to_ascii,
    contains_injection,
    detect_language,
    normalize_phone,
    phones_match,
)

if TYPE_CHECKING:
    from .models import TicketRequest, TransactionEntry

DUPLICATE_WINDOW_SECONDS = 300  # two near-identical payments within 5 min => duplicate

# ---- keyword sets (en + bn + romanized banglish); essence, not exhaustive ----
_KW = {
    "phishing": [
        "otp", "pin", "password", "ওটিপি", "পিন", "পাসওয়ার্ড", "verification code",
        "scam", "fraud", "প্রতারণা", "blocked", "block kore", "ব্লক", "suspicious",
        "asking for", "চাইছে", "account will be", "verify your account",
    ],
    "duplicate": [
        "twice", "two times", "double", "duplicate", "দুইবার", "দুবার", "dui bar",
        "again", "second time", "charged twice", "deducted twice",
    ],
    "failed": [
        "failed", "fail", "ফেইল", "ফেল", "unsuccessful", "declined", "did not go through",
        "hoyni", "হয়নি", "but balance", "balance deducted", "টাকা কেটে",
    ],
    "wrong_transfer": [
        "wrong number", "wrong person", "ভুল নম্বর", "ভুল মানুষ", "bhul number",
        "wrong recipient", "mistake", "ভুল করে", "bhul kore", "typed it wrong",
        "did not get it", "didn't get it", "did not receive", "পায়নি", "পাইনি", "পাননি",
    ],
    "cash_in": [
        "cash in", "cash-in", "ক্যাশ ইন", "cash in koresi", "agent", "এজেন্ট",
        "deposit", "জমা", "balance ashe ni", "ব্যালেন্সে আসেনি", "balance e ashe ni",
    ],
    "settlement": [
        # 'merchant' alone is too broad (a refund mentions paying a merchant), so
        # settlement fires only on genuine settlement/payout signals.
        "settlement", "settle", "সেটেলমেন্ট", "payout", "not settled", "settle hoyni",
    ],
    "refund": [
        "refund", "রিফান্ড", "ferot", "ফেরত", "money back", "return my", "changed my mind",
        "don't want", "চাই না", "cancel",
    ],
}


def _norm(text: Optional[str]) -> str:
    return bangla_to_ascii(text or "").lower()


def _has_any(text: str, keys: list[str]) -> bool:
    t = text.lower()
    return any(k.lower() in t for k in keys)


def _parse_ts(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


# ---------------------------------------------------------------- amount match
def _complaint_numbers(complaint: str) -> set[float]:
    """All number-like tokens in the complaint (Bangla numerals normalized)."""
    text = bangla_to_ascii(complaint or "")
    nums: set[float] = set()
    for tok in re.findall(r"\d[\d,]*(?:\.\d+)?", text):
        try:
            nums.add(float(tok.replace(",", "")))
        except ValueError:
            continue
    return nums


def _match_amount(complaint: str, history: list) -> Optional[float]:
    """Pick the transaction amount the complaint refers to.

    Robust to phone numbers: we intersect the numbers in the complaint with the
    amounts actually present in the history, so '01712345678' never reads as an
    amount. Returns the amount only if exactly one history amount is mentioned.
    """
    if not history:
        return None
    nums = _complaint_numbers(complaint)
    hist_amounts = {t.amount for t in history}
    hit = [a for a in hist_amounts if a in nums]
    return hit[0] if len(hit) == 1 else None


def _complaint_phones(complaint: str) -> list[str]:
    text = bangla_to_ascii(complaint or "")
    return [normalize_phone(m) for m in re.findall(r"(?:\+?880)?1\d{8,9}", text)]


# ---------------------------------------------------------------- matcher
def _find_duplicate(history: list) -> Optional["TransactionEntry"]:
    """Return the LATER of a duplicate completed-payment pair, if any."""
    payments = [t for t in history if t.type.value == "payment" and t.status.value == "completed"]
    for i in range(len(payments)):
        for j in range(i + 1, len(payments)):
            a, b = payments[i], payments[j]
            if a.amount == b.amount and a.counterparty == b.counterparty:
                ta, tb = _parse_ts(a.timestamp), _parse_ts(b.timestamp)
                if ta and tb and abs((tb - ta).total_seconds()) <= DUPLICATE_WINDOW_SECONDS:
                    return b if tb >= ta else a
    return None


def match_transaction(request: "TicketRequest") -> tuple[Optional[str], bool, Optional["TransactionEntry"]]:
    """Return (relevant_transaction_id, is_duplicate, matched_tx)."""
    history = request.transaction_history or []
    if not history:
        return None, False, None

    dup = _find_duplicate(history)
    if dup is not None:
        return dup.transaction_id, True, dup

    amount = _match_amount(request.complaint, history)
    if amount is None:
        return None, False, None

    candidates = [t for t in history if t.amount == amount]
    if len(candidates) == 1:
        return candidates[0].transaction_id, False, candidates[0]

    # Several share the amount: disambiguate ONLY by a phone named in the complaint.
    phones = [p for p in _complaint_phones(request.complaint) if p]
    if phones:
        narrowed = [t for t in candidates if any(phones_match(t.counterparty, p) for p in phones)]
        if len(narrowed) == 1:
            return narrowed[0].transaction_id, False, narrowed[0]

    # Ambiguous -> do not guess.
    return None, False, None


# ---------------------------------------------------------------- verdict
def determine_verdict(
    request: "TicketRequest", matched: Optional["TransactionEntry"], is_duplicate: bool
) -> str:
    history = request.transaction_history or []
    if matched is None:
        return "insufficient_data"
    if is_duplicate:
        return "consistent"

    text = _norm(request.complaint)
    status = matched.status.value

    # Status mismatch: claims failure but the transaction completed.
    claims_failed = _has_any(text, _KW["failed"])
    if claims_failed and status == "completed" and matched.type.value != "payment":
        return "inconsistent"

    # Behavioural mismatch: wrong/unknown recipient claim, but >=2 transfers to
    # the same counterparty (an established recipient).
    claims_wrong = _has_any(text, ["wrong number", "wrong person", "ভুল নম্বর", "bhul number",
                                    "wrong recipient", "unknown"])
    if claims_wrong and matched.type.value == "transfer":
        same_cp = [t for t in history if t.type.value == "transfer"
                   and t.counterparty == matched.counterparty]
        if len(same_cp) >= 2:
            return "inconsistent"

    return "consistent"


# ---------------------------------------------------------------- classifier
def classify_case(
    request: "TicketRequest", matched: Optional["TransactionEntry"], is_duplicate: bool
) -> str:
    text = _norm(request.complaint)

    if _has_any(text, _KW["phishing"]):
        return "phishing_or_social_engineering"
    if is_duplicate or (_has_any(text, _KW["duplicate"]) and matched is not None):
        return "duplicate_payment"
    # A "refund" ask on a FAILED payment is still payment_failed.
    if _has_any(text, _KW["failed"]) or (matched is not None and matched.status.value == "failed"):
        return "payment_failed"
    if _has_any(text, _KW["cash_in"]) and (
        matched is None or matched.type.value == "cash_in"
    ):
        # cash_in keywords + agent context
        if _has_any(text, ["agent", "এজেন্ট", "cash in", "ক্যাশ ইন"]):
            return "agent_cash_in_issue"
    if _has_any(text, _KW["settlement"]) or (
        matched is not None and matched.type.value == "settlement"
    ):
        return "merchant_settlement_delay"
    if _has_any(text, _KW["wrong_transfer"]):
        return "wrong_transfer"
    if _has_any(text, _KW["refund"]):
        return "refund_request"
    return "other"


# ---------------------------------------------------------------- prose (bilingual)
def _is_bangla(request: "TicketRequest") -> bool:
    lang = request.language.value if request.language else detect_language(request.complaint)
    return lang == "bn"


def _customer_facing(request: "TicketRequest") -> bool:
    ut = request.user_type.value if request.user_type else "unknown"
    return ut in ("customer", "unknown")


_PIN_EN = " Please do not share your PIN or OTP with anyone."
_PIN_BN = " অনুগ্রহ করে আপনার পিন বা ওটিপি কারও সাথে শেয়ার করবেন না।"

# Per-case templates: (summary, next_action, reply_en, reply_bn). Replies use the
# safe refund phrasing and never request credentials.
_TEMPLATES = {
    "wrong_transfer": (
        "Customer reports a transfer to an unintended recipient{txn}.",
        "Verify the transaction with the customer and initiate the wrong-transfer dispute workflow per policy.",
        "We have noted your concern about transaction{txn}. Our dispute team will review the case and contact you through official support channels.",
        "আপনার লেনদেন{txn} সম্পর্কে আমরা অবগত হয়েছি। আমাদের ডিসপিউট দল বিষয়টি পর্যালোচনা করে অফিসিয়াল চ্যানেলে আপনার সাথে যোগাযোগ করবে।",
    ),
    "payment_failed": (
        "Customer reports a failed payment with a possible balance deduction{txn}.",
        "Investigate the transaction ledger status; if balance was deducted on a failed payment, initiate the automatic reversal flow within standard SLA.",
        "We have noted that transaction{txn} may have caused an unexpected balance deduction. Our payments team will review the case and any eligible amount will be returned through official channels.",
        "আমরা লক্ষ্য করেছি যে লেনদেন{txn} এর কারণে অনাকাঙ্ক্ষিত ব্যালেন্স কাটা যেতে পারে। আমাদের পেমেন্টস দল বিষয়টি যাচাই করবে এবং প্রযোজ্য কোনো অর্থ অফিসিয়াল চ্যানেলের মাধ্যমে ফেরত দেওয়া হবে।",
    ),
    "duplicate_payment": (
        "Customer reports a possible duplicate payment{txn}.",
        "Verify the duplicate with payments operations; if the biller confirms a single payment, initiate reversal of the duplicate.",
        "We have noted the possible duplicate payment for transaction{txn}. Our payments team will verify with the biller and any eligible amount will be returned through official channels.",
        "লেনদেন{txn} এর সম্ভাব্য ডুপ্লিকেট পেমেন্ট আমরা লক্ষ্য করেছি। আমাদের পেমেন্টস দল বিলারের সাথে যাচাই করবে এবং প্রযোজ্য কোনো অর্থ অফিসিয়াল চ্যানেলের মাধ্যমে ফেরত দেওয়া হবে।",
    ),
    "agent_cash_in_issue": (
        "Customer reports an agent cash-in not reflected in balance{txn}.",
        "Investigate the pending cash-in status with agent operations and resolve within the standard cash-in SLA.",
        "We have noted your concern about transaction{txn}. Our agent operations team will investigate quickly and update you through official channels.",
        "আপনার লেনদেন{txn} এর বিষয়ে আমরা অবগত হয়েছি। আমাদের এজেন্ট অপারেশনস দল দ্রুত যাচাই করবে এবং অফিসিয়াল চ্যানেলে আপনাকে জানাবে।",
    ),
    "merchant_settlement_delay": (
        "Merchant reports a delayed settlement beyond the expected window{txn}.",
        "Route to merchant operations to verify the settlement batch status and communicate a revised ETA if delayed.",
        "We have noted your concern about settlement{txn}. Our merchant operations team will check the batch status and update you on the expected settlement time through official channels.",
        "সেটেলমেন্ট{txn} সম্পর্কে আমরা অবগত হয়েছি। আমাদের মার্চেন্ট অপারেশনস দল ব্যাচ স্ট্যাটাস যাচাই করে অফিসিয়াল চ্যানেলে আপনাকে জানাবে।",
    ),
    "phishing_or_social_engineering": (
        "Customer reports a suspected phishing / social-engineering attempt.",
        "Escalate to the fraud_risk team. Confirm to the customer that the company never asks for OTP/PIN, and log any reported number for fraud pattern analysis.",
        "Thank you for reaching out. We never ask for your PIN, OTP, or password under any circumstances. Please do not share these with anyone, even if they claim to be from us. Our fraud team has been notified.",
        "যোগাযোগ করার জন্য ধন্যবাদ। আমরা কখনোই আপনার পিন, ওটিপি বা পাসওয়ার্ড চাই না। কেউ আমাদের নাম করলেও এগুলো কারও সাথে শেয়ার করবেন না। আমাদের ফ্রড দলকে বিষয়টি জানানো হয়েছে।",
    ),
    "refund_request": (
        "Customer requests a refund for a completed payment{txn} (change of mind, not a service failure).",
        "Inform the customer that refund eligibility depends on the merchant's policy and guide them to contact the merchant directly.",
        "Thank you for reaching out. Refunds for completed merchant payments depend on the merchant's own policy. We recommend contacting the merchant directly; if you need help reaching them, please reply and we will guide you.",
        "যোগাযোগ করার জন্য ধন্যবাদ। সম্পন্ন হওয়া মার্চেন্ট পেমেন্টের রিফান্ড মার্চেন্টের নিজস্ব নীতির উপর নির্ভর করে। সরাসরি মার্চেন্টের সাথে যোগাযোগ করার পরামর্শ দিচ্ছি; প্রয়োজনে উত্তর দিন, আমরা সহায়তা করব।",
    ),
    "other": (
        "Customer reports a concern that lacks enough detail to identify a specific transaction.",
        "Reply to the customer asking for specifics: which transaction, what amount, what went wrong, and approximate time.",
        "Thank you for reaching out. To help you faster, please share the transaction ID, the amount involved, and a short description of what went wrong.",
        "যোগাযোগ করার জন্য ধন্যবাদ। দ্রুত সহায়তার জন্য অনুগ্রহ করে লেনদেন আইডি, সংশ্লিষ্ট পরিমাণ এবং কী সমস্যা হয়েছে তার সংক্ষিপ্ত বিবরণ জানান।",
    ),
}


def _build_prose(request: "TicketRequest", case_type: str, txn_id: Optional[str]) -> dict:
    summary_t, action_t, reply_en, reply_bn = _TEMPLATES.get(case_type, _TEMPLATES["other"])
    txn_phrase = f" {txn_id}" if txn_id else ""
    summary = summary_t.format(txn=f" ({txn_id})" if txn_id else "")
    action = action_t

    bangla = _is_bangla(request)
    reply = reply_bn if bangla else reply_en
    reply = reply.format(txn=txn_phrase)

    # Append the credential-safety reminder for customer-facing replies only.
    if _customer_facing(request) and case_type != "phishing_or_social_engineering":
        reply += _PIN_BN if bangla else _PIN_EN

    return {
        "agent_summary": summary,
        "recommended_next_action": action,
        "customer_reply": reply.strip(),
    }


def _reason_codes(case_type: str, verdict: str, matched, injection: bool) -> list[str]:
    codes = [case_type]
    if matched is not None:
        codes.append("transaction_match")
    else:
        codes.append("no_transaction_match")
    codes.append(f"evidence_{verdict}")
    if injection:
        codes.append("prompt_injection_detected")
    return codes[:4]


# ---------------------------------------------------------------- entry point
def run_deterministic(request: "TicketRequest") -> dict:
    """Full rule-based investigation. Always returns a complete response dict."""
    txn_id, is_duplicate, matched = match_transaction(request)
    case_type = classify_case(request, matched, is_duplicate)

    # Phishing never depends on a transaction; force null match for it.
    if case_type == "phishing_or_social_engineering":
        txn_id, matched = None, None
        verdict = "insufficient_data"
    else:
        verdict = determine_verdict(request, matched, is_duplicate)

    prose = _build_prose(request, case_type, txn_id)
    injection = contains_injection(request.complaint)

    judgment = {
        "relevant_transaction_id": txn_id,
        "evidence_verdict": verdict,
        "case_type": case_type,
        "agent_summary": prose["agent_summary"],
        "recommended_next_action": prose["recommended_next_action"],
        "customer_reply": prose["customer_reply"],
        "reason_codes": _reason_codes(case_type, verdict, matched, injection),
    }
    return apply_derivations(judgment, request)
