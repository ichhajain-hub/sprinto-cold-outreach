"""Step 3 — qualify a company against G1/G2/G3 and score S1-S4.

Flow per company:
  1. Pre-check each gate/signal against the Step 2 extract (free, no API).
  2. For any gate still "unknown", run a Gemini function-calling loop with
     two evidence tools (fetch_page, search_news) and a return_verdict tool.
     Per-tool attempt limits are enforced in code, not just in the prompt.
  3. Compute decision and 0-100 priority score.

G1 and G2 drop the company on fail or unresolved unknown. G3 unknown becomes
"qualify_and_flag" (email uses a question-hook, not an assertion).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus, urlparse

import requests
import yaml
from pydantic import BaseModel, Field

from google import genai
from google.genai import types

import scrape as _scrape
from extract import Extracted

ROOT = Path(__file__).resolve().parent
PROMPTS = ROOT / "prompts"
CONFIG = yaml.safe_load((ROOT / "evidence_map.yaml").read_text())

NEWS_CACHE = ROOT / "cache" / "news"
NEWS_CACHE.mkdir(parents=True, exist_ok=True)


# ---- Result schemas --------------------------------------------------------

class GateResult(BaseModel):
    gate_id: str
    name: str
    verdict: str                      # pass | fail | unknown
    evidence: Optional[str] = None
    source_url: Optional[str] = None
    attempts: dict = Field(default_factory=dict)
    flag_email_as_question: bool = False  # only ever set for G3=unknown


class SignalResult(BaseModel):
    signal_id: str
    name: str
    verdict: str                      # pass | unknown  (signals never fail)
    evidence: Optional[str] = None
    source_url: Optional[str] = None
    score_awarded: int = 0


class QualifyResult(BaseModel):
    domain: str
    decision: str                     # qualify | qualify_and_flag | drop
    reason: str
    gates: list[GateResult]
    signals: list[SignalResult]
    priority_score: int               # 0-100


# ---- Gemini plumbing -------------------------------------------------------

def _client() -> genai.Client:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set.")
    return genai.Client(api_key=key)


def _model() -> str:
    return (
        os.environ.get("GEMINI_MODEL_QUALIFY")
        or os.environ.get("GEMINI_MODEL")
        or "gemini-3.8-flash"
    )


def _tool_decls() -> list:
    return [types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name="fetch_page",
            description=(
                "Fetch a page on the target company's own domain (any subdomain) "
                "and return cleaned text with inlined [-> URL] link targets."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={"url": types.Schema(type="STRING",
                    description="Full URL on the target company's domain.")},
                required=["url"],
            ),
        ),
        types.FunctionDeclaration(
            name="search_news",
            description=(
                "Search Google News and return the top ~10 recent items as JSON "
                "(title, pubDate, link). Use for funding, acquisitions, headcount."
            ),
            parameters=types.Schema(
                type="OBJECT",
                properties={"query": types.Schema(type="STRING",
                    description="Google News query. Include the company's product name "
                                "or domain to disambiguate.")},
                required=["query"],
            ),
        ),
        types.FunctionDeclaration(
            name="return_verdict",
            description="Call once to end the run. Returns the final verdict for this gate.",
            parameters=types.Schema(
                type="OBJECT",
                properties={
                    "verdict": types.Schema(type="STRING", enum=["pass", "fail", "unknown"]),
                    "evidence": types.Schema(type="STRING",
                        description="One short sentence citing the key fact."),
                    "source_url": types.Schema(type="STRING",
                        description="URL where the evidence was found. Empty string if unknown."),
                },
                required=["verdict", "evidence"],
            ),
        ),
    ])]


# ---- Tool implementations --------------------------------------------------

def _fetch_page_tool(url: str, domain: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if host != domain and not host.endswith("." + domain):
        return f"REFUSED: {url} is not on the target domain {domain}."
    html = _scrape._fetch(url, use_cache=True)
    if html is None:
        return f"NOT_FOUND_OR_ERROR for {url}"
    text = _scrape._clean(html, url)
    return text[:8000] + ("\n...[truncated]" if len(text) > 8000 else "")


def _search_news_tool(query: str) -> str:
    cp = NEWS_CACHE / f"{hashlib.sha1(query.encode()).hexdigest()}.xml"
    if cp.exists():
        xml = cp.read_text(encoding="utf-8")
    else:
        url = f"https://news.google.com/rss/search?q={quote_plus(query)}"
        try:
            r = requests.get(
                url,
                timeout=10,
                headers={"User-Agent": CONFIG["paths"]["user_agent"]},
            )
            r.raise_for_status()
            xml = r.text
            cp.write_text(xml, encoding="utf-8")
        except requests.RequestException as e:
            return f"SEARCH_ERROR: {type(e).__name__}: {e}"
    try:
        root = ET.fromstring(xml.encode("utf-8"))
    except ET.ParseError as e:
        return f"PARSE_ERROR: {e}"
    items = []
    for it in root.findall(".//item")[:10]:
        items.append({
            "title": (it.findtext("title") or "").strip(),
            "pubDate": (it.findtext("pubDate") or "").strip(),
            "link": (it.findtext("link") or "").strip(),
        })
    return json.dumps(items, indent=2) if items else "NO_RESULTS"


def _execute_tool(name: str, args: dict, domain: str) -> str:
    if name == "fetch_page":
        return _fetch_page_tool(args.get("url", ""), domain)
    if name == "search_news":
        return _search_news_tool(args.get("query", ""))
    return f"UNKNOWN_TOOL: {name}"


# ---- Pre-checks (free, from Step 2 extract only) --------------------------

_RECURRING_RE = re.compile(r"/mo\b|/yr\b|per\s*user|per\s*seat|\$\s*\d+\s*/", re.I)
_SAAS_BM = {"saas", "freemium saas", "subscription software",
            "self-serve + enterprise", "open-source with hosted tier"}
_NON_SAAS_BM = {"agency", "services firm", "d2c", "hardware"}


def _precheck_G1(ex: Extracted) -> Optional[GateResult]:
    pricing = ex.pricing_signals.value or []
    recurring = any(_RECURRING_RE.search(p) for p in pricing)
    login = ex.login_or_trial_link.value
    bm = (ex.business_model.value or "").strip().lower()
    if recurring or login or bm in _SAAS_BM:
        src = (ex.pricing_signals.source_url if recurring else None) \
              or (ex.login_or_trial_link.source_url if login else None) \
              or ex.business_model.source_url
        evidence_bits = []
        if recurring:
            evidence_bits.append(f"recurring pricing ({pricing[0]})")
        if login:
            evidence_bits.append(f"login link {login}")
        if bm in _SAAS_BM:
            evidence_bits.append(f'business_model="{bm}"')
        return GateResult(gate_id="G1", name="saas", verdict="pass",
                          evidence="; ".join(evidence_bits), source_url=src)
    if bm in _NON_SAAS_BM:
        return GateResult(gate_id="G1", name="saas", verdict="fail",
                          evidence=f'business_model="{bm}"',
                          source_url=ex.business_model.source_url)
    return None


_SERIES_ABC = re.compile(r"\bseries\s*[abc]\b", re.I)
_SERIES_BAD = re.compile(r"\bseries\s*[d-z]\b|\bseed\b|\bpre-?seed\b|\bipo\b|\bacqui(red|sition)\b", re.I)


def _precheck_G2(ex: Extracted) -> Optional[GateResult]:
    funding = ex.funding_mentions.value or []
    for f in funding:
        if _SERIES_BAD.search(f):
            return GateResult(gate_id="G2", name="funding_stage", verdict="fail",
                              evidence=f, source_url=ex.funding_mentions.source_url)
        if _SERIES_ABC.search(f):
            return GateResult(gate_id="G2", name="funding_stage", verdict="pass",
                              evidence=f, source_url=ex.funding_mentions.source_url)
    return None


_SEC_ROLE_RE = re.compile(r"security|compliance|\bgrc\b|ciso|soc\s*2", re.I)
_AUDIT_CERTS_RE = re.compile(r"\bsoc\s*2\b|iso\s*/?\s*iec\s*27001|iso\s*27001|in\s+progress|working\s+toward", re.I)


def _precheck_G3(ex: Extracted) -> Optional[GateResult]:
    comp = ex.compliance_mentions.value or []
    for c in comp:
        if _AUDIT_CERTS_RE.search(c):
            return GateResult(gate_id="G3", name="audit_readiness", verdict="pass",
                              evidence=f"compliance: {c}", source_url=ex.compliance_mentions.source_url)
    roles = ex.open_roles.value or []
    for r in roles:
        if _SEC_ROLE_RE.search(r):
            return GateResult(gate_id="G3", name="audit_readiness", verdict="pass",
                              evidence=f"open security/compliance role: {r}",
                              source_url=ex.open_roles.source_url)
    pricing = ex.pricing_signals.value or []
    ent = next((p for p in pricing if "enterprise" in p.lower()), None)
    if ent:
        return GateResult(gate_id="G3", name="audit_readiness", verdict="pass",
                          evidence=f"Enterprise tier present ({ent}) — audit demand likely",
                          source_url=ex.pricing_signals.source_url)
    return None


# ---- Agent loop for an unresolved gate ------------------------------------

def _format_extract_context(ex: Extracted) -> str:
    """Compact JSON of the extracted facts, for the agent's context."""
    d = ex.model_dump()
    return json.dumps(d, indent=2, default=str)


def _run_agent(gate_cfg: dict, domain: str, ex: Extracted,
               limits: dict[str, int]) -> GateResult:
    """Gemini function-calling loop. Returns a GateResult."""
    system = (PROMPTS / "qualify_agent_system.txt").read_text()
    question = gate_cfg["question"]
    short_circuit = gate_cfg.get("short_circuit_fail") or {}
    on_unanswered = gate_cfg.get("on_unanswered", "drop")

    user_prompt = (
        f"Target company domain: {domain}\n\n"
        f"Question (gate {gate_cfg['id']} — {gate_cfg['name']}): {question}\n\n"
        f"Short-circuit FAIL reasons (any of these mean 'fail'):\n"
        f"  {json.dumps(short_circuit, indent=2) if short_circuit else '(none — this gate cannot short-circuit fail)'}\n\n"
        f"Attempt budget (hard cap): "
        f"fetch_page x {limits.get('fetch_page', 0)}, "
        f"search_news x {limits.get('search_news', 0)}.\n\n"
        f"Facts already extracted from the company's homepage/pricing/about/"
        f"careers/security/customers (null = not seen):\n{_format_extract_context(ex)}\n\n"
        f"If the extract already answers the question, call return_verdict "
        f"immediately. Otherwise use the tools, then call return_verdict. "
        f"When your budget is exhausted, call return_verdict with "
        f"verdict=\"unknown\"."
    )

    contents = [types.Content(role="user", parts=[types.Part(text=user_prompt)])]
    tools = _tool_decls()
    client = _client()
    model = _model()
    attempts = {"fetch_page": 0, "search_news": 0}
    verdict_args: Optional[dict] = None

    for _ in range(12):   # hard safety cap on loop iterations
        resp = client.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                tools=tools,
                system_instruction=system,
                temperature=0,
            ),
        )
        if not resp.candidates:
            break
        cand = resp.candidates[0]
        parts = cand.content.parts if cand.content else []
        if not parts:
            break

        contents.append(cand.content)

        fc_parts = [p for p in parts if getattr(p, "function_call", None)]
        if not fc_parts:
            # Model gave text without calling anything — treat as "unknown".
            break

        response_parts = []
        for p in fc_parts:
            fc = p.function_call
            name = fc.name
            args = dict(fc.args) if fc.args else {}

            if name == "return_verdict":
                verdict_args = args
                break

            if attempts.get(name, 0) >= limits.get(name, 0):
                result = (f"BUDGET_EXHAUSTED for {name}. You must call "
                          f"return_verdict now.")
            else:
                attempts[name] = attempts.get(name, 0) + 1
                result = _execute_tool(name, args, domain)

            response_parts.append(types.Part.from_function_response(
                name=name, response={"result": result}))

        if verdict_args is not None:
            break
        contents.append(types.Content(role="user", parts=response_parts))

    if verdict_args:
        verdict = (verdict_args.get("verdict") or "unknown").lower()
        evidence = verdict_args.get("evidence")
        source_url = verdict_args.get("source_url") or None
    else:
        verdict = "unknown"
        evidence = "agent did not return a verdict within budget"
        source_url = None

    flag = (gate_cfg["id"] == "G3" and verdict == "unknown"
            and on_unanswered == "qualify_and_flag")

    return GateResult(gate_id=gate_cfg["id"], name=gate_cfg["name"],
                      verdict=verdict, evidence=evidence,
                      source_url=source_url, attempts=attempts,
                      flag_email_as_question=flag)


# ---- Signal pre-checks (score contributors; never drop) -------------------

def _parse_range(s: str) -> Optional[tuple[int, int]]:
    if not s:
        return None
    s = s.strip()
    m = re.match(r"^(\d+)\s*[-–]\s*(\d+)$", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.match(r"^(\d+)\+?$", s)
    if m:
        n = int(m.group(1))
        return n, n
    return None


def _check_S1(ex: Extracted) -> SignalResult:
    rng = _parse_range(ex.employee_count.value or "")
    if rng and rng[0] <= 200 and rng[1] >= 50:
        return SignalResult(signal_id="S1", name="headcount_50_200",
                            verdict="pass", evidence=f"headcount {ex.employee_count.value}",
                            source_url=ex.employee_count.source_url, score_awarded=34)
    return SignalResult(signal_id="S1", name="headcount_50_200",
                        verdict="unknown",
                        evidence=f"employee_count={ex.employee_count.value}")


def _check_S2(ex: Extracted) -> SignalResult:
    logos = ex.customer_logos.value or []
    if len(logos) >= 2:
        preview = ", ".join(logos[:5])
        return SignalResult(signal_id="S2", name="clients",
                            verdict="pass", evidence=f"{len(logos)} logos: {preview}",
                            source_url=ex.customer_logos.source_url, score_awarded=33)
    return SignalResult(signal_id="S2", name="clients", verdict="unknown",
                        evidence=f"{len(logos)} logos found")


_S3_TIERS = {"fintech": 33, "healthtech": 33, "hrtech": 20, "devtools": 20, "other": 10}


def _check_S3(ex: Extracted) -> SignalResult:
    ind = (ex.industry.value or "").strip().lower()
    if ind in _S3_TIERS:
        return SignalResult(signal_id="S3", name="industry",
                            verdict="pass", evidence=f"industry={ind}",
                            source_url=ex.industry.source_url,
                            score_awarded=_S3_TIERS[ind])
    return SignalResult(signal_id="S3", name="industry", verdict="unknown",
                        evidence=f"industry={ex.industry.value}")


def _check_S4(ex: Extracted) -> SignalResult:
    cp = (ex.cloud_provider.value or "").strip().lower()
    if cp in {"aws", "gcp", "azure"}:
        return SignalResult(signal_id="S4", name="cloud_stack",
                            verdict="pass", evidence=f"cloud={cp}",
                            source_url=ex.cloud_provider.source_url,
                            score_awarded=0)   # hook-only per spec
    return SignalResult(signal_id="S4", name="cloud_stack",
                        verdict="unknown", evidence="not stated")


# ---- Orchestrator ----------------------------------------------------------

def _gate_cfg(gid: str) -> dict:
    return next(g for g in CONFIG["gates"] if g["id"] == gid)


def _gate_limits(gid: str) -> dict[str, int]:
    cfg = _gate_cfg(gid)
    lim = cfg.get("attempt_limit")
    if isinstance(lim, int):
        # G1 / G2 — pool of N across tools; we split pragmatically.
        if gid == "G1":
            return {"fetch_page": lim, "search_news": lim}
        return {"fetch_page": lim, "search_news": lim}
    if isinstance(lim, dict):
        return {"fetch_page": int(lim.get("pages", 0)),
                "search_news": int(lim.get("searches", 0))}
    return {"fetch_page": 0, "search_news": 0}


def qualify(domain: str, ex: Extracted) -> QualifyResult:
    gates: list[GateResult] = []

    # ---- G1 ----
    g1 = _precheck_G1(ex) or _run_agent(_gate_cfg("G1"), domain, ex, _gate_limits("G1"))
    gates.append(g1)
    if g1.verdict == "fail":
        return _finalise(domain, gates, [], decision="drop",
                         reason=f"G1 fail: {g1.evidence}")
    if g1.verdict == "unknown":
        return _finalise(domain, gates, [], decision="drop",
                         reason="G1 unanswered (insufficient data)")

    # ---- G2 ----
    g2 = _precheck_G2(ex) or _run_agent(_gate_cfg("G2"), domain, ex, _gate_limits("G2"))
    gates.append(g2)
    if g2.verdict == "fail":
        return _finalise(domain, gates, [], decision="drop",
                         reason=f"G2 fail: {g2.evidence}")
    if g2.verdict == "unknown":
        return _finalise(domain, gates, [], decision="drop",
                         reason="G2 unanswered (funding stage not confirmed)")

    # ---- G3 ----
    g3 = _precheck_G3(ex) or _run_agent(_gate_cfg("G3"), domain, ex, _gate_limits("G3"))
    gates.append(g3)
    # G3 never short-circuits fail, but if the agent returned fail we honour it.
    if g3.verdict == "fail":
        return _finalise(domain, gates, [], decision="drop",
                         reason=f"G3 fail: {g3.evidence}")

    # ---- Signals ----
    signals = [_check_S1(ex), _check_S2(ex), _check_S3(ex), _check_S4(ex)]

    # ---- Decision ----
    if g3.verdict == "unknown":
        decision = "qualify_and_flag"
        reason = "G1,G2 pass; G3 unknown -> email uses question-hook"
    else:
        decision = "qualify"
        reason = "G1,G2,G3 pass"

    return _finalise(domain, gates, signals, decision=decision, reason=reason)


def _finalise(domain: str, gates: list[GateResult], signals: list[SignalResult],
              decision: str, reason: str) -> QualifyResult:
    score = sum(s.score_awarded for s in signals)
    return QualifyResult(domain=domain, decision=decision, reason=reason,
                         gates=gates, signals=signals,
                         priority_score=min(score, 100))


# ---- CLI harness ----------------------------------------------------------

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    from extract import extract as run_extract

    if len(sys.argv) < 2:
        print("usage: python qualify.py <domain>", file=sys.stderr)
        sys.exit(2)
    domain = sys.argv[1]
    scraped = _scrape.scrape_domain(domain, use_cache=True)
    ex = run_extract(scraped)
    result = qualify(domain, ex)
    print(json.dumps(result.model_dump(), indent=2))
