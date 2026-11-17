"""Step 7 — send the drafted email via Resend.

Every send goes to TO_EMAIL (safety override), never the real prospect.
The body carries an "Intended recipient:" prefix line so the inbox shows
who it was meant for, which is required for class defensibility.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent


class SendResult(BaseModel):
    sent: bool
    dry_run: bool
    message_id: Optional[str] = None
    to: Optional[str] = None
    from_display: Optional[str] = None
    error: Optional[str] = None


def _sender_name() -> str:
    return os.environ.get("SENDER_NAME", "Sprinto Outreach")


def _from_email() -> str:
    return os.environ.get("FROM_EMAIL") or "onboarding@resend.dev"


def _format_from() -> str:
    return f"{_sender_name()} <{_from_email()}>"


def _intended_recipient_line(poc) -> str:
    name = (poc.name or "unknown").strip()
    email = (poc.email or "unknown").strip()
    title = (poc.title or "unknown").strip()
    return f"Intended recipient: {name} <{email}>, {title}"


def build_full_body(body: str, poc) -> str:
    return f"{_intended_recipient_line(poc)}\n\n{body}"


def send(subject: str, body: str, poc, dry_run: bool = False) -> SendResult:
    to_email = os.environ.get("TO_EMAIL")
    if not to_email:
        return SendResult(sent=False, dry_run=dry_run,
                          error="TO_EMAIL not set in .env")
    full_body = build_full_body(body, poc)
    from_display = _format_from()

    if dry_run:
        print("---- DRY RUN — not sending ----")
        print(f"From: {from_display}")
        print(f"To:   {to_email}")
        print(f"Subject: {subject}")
        print()
        print(full_body)
        print("--------------------------------")
        return SendResult(sent=False, dry_run=True,
                          to=to_email, from_display=from_display)

    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        return SendResult(sent=False, dry_run=False, to=to_email,
                          from_display=from_display,
                          error="RESEND_API_KEY not set in .env")

    import resend
    resend.api_key = api_key
    try:
        resp = resend.Emails.send({
            "from": from_display,
            "to": [to_email],
            "subject": subject,
            "text": full_body,
        })
        msg_id = (resp or {}).get("id") if isinstance(resp, dict) else getattr(resp, "id", None)
        return SendResult(sent=True, dry_run=False, message_id=msg_id,
                          to=to_email, from_display=from_display)
    except Exception as e:
        return SendResult(sent=False, dry_run=False, to=to_email,
                          from_display=from_display,
                          error=f"{type(e).__name__}: {e}")
