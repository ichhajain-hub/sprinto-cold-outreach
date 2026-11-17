"""Step 4 — pick a Point of Contact via Hunter.io.

Flow per domain:
  1. If `cache/hunter/<domain>.json` exists, use it. Hunter's free tier is 25
     searches/month, so we NEVER call twice for the same domain.
  2. Otherwise call Hunter domain-search once, cache the full response.
  3. Score each returned email by title tier (evidence_map.yaml):
       tier 1 > tier 2 > tier 3.
     tier 3 (CEO / Co-founder / Founder) only applies when headcount is
     unknown or below `tier_3_headcount_ceiling` (default 80).
  4. Require first name + confidence >= `hunter_min_confidence` (default 70).
  5. If Hunter gives nothing usable OR there's no API key, fall back to
     poc_overrides.json (filled manually from Apollo).

Returned PocResult also carries Hunter's company headcount — this is often
the only reliable source for the S1 (headcount 50-200) signal.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

import requests
import yaml
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
CONFIG = yaml.safe_load((ROOT / "evidence_map.yaml").read_text())

CACHE_DIR = ROOT / "cache" / "hunter"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
OVERRIDES_FILE = ROOT / "poc_overrides.json"

MIN_CONFIDENCE = int(CONFIG.get("hunter_min_confidence", 70))
TIERS = CONFIG["poc_title_priority"]
TIER_3_CEILING = int(CONFIG.get("tier_3_headcount_ceiling", 80))


class PocResult(BaseModel):
    source: str                           # "hunter" | "override" | "none"
    name: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    title: Optional[str] = None
    tier: Optional[int] = None            # 1, 2, 3
    confidence: Optional[int] = None
    linkedin: Optional[str] = None
    headcount: Optional[str] = None       # Hunter company metadata, e.g. "50-200"
    organization: Optional[str] = None
    reason: str = ""                      # defensibility explanation


# ---- Helpers ---------------------------------------------------------------

def _title_tier(title: str) -> Optional[int]:
    """Match an email's position against evidence_map.yaml tiers."""
    if not title:
        return None
    t = title.lower()
    for tier_num, keys in [(1, TIERS["tier_1"]),
                           (2, TIERS["tier_2"]),
                           (3, TIERS["tier_3"])]:
        for k in keys:
            if k.lower() in t:
                return tier_num
    return None


def _headcount_min(hc: Optional[str]) -> Optional[int]:
    """Smallest number in a Hunter headcount range like '50-200' or '201-500'."""
    if not hc:
        return None
    m = re.match(r"^\s*(\d+)", hc)
    return int(m.group(1)) if m else None


# ---- Hunter call (cached) --------------------------------------------------

def _call_hunter(domain: str, api_key: str) -> Optional[dict]:
    cp = CACHE_DIR / f"{domain}.json"
    if cp.exists():
        return json.loads(cp.read_text())
    try:
        r = requests.get(
            "https://api.hunter.io/v2/domain-search",
            params={"domain": domain, "api_key": api_key},
            timeout=15,
        )
    except requests.RequestException as e:
        print(f"  ! Hunter network error for {domain}: {e}", file=sys.stderr)
        return None
    if r.status_code == 401:
        print("  ! Hunter 401 — HUNTER_API_KEY invalid", file=sys.stderr)
        return None
    if r.status_code == 429 or (r.status_code == 403 and "quota" in r.text.lower()):
        print("  ! Hunter quota exhausted (25/month on free tier)", file=sys.stderr)
        return None
    if not r.ok:
        print(f"  ! Hunter {r.status_code} for {domain}: {r.text[:200]}", file=sys.stderr)
        return None
    data = r.json()
    cp.write_text(json.dumps(data, indent=2))
    return data


def _pick_from_hunter(data: dict) -> tuple[Optional[dict], Optional[int], str]:
    """Scan Hunter emails, respect tier/confidence rules, return the best."""
    org = (data or {}).get("data", {})
    emails = org.get("emails", []) or []
    hc_min = _headcount_min(org.get("headcount") or "")
    tier_3_allowed = (hc_min is None) or (hc_min < TIER_3_CEILING)

    best: dict[int, Optional[dict]] = {1: None, 2: None, 3: None}
    for e in emails:
        if not e.get("first_name"):
            continue
        if (e.get("confidence") or 0) < MIN_CONFIDENCE:
            continue
        tier = _title_tier(e.get("position", "") or "")
        if tier is None:
            continue
        if tier == 3 and not tier_3_allowed:
            continue
        existing = best[tier]
        if existing is None or (e.get("confidence") or 0) > (existing.get("confidence") or 0):
            best[tier] = e

    for tier in (1, 2, 3):
        if best[tier]:
            c = best[tier]
            return (c, tier,
                    f"tier {tier} match on title='{c.get('position')}', "
                    f"confidence {c.get('confidence')}%")

    return (None, None,
            f"no match at tier 1/2/3 with confidence >= {MIN_CONFIDENCE}%; "
            f"tier_3_allowed={tier_3_allowed} (headcount={org.get('headcount')})")


# ---- Override fallback -----------------------------------------------------

def _try_override(domain: str) -> Optional[PocResult]:
    if not OVERRIDES_FILE.exists():
        return None
    try:
        overrides = json.loads(OVERRIDES_FILE.read_text())
    except json.JSONDecodeError as e:
        print(f"  ! poc_overrides.json invalid JSON: {e}", file=sys.stderr)
        return None
    o = overrides.get(domain)
    if not o:
        return None
    fn = o.get("first_name")
    ln = o.get("last_name") or ""
    title = o.get("title") or ""
    return PocResult(
        source="override",
        name=f"{fn} {ln}".strip() if fn else None,
        first_name=fn,
        last_name=o.get("last_name"),
        email=o.get("email"),
        title=title,
        tier=_title_tier(title),
        linkedin=o.get("linkedin"),
        reason=f"from poc_overrides.json for {domain}",
    )


# ---- Public API ------------------------------------------------------------

def find_poc(domain: str) -> PocResult:
    api_key = os.environ.get("HUNTER_API_KEY")
    hunter_data = _call_hunter(domain, api_key) if api_key else None

    if hunter_data:
        org = (hunter_data or {}).get("data", {}) or {}
        chosen, tier, reason = _pick_from_hunter(hunter_data)
        if chosen:
            fn = chosen.get("first_name") or ""
            ln = chosen.get("last_name") or ""
            return PocResult(
                source="hunter",
                name=f"{fn} {ln}".strip() or None,
                first_name=fn or None,
                last_name=ln or None,
                email=chosen.get("value"),
                title=chosen.get("position"),
                tier=tier,
                confidence=chosen.get("confidence"),
                linkedin=chosen.get("linkedin"),
                headcount=org.get("headcount"),
                organization=org.get("organization"),
                reason=reason,
            )
        # Hunter responded but nothing met the bar — try overrides, then fail.
        over = _try_override(domain)
        if over:
            over.headcount = org.get("headcount")
            over.organization = org.get("organization")
            over.reason = f"{over.reason} (Hunter had no qualifying title; reason: {reason})"
            return over
        return PocResult(
            source="none",
            headcount=org.get("headcount"),
            organization=org.get("organization"),
            reason=f"Hunter had {len(org.get('emails') or [])} emails, {reason}",
        )

    # No Hunter data (no key, or network/quota error) — rely on overrides.
    over = _try_override(domain)
    if over:
        return over
    return PocResult(
        source="none",
        reason=("no HUNTER_API_KEY and no override in poc_overrides.json for "
                + domain) if not api_key else
               (f"Hunter unavailable and no override in poc_overrides.json for {domain}"),
    )


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    if len(sys.argv) < 2:
        print("usage: python poc.py <domain>", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(find_poc(sys.argv[1]).model_dump(), indent=2))
