"""
test_samples.py — Automated test runner for QueueStorm Investigator.

Usage:
  1. Start the server: uvicorn main:app --port 8000
  2. Run: python test_samples.py [--url http://localhost:8000] [--file path/to/cases.json]

Tests all cases in the JSON file against the live server and reports pass/fail.
Exits with code 1 if any case fails.

Checks performed per case:
  - HTTP 200
  - ticket_id echoed correctly
  - evidence_verdict matches expected
  - case_type matches expected
  - department matches expected
  - relevant_transaction_id matches expected
  - customer_reply does not contain PIN or OTP
  - customer_reply does not contain "we will refund" (promise)
"""
import argparse
import json
import sys
import textwrap
import requests

# ── Argument Parsing ──────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="QueueStorm test runner")
parser.add_argument(
    "--url",
    default="http://localhost:8000",
    help="Base URL of the running server (default: http://localhost:8000)",
)
parser.add_argument(
    "--file",
    default="SUST_Preli_Sample_Cases.json",
    help="Path to the sample cases JSON file",
)
args = parser.parse_args()

BASE = args.url.rstrip("/")
ENDPOINT = f"{BASE}/analyze-ticket"

# ── Load Cases ────────────────────────────────────────────────────────────────
try:
    with open(args.file, encoding="utf-8") as f:
        data = json.load(f)
except FileNotFoundError:
    print(f"[ERROR] Cases file not found: {args.file}")
    print("       Provide the path with --file <path>")
    sys.exit(1)

cases = data.get("cases", data) if isinstance(data, dict) else data

# ── Test Loop ─────────────────────────────────────────────────────────────────
passed = 0
failed = 0
WIDTH = 72

print(f"\n{'='*WIDTH}")
print(f"  QueueStorm Investigator — Test Suite ({len(cases)} cases)")
print(f"  Target: {ENDPOINT}")
print(f"{'='*WIDTH}\n")

for case in cases:
    inp = case["input"]
    expected = case.get("expected_output", {})
    label = case.get("label", "")
    ticket_id = inp["ticket_id"]

    try:
        r = requests.post(ENDPOINT, json=inp, timeout=30)
        if r.status_code != 200:
            print(f"  FAIL  {ticket_id} ({label}): HTTP {r.status_code}")
            print(f"        Response: {r.text[:200]}")
            failed += 1
            continue

        out = r.json()
        reply_lower = out.get("customer_reply", "").lower()

        checks: dict[str, bool] = {
            "ticket_id":            out.get("ticket_id") == ticket_id,
            "evidence_verdict":     out.get("evidence_verdict") == expected.get("evidence_verdict"),
            "case_type":            out.get("case_type") == expected.get("case_type"),
            "department":           out.get("department") == expected.get("department"),
            "relevant_txn_id":      out.get("relevant_transaction_id") == expected.get("relevant_transaction_id"),
            "no_pin_otp":           "pin" not in reply_lower and "otp" not in reply_lower,
            "no_refund_promise":    "we will refund" not in reply_lower,
        }

        ok = all(checks.values())
        if ok:
            passed += 1
            print(
                f"  PASS  {ticket_id} ({label}) | "
                f"conf={out.get('confidence', '?')} | "
                f"case={out.get('case_type')} | "
                f"verdict={out.get('evidence_verdict')}"
            )
        else:
            failed += 1
            failed_checks = [k for k, v in checks.items() if not v]
            print(f"  FAIL  {ticket_id} ({label}): failed checks: {failed_checks}")
            rows = [
                ("evidence_verdict", expected.get("evidence_verdict"), out.get("evidence_verdict")),
                ("case_type",        expected.get("case_type"),        out.get("case_type")),
                ("department",       expected.get("department"),        out.get("department")),
                ("relevant_txn_id",  expected.get("relevant_transaction_id"), out.get("relevant_transaction_id")),
            ]
            for field, exp_val, got_val in rows:
                match = "✓" if exp_val == got_val else "✗"
                print(f"        {match} {field}: expected={exp_val!r}  got={got_val!r}")
            if out.get("customer_reply"):
                wrapped = textwrap.shorten(out["customer_reply"], width=80, placeholder="…")
                print(f"        customer_reply: {wrapped}")

    except requests.exceptions.ConnectionError:
        print(f"  ERROR {ticket_id}: Cannot connect to {ENDPOINT}")
        print("        Is the server running? Start with: uvicorn main:app --port 8000")
        failed += 1
    except Exception as e:
        print(f"  ERROR {ticket_id}: {e}")
        failed += 1

# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\n{'='*WIDTH}")
total = len(cases)
print(f"  Results: {passed}/{total} passed, {failed}/{total} failed")
print(f"{'='*WIDTH}\n")

sys.exit(0 if failed == 0 else 1)
