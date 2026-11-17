"""Step 2 — extract structured facts from scraped pages using Gemini.

One LLM call turns the raw text from scrape.py into a validated pydantic
document. Every field carries the URL it was extracted from so downstream
steps (qualify, pain_points, email) can cite evidence.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, ValidationError

from google import genai
from google.genai import types

ROOT = Path(__file__).resolve().parent
PROMPTS = ROOT / "prompts"
CONFIG = yaml.safe_load((ROOT / "evidence_map.yaml").read_text())


# ---- Schema ----------------------------------------------------------------

class SourcedStr(BaseModel):
    value: Optional[str] = None
    source_url: Optional[str] = None


class SourcedList(BaseModel):
    value: list[str] = Field(default_factory=list)
    source_url: Optional[str] = None


class Extracted(BaseModel):
    company_name: SourcedStr
    one_line_description: SourcedStr
    business_model: SourcedStr
    pricing_signals: SourcedList
    login_or_trial_link: SourcedStr
    compliance_mentions: SourcedList
    open_roles: SourcedList
    customer_logos: SourcedList
    industry: SourcedStr                 # fintech|healthtech|hrtech|devtools|other
    cloud_provider: SourcedStr           # aws|gcp|azure|null
    employee_count: SourcedStr           # "147" or "50-200" or null
    funding_mentions: SourcedList


# ---- Prompt + Gemini plumbing ---------------------------------------------

def _format_pages(scrape_result: dict) -> str:
    chunks = []
    for p in scrape_result["pages"]:
        if p["status"] != "ok" or not p["text"].strip():
            continue
        chunks.append(f"===== URL: {p['url']} =====\n{p['text']}")
    return "\n\n".join(chunks)


def _build_prompt(scrape_result: dict) -> str:
    template = (PROMPTS / "extract.txt").read_text()
    return template.replace("{{PAGES}}", _format_pages(scrape_result))


def _get_client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set. Check .env (dotenv is loaded in __main__).")
    return genai.Client(api_key=api_key)


def _model_name() -> str:
    return (
        os.environ.get("GEMINI_MODEL_EXTRACT")
        or os.environ.get("GEMINI_MODEL")
        or "gemini-3.8-flash"
    )


# ---- Public API ------------------------------------------------------------

def extract(scrape_result: dict) -> Extracted:
    """Return a validated Extracted. Retries once on invalid JSON."""
    prompt = _build_prompt(scrape_result)
    client = _get_client()
    model = _model_name()

    last_err: Exception | None = None
    for attempt in (1, 2):
        resp = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=Extracted,
                temperature=0,
            ),
        )
        try:
            if getattr(resp, "parsed", None) is not None:
                return resp.parsed
            data = json.loads(resp.text or "")
            return Extracted.model_validate(data)
        except (json.JSONDecodeError, ValidationError, TypeError) as e:
            last_err = e
            continue
    raise RuntimeError(f"extract: invalid JSON after retry: {last_err}")


# ---- CLI harness -----------------------------------------------------------

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    import scrape as _scrape  # noqa: E402

    if len(sys.argv) < 2:
        print("usage: python extract.py <domain>", file=sys.stderr)
        sys.exit(2)

    domain = sys.argv[1]
    s = _scrape.scrape_domain(domain, use_cache=True)
    out = extract(s)
    print(json.dumps(out.model_dump(), indent=2))
