"""
models/request.py — Pydantic models for incoming ticket requests.

MVC Role: Model — defines the shape of data entering the system.
"""
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field, field_validator


class TransactionEntry(BaseModel):
    """A single transaction record from the user's history."""
    transaction_id: str
    timestamp: str                  # ISO-8601 string; parsed downstream
    type: str                       # e.g. "transfer", "payment", "cash_in"
    amount: float
    counterparty: Optional[str] = None
    status: str                     # "pending" | "completed" | "failed"


class TicketRequest(BaseModel):
    """
    Incoming complaint ticket.

    Fields are deliberately lenient (Optional) because not all channels
    provide complete metadata. Downstream layers degrade gracefully.
    """
    ticket_id: str = Field(..., description="Unique ticket identifier")
    complaint: str = Field(..., description="Raw complaint text — may be in English or Bangla")
    language: Optional[str] = Field("en", description="ISO 639-1 language code; 'bn' for Bangla")
    channel: Optional[str] = Field(None, description="Submission channel, e.g. 'merchant_portal'")
    user_type: Optional[str] = Field(None, description="'customer' | 'merchant' | 'agent'")
    campaign_context: Optional[str] = Field(None, description="Active campaign name if any")
    transaction_history: Optional[list[TransactionEntry]] = Field(
        default_factory=list, description="Recent transactions for evidence matching"
    )
    metadata: Optional[dict] = Field(None, description="Arbitrary extra fields — reserved for future use")

    @field_validator("complaint")
    @classmethod
    def complaint_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("complaint cannot be empty")
        return v.strip()

    @field_validator("transaction_history", mode="before")
    @classmethod
    def normalize_history(cls, v):
        """Ensure transaction_history is always a list, never None."""
        return v or []
