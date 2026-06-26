"""
api/routes.py — FastAPI router (MVC View layer).

Defines all HTTP endpoints. Business logic is delegated to TicketAnalyzer.
This file only handles HTTP concerns: request/response shaping, status codes,
and route registration.
"""
import logging
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.models.request import TicketRequest
from app.models.response import AnalyzeResponse
from app.services.analyzer import TicketAnalyzer
from app.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter()

# One analyzer instance, reused across all requests
_analyzer = TicketAnalyzer()
settings = get_settings()


@router.get("/health", tags=["System"])
async def health_check():
    """
    Liveness probe. Returns 200 OK when the service is ready.
    Also reports the number of configured API keys (without exposing values).
    """
    return {
        "status": "ok",
        "model": settings.groq_model,
        "api_keys_loaded": len(settings.groq_api_keys),
    }


@router.post(
    "/analyze-ticket",
    response_model=AnalyzeResponse,
    tags=["Analysis"],
    summary="Analyze a support ticket",
    description=(
        "Accepts a complaint ticket with optional transaction history. "
        "Returns a fully structured analysis including case type, severity, "
        "department routing, agent summary, and a safe customer reply."
    ),
)
async def analyze_ticket(req: TicketRequest) -> AnalyzeResponse:
    """
    Main analysis endpoint. Single-ticket, synchronous within the request lifecycle.

    Pydantic validation errors (missing fields, empty complaint) are handled
    automatically by FastAPI and return HTTP 422 with structured error detail.
    """
    # Complaint length guard (beyond Pydantic's type check)
    if len(req.complaint) > settings.complaint_max_length:
        raise HTTPException(
            status_code=422,
            detail=f"Complaint exceeds maximum length of {settings.complaint_max_length} characters.",
        )

    logger.info("Analyzing ticket %s via channel=%s", req.ticket_id, req.channel)
    result = await _analyzer.analyze(req)
    logger.info(
        "Ticket %s → case=%s verdict=%s confidence=%s",
        req.ticket_id,
        result.case_type,
        result.evidence_verdict,
        result.confidence,
    )
    return result
