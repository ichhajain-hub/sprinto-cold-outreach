"""Step 6 — draft the actual cold email.

One Gemini call. Takes extract, qualify, PoC and pain points, returns a
subject + body. Enforces:
  - under 120 words in the body (retry once with stricter instruction if long)
  - question-hook when the audit gate was flagged
  - no banned filler/buzzwords (post-check; retry once if violated)
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, ValidationError

from google import genai
from google.genai import types

from extract import Extracted
from qualify import QualifyResult
from poc import PocResult
from pain_points import PainPointsResult

ROOT = Path(__file__).resolve().parent
PROMPTS = ROOT / "prompts"
CONFIG = yaml.safe_load((ROOT / "evidence_map.yaml").read_text())

MAX_WORDS = 120
BANNED = [
    "seamless", "seamlessly", "cutting-edge", "cutting edge", "synergy",
    "digital transformation", "unlock value", "growing fast",
    "congrats on the funding", "hope this finds you well",
    "i know you're busy", "quick question", "touching base",
    "circling back", "just wanted to", "pick your brain",
]


class EmailDraft(BaseModel):
    subject: str
    body: str


class EmailResult(BaseModel):
    subject: str
    body: str
    word_count: int
    hook_is_question: bool
    banned_phrases_found: list[str] = []


def _client() -> genai.Client:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set.")
    return genai.Client(api_key=key)


def _model() -> str:
    return (os.environ.get("GEMINI_MODEL_EMAIL")
            or os.environ.get("GEMINI_MODEL")
            or "gemini-3.8-flash")


def _sender_name() -> str:
    return os.environ.get("SENDER_NAME", "Priya from Sprinto")


def _word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'’-]+\b", text or ""))


def _first_sentence(body: str) -> str:
    s = re.split(r"(?<=[.!?])\s+", (body or "").strip(), maxsplit=1)
    return s[0] if s else body


def _find_banned(text: str) -> list[str]:
    low = text.lower()
    return [b for b in BANNED if b in low]


def _build_input(domain: str, ex: Extracted, qr: QualifyResult,
                 poc: PocResult, pp: PainPointsResult) -> str:
    return json.dumps({
        "sender_name": _sender_name(),
        "company_name": ex.company_name.value or poc.organization or domain,
        "company_domain": domain,
        "industry": ex.industry.value,
        "compliance_already_held": ex.compliance_mentions.value or [],
        "decision_maker": {
            "first_name": poc.first_name,
            "title": poc.title,
        },
        "pain_points": [p.model_dump() for p in pp.pain_points],
        "audit_flagged": pp.audit_flagged,
        "constraints": {
            "max_body_words": MAX_WORDS,
            "banned_phrases": BANNED,
        },
    }, indent=2, default=str)


def draft_email(domain: str, ex: Extracted, qr: QualifyResult,
                poc: PocResult, pp: PainPointsResult,
                extra_instruction: str = "") -> EmailResult:
    """Return a validated EmailResult. Retries once on invalid JSON, long body,
    or banned-phrase presence."""
    tmpl = (PROMPTS / "email_draft.txt").read_text()
    base_input = _build_input(domain, ex, qr, poc, pp)
    client = _client()
    model = _model()

    last_err: Optional[Exception] = None
    attempt_notes = extra_instruction

    for attempt in (1, 2):
        prompt = tmpl.replace(
            "{{INPUT}}",
            base_input + (f"\n\nREMINDER FROM PREVIOUS ATTEMPT: {attempt_notes}"
                          if attempt_notes else ""),
        )
        resp = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=EmailDraft,
                temperature=0.3,
            ),
        )
        try:
            parsed = getattr(resp, "parsed", None)
            if parsed is None:
                parsed = EmailDraft.model_validate(json.loads(resp.text or ""))
        except (json.JSONDecodeError, ValidationError, TypeError) as e:
            last_err = e
            attempt_notes = "Previous JSON was invalid. Return JSON only."
            continue

        wc = _word_count(parsed.body)
        banned_found = _find_banned(parsed.body) + _find_banned(parsed.subject)
        hook_is_question = _first_sentence(parsed.body).rstrip().endswith("?")

        violations = []
        if wc > MAX_WORDS:
            violations.append(f"body is {wc} words (max {MAX_WORDS}) — tighten")
        if banned_found:
            violations.append(f"body contains banned phrases: {banned_found}")
        if pp.audit_flagged and not hook_is_question:
            violations.append("audit_flagged=true so the first sentence must "
                              "end with '?' (phrased as a question)")

        if violations and attempt == 1:
            attempt_notes = " | ".join(violations)
            continue

        return EmailResult(
            subject=parsed.subject.strip(),
            body=parsed.body.strip(),
            word_count=wc,
            hook_is_question=hook_is_question,
            banned_phrases_found=banned_found,
        )

    raise RuntimeError(f"email_draft: failed after retry: {last_err or 'rule violations'}")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    import scrape as _scrape
    from extract import extract as _extract
    from qualify import qualify as _qualify
    from poc import find_poc as _find_poc
    from pain_points import pain_points as _pains

    if len(sys.argv) < 2:
        print("usage: python email_draft.py <domain>", file=sys.stderr)
        sys.exit(2)

    domain = sys.argv[1]
    scraped = _scrape.scrape_domain(domain, use_cache=True)
    ex = _extract(scraped)
    qr = _qualify(domain, ex)
    poc = _find_poc(domain)
    pp = _pains(domain, ex, qr, poc)
    out = draft_email(domain, ex, qr, poc, pp)

    print(f"Subject: {out.subject}")
    print(f"Words:   {out.word_count}")
    print(f"Hook is question: {out.hook_is_question}")
    print(f"Banned phrases:   {out.banned_phrases_found or 'none'}")
    print("-" * 60)
    print(out.body)
    print("-" * 60)
