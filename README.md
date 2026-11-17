# Sprinto Cold Outreach Pipeline

End-to-end Python pipeline that takes a list of company domains and — with
no human decisions in between — qualifies each against Sprinto's ICP,
finds the right decision-maker, drafts a personalised cold email, and
sends it via Resend.

Built for the MESA AI-Native case "Cold Outreach at Scale."

---

## What it does

```
domains.txt
    │
    ▼
┌─────────────┐   ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
│ 1. Scrape   │──▶│ 2. Extract  │──▶│ 3. Qualify  │──▶│ 4. Find PoC │
│  (requests) │   │  (Gemini)   │   │ (Gemini     │   │ (Hunter.io) │
│             │   │             │   │  agent loop)│   │             │
└─────────────┘   └─────────────┘   └─────────────┘   └─────────────┘
                                                             │
                                                             ▼
                        ┌─────────────┐   ┌─────────────┐   ┌─────────────┐
                        │ 7. Send     │◀──│ 6. Draft    │◀──│ 5. Pain     │
                        │  (Resend)   │   │  (Gemini)   │   │  (Gemini)   │
                        └─────────────┘   └─────────────┘   └─────────────┘
```

Every decision is written to `runs/<date>/<domain>.json` with gate verdicts,
source URLs, chosen PoC, pain points, final email, and send status — so any
rejection can be defended after the fact.

---

## Setup

### Requirements

- Python 3.11 (3.9+ works with `from __future__ import annotations`)
- Accounts: Google AI Studio (Gemini), Hunter.io, Resend

### Install

```bash
python3 -m pip install -r requirements.txt
```

### Configure

Copy `.env.example` to `.env` and fill in:

```
GEMINI_API_KEY=...          # aistudio.google.com/apikey
GEMINI_MODEL=gemini-3.8-flash

HUNTER_API_KEY=...          # hunter.io/api-keys (free: 25 searches/month)

RESEND_API_KEY=re_...       # resend.com — sign up with the email you want to receive at
FROM_EMAIL=                 # leave blank to use onboarding@resend.dev

SENDER_NAME=Your Name       # sign-off used in the email body + From display
TO_EMAIL=you@wherever.com   # every email is routed here, never the real prospect
```

**Resend note.** With `FROM_EMAIL` blank (recommended), Resend uses its shared
`onboarding@resend.dev` sender. On that sender you can **only** send to the
email you registered your Resend account with. Make sure that matches
`TO_EMAIL`.

---

## Run

```bash
python3 run.py domains.txt              # live run (sends email via Resend)
python3 run.py domains.txt --dry-run    # everything except the final send
```

Per-domain trace and a per-batch CSV + report land in `runs/<date>/`:

```
runs/2026-11-17/
  cal.com.json         ← full trace (all 7 steps, every source URL, every verdict)
  clay.com.json
  ...
  summary.csv          ← one row per domain
  report.txt           ← human-readable brief (same as the one printed at run-end)
```

---

## Tuning the ICP

All the shortlisting rules live in a single file: [`evidence_map.yaml`](evidence_map.yaml).

No code change needed to edit the ICP — tweak the YAML, rerun. Common
knobs:

- **G2 agent budget** — `attempt_limit: 3` → raise for more thorough news search
- **PoC confidence** — `hunter_min_confidence: 70` → lower for more coverage
- **Tier-3 ceiling** — `tier_3_headcount_ceiling: 80` → when CEO/founder is allowed
- **Industry weights** — S3's `tiering` block

---

## Dashboard

The `public/index.html` is a static snapshot of all runs under `runs/`,
rebuilt by:

```bash
python3 build_site.py
```

Vercel serves `public/` as a plain static site — no backend.

---

## Files

```
run.py                  orchestrator (entry point)
scrape.py               Step 1 — fetch + clean company pages
extract.py              Step 2 — Gemini → structured JSON
qualify.py              Step 3 — gates (pre-check + agent loop)
poc.py                  Step 4 — Hunter.io + title priority + override fallback
pain_points.py          Step 5 — Gemini → 2 pains with cited evidence
email_draft.py          Step 6 — Gemini → subject + body (word-count enforced)
send.py                 Step 7 — Resend + "Intended recipient" prefix
report.py               brief post-run report
build_site.py           renders public/index.html from runs/

evidence_map.yaml       the ICP / shortlisting rules (editable)
poc_overrides.json      manual Apollo fallback for domains Hunter can't cover
prompts/*.txt           one prompt per LLM step
```

---

## Cost

Per qualified domain on Gemini Flash: ~$0.01 (dominated by G2's agent
loop, which grows with conversation length). Hunter + Resend free tiers
cover this scale at $0.
