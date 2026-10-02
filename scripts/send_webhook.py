"""Play the payment provider: send a signed webhook to the API.

Usage:
    python scripts/send_webhook.py pay_abc123 SUCCESS
    python scripts/send_webhook.py pay_abc123 FAILED --event-id evt_1 --reason "Card declined"
    python scripts/send_webhook.py pay_abc123 SUCCESS --event-id evt_1   # send again -> "duplicate"
"""
import argparse
import hashlib
import hmac
import json
import os
import uuid

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("provider_reference")
    parser.add_argument("status", choices=["SUCCESS", "FAILED"])
    parser.add_argument("--event-id", default=f"evt_{uuid.uuid4().hex}")
    parser.add_argument("--amount", help="Optional; must match the payment amount")
    parser.add_argument("--reason", help="failure_reason for FAILED events")
    parser.add_argument("--url", default=os.getenv("API_URL", "http://localhost:8000") + "/payments/webhook/")
    parser.add_argument("--secret", default=os.getenv("WEBHOOK_SECRET", "whsec_dev_secret"))
    args = parser.parse_args()

    payload = {"event_id": args.event_id, "provider_reference": args.provider_reference, "status": args.status}
    if args.amount:
        payload["amount"] = args.amount
    if args.reason:
        payload["failure_reason"] = args.reason

    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(args.secret.encode(), body, hashlib.sha256).hexdigest()
    response = httpx.post(
        args.url, content=body, headers={"Content-Type": "application/json", "X-Webhook-Signature": signature}
    )
    print(f"POST {args.url}  event_id={args.event_id}")
    print(response.status_code, response.text)


if __name__ == "__main__":
    main()
