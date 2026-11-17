"""Step 5 — name the 2 most likely pain points for the chosen PoC.

One Gemini call. Takes the Step 2 extract, the Step 3 qualify trace, and
the Step 4 PoC. Returns exactly 2 pain points, each citing a specific
evidence fact + the URL it came from. Validates with pydantic; retries once.
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

from extract import Extracted
from qualify import QualifyResult
from poc import PocResult

ROOT = Path(__file__).resolve().parent
PROMPTS = ROOT / "prompts"
CONFIG = yaml.safe_load((ROOT / "evidence_map.yaml").read_text())


class PainPoint(BaseModel):
    pain: str
    evidence_fact: str
    evidence_url: str


class PainPointsResult(BaseModel):
    pain_points: list[PainPoint] = Field(default_factory=list)
    audit_flagged: bool = False     # mirrors G3 flag so email_draft can read it


def _client() -> genai.Client:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set.")
    return genai.Client(api_key=key)


def _model() -> str:
    return (os.environ.get("GEMINI_MODEL_PAIN")
            or os.environ.get("GEMINI_MODEL")
            or "gemini-3.8-flash")


def _build_input(domain: str, ex: Extracted, qr: QualifyResult, poc: PocResult) -> str:
    return json.dumps({
        "company": {
            "domain": domain,
            "name": ex.company_name.value or poc.organization,
            "one_line": ex.one_line_description.value,
            "industry": ex.industry.value,
        },
        "decision_maker": {
            "name": poc.name,
            "first_name": poc.first_name,
            "title": poc.title,
            "tier": poc.tier,
        },
        "extracted_facts": ex.model_dump(),
        "qualification": qr.model_dump(),
    }, indent=2, default=str)


def pain_points(domain: str, ex: Extracted, qr: QualifyResult,
                poc: PocResult) -> PainPointsResult:
    tmpl = (PROMPTS / "pain_points.txt").read_text()
    prompt = tmpl.replace("{{INPUT}}", _build_input(domain, ex, qr, poc))
    client = _client()
    model = _model()

    g3 = next((g for g in qr.gates if g.gate_id == "G3"), None)
    flagged = bool(g3 and g3.flag_email_as_question)

    last_err: Optional[Exception] = None
    for _ in range(2):
        resp = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=PainPointsResult,
                temperature=0.2,
            ),
        )
        try:
            parsed = getattr(resp, "parsed", None)
            if parsed is None:
                parsed = PainPointsResult.model_validate(json.loads(resp.text or ""))
            if len(parsed.pain_points) < 2:
                raise ValueError(f"got {len(parsed.pain_points)} pain points, need 2")
            parsed.pain_points = parsed.pain_points[:2]
            parsed.audit_flagged = flagged
            return parsed
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as e:
            last_err = e
            continue
    raise RuntimeError(f"pain_points: invalid JSON after retry: {last_err}")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    import scrape as _scrape
    from extract import extract as _extract
    from qualify import qualify as _qualify
    from poc import find_poc as _find_poc

    if len(sys.argv) < 2:
        print("usage: python pain_points.py <domain>", file=sys.stderr)
        sys.exit(2)

    domain = sys.argv[1]
    scraped = _scrape.scrape_domain(domain, use_cache=True)
    ex = _extract(scraped)
    qr = _qualify(domain, ex)
    poc = _find_poc(domain)
    result = pain_points(domain, ex, qr, poc)
    print(json.dumps(result.model_dump(), indent=2))
