"""QueueStorm Investigator — FastAPI service.

Endpoints:
  GET  /health          -> {"status":"ok"}  (instant, no dependencies)
  POST /analyze-ticket  -> structured investigation per the response schema

Pipeline for /analyze-ticket:
  validate -> LLM judgment (Groq, 20s) | deterministic fallback
           -> apply_derivations (severity/department/human_review/confidence)
           -> safety gate -> schema gate -> 200

The service never crashes on bad input: malformed/missing fields -> 400,
semantically invalid -> 422, unexpected internal error -> 500 (no stack traces).
"""
from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .derive import apply_derivations
from .deterministic import run_deterministic
from .llm_engine import call_llm, init_clients, is_enabled
from .models import TicketRequest, TicketResponse
from .normalizer import extract_valid_txn_ids
from .safety_gate import enforce_safety, enforce_schema

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("queuestorm")

app = FastAPI(title="QueueStorm Investigator", version="1.0")

LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "20"))


@app.on_event("startup")
def _startup() -> None:
    """Pre-warm LLM clients so the first request is not slowed by setup."""
    init_clients()
    logger.info("Startup complete. LLM path enabled=%s", is_enabled())


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def _required_fields_present(body: dict) -> bool:
    """ticket_id and complaint must be present and be strings."""
    return (
        isinstance(body, dict)
        and isinstance(body.get("ticket_id"), str)
        and isinstance(body.get("complaint"), str)
    )


@app.post("/analyze-ticket")
async def analyze_ticket(ticket: TicketRequest):
    # --- semantic validation -> 422 ------------------------------------------
    if not ticket.complaint.strip():
        return JSONResponse(
            status_code=422,
            content={"error": "complaint must not be empty."},
        )

    # --- core pipeline (never crashes) ---------------------------------------
    try:
        judgment = await call_llm(ticket, timeout=LLM_TIMEOUT)
        if judgment is not None:
            result = apply_derivations(judgment, ticket)
            source = "llm"
        else:
            result = run_deterministic(ticket)
            source = "deterministic"

        user_type = ticket.user_type.value if ticket.user_type else None
        language = ticket.language.value if ticket.language else None
        result = enforce_safety(result, ticket.complaint, user_type, language)
        result = enforce_schema(result, extract_valid_txn_ids(ticket.transaction_history))

        # Final guarantee: conform to the response model exactly.
        response = TicketResponse.model_validate(result)
        logger.info("Analyzed %s via %s", ticket.ticket_id, source)
        return JSONResponse(status_code=200, content=response.model_dump(mode="json"))

    except Exception:
        # Last-resort: try a pure deterministic + gated answer so a single bad
        # path still yields a valid 200 rather than a 500.
        try:
            result = run_deterministic(ticket)
            result = enforce_schema(
                enforce_safety(
                    result,
                    ticket.complaint,
                    ticket.user_type.value if ticket.user_type else None,
                    ticket.language.value if ticket.language else None,
                ),
                extract_valid_txn_ids(ticket.transaction_history),
            )
            response = TicketResponse.model_validate(result)
            logger.warning("Recovered %s via deterministic after error", ticket.ticket_id)
            return JSONResponse(status_code=200, content=response.model_dump(mode="json"))
        except Exception:
            logger.exception("Unhandled error analyzing ticket")
            return JSONResponse(
                status_code=500, content={"error": "Internal server error."}
            )


# --- map FastAPI/Pydantic validation errors to 400 (not the default 422) -----
@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=400, content={"error": "Invalid request body."})
