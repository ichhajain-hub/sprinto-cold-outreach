# Components Map — Sprinto Cold Outreach

The MESA Session 1 deck names five parts of any app: **Frontend, Backend,
Database, API, Version Control** (deck p.18). This is where each part lives
for this build.

```
                   ┌────────────────────────────────────────────────────────┐
                   │              1. FRONTEND  (two faces)                  │
                   │                                                        │
                   │   A. CLI (yours)              B. Dashboard (shareable) │
                   │   python run.py               sprinto-cold-outreach    │
                   │       domains.txt              .vercel.app             │
                   │   What you run                What mentors + peers see │
                   └───────────────────────┬────────────────────────────────┘
                                           │
                   ┌───────────────────────▼────────────────────────────────┐
                   │              2. BACKEND  (Python, 8 modules)           │
                   │                                                        │
                   │   scrape → extract → qualify → poc → pain → draft → send
                   │                                                        │
                   │   Orchestrator: run.py                                 │
                   │   Report:       report.py                              │
                   │   Dashboard:    build_site.py  (reads runs/, writes HTML)
                   └──────┬────────────────────────────────────────┬────────┘
                          │                                        │
    ┌─────────────────────▼──────────────┐       ┌─────────────────▼─────────────┐
    │          4. APIs (6 external)       │       │   3. DATABASE  (filesystem)   │
    │                                     │       │                               │
    │   Google Gemini    (4 LLM calls)    │       │   cache/pages/    HTML cache  │
    │   Hunter.io        (PoC lookup)     │       │   cache/news/     RSS XML     │
    │   Google News RSS  (funding search) │       │   cache/hunter/   per-domain  │
    │   Target websites  (scraper +       │       │   runs/<date>/    traces      │
    │                     agent fetch)    │       │   public/index.html           │
    │   Resend           (send email)     │       │                               │
    │   (GitHub + Vercel at deploy time)  │       │   runs/ is committed — review │
    │                                     │       │   defensibility on GitHub.    │
    │   6 waiters. 6 kitchens. (deck p.22)│       │   cache/ stays local.         │
    └─────────────────────────────────────┘       └───────────────────────────────┘

                   ┌────────────────────────────────────────────────────────┐
                   │        5. VERSION CONTROL  &  DEPLOY                    │
                   │                                                         │
                   │   GitHub:  ichhajain-hub/sprinto-cold-outreach (public) │
                   │   Vercel:  static site, auto-redeploys on push to main  │
                   │   .gitignore protects:  .env   cache/   claude/   *.pdf │
                   └────────────────────────────────────────────────────────┘
```

## What fills each layer

| Layer | Concretely | Files / services |
|---|---|---|
| **1. Frontend** | A CLI *and* a static dashboard. The CLI is for the operator; the dashboard is for anyone with the link. | `run.py` (CLI), `public/index.html` (dashboard rendered from `runs/`) |
| **2. Backend** | Eight Python modules, each a single step. `run.py` orchestrates them in order and catches per-domain exceptions. | `scrape.py`, `extract.py`, `qualify.py`, `poc.py`, `pain_points.py`, `email_draft.py`, `send.py` + `run.py`, `report.py`, `build_site.py` |
| **3. Database** | The filesystem. Not Supabase, not Postgres — flat JSON / HTML / XML files, keyed deliberately so every write is idempotent and inspectable with `cat`. `runs/` is in git now; `cache/` stays local. | `cache/pages/<sha1>.html`, `cache/news/<sha1>.xml`, `cache/hunter/<domain>.json`, `runs/<date>/<domain>.json`, `runs/<date>/summary.csv`, `runs/<date>/report.txt` |
| **4. APIs** | Six external services the backend talks to during a run, plus two more at deploy time. The backend never scrapes human-side data on its own — it asks a waiter. | Google Gemini · Hunter.io · Google News RSS · target company HTTP · Resend · GitHub CLI · Vercel CLI |
| **5. Version Control & Deploy** | A public GitHub repo, a Vercel project linked to it, and a `.gitignore` that keeps secrets and cache out while letting traces in. | `.gitignore`, `.env.example`, `vercel.json`, the repo itself |

## Data flow in one sentence (for the viva)

> `run.py` reads `domains.txt`; for each domain the backend calls four APIs in
> sequence (the company's own site, Gemini, Google News, Hunter.io, Resend),
> writes every intermediate decision to `runs/<date>/<domain>.json`, and the
> dashboard at `sprinto-cold-outreach.vercel.app` is a static snapshot of
> that folder rebuilt by `build_site.py` on each push.

## Secrets map (where keys live)

```
.env  (never committed — in .gitignore)
  ├── GEMINI_API_KEY         → Google Gemini
  ├── GEMINI_MODEL           → gemini-3.8-flash (not a secret, lives here for convenience)
  ├── HUNTER_API_KEY         → Hunter.io (free tier: 25 searches/month)
  ├── RESEND_API_KEY         → Resend
  ├── FROM_EMAIL             → sender ("" = onboarding@resend.dev)
  ├── SENDER_NAME            → sign-off used in body + From display
  └── TO_EMAIL               → safety override: every email is routed here
```

## Deploy flow

```
  change code locally
        │
        ▼
  git commit + git push                                   (your machine)
        │
        ▼
  GitHub stores main                                      (github.com)
        │
        ▼
  Vercel detects push, builds static site                 (vercel.com)
        │
        ▼
  https://sprinto-cold-outreach.vercel.app is updated     (~30s later)
```

No build step, no bundler, no backend on Vercel — the CLI runs on your
machine, writes JSON to `runs/`, `build_site.py` turns those JSON files into
one static HTML page, and Vercel serves it.
