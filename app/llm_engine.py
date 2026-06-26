"""Groq-backed LLM judgment engine with comma-separated multi-key rotation.

The engine asks Groq for the judgment + prose fields only (see app/llm_prompt).
`GROQ_API_KEY` may hold several comma-separated keys; on any failure (rate
limit, auth error, timeout, bad JSON) the engine rotates to the next key.

`call_llm()` returns a validated judgment dict, or `None` when every key fails
or the overall 20s budget is exhausted — the caller then uses the deterministic
fallback. Nothing here ever raises to the caller.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import TYPE_CHECKING, Optional

from .llm_prompt import build_system_prompt, build_user_message
from .models import LLMJudgment

if TYPE_CHECKING:
    from .models import TicketRequest

logger = logging.getLogger("queuestorm.llm")

DEFAULT_MODEL = "llama-3.3-70b-versatile"
OVERALL_TIMEOUT = 20.0          # hard cap for the whole rotation
PER_CALL_TIMEOUT = 12.0         # cap for a single Groq call

# Module-level state, populated by init_clients() at app startup.
_clients: list = []             # list[(masked_key, AsyncGroq)]
_model: str = DEFAULT_MODEL
_system_prompt: str = ""


def _parse_keys(raw: Optional[str]) -> list[str]:
    """Split the comma-separated GROQ_API_KEY into a clean, de-duplicated list."""
    if not raw:
        return []
    seen: set[str] = set()
    keys: list[str] = []
    for part in raw.split(","):
        k = part.strip()
        if k and k not in seen and not k.startswith("key_"):  # ignore placeholder values
            seen.add(k)
            keys.append(k)
    return keys


def _mask(key: str) -> str:
    return f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "key"


def init_clients() -> None:
    """Pre-warm Groq clients at startup (instantiate, don't call). Idempotent."""
    global _clients, _model, _system_prompt
    _model = os.getenv("GROQ_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    _system_prompt = build_system_prompt()
    keys = _parse_keys(os.getenv("GROQ_API_KEY"))
    _clients = []
    if not keys:
        logger.warning("No usable GROQ_API_KEY configured; LLM path disabled, fallback only.")
        return
    try:
        from groq import AsyncGroq
    except Exception as exc:  # pragma: no cover - import guard
        logger.warning("groq SDK unavailable (%s); LLM path disabled.", exc)
        return
    for key in keys:
        try:
            # max_retries=2 lets the SDK back off on transient connection errors
            # and 429s on the SAME key before we rotate to the next one.
            _clients.append((_mask(key), AsyncGroq(api_key=key, max_retries=2)))
        except Exception as exc:
            logger.warning("Failed to init Groq client %s: %s", _mask(key), exc)
    logger.info("Initialized %d Groq client(s), model=%s", len(_clients), _model)


def is_enabled() -> bool:
    return bool(_clients)


def _extract_json(content: str) -> dict:
    """Parse the model output into a dict, tolerating stray fences/prose."""
    content = (content or "").strip()
    if content.startswith("```"):
        # strip a ```json ... ``` fence if the model added one
        content = content.split("```", 2)[1] if content.count("```") >= 2 else content
        if content.lstrip().lower().startswith("json"):
            content = content.lstrip()[4:]
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start, end = content.find("{"), content.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(content[start : end + 1])
        raise


async def _call_one(client, user_message: str) -> dict:
    """One Groq attempt -> validated judgment dict. Raises on any failure."""
    resp = await client.chat.completions.create(
        model=_model,
        temperature=0,
        response_format={"type": "json_object"},
        timeout=PER_CALL_TIMEOUT,
        messages=[
            {"role": "system", "content": _system_prompt},
            {"role": "user", "content": user_message},
        ],
    )
    raw = _extract_json(resp.choices[0].message.content)
    judgment = LLMJudgment.model_validate(raw)
    # mode="json" so enum fields serialize to plain strings, not enum objects.
    result = judgment.model_dump(mode="json")
    # 'reasoning' is an internal CoT scratchpad — drop it from the returned judgment.
    result.pop("reasoning", None)
    return result


async def _run_rotation(request: "TicketRequest") -> Optional[dict]:
    user_message = build_user_message(request)
    for masked, client in _clients:
        try:
            result = await _call_one(client, user_message)
            logger.info("Groq judgment ok via %s for %s", masked, request.ticket_id)
            return result
        except Exception as exc:
            logger.warning("Groq key %s failed for %s: %s", masked, request.ticket_id, exc)
            continue
    return None


async def call_llm(request: "TicketRequest", timeout: float = OVERALL_TIMEOUT) -> Optional[dict]:
    """Return a judgment dict from Groq, or None if all keys fail / time out."""
    if not _clients:
        return None
    try:
        return await asyncio.wait_for(_run_rotation(request), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("Groq overall timeout (%ss) for %s", timeout, request.ticket_id)
        return None
    except Exception as exc:  # defensive: never propagate
        logger.warning("Groq engine unexpected error for %s: %s", request.ticket_id, exc)
        return None
