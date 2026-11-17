"""Orchestrator — the entry point for the cold outreach pipeline.

Usage:
  python run.py domains.txt
  python run.py domains.txt --dry-run

For each domain:
  1. scrape -> 2. extract -> 3. qualify -> 4. poc -> 5. pain -> 6. draft -> 7. send
Writes a per-domain trace to runs/<date>/<domain>.json and a summary row to
runs/<date>/summary.csv. One failing domain never crashes the run.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
import traceback
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

import scrape as _scrape                       # noqa: E402
from extract import extract as _extract        # noqa: E402
from qualify import qualify as _qualify        # noqa: E402
from poc import find_poc as _find_poc          # noqa: E402
from pain_points import pain_points as _pains  # noqa: E402
from email_draft import draft_email as _draft  # noqa: E402
from send import send as _send                 # noqa: E402
import report as _report                        # noqa: E402


def _runs_dir() -> Path:
    d = ROOT / "runs" / dt.date.today().isoformat()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_trace(domain: str, trace: dict) -> Path:
    out = _runs_dir() / f"{domain}.json"
    out.write_text(json.dumps(trace, indent=2, default=str))
    return out


def _write_summary(rows: list[dict]) -> Path:
    out = _runs_dir() / "summary.csv"
    cols = ["domain", "decision", "reason", "poc_name", "poc_title",
            "priority_score", "email_sent"]
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})
    return out


def _row(domain: str, decision: str, reason: str, poc, score: int,
         sent: bool) -> dict:
    return {
        "domain": domain,
        "decision": decision,
        "reason": reason,
        "poc_name": getattr(poc, "name", "") or "" if poc else "",
        "poc_title": getattr(poc, "title", "") or "" if poc else "",
        "priority_score": score,
        "email_sent": "yes" if sent else "no",
    }


def _process(domain: str, dry_run: bool) -> dict:
    trace: dict = {"domain": domain, "steps": {}}
    try:
        print(f"\n==== {domain} ====")
        print("  [1/7] scraping...")
        scraped = _scrape.scrape_domain(domain, use_cache=True)
        trace["steps"]["scrape"] = {
            "pages": [{"url": p["url"], "status": p["status"], "chars": p["chars"]}
                      for p in scraped["pages"]]
        }

        print("  [2/7] extracting...")
        ex = _extract(scraped)
        trace["steps"]["extract"] = ex.model_dump()

        print("  [3/7] qualifying...")
        qr = _qualify(domain, ex)
        trace["steps"]["qualify"] = qr.model_dump()
        print(f"       -> {qr.decision} (score {qr.priority_score}): {qr.reason}")

        if qr.decision == "drop":
            _write_trace(domain, trace)
            return _row(domain, qr.decision, qr.reason, None,
                        qr.priority_score, False)

        print("  [4/7] finding PoC via Hunter...")
        poc = _find_poc(domain)
        trace["steps"]["poc"] = poc.model_dump()
        if not poc.email or not poc.first_name:
            print(f"       -> no usable PoC: {poc.reason}")
            _write_trace(domain, trace)
            return _row(domain, qr.decision,
                        f"{qr.reason}; dropped: no PoC ({poc.reason})",
                        poc, qr.priority_score, False)
        print(f"       -> {poc.name} ({poc.title}, tier {poc.tier},"
              f" confidence {poc.confidence}%)")

        print("  [5/7] pain points...")
        pp = _pains(domain, ex, qr, poc)
        trace["steps"]["pain_points"] = pp.model_dump()

        print("  [6/7] drafting email...")
        em = _draft(domain, ex, qr, poc, pp)
        trace["steps"]["email_draft"] = em.model_dump()
        print(f"       -> {em.word_count} words: \"{em.subject}\"")

        print(f"  [7/7] sending{' (DRY)' if dry_run else ''}...")
        sr = _send(em.subject, em.body, poc, dry_run=dry_run)
        trace["steps"]["send"] = sr.model_dump()
        if sr.sent:
            print(f"       -> sent, resend id={sr.message_id}")
        elif sr.dry_run:
            print(f"       -> dry run")
        else:
            print(f"       -> send FAILED: {sr.error}")

        _write_trace(domain, trace)
        return _row(domain, qr.decision, qr.reason, poc, qr.priority_score,
                    sr.sent or sr.dry_run)

    except Exception as e:
        trace["error"] = f"{type(e).__name__}: {e}"
        trace["traceback"] = traceback.format_exc()
        _write_trace(domain, trace)
        print(f"  ! {domain} crashed: {e}", file=sys.stderr)
        return _row(domain, "error", str(e), None, 0, False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("domains_file")
    ap.add_argument("--dry-run", action="store_true",
                    help="run all steps except actually sending email")
    args = ap.parse_args()

    domains = []
    with open(args.domains_file) as f:
        for line in f:
            d = line.strip()
            if d and not d.startswith("#"):
                domains.append(d)

    rows = [_process(d, dry_run=args.dry_run) for d in domains]
    _write_summary(rows)
    print()
    _report.write_and_print(dt.date.today().isoformat())
    return 0


if __name__ == "__main__":
    sys.exit(main())
