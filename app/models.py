from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class Language(str, Enum):
    en = "en"
    bn = "bn"
    mixed = "mixed"


class Channel(str, Enum):
    in_app_chat = "in_app_chat"
    call_center = "call_center"
    email = "email"
    merchant_portal = "merchant_portal"
    field_agent = "field_agent"


class UserType(str, Enum):
    customer = "customer"
    merchant = "merchant"
    agent = "agent"
    unknown = "unknown"


class TransactionType(str, Enum):
    transfer = "transfer"
    payment = "payment"
    cash_in = "cash_in"
    cash_out = "cash_out"
    settlement = "settlement"
    refund = "refund"


class TransactionStatus(str, Enum):
    completed = "completed"
    failed = "failed"
    pending = "pending"
    reversed = "reversed"


class EvidenceVerdict(str, Enum):
    consistent = "consistent"
    inconsistent = "inconsistent"
    insufficient_data = "insufficient_data"


class CaseType(str, Enum):
    wrong_transfer = "wrong_transfer"
    payment_failed = "payment_failed"
    refund_request = "refund_request"
    duplicate_payment = "duplicate_payment"
    merchant_settlement_delay = "merchant_settlement_delay"
    agent_cash_in_issue = "agent_cash_in_issue"
    phishing_or_social_engineering = "phishing_or_social_engineering"
    other = "other"


class Severity(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class Department(str, Enum):
    customer_support = "customer_support"
    dispute_resolution = "dispute_resolution"
    payments_ops = "payments_ops"
    merchant_operations = "merchant_operations"
    agent_operations = "agent_operations"
    fraud_risk = "fraud_risk"


class TransactionEntry(BaseModel):
    transaction_id: str
    timestamp: str
    type: TransactionType
    amount: float
    counterparty: str
    status: TransactionStatus


class TicketRequest(BaseModel):
    ticket_id: str
    complaint: str
    language: Optional[Language] = None
    channel: Optional[Channel] = None
    user_type: Optional[UserType] = None
    campaign_context: Optional[str] = None
    transaction_history: Optional[list[TransactionEntry]] = Field(default_factory=list)
    metadata: Optional[dict[str, Any]] = None


class TicketResponse(BaseModel):
    ticket_id: str
    relevant_transaction_id: Optional[str] = None
    evidence_verdict: EvidenceVerdict
    case_type: CaseType
    severity: Severity
    department: Department
    agent_summary: str
    recommended_next_action: str
    customer_reply: str
    human_review_required: bool
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    reason_codes: Optional[list[str]] = Field(default_factory=list)


class LLMJudgment(BaseModel):
    """The judgment + prose fields the LLM is responsible for.

    The hybrid pipeline asks the LLM ONLY for these. Severity, department,
    human_review_required and confidence are derived in code (see app/derive.py),
    so they are intentionally absent here.

    `reasoning` is an internal chain-of-thought scratchpad the model fills BEFORE
    the verdict (it appears first in the schema). It improves judgment quality but
    is stripped from the final response — it is not part of TicketResponse.
    """
    reasoning: Optional[str] = None
    relevant_transaction_id: Optional[str] = None
    evidence_verdict: EvidenceVerdict
    case_type: CaseType
    agent_summary: str
    recommended_next_action: str
    customer_reply: str
    reason_codes: Optional[list[str]] = Field(default_factory=list)
