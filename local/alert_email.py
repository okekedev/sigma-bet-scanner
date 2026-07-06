#!/usr/bin/env python3
"""Send a sigma-bet alert email via Azure Communication Services.
Run with the project venv (has azure-communication-email + dotenv):
    ../.venv/bin/python alert_email.py "<subject>" "<html-file-or-inline>"
Creds come from ~/dev/Ops/.env (ACS_CONNECTION_STRING, EMAIL_SENDER_DOMAIN).
Recipient: EMAIL_TO env var, required.
"""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

OPS_ENV = Path.home() / "dev" / "Ops" / ".env"
if OPS_ENV.exists():
    load_dotenv(OPS_ENV)

def send(subject: str, html: str, to: str = None) -> str:
    from azure.communication.email import EmailClient
    conn = os.environ["ACS_CONNECTION_STRING"]
    domain = os.environ["EMAIL_SENDER_DOMAIN"]
    to = to or os.environ["EMAIL_TO"]
    client = EmailClient.from_connection_string(conn)
    poller = client.begin_send({
        "senderAddress": f"DoNotReply@{domain}",
        "recipients": {"to": [{"address": to}]},
        "content": {"subject": subject, "plainText": subject, "html": html},
    })
    result = poller.result(timeout=60)
    return result.get("id", "?") if isinstance(result, dict) else str(result)

if __name__ == "__main__":
    subject = sys.argv[1] if len(sys.argv) > 1 else "Sigma-bet test alert"
    body_arg = sys.argv[2] if len(sys.argv) > 2 else "<p>test</p>"
    html = Path(body_arg).read_text() if Path(body_arg).exists() else body_arg
    msg_id = send(subject, html)
    print(f"sent: {msg_id}")
