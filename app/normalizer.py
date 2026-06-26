"""Text and data normalization helpers.

Used by both the LLM prompt builder (to feed clean text to the model) and the
deterministic fallback engine (to parse amounts, phones, language, etc.).
All functions are pure and dependency-free so they are safe to call anywhere.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

# Bangla (Bengali) digit code points U+09E6..U+09EF map to ASCII 0..9.
_BANGLA_DIGITS = "০১২৩৪৫৬৭৮৯"
_ASCII_DIGITS = "0123456789"
_BANGLA_TO_ASCII_TABLE = {ord(b): a for b, a in zip(_BANGLA_DIGITS, _ASCII_DIGITS)}

# Unicode range for Bengali script (letters), used for language detection.
_BENGALI_LETTER_RE = re.compile(r"[ঀ-৿]")
_LATIN_LETTER_RE = re.compile(r"[A-Za-z]")

# Prompt-injection markers. Kept conservative to avoid false positives on
# normal complaints. Matched case-insensitively against normalized text.
_INJECTION_PATTERNS = [
    r"ignore (?:all |any |the )?(?:previous|prior|above|earlier) (?:instruction|prompt|message|rule)",
    r"disregard (?:all |any |the )?(?:previous|prior|above|earlier)",
    r"forget (?:all |any |the )?(?:previous|prior|above|earlier)",
    r"system prompt",
    r"you are now",
    r"act as (?:a |an )?(?:different|new)",
    r"set\s+human_review_required",
    r"set\s+\w+\s*=\s*(?:true|false)",
    r"override (?:the )?(?:system|safety|rule)",
    r"new instruction",
    r"do not (?:flag|escalate|review)",
    r"reveal (?:your )?(?:system )?(?:prompt|instruction)",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)


def bangla_to_ascii(text: Optional[str]) -> str:
    """Convert Bangla numerals (০-৯) to ASCII digits (0-9). Leaves the rest intact."""
    if not text:
        return ""
    return text.translate(_BANGLA_TO_ASCII_TABLE)


def normalize_phone(phone: Optional[str]) -> str:
    """Canonicalize a Bangladeshi phone number to local `01XXXXXXXXX` form.

    Handles `+8801…`, `8801…`, `01…`, and Bangla-digit variants. Returns the
    digit string stripped of spaces/dashes; if it does not look like a BD number
    the best-effort digit string is returned so callers can still compare.
    """
    if not phone:
        return ""
    digits = re.sub(r"\D", "", bangla_to_ascii(phone))
    if not digits:
        return ""
    # +8801XXXXXXXXX / 8801XXXXXXXXX -> 01XXXXXXXXX
    if digits.startswith("880") and len(digits) >= 13:
        digits = "0" + digits[3:]
    elif digits.startswith("8801"):
        digits = digits[2:]
    # Ensure a leading 0 for an 11-digit local mobile that lost it (1XXXXXXXXX).
    if len(digits) == 10 and digits.startswith("1"):
        digits = "0" + digits
    return digits


def phones_match(a: Optional[str], b: Optional[str]) -> bool:
    """True if two phone-ish strings refer to the same number after normalization."""
    na, nb = normalize_phone(a), normalize_phone(b)
    if not na or not nb:
        return False
    # Compare on the last 10 significant digits to be tolerant of prefixes.
    return na[-10:] == nb[-10:]


def detect_language(text: Optional[str]) -> str:
    """Classify text as 'en', 'bn', or 'mixed' from script composition."""
    if not text:
        return "en"
    has_bengali = bool(_BENGALI_LETTER_RE.search(text))
    has_latin = bool(_LATIN_LETTER_RE.search(text))
    if has_bengali and has_latin:
        return "mixed"
    if has_bengali:
        return "bn"
    return "en"


def contains_injection(text: Optional[str]) -> bool:
    """Detect prompt-injection markers in untrusted complaint text."""
    if not text:
        return False
    return bool(_INJECTION_RE.search(bangla_to_ascii(text)))


def extract_valid_txn_ids(history: Optional[Iterable]) -> set[str]:
    """Collect the set of transaction IDs present in a transaction history.

    Accepts either Pydantic `TransactionEntry` objects or plain dicts, so it is
    usable both before and after request validation. Used by the schema gate to
    verify the model never invents a `relevant_transaction_id`.
    """
    ids: set[str] = set()
    if not history:
        return ids
    for entry in history:
        txn_id = None
        if isinstance(entry, dict):
            txn_id = entry.get("transaction_id")
        else:
            txn_id = getattr(entry, "transaction_id", None)
        if isinstance(txn_id, str) and txn_id:
            ids.add(txn_id)
    return ids
