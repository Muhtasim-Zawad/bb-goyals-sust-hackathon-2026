# QueueStorm Investigator

An AI-powered support ticket analysis system for a Bangladeshi digital-finance platform. It acts as an investigator to classify complaints, match transactions from history, and route cases to the appropriate departments safely and deterministically.

## Deployed URL
**[https://bb-goyals-sust-hackathon-2026-1.onrender.com]**


## Setup Instructions
1. Clone the repository to your local machine.
2. Create a `.env` file in the root directory with the following configuration (replace with your actual Groq API keys):

   ```env
   # Groq API Configuration
   # Supports multiple API keys separated by commas for round-robin rotation
   GROQ_API_KEY="your_groq_api_key_1,your_groq_api_key_2"

   # Optional Groq Settings
   # GROQ_MODEL="llama-3.3-70b-versatile"
   # GROQ_TEMPERATURE="0.1"
   # GROQ_MAX_TOKENS="1000"
   # GROQ_MAX_RETRIES="3"
   # GROQ_TIMEOUT_BUDGET_SECONDS="20.0"

   # Optional App Settings
   # COMPLAINT_MAX_LENGTH="10000"
   ```

## Run Command
### Using Docker (Recommended)
You can build and start the application using Docker Compose:
```bash
docker-compose up --build
```
The API documentation and test interface will be available at `http://localhost:8000/docs`.

### Local Setup
```bash
python -m venv .venv
source .venv/bin/activate  # On Windows use: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Tech Stack
* **Backend Framework**: FastAPI (Python 3.11)
* **LLM Provider**: Groq
* **Validation**: Pydantic
* **Deployment**: Docker & Docker Compose
* **Dependency Management**: `requirements.txt`

## AI Approach
The system uses a **three-layer Chain of Responsibility pipeline** (Template Method pattern):
1. **Layer 1 (Safety Guard)**: A deterministic pre-filter for adversarial inputs/prompt injections. Short-circuits the process if triggered.
2. **Layer 2 (Evidence Engine)**: Parses amounts deterministically, matches them against the provided transaction history, and calculates a base match score.
3. **Layer 3 (LLM Reasoning)**: The LLM receives the complaint, transaction history, and deterministic pre-analysis. It uses a "reasoning" scratchpad to perform a chain-of-thought analysis (verifying amount matches, duplicate checks, behavioral inconsistencies). It outputs exactly 8 fields.
*Note: Final routing (`severity`, `department`, `human_review_required`) and `confidence` scoring are computed deterministically in Python based on the LLM's classification.*

## Safety Logic
* **Credential Protection**: The LLM is strictly instructed (via system prompt) never to ask for or confirm PINs, OTPs, or passwords.
* **Outcome Promises**: The LLM is blocked from promising refunds or reversals. It uses a strict templated safe-phrase ("any eligible amount will be returned through official channels").
* **Output Scrubber**: A post-generation Python `SafetyGuard` checks the final output and overwrites any hallucinated refund promises with safe templates.
* **Adversarial Guard**: If injection keywords ("ignore instructions", "jailbreak") are detected, the system immediately routes to `fraud_risk` with `critical` severity without calling the LLM.

## MODELS
* **Model**: `llama-3.3-70b-versatile`
* **Where it runs**: Groq (Cloud API)
* **Why it was chosen**: Groq provides ultra-low latency inference, crucial for staying under the strict 20-second hard budget while performing complex reasoning. The `llama-3.3-70b` model possesses the robust reasoning capability required for the chain-of-thought processing and precise JSON formatting needed for this investigator role.

## Cost Reasoning
Using Groq provides extremely fast inference at very competitive pricing compared to GPT-4 or Claude. Since the system compresses the system prompt efficiently (~1400 tokens) and forces a strict, short JSON output, per-request costs are minimized. The custom round-robin API key pool ensures high availability and rate-limit avoidance without needing expensive enterprise tiers.

## Assumptions
* The transaction history provided in the request accurately reflects the user's recent activity.
* "Amount" is a reliable primary key for transaction matching in the majority of use cases.
* Customer complaints will be in English, Bangla, or Banglish (Latin script Bangla).

## Known Limitations
* If a customer has multiple identical transactions to the same recipient on the same day and complains vaguely, the system conservatively returns `insufficient_data` instead of guessing.
* Extremely obfuscated prompt injections might bypass the regex-based Layer 1 guard, though the LLM is instructed to identify and flag them.
* No database is attached; the system is stateless and relies entirely on the transaction history provided in the API payload.

## Sample Output
Generated from a public sample case (Wrong Transfer in Banglish):
```json
{
  "reasoning": "Amount 5000 matches TXN-9101 (5000 BDT, transfer, completed, 14:08). Only one transfer to this counterparty in history — single wrong transfer is consistent. Completed transfer = direct financial loss.",
  "relevant_transaction_id": "TXN-9101",
  "evidence_verdict": "consistent",
  "case_type": "wrong_transfer",
  "severity": "high",
  "department": "dispute_resolution",
  "agent_summary": "Customer reports accidental transfer of 5000 BDT via TXN-9101 at 14:08. Single transfer to this counterparty supports the wrong-transfer claim.",
  "recommended_next_action": "Verify TXN-9101 counterparty and initiate the wrong-transfer dispute workflow.",
  "customer_reply": "Apnar TXN-9101 niye amra ticket khulechi. Amader dispute team review korbe. Kono eligible amount official channel e return hobe. PIN ba OTP karo shathe share korben na.",
  "human_review_required": true,
  "confidence": 0.88,
  "reason_codes": [
    "wrong_transfer",
    "transaction_match",
    "single_recipient"
  ]
}
```
