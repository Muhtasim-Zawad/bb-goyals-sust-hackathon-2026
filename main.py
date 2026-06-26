"""
main.py — FastAPI application entry point.

Imports from the app package. Registers:
- API router (all ticket analysis routes)
- Global exception handler (never leaks stack traces)
- Application metadata (title, version, docs URL)
"""
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import router

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# ── Application ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="QueueStorm Investigator",
    version="1.0.0",
    description=(
        "AI-powered bKash support ticket analysis system. "
        "Classifies complaints, matches transactions, and routes to the correct department."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
)

app.include_router(router)


# ── Global Exception Handler ──────────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """
    Last-resort handler. Ensures no stack trace or internal detail
    is ever returned to the client.
    """
    logging.getLogger(__name__).error(
        "Unhandled exception for %s %s: %s",
        request.method,
        request.url.path,
        exc,
        exc_info=True,
    )
    return JSONResponse(
        status_code=500,
        content={"error": "Internal processing error. Please try again."},
    )