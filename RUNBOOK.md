# RUNBOOK — QueueStorm Investigator

Judge / operator guide for deploying and verifying the service from scratch.

---

## Prerequisites

- Docker **or** Python 3.11+
- A Groq API key (free at https://console.groq.com)
- Ports: 8000 (configurable via `PORT`)

---

## Environment Setup

```bash
cp .env.example .env
```

Edit `.env` and set at minimum:

```
GROQ_API_KEY=your_key_here
```

For redundancy with multiple keys (recommended during judging):
```
GROQ_API_KEY=key_1,key_2,key_3
```

---

## Option A: Docker (Recommended)

```bash
# Build
docker build -t queuestorm .

# Run (reads .env automatically)
docker run -p 8000:8000 --env-file .env queuestorm
```

Expected startup output:
```
INFO  queuestorm.llm Initialized N Groq client(s), model=llama-3.3-70b-versatile
INFO  queuestorm     Startup complete. LLM path enabled=True
INFO  Uvicorn running on http://0.0.0.0:8000
```

---

## Option B: Local Python

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## Health Check

```bash
curl http://localhost:8000/health
# Expected: {"status":"ok"}
```

Must respond within 60 seconds of startup.

---

## Smoke Test

```bash
curl -s -X POST http://localhost:8000/analyze-ticket \
  -H "Content-Type: application/json" \
  -d '{
    "ticket_id": "SMOKE-01",
    "complaint": "I sent 5000 taka to the wrong number around 2pm.",
    "user_type": "customer",
    "transaction_history": [{
      "transaction_id": "TXN-TEST",
      "timestamp": "2026-04-14T14:08:22Z",
      "type": "transfer",
      "amount": 5000,
      "counterparty": "+8801719876543",
      "status": "completed"
    }]
  }' | python3 -m json.tool
```

Expected: `200` with `case_type=wrong_transfer`, `severity=high`, `department=dispute_resolution`, `human_review_required=true`.

---

## Full Regression Test

Requires the 10-case sample file at `../SUST_Preli_Sample_Cases.json` (relative to `preli_app/`).

```bash
# Service must be running first
python run_samples.py

# Against a deployed URL
python run_samples.py --url https://your-service-url.onrender.com

# Custom samples path
python run_samples.py --samples /path/to/SUST_Preli_Sample_Cases.json
```

All 10 cases should print `PASS`. Outputs are written to `sample_outputs/`.

---

## Error Scenarios

| Symptom | Cause | Fix |
|---|---|---|
| `LLM path enabled=False` at startup | `GROQ_API_KEY` not set or set to placeholder | Set real key(s) in `.env` |
| Responses correct but slow (10-20s) | First key is rate-limited, rotating to backup | Add more keys to `GROQ_API_KEY` (comma-separated) |
| All responses come from deterministic fallback | All Groq keys exhausted or network down | Add keys / wait for quota reset. Fallback still passes all 10 samples. |
| `500 Internal server error` | Check server logs | Never exposes stack traces — check stdout for details |

---

## Environment Variables Reference

| Variable | Default | Description |
|---|---|---|
| `GROQ_API_KEY` | *(required)* | Comma-separated Groq API key(s) |
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | Groq model name |
| `PORT` | `8000` | Bind port |
| `LLM_TIMEOUT` | `20` | LLM call timeout in seconds |
| `LOG_LEVEL` | `INFO` | Python log level (`DEBUG`/`INFO`/`WARNING`) |

---

## Deployment on Render (Free Tier)

1. Push repo to GitHub (make sure `.env` is gitignored — it is).
2. Create a new **Web Service** on Render, connect the repo.
3. Set **Build Command:** `pip install -r requirements.txt`
4. Set **Start Command:** `uvicorn app.main:app --host 0.0.0.0 --port 8000`
5. Add environment variables in the Render dashboard (do not commit real keys).
6. To prevent cold-start during eval: set a keep-alive cron or uptime monitor to ping `/health` every 10 minutes.

---

## File Structure

```
preli_app/
├── app/
│   ├── __init__.py
│   ├── main.py          # FastAPI app, endpoints, pipeline orchestration
│   ├── models.py        # Pydantic request/response/enum models
│   ├── normalizer.py    # Text utilities (Bangla digits, phone, injection detection)
│   ├── llm_prompt.py    # Groq system prompt + user message builder
│   ├── llm_engine.py    # Groq client, multi-key rotation, 20s timeout
│   ├── derive.py        # Table-driven severity/department/human_review/confidence
│   ├── deterministic.py # Rule-based fallback (runs when LLM unavailable)
│   └── safety_gate.py   # Safety enforcement + schema validation on every response
├── run_samples.py       # Regression test script (10 public samples)
├── sample_outputs/      # Output files from regression test (gitignored)
├── requirements.txt
├── Dockerfile
├── .env.example
├── README.md
└── RUNBOOK.md           # This file
```
