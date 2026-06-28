from __future__ import annotations
from dotenv import load_dotenv
load_dotenv()

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from models import TicketRequest, TicketResponse, ErrorResponse
from chain import analyze_ticket

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="QueueStorm Investigator",
    description="AI copilot for bKash support: analyzes complaints + transaction history.",
    version="1.0.0",
)


# ---------------------------------------------------------------------------
# Exception handlers (400 / 422 / 500)
# ---------------------------------------------------------------------------

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Malformed/invalid request body -> 422."""
    logger.warning("Validation error on %s: %s", request.url.path, exc.errors())
    return JSONResponse(
        status_code=422,
        content=ErrorResponse(
            error="validation_error",
            message="The request body failed validation. Check required fields and types.",
        ).model_dump(),
    )


@app.exception_handler(ValidationError)
async def pydantic_validation_exception_handler(request: Request, exc: ValidationError) -> JSONResponse:
    """Pydantic model validation error raised manually -> 422."""
    logger.warning("Pydantic validation error on %s: %s", request.url.path, exc.errors())
    return JSONResponse(
        status_code=422,
        content=ErrorResponse(
            error="validation_error",
            message="The request body failed validation. Check required fields and types.",
        ).model_dump(),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all -> 500. Never leak internals (stack traces, API keys, etc.)."""
    logger.error("Unhandled exception on %s: %s", request.url.path, exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(
            error="internal_error",
            message="An internal error occurred while processing the request.",
        ).model_dump(),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health", tags=["system"])
async def health() -> dict:
    """Liveness check. Always returns ok, even if the LLM/Groq backend is down."""
    return {"status": "ok"}


@app.post(
    "/analyze-ticket",
    response_model=TicketResponse,
    responses={
        400: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    tags=["analysis"],
)
async def analyze_ticket_endpoint(request: TicketRequest):
    """
    Run the QueueStorm investigation chain on a support ticket and
    return a structured, safety-scrubbed analysis.
    """
    try:
        result = await analyze_ticket(request)
        return result

    except RuntimeError as exc:
        # Both primary and fallback Groq chains failed.
        logger.error("Both LLM chains failed for ticket_id=%s: %s", request.ticket_id, exc)
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error="llm_failure",
                message="The analysis service is temporarily unavailable. Please retry shortly.",
            ).model_dump(),
        )

    except ValueError as exc:
        # Defensive: any explicit bad-input signal raised deeper in the chain.
        logger.warning("Bad input for ticket_id=%s: %s", request.ticket_id, exc)
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                error="bad_request",
                message=str(exc),
            ).model_dump(),
        )

    # Anything else falls through to the generic Exception handler -> 500.


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
