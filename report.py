"""A brief, defensible one-screen report for a batch run.

Reads runs/<date>/*.json (the per-domain traces written by run.py) and prints
a tight per-domain block: outcome, score, chosen PoC, email subject, and the
one-line reason for drops. Saves the same text to runs/<date>/report.txt.

CLI:
  python report.py                   # latest run
  python report.py 2026-11-17        # specific date
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"

WIDTH = 68
RULE = "─" * WIDTH


def _latest_date() -> Optional[str]:
    if not RUNS.exists():
        return None
    dates = sorted(p.name for p in RUNS.iterdir() if p.is_dir())
    return dates[-1] if dates else None


def _row(domain: str, trace: dict) -> list[str]:
    """Return 1-3 lines for this domain's block."""
    steps = trace.get("steps", {})
    qr = steps.get("qualify") or {}
    poc = steps.get("poc") or {}
    em = steps.get("email_draft") or {}
    send = steps.get("send") or {}

    decision = qr.get("decision", "error")
    score = qr.get("priority_score", 0)
    sent = bool(send.get("sent"))
    dry = bool(send.get("dry_run"))

    # Determine symbol + right-hand tag
    if trace.get("error"):
        sym, tag = "✗", "crashed"
    elif decision == "drop":
        gid = _first_failed_gate(qr)
        tag = f"drop · {gid} fail" if _any_fail(qr) else f"drop · {gid} unresolved" if gid else "drop"
        sym = "✗"
    elif poc and not poc.get("email"):
        tag = "no PoC"
        sym = "·"
    elif sent:
        sym, tag = "✓", f"qualify · {score}/100 · sent"
    elif dry:
        sym, tag = "·", f"qualify · {score}/100 · dry"
    else:
        sym, tag = "·", f"qualify · {score}/100"

    header = _pad(f" {sym} {domain}", tag)
    lines = [header]

    # PoC line (if we have one)
    if poc.get("name"):
        conf = poc.get("confidence")
        tier = poc.get("tier")
        bits = [poc.get("name", ""), poc.get("title", "")]
        if tier:
            bits.append(f"tier {tier}")
        if conf is not None:
            bits.append(f"{conf}%")
        lines.append(f"     ↳ " + " · ".join(b for b in bits if b))

    # Email line (if drafted)
    if em.get("subject"):
        subj = em["subject"]
        wc = em.get("word_count")
        send_bit = "sent" if sent else ("dry" if dry else "not sent")
        lines.append(f'     ↳ "{subj}" · {wc} w · {send_bit}')

    # Reason line for drops / no-PoC
    if decision == "drop":
        reason = _short_reason(qr)
        if reason:
            lines.append(f"     ↳ {reason}")
    elif poc and not poc.get("email"):
        r = (poc.get("reason") or "").split(";")[0]
        lines.append(f"     ↳ {r}")

    return lines


def _any_fail(qr: dict) -> bool:
    return any(g.get("verdict") == "fail" for g in qr.get("gates", []))


def _first_failed_gate(qr: dict) -> str:
    for g in qr.get("gates", []):
        if g.get("verdict") in ("fail", "unknown"):
            return g.get("gate_id", "")
    return ""


def _short_reason(qr: dict) -> str:
    for g in qr.get("gates", []):
        if g.get("verdict") in ("fail", "unknown"):
            ev = (g.get("evidence") or "").strip()
            if len(ev) > 70:
                ev = ev[:67].rstrip() + "..."
            prefix = f"{g['gate_id']} {g['verdict']}:"
            return f"{prefix} {ev}" if ev else prefix.rstrip(":")
    return ""


def _pad(left: str, right: str) -> str:
    gap = max(2, WIDTH - len(left) - len(right) - 1)
    return f"{left}{' ' * gap}{right}"


def build(date: str) -> str:
    date_dir = RUNS / date
    traces = sorted(date_dir.glob("*.json"))
    rows: list[str] = []
    totals = {"total": 0, "sent": 0, "dry": 0, "drop": 0, "no_poc": 0, "error": 0}

    for p in traces:
        trace = json.loads(p.read_text())
        rows.extend(_row(p.stem, trace))
        rows.append("")
        totals["total"] += 1
        steps = trace.get("steps", {})
        qr = steps.get("qualify") or {}
        poc = steps.get("poc") or {}
        send = steps.get("send") or {}
        if trace.get("error"):
            totals["error"] += 1
        elif qr.get("decision") == "drop":
            totals["drop"] += 1
        elif poc and not poc.get("email"):
            totals["no_poc"] += 1
        elif send.get("sent"):
            totals["sent"] += 1
        elif send.get("dry_run"):
            totals["dry"] += 1

    if rows and rows[-1] == "":
        rows.pop()

    out: list[str] = []
    out.append(RULE)
    out.append(f" Sprinto cold outreach · {date} · {totals['total']} domain(s)")
    out.append(RULE)
    out.append("")
    out.extend(rows)
    out.append("")
    out.append(RULE)
    summary_parts = []
    if totals["sent"]:
        summary_parts.append(f"{totals['sent']} sent")
    if totals["dry"]:
        summary_parts.append(f"{totals['dry']} drafted (dry)")
    if totals["no_poc"]:
        summary_parts.append(f"{totals['no_poc']} no PoC")
    if totals["drop"]:
        summary_parts.append(f"{totals['drop']} dropped")
    if totals["error"]:
        summary_parts.append(f"{totals['error']} crashed")
    out.append(f" {' · '.join(summary_parts) or '—'} · trace: runs/{date}/")
    out.append(RULE)
    return "\n".join(out)


def write_and_print(date: str) -> Path:
    text = build(date)
    out_path = RUNS / date / "report.txt"
    out_path.write_text(text + "\n")
    print(text)
    return out_path


if __name__ == "__main__":
    date = sys.argv[1] if len(sys.argv) >= 2 else _latest_date()
    if not date:
        print("no runs found under ./runs/", file=sys.stderr)
        sys.exit(1)
    if not (RUNS / date).exists():
        print(f"no run for {date} under ./runs/", file=sys.stderr)
        sys.exit(1)
    write_and_print(date)
