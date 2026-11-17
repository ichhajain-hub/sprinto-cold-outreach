"""Step 1 — scrape a company's website.

Fetches a fixed set of common paths, strips chrome (nav/footer/scripts),
and inlines anchor hrefs into the text so downstream steps can see things
like "Sign in [-> https://app.example.com/login]" as SaaS evidence.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
CONFIG = yaml.safe_load((ROOT / "evidence_map.yaml").read_text())
PATHS = CONFIG["paths"]["scrape_subpaths"]
TIMEOUT = CONFIG["paths"]["scrape_timeout_seconds"]
UA = " ".join(CONFIG["paths"]["user_agent"].split())

CACHE_DIR = ROOT / "cache" / "pages"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

STRIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "header"}
STRIP_ROLES = {"navigation", "banner", "contentinfo"}


def _cache_path(url: str) -> Path:
    return CACHE_DIR / f"{hashlib.sha1(url.encode()).hexdigest()}.html"


def _fetch(url: str, use_cache: bool = True) -> str | None:
    """Return HTML, or None on 404/error. Caches successful fetches to disk."""
    cp = _cache_path(url)
    if use_cache and cp.exists():
        return cp.read_text(encoding="utf-8", errors="replace")
    try:
        r = requests.get(
            url,
            timeout=TIMEOUT,
            headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"},
            allow_redirects=True,
        )
    except requests.RequestException as e:
        print(f"  ! {url} -> {type(e).__name__}: {e}", file=sys.stderr)
        return None
    if r.status_code == 404:
        return None
    if not r.ok:
        print(f"  ! {url} -> HTTP {r.status_code}", file=sys.stderr)
        return None
    # Respect declared charset when it looks real, else sniff — fixes mojibake on
    # sites whose Content-Type lies.
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding or "utf-8"
    html = r.text
    cp.write_text(html, encoding="utf-8")
    return html


def _clean(html: str, base_url: str) -> str:
    """Strip chrome, inline anchor hrefs, return newline-separated clean text."""
    soup = BeautifulSoup(html, "lxml")
    for name in STRIP_TAGS:
        for t in soup.find_all(name):
            t.decompose()
    for t in soup.find_all(attrs={"role": lambda v: v in STRIP_ROLES if v else False}):
        t.decompose()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        abs_href = urljoin(base_url, href)
        text = a.get_text(" ", strip=True)
        a.clear()
        a.append(f"{text} [-> {abs_href}]" if text else f"[-> {abs_href}]")
    text = soup.get_text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines)


def scrape_domain(domain: str, use_cache: bool = True) -> dict:
    """Fetch all configured paths for a domain.

    Returns:
      {"domain": str,
       "pages": [{"url": str, "status": "ok"|"missing", "text": str, "chars": int}, ...]}
    """
    base = f"https://{domain}"
    pages = []
    for path in PATHS:
        url = urljoin(base, path)
        html = _fetch(url, use_cache=use_cache)
        if html is None:
            pages.append({"url": url, "status": "missing", "text": "", "chars": 0})
            continue
        text = _clean(html, url)
        pages.append({"url": url, "status": "ok", "text": text, "chars": len(text)})
    return {"domain": domain, "pages": pages}


def summarise(result: dict) -> str:
    lines = [f"Domain: {result['domain']}"]
    for p in result["pages"]:
        lines.append(f"  {p['status']:<7} {p['chars']:>7} chars  {p['url']}")
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python scrape.py <domain> [--no-cache] [--full]", file=sys.stderr)
        sys.exit(2)
    domain = sys.argv[1]
    use_cache = "--no-cache" not in sys.argv
    show_full = "--full" in sys.argv
    result = scrape_domain(domain, use_cache=use_cache)
    print(summarise(result))
    for p in result["pages"]:
        if p["status"] != "ok":
            continue
        print(f"\n----- {p['url']} -----")
        text = p["text"]
        if not show_full and len(text) > 1500:
            print(text[:800])
            print(f"\n   ... [{len(text) - 1500} chars omitted] ...\n")
            print(text[-700:])
        else:
            print(text)
