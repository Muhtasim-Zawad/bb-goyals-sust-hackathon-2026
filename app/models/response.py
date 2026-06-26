"""
models/response.py — Pydantic models for the analysis response.

MVC Role: Model — defines the shape of data exiting the system.
"""
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field

from app.enums import CaseType, Department, EvidenceVerdict, Severity


class AnalyzeResponse(BaseModel):
    """
    Fully structured ticket analysis result.

    Every field must be present in the HTTP response. Optional fields
    have explicit defaults to avoid schema drift.
    """
    ticket_id: str
    relevant_transaction_id: Optional[str] = Field(
        None,
        description="ID of the transaction most relevant to this complaint. Null if none found."
    )
    evidence_verdict: EvidenceVerdict
    case_type: CaseType
    severity: Severity
    department: Department
    agent_summary: str = Field(
        ..., description="1-2 sentence summary for the human agent."
    )
    recommended_next_action: str = Field(
        ..., description="Single actionable instruction. No refund confirmations."
    )
    customer_reply: str = Field(
        ..., description="Safe, official reply to show/send to the customer."
    )
    human_review_required: bool
    confidence: Optional[float] = Field(
        None, ge=0.0, le=1.0, description="Blended confidence score [0.0-1.0]."
    )
    reason_codes: Optional[list[str]] = Field(
        default_factory=list, description="Machine-readable labels for downstream routing."
    )
