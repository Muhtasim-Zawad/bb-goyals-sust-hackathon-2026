from __future__ import annotations

from typing import List, Literal, Optional
from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Enums (as Literals so Pydantic enforces exact strings)
# ---------------------------------------------------------------------------

LanguageEnum   = Literal["en", "bn", "mixed"]
ChannelEnum    = Literal["in_app_chat", "call_center", "email", "merchant_portal", "field_agent"]
UserTypeEnum   = Literal["customer", "merchant", "agent", "unknown"]
TxnTypeEnum    = Literal["transfer", "payment", "cash_in", "cash_out", "settlement", "refund"]
TxnStatusEnum  = Literal["completed", "failed", "pending", "reversed"]

EvidenceVerdict = Literal["consistent", "inconsistent", "insufficient_data"]
CaseType        = Literal[
    "wrong_transfer",
    "payment_failed",
    "refund_request",
    "duplicate_payment",
    "merchant_settlement_delay",
    "agent_cash_in_issue",
    "phishing_or_social_engineering",
    "other",
]
Severity   = Literal["low", "medium", "high", "critical"]
Department = Literal[
    "customer_support",
    "dispute_resolution",
    "payments_ops",
    "merchant_operations",
    "agent_operations",
    "fraud_risk",
]


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class TransactionEntry(BaseModel):
    transaction_id: str          = Field(..., description="Unique transaction identifier.")
    timestamp:      str          = Field(..., description="ISO 8601 timestamp.")
    type:           TxnTypeEnum  = Field(..., description="Transaction type.")
    amount:         float        = Field(..., description="Amount in BDT.")
    counterparty:   str          = Field(..., description="Recipient phone, merchant ID, or agent ID.")
    status:         TxnStatusEnum = Field(..., description="Transaction status.")


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------

class TicketRequest(BaseModel):
    ticket_id:           str                         = Field(..., description="Unique ticket identifier.")
    complaint:           str                         = Field(..., description="Customer complaint text.")
    language:            Optional[LanguageEnum]      = Field(None)
    channel:             Optional[ChannelEnum]       = Field(None)
    user_type:           Optional[UserTypeEnum]      = Field(None)
    campaign_context:    Optional[str]               = Field(None)
    transaction_history: Optional[List[TransactionEntry]] = Field(default_factory=list)

    @field_validator("complaint")
    @classmethod
    def complaint_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("complaint must not be empty.")
        return v.strip()

    @field_validator("ticket_id")
    @classmethod
    def ticket_id_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("ticket_id must not be empty.")
        return v.strip()


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------

class TicketResponse(BaseModel):
    ticket_id:                str                        = Field(..., description="Must match the request ticket_id.")
    relevant_transaction_id:  Optional[str]              = Field(None,  description="Transaction ID the complaint refers to, or null.")
    evidence_verdict:         EvidenceVerdict             = Field(..., description="Whether data supports the complaint.")
    case_type:                CaseType                   = Field(..., description="Complaint category.")
    severity:                 Severity                   = Field(..., description="Case severity level.")
    department:               Department                 = Field(..., description="Routing destination.")
    agent_summary:            str                        = Field(..., description="Concise one-to-two sentence summary for the support agent.")
    recommended_next_action:  str                        = Field(..., description="Suggested next operational step for the agent.")
    customer_reply:           str                        = Field(..., description="Safe, official reply to send to the customer.")
    human_review_required:    bool                       = Field(..., description="True if a human agent must review this case.")
    confidence:               Optional[float]            = Field(None, ge=0.0, le=1.0, description="Model confidence between 0 and 1.")
    reason_codes:             Optional[List[str]]        = Field(default_factory=list, description="Short labels supporting the decision.")


# ---------------------------------------------------------------------------
# Error response (for 400 / 422 / 500)
# ---------------------------------------------------------------------------

class ErrorResponse(BaseModel):
    error:   str = Field(..., description="Short error category.")
    message: str = Field(..., description="Non-sensitive description of the problem.")