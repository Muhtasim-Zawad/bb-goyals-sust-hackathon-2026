"""
core/evidence.py — Deterministic evidence engine.

Design pattern: Strategy
- Amount, time, and operation-type extractors are independent strategies.
- score_transaction() composes them for a final match score.
- detect_duplicate() is a separate pass specifically for duplicate-payment detection.

This layer runs BEFORE the LLM and provides a structured pre-analysis block
that anchors the model's reasoning. This dramatically improves accuracy for
amounts, timestamps, and duplicate patterns.
"""
import re
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

from app.models.request import TransactionEntry


# ── Extraction Patterns ────────────────────────────────────────────────────────

AMOUNT_PATTERNS: list[str] = [
    r"৳\s*(\d[\d,]*)",                              # ৳5000
    r"(\d[\d,]*)\s*(?:taka|টাকা|tk|bdt)",           # 500 taka / ৫০০ টাকা / 500 tk
    r"\b(\d{3,6})\b",                               # bare number 500–999999
]

OP_KEYWORDS: dict[str, list[str]] = {
    "transfer":  ["sent", "pathiye", "pathai", "transfer", "pathiyechi", "diyechi", "pathano"],
    "payment":   ["paid", "payment", "kine", "merchant", "shop", "buy", "bought", "bill"],
    "cash_out":  ["withdraw", "cash out", "tola", "tulte"],
    "cash_in":   ["deposit", "cash in", "joma", "add money", "ক্যাশ ইন"],
    "refund":    ["refund", "ফেরত", "ferot", "return"],
    "settlement": ["settle", "settlement", "settled"],
}


# ── Dataclass for Evidence Result ─────────────────────────────────────────────

@dataclass
class EvidenceResult:
    transaction_id: Optional[str]
    pre_verdict_signal: str       # "consistent_signal" | "inconsistent_signal" | "check_required"
    match_score: int
    duplicate_id: Optional[str] = None


# ── Strategy: Amount Extraction ───────────────────────────────────────────────

def extract_amounts(text: str) -> set[float]:
    """Extract all monetary amounts mentioned in the complaint."""
    amounts: set[float] = set()
    for pattern in AMOUNT_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            try:
                amounts.add(float(match.group(1).replace(",", "")))
            except (IndexError, ValueError):
                pass
    return amounts


# ── Strategy: Time Window Extraction ─────────────────────────────────────────

_TIME_LABELS: dict[str, tuple[int, int]] = {
    "morning":   (6, 12),
    "afternoon": (12, 17),
    "evening":   (17, 21),
    "night":     (21, 24),
    "সকাল":     (6, 12),   # Bangla: morning
    "বিকাল":    (12, 17),  # Bangla: afternoon
    "রাত":      (20, 24),  # Bangla: night
}


def extract_time_window(text: str) -> Optional[tuple[int, int]]:
    """
    Return an (hour_start, hour_end) window hinted by the complaint.
    Returns None if no time hint — downstream treats None as neutral (no penalty).
    """
    text_lower = text.lower()

    # Explicit hour: "around 2pm", "at 14:00", "9:30 am"
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", text_lower, re.IGNORECASE)
    if m:
        hour = int(m.group(1))
        if m.group(3).lower() == "pm" and hour != 12:
            hour += 12
        elif m.group(3).lower() == "am" and hour == 12:
            hour = 0
        return (max(0, hour - 1), min(23, hour + 1))

    # 24h clock: "09:30", "18:00"
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", text_lower)
    if m:
        hour = int(m.group(1))
        return (max(0, hour - 1), min(23, hour + 1))

    # Label-based
    for label, window in _TIME_LABELS.items():
        if label in text_lower:
            return window

    return None


# ── Strategy: Operation Type Extraction ──────────────────────────────────────

def extract_op_type(text: str) -> set[str]:
    """Return set of operation types hinted by the complaint text."""
    text_lower = text.lower()
    matched: set[str] = set()
    for op_type, keywords in OP_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            matched.add(op_type)
    return matched


# ── Time Window Matching ──────────────────────────────────────────────────────

def _time_within_window(timestamp_iso: str, window: Optional[tuple[int, int]]) -> bool:
    if window is None:
        return True  # no time hint → neutral, don't penalise
    try:
        dt = datetime.fromisoformat(timestamp_iso.replace("Z", "+00:00"))
        return window[0] <= dt.hour <= window[1]
    except Exception:
        return True  # parse failure → neutral


# ── Transaction Scorer ────────────────────────────────────────────────────────

def _score_transaction(
    txn: TransactionEntry,
    complaint: str,
    amounts: set[float],
    time_window: Optional[tuple[int, int]],
    op_types: set[str],
) -> int:
    """
    Score a single transaction against extracted complaint signals.
    Higher score = stronger match.

    Max achievable: ~14 points
    Threshold for a valid match: ≥ 2 points
    Threshold for consistent_signal: ≥ 5 points
    """
    score = 0

    # Amount match (strongest signal)
    if txn.amount in amounts:
        score += 4
    elif amounts and any(abs(txn.amount - a) / max(a, 1) < 0.05 for a in amounts):
        score += 2  # within 5% — rounding artefact

    # Time window match
    if _time_within_window(txn.timestamp, time_window):
        score += 2

    # Operation type match
    if txn.type in op_types:
        score += 2

    # Status-based signals
    complaint_lower = complaint.lower()
    if txn.status == "failed" and ("deduct" in complaint_lower or "kete" in complaint_lower):
        score += 3
    if txn.status == "pending" and ("not received" in complaint_lower
                                    or "আসেনি" in complaint
                                    or "পাইনি" in complaint):
        score += 2
    if txn.status == "completed" and "দেখছি না" in complaint:
        score += 1

    return score


# ── Duplicate Detector ────────────────────────────────────────────────────────

def detect_duplicate(history: list[TransactionEntry]) -> Optional[str]:
    """
    Return the transaction_id of the likely duplicate if a duplicate payment
    pattern is detected (same type + amount + counterparty within 5 minutes).
    """
    seen: dict[tuple, tuple[str, datetime]] = {}

    for txn in sorted(history, key=lambda t: t.timestamp):
        try:
            dt = datetime.fromisoformat(txn.timestamp.replace("Z", "+00:00"))
            key = (txn.type, txn.amount, txn.counterparty)
            if key in seen:
                prev_id, prev_dt = seen[key]
                if abs((dt - prev_dt).total_seconds()) < 300:  # within 5 minutes
                    return txn.transaction_id  # this is the duplicate
            seen[key] = (txn.transaction_id, dt)
        except Exception:
            continue

    return None


# ── Main Entry Point ──────────────────────────────────────────────────────────

class EvidenceEngine:
    """
    Strategy-composed evidence engine.
    Orchestrates extraction strategies and produces an EvidenceResult.
    """

    def match(
        self,
        complaint: str,
        history: list[TransactionEntry],
    ) -> EvidenceResult:
        """
        Run all extraction strategies and score each transaction.
        Returns an EvidenceResult with the best match and a pre-verdict signal.
        """
        dup_id = detect_duplicate(history)

        if not history:
            return EvidenceResult(
                transaction_id=None,
                pre_verdict_signal="check_required",
                match_score=0,
                duplicate_id=dup_id,
            )

        amounts = extract_amounts(complaint)
        time_window = extract_time_window(complaint)
        op_types = extract_op_type(complaint)

        best_txn: Optional[TransactionEntry] = None
        best_score: int = 0

        for txn in history:
            s = _score_transaction(txn, complaint, amounts, time_window, op_types)
            if s > best_score:
                best_score = s
                best_txn = txn

        # Require at least score=2 for any match
        if best_txn is None or best_score < 2:
            return EvidenceResult(
                transaction_id=None,
                pre_verdict_signal="check_required",
                match_score=0,
                duplicate_id=dup_id,
            )

        # Determine pre-verdict signal
        if best_score >= 5:
            signal = "consistent_signal"
        elif (
            best_txn.status == "completed"
            and amounts
            and best_txn.amount not in amounts
        ):
            signal = "inconsistent_signal"
        else:
            signal = "check_required"

        return EvidenceResult(
            transaction_id=best_txn.transaction_id,
            pre_verdict_signal=signal,
            match_score=best_score,
            duplicate_id=dup_id,
        )
