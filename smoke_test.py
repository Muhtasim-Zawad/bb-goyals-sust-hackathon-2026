"""Quick smoke test for the 3 provided sample inputs."""
import urllib.request
import json
import sys


def post(data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        "http://localhost:8000/analyze-ticket",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    r = urllib.request.urlopen(req, timeout=35)
    return json.loads(r.read())


CASES = [
    {
        "label": "TKT-009 Merchant settlement delay",
        "input": {
            "ticket_id": "TKT-009",
            "complaint": "I am a merchant. My yesterday's sales of 15000 taka have not been settled to my account. Settlement usually happens by 11am next day. Please check.",
            "language": "en",
            "channel": "merchant_portal",
            "user_type": "merchant",
            "transaction_history": [
                {"transaction_id": "TXN-9901", "timestamp": "2026-04-13T18:00:00Z",
                 "type": "settlement", "amount": 15000, "counterparty": "MERCHANT-SELF", "status": "pending"}
            ],
        },
        "expected": {
            "case_type": "merchant_settlement_delay",
            "department": "merchant_operations",
            "evidence_verdict": "consistent",
            "relevant_transaction_id": "TXN-9901",
            "human_review_required": False,
        },
    },
    {
        "label": "TKT-007 Agent cash-in, Bangla",
        "input": {
            "ticket_id": "TKT-007",
            "complaint": "আমি আজ সকালে এজেন্টের কাছে ২০০০ টাকা ক্যাশ ইন করেছি কিন্তু আমার ব্যালেন্সে টাকা আসেনি। এজেন্ট বলছে টাকা পাঠিয়েছে কিন্তু আমি দেখছি না।",
            "language": "bn",
            "channel": "call_center",
            "user_type": "customer",
            "transaction_history": [
                {"transaction_id": "TXN-9701", "timestamp": "2026-04-14T09:30:00Z",
                 "type": "cash_in", "amount": 2000, "counterparty": "AGENT-318", "status": "pending"}
            ],
        },
        "expected": {
            "case_type": "agent_cash_in_issue",
            "department": "agent_operations",
            "evidence_verdict": "consistent",
            "relevant_transaction_id": "TXN-9701",
            "human_review_required": True,
        },
    },
    {
        "label": "TKT-010 Duplicate payment",
        "input": {
            "ticket_id": "TKT-010",
            "complaint": "I paid my electricity bill 850 taka but it deducted twice from my account. Please check, I only paid once.",
            "language": "en",
            "channel": "in_app_chat",
            "user_type": "customer",
            "transaction_history": [
                {"transaction_id": "TXN-10001", "timestamp": "2026-04-14T08:15:30Z",
                 "type": "payment", "amount": 850, "counterparty": "BILLER-DESCO", "status": "completed"},
                {"transaction_id": "TXN-10002", "timestamp": "2026-04-14T08:15:42Z",
                 "type": "payment", "amount": 850, "counterparty": "BILLER-DESCO", "status": "completed"},
            ],
        },
        "expected": {
            "case_type": "duplicate_payment",
            "department": "payments_ops",
            "evidence_verdict": "consistent",
            "relevant_transaction_id": "TXN-10002",
            "human_review_required": True,
        },
    },
]

passed = 0
failed = 0

print("\n" + "=" * 70)
print("  QueueStorm — Smoke Test (3 sample cases)")
print("=" * 70 + "\n")

for case in CASES:
    label = case["label"]
    inp = case["input"]
    exp = case["expected"]

    try:
        out = post(inp)
        checks = {
            "ticket_id":      out.get("ticket_id") == inp["ticket_id"],
            "case_type":      out.get("case_type") == exp["case_type"],
            "department":     out.get("department") == exp["department"],
            "verdict":        out.get("evidence_verdict") == exp["evidence_verdict"],
            "txn_id":         out.get("relevant_transaction_id") == exp["relevant_transaction_id"],
            "no_pin_otp":     "pin" not in out.get("customer_reply", "").lower()
                              and "otp" not in out.get("customer_reply", "").lower(),
            "no_refund_prom": "we will refund" not in out.get("customer_reply", "").lower(),
        }
        ok = all(checks.values())
        if ok:
            passed += 1
            print(f"  PASS  {label}")
            print(f"        case_type={out['case_type']} | dept={out['department']}")
            print(f"        txn_id={out['relevant_transaction_id']} | conf={out['confidence']}")
            print(f"        human_review={out['human_review_required']}")
        else:
            failed += 1
            bad = [k for k, v in checks.items() if not v]
            print(f"  FAIL  {label}  — failed: {bad}")
            for field in ("case_type", "department", "evidence_verdict", "relevant_transaction_id"):
                mark = "OK" if out.get(field) == exp.get(field) else "!!"
                print(f"    [{mark}] {field}: expected={exp.get(field)!r}  got={out.get(field)!r}")
        print()
    except Exception as e:
        failed += 1
        print(f"  ERROR {label}: {e}\n")

print("=" * 70)
print(f"  Results: {passed}/3 passed, {failed}/3 failed")
print("=" * 70 + "\n")
sys.exit(0 if failed == 0 else 1)
