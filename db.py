"""Postgres (Neon) persistence layer.

One row per (date, domain) in `runs`. The full trace stays as JSONB; flat
columns index the fields the dashboard and SQL queries actually use.

CLI:
    python3 db.py init           # apply schema.sql
    python3 db.py backfill       # insert every runs/<date>/<domain>.json
    python3 db.py stats          # quick counts: total, by decision, by date
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Optional

import psycopg
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"
SCHEMA = ROOT / "schema.sql"


def _dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set. Check .env (dotenv is loaded in __main__ / run.py).")
    return url


def connect() -> psycopg.Connection:
    """Open a short-lived connection. Caller closes it (or uses a `with`)."""
    return psycopg.connect(_dsn())


# ---- Trace → flat columns --------------------------------------------------

def _flatten(date: str, domain: str, trace: dict) -> dict:
    steps = trace.get("steps", {}) or {}
    qr = steps.get("qualify") or {}
    poc = steps.get("poc") or {}
    em = steps.get("email_draft") or {}
    send = steps.get("send") or {}

    return {
        "run_date": date,
        "domain": domain,
        "decision": (qr.get("decision") or ("error" if trace.get("error") else "unknown")),
        "priority_score": qr.get("priority_score"),
        "reason": qr.get("reason") or trace.get("error"),
        "poc_name": poc.get("name"),
        "poc_email": poc.get("email"),
        "poc_title": poc.get("title"),
        "poc_tier": poc.get("tier"),
        "poc_confidence": poc.get("confidence"),
        "email_subject": em.get("subject"),
        "email_body": em.get("body"),
        "email_word_count": em.get("word_count"),
        "email_sent": bool(send.get("sent")),
        "resend_id": send.get("message_id"),
        "trace": trace,
    }


_INSERT_SQL = """
INSERT INTO runs (
  run_date, domain, decision, priority_score, reason,
  poc_name, poc_email, poc_title, poc_tier, poc_confidence,
  email_subject, email_body, email_word_count, email_sent, resend_id,
  trace
) VALUES (
  %(run_date)s, %(domain)s, %(decision)s, %(priority_score)s, %(reason)s,
  %(poc_name)s, %(poc_email)s, %(poc_title)s, %(poc_tier)s, %(poc_confidence)s,
  %(email_subject)s, %(email_body)s, %(email_word_count)s, %(email_sent)s, %(resend_id)s,
  %(trace)s
)
ON CONFLICT (run_date, domain) DO UPDATE SET
  decision = EXCLUDED.decision,
  priority_score = EXCLUDED.priority_score,
  reason = EXCLUDED.reason,
  poc_name = EXCLUDED.poc_name,
  poc_email = EXCLUDED.poc_email,
  poc_title = EXCLUDED.poc_title,
  poc_tier = EXCLUDED.poc_tier,
  poc_confidence = EXCLUDED.poc_confidence,
  email_subject = EXCLUDED.email_subject,
  email_body = EXCLUDED.email_body,
  email_word_count = EXCLUDED.email_word_count,
  email_sent = EXCLUDED.email_sent,
  resend_id = EXCLUDED.resend_id,
  trace = EXCLUDED.trace
RETURNING id;
"""


def insert_run(date: str, domain: str, trace: dict,
               conn: Optional[psycopg.Connection] = None) -> Optional[int]:
    """Upsert one (date, domain). Never raise — pipeline must not crash on DB error."""
    row = _flatten(date, domain, trace)
    row["trace"] = Jsonb(row["trace"])
    own = conn is None
    try:
        conn = conn or connect()
        with conn.cursor() as cur:
            cur.execute(_INSERT_SQL, row)
            run_id = cur.fetchone()[0]
        if own:
            conn.commit()
        return run_id
    except Exception as e:
        print(f"  ! db.insert_run({date}, {domain}): {type(e).__name__}: {e}",
              file=sys.stderr)
        return None
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---- Admin commands --------------------------------------------------------

def init_schema() -> None:
    sql = SCHEMA.read_text()
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql)
    print("schema applied.")


def backfill() -> None:
    if not RUNS.exists():
        print("no runs/ directory", file=sys.stderr)
        return
    inserted = 0
    with connect() as conn:
        for date_dir in sorted((p for p in RUNS.iterdir() if p.is_dir()),
                               key=lambda p: p.name):
            for jf in sorted(date_dir.glob("*.json")):
                try:
                    trace = json.loads(jf.read_text())
                except json.JSONDecodeError:
                    continue
                rid = insert_run(date_dir.name, jf.stem, trace, conn=conn)
                if rid is not None:
                    inserted += 1
        conn.commit()
    print(f"backfilled {inserted} rows.")


def stats() -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM runs;")
        total = cur.fetchone()[0]
        cur.execute("""SELECT decision, COUNT(*)
                       FROM runs GROUP BY decision ORDER BY 2 DESC;""")
        by_dec = cur.fetchall()
        cur.execute("""SELECT run_date, COUNT(*)
                       FROM runs GROUP BY run_date ORDER BY run_date DESC;""")
        by_date = cur.fetchall()
    print(f"total: {total}")
    print("by decision:")
    for d, c in by_dec:
        print(f"  {d:<20} {c}")
    print("by date:")
    for d, c in by_date:
        print(f"  {d}  {c}")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "init":
        init_schema()
    elif cmd == "backfill":
        backfill()
    elif cmd == "stats":
        stats()
    else:
        print("usage: python db.py {init|backfill|stats}", file=sys.stderr)
        sys.exit(2)
