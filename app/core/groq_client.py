"""
core/groq_client.py — Groq API wrapper with multi-key pool and 20-second budget.

Design pattern: Singleton (module-level pool) + Iterator (key rotation)

Key behaviours:
1. Multiple API keys loaded from GROQ_API_KEY (comma-separated).
2. Keys are tried in round-robin order; on rate-limit (429) or key exhaustion,
   the next key is attempted immediately.
3. A hard 20-second wall-clock budget is enforced across ALL retries and ALL keys.
   If the budget is exceeded, a RuntimeError is raised and the caller falls back.
4. JSON fence stripping + enum coercion ensure schema compliance even when the
   model wraps output in markdown or uses non-canonical enum strings.
"""
import json
import re
import time
import logging
from typing import Optional

from groq import Groq
from groq import RateLimitError, APIStatusError

from app.config import get_settings
from app.enums import CaseType, Department, EvidenceVerdict, Severity

logger = logging.getLogger(__name__)

# ── Valid Enum Sets ────────────────────────────────────────────────────────────
_VALID_ENUMS: dict[str, set[str]] = {
    "case_type":        {e.value for e in CaseType},
    "department":       {e.value for e in Department},
    "evidence_verdict": {e.value for e in EvidenceVerdict},
    "severity":         {e.value for e in Severity},
}

_ENUM_DEFAULTS: dict[str, str] = {
    "case_type":        "other",
    "department":       "customer_support",
    "evidence_verdict": "insufficient_data",
    "severity":         "medium",
}


# ── Module-level Key Pool (Singleton) ─────────────────────────────────────────

class _GroqKeyPool:
    """
    Round-robin pool of Groq API keys.
    Thread-safe enough for single-process async use.
    """

    def __init__(self, keys: list[str]) -> None:
        if not keys:
            raise ValueError("No GROQ_API_KEY provided. Add keys to .env.")
        self._keys = keys
        self._clients = [Groq(api_key=k) for k in keys]
        self._index = 0

    @property
    def current_client(self) -> Groq:
        return self._clients[self._index % len(self._clients)]

    def rotate(self) -> None:
        """Advance to the next key."""
        self._index = (self._index + 1) % len(self._clients)

    def __len__(self) -> int:
        return len(self._clients)


_pool: Optional[_GroqKeyPool] = None


def _get_pool() -> _GroqKeyPool:
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = _GroqKeyPool(settings.groq_api_keys)
    return _pool


# ── Helpers ────────────────────────────────────────────────────────────────────

def _strip_json_fences(text: str) -> str:
    """Remove ```json ... ``` or ``` ... ``` wrappers if the model added them."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def coerce_enum(value: str, field: str) -> str:
    """
    Fuzzy-match an LLM-emitted string to a valid enum value.
    Order of attempts:
      1. Exact match
      2. Lowercase + underscore normalisation
      3. Partial match (substring either way)
      4. Safe default
    """
    valid = _VALID_ENUMS[field]
    if value in valid:
        return value
    normalised = value.lower().strip().replace(" ", "_").replace("-", "_")
    if normalised in valid:
        return normalised
    for v in valid:
        if normalised in v or v in normalised:
            return v
    logger.warning("coerce_enum: could not match '%s' for field '%s', using default", value, field)
    return _ENUM_DEFAULTS[field]


def _ensure_required_keys(parsed: dict) -> dict:
    """
    Apply safe defaults for any required keys missing from the LLM response.
    Also normalise relevant_transaction_id empty string → None.
    """
    parsed.setdefault("human_review_required", True)
    parsed.setdefault("confidence", 0.5)
    parsed.setdefault("reason_codes", [])
    parsed.setdefault("relevant_transaction_id", None)

    if parsed.get("relevant_transaction_id") == "":
        parsed["relevant_transaction_id"] = None

    if parsed.get("reason_codes") is None:
        parsed["reason_codes"] = []

    return parsed


# ── Main Call ─────────────────────────────────────────────────────────────────

def call_groq(system_prompt: str, user_message: str) -> dict:
    """
    Call Groq with json_object mode and retry across multiple API keys.

    Strategy:
    - Outer loop: rotate through all available keys once.
    - Inner loop: retry up to max_retries on transient errors per key.
    - Hard deadline: raise RuntimeError if 20-second budget is exceeded.

    Returns a dict with all required response fields, enum-coerced.
    Raises RuntimeError on full failure (caller uses fallback_response).
    """
    settings = get_settings()
    pool = _get_pool()
    budget = settings.groq_timeout_budget_seconds
    deadline = time.monotonic() + budget

    total_keys = len(pool)
    keys_tried = 0

    while keys_tried < total_keys:
        client = pool.current_client
        key_label = f"key[{pool._index}]"

        for attempt in range(settings.groq_max_retries):
            # Hard budget check
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(
                    f"Groq call exceeded {budget}s budget — activating fallback."
                )

            try:
                logger.debug(
                    "Groq call: %s attempt=%d remaining=%.1fs",
                    key_label, attempt + 1, remaining,
                )
                response = client.chat.completions.create(
                    model=settings.groq_model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": user_message},
                    ],
                    response_format={"type": "json_object"},
                    temperature=settings.groq_temperature,
                    max_tokens=settings.groq_max_tokens,
                    timeout=min(remaining - 0.5, 15.0),  # leave 0.5s for overhead
                )

                raw = response.choices[0].message.content
                raw = _strip_json_fences(raw)
                parsed: dict = json.loads(raw)

                # Coerce enum fields
                for enum_field in ("case_type", "department", "evidence_verdict", "severity"):
                    if enum_field in parsed:
                        parsed[enum_field] = coerce_enum(str(parsed[enum_field]), enum_field)

                parsed = _ensure_required_keys(parsed)
                logger.debug("Groq call succeeded: %s", key_label)
                return parsed

            except RateLimitError:
                logger.warning(
                    "%s hit rate limit on attempt %d — rotating to next key.",
                    key_label, attempt + 1,
                )
                # Rate limit → rotate immediately, no sleep
                break  # break inner loop to try next key

            except json.JSONDecodeError as e:
                logger.warning("%s JSON parse error: %s", key_label, e)
                # Retry on same key (model output glitch)
                if attempt < settings.groq_max_retries - 1:
                    sleep_time = min(2 ** attempt, deadline - time.monotonic() - 0.5)
                    if sleep_time > 0:
                        time.sleep(sleep_time)
                    continue
                break  # exhausted retries on this key

            except APIStatusError as e:
                if e.status_code == 429:
                    logger.warning("%s 429 rate limit — rotating key.", key_label)
                    break
                logger.warning("%s API error %d: %s", key_label, e.status_code, e)
                if attempt < settings.groq_max_retries - 1:
                    sleep_time = min(2 ** attempt, deadline - time.monotonic() - 0.5)
                    if sleep_time > 0:
                        time.sleep(sleep_time)
                    continue
                break

            except Exception as e:
                logger.warning("%s unexpected error: %s", key_label, e)
                if attempt < settings.groq_max_retries - 1:
                    sleep_time = min(1.0, deadline - time.monotonic() - 0.5)
                    if sleep_time > 0:
                        time.sleep(sleep_time)
                    continue
                break

        # Rotate to next key and count the attempt
        pool.rotate()
        keys_tried += 1

    raise RuntimeError("All Groq API keys exhausted — activating fallback.")
