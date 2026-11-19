"""Generate a static HTML dashboard from runs/<date>/*.json traces.

Output: public/index.html — single self-contained file (CSS + JS inline, data
baked in at build time). Vercel serves it as a plain static site; no backend.

Layout:
  1. Funnel summary — four-stage conversion across all runs (or filtered date)
  2. Date filter — all-time / per-batch
  3. Emails sent — just the deliverables
  4. Per-batch cards — full gate + signal detail, unchanged from v1

Run this after a new batch:
    python3 run.py domains.txt
    python3 build_site.py
    git add public/index.html && git commit -m "refresh dashboard" && git push
"""
from __future__ import annotations

import datetime as dt
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"
OUT = ROOT / "public" / "index.html"


def _esc(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


# ---- Per-trace reducer -----------------------------------------------------

def _classify(trace: dict) -> dict:
    """Reduce a trace to the flags the funnel + emails section need."""
    steps = trace.get("steps", {})
    qr = steps.get("qualify") or {}
    poc = steps.get("poc") or {}
    em = steps.get("email_draft") or {}
    send = steps.get("send") or {}

    decision = qr.get("decision", "error")
    qualified = decision in ("qualify", "qualify_and_flag")
    poc_found = bool(poc.get("email") and poc.get("first_name"))
    email_drafted = bool(em.get("subject"))
    sent = bool(send.get("sent"))

    return {
        "qualified": qualified,
        "poc_found": poc_found and qualified,
        "email_drafted": email_drafted and qualified and poc_found,
        "sent": sent,
    }


# ---- Email card ------------------------------------------------------------

def _email_card(date: str, domain: str, trace: dict) -> str:
    steps = trace.get("steps", {})
    em = steps.get("email_draft") or {}
    send = steps.get("send") or {}
    poc = steps.get("poc") or {}
    if not em.get("subject"):
        return ""
    sent = send.get("sent")
    status_label = "sent" if sent else ("dry" if send.get("dry_run") else "not sent")
    status_class = "status-sent" if sent else "status-other"
    recipient = (f"Intended recipient: {poc.get('name','?')} "
                 f"<{poc.get('email','?')}>, {poc.get('title','?')}")
    return (
        f'<article class="email-card" data-date="{_esc(date)}">'
        f'  <header>'
        f'    <div><strong>{_esc(domain)}</strong>'
        f'      <span class="muted"> · {_esc(date)} · {em.get("word_count","?")} w</span></div>'
        f'    <span class="status {status_class}">{status_label}</span>'
        f'  </header>'
        f'  <p class="meta">Subject: <strong>{_esc(em.get("subject"))}</strong></p>'
        f'  <pre class="email">{_esc(recipient)}\n\n{_esc(em.get("body",""))}</pre>'
        f'</article>'
    )


# ---- Domain card (per-batch detail, unchanged shape) -----------------------

def _domain_card(date: str, domain: str, trace: dict) -> str:
    steps = trace.get("steps", {})
    qr = steps.get("qualify") or {}
    poc = steps.get("poc") or {}
    em = steps.get("email_draft") or {}
    send = steps.get("send") or {}

    if trace.get("error"):
        klass, label = "status-error", "crashed"
    elif qr.get("decision") == "drop":
        klass, label = "status-drop", "dropped"
    elif poc and not poc.get("email"):
        klass, label = "status-no-poc", "no PoC"
    elif send.get("sent"):
        klass, label = "status-sent", "sent"
    elif send.get("dry_run"):
        klass, label = "status-other", "drafted (dry)"
    else:
        klass, label = "status-other", qr.get("decision", "?")

    score = qr.get("priority_score")
    score_tag = (f'<span class="score">{score}/100</span>'
                 if qr.get("decision") != "drop" and score is not None else "")

    gates_items = []
    for g in qr.get("gates", []):
        v = g.get("verdict", "?")
        sym = {"pass": "✓", "fail": "✗", "unknown": "·"}.get(v, "?")
        src = g.get("source_url")
        src_link = (f' <a href="{_esc(src)}" target="_blank" rel="noopener">source</a>'
                    if src and src.startswith("http") else "")
        gates_items.append(
            f'<li><code>{_esc(g.get("gate_id"))}</code> '
            f'<span class="v-{v}">{sym} {v}</span> — '
            f'{_esc(g.get("evidence"))}{src_link}</li>'
        )
    gates_html = "<ul>" + "".join(gates_items) + "</ul>"

    sig_items = []
    for s in qr.get("signals", []):
        v = s.get("verdict", "?")
        sym = {"pass": "✓", "unknown": "·"}.get(v, "?")
        sig_items.append(
            f'<li><code>{_esc(s.get("signal_id"))}</code> '
            f'<span class="v-{v}">{sym} {v}</span>, '
            f'+{s.get("score_awarded", 0)} pts — {_esc(s.get("evidence"))}</li>'
        )
    sig_html = "<ul>" + "".join(sig_items) + "</ul>"

    poc_html = ""
    if poc.get("email"):
        poc_html = (
            f'<p class="meta"><strong>PoC:</strong> '
            f'{_esc(poc.get("name"))} &lt;{_esc(poc.get("email"))}&gt; · '
            f'{_esc(poc.get("title"))} · tier {_esc(poc.get("tier"))} · '
            f'{_esc(poc.get("confidence"))}%</p>'
        )
    elif poc.get("reason"):
        poc_html = f'<p class="meta"><strong>PoC:</strong> none — {_esc(poc.get("reason"))}</p>'

    drop_html = ""
    if qr.get("decision") == "drop":
        for g in qr.get("gates", []):
            if g.get("verdict") in ("fail", "unknown"):
                drop_html = (f'<p class="reason"><strong>Reason:</strong> '
                             f'<code>{_esc(g.get("gate_id"))}</code> '
                             f'{_esc(g.get("verdict"))} — {_esc(g.get("evidence"))}</p>')
                break

    return (
        f'<article class="domain">'
        f'  <header>'
        f'    <h3>{_esc(domain)}</h3>'
        f'    <div><span class="status {klass}">{label}</span>{score_tag}</div>'
        f'  </header>'
        f'  {drop_html}'
        f'  {poc_html}'
        f'  <details><summary>Gates &amp; signals</summary>'
        f'    <h4>Gates</h4>{gates_html}'
        f'    <h4>Signals</h4>{sig_html}'
        f'  </details>'
        f'</article>'
    )


# ---- Build pieces ----------------------------------------------------------

def _collect_traces() -> list[tuple[str, str, dict]]:
    """Return [(date, domain, trace)] sorted newest-date-first then alpha."""
    out = []
    if not RUNS.exists():
        return out
    for date_dir in sorted((p for p in RUNS.iterdir() if p.is_dir()),
                           key=lambda p: p.name, reverse=True):
        for jf in sorted(date_dir.glob("*.json")):
            try:
                trace = json.loads(jf.read_text())
            except json.JSONDecodeError:
                continue
            out.append((date_dir.name, jf.stem, trace))
    return out


def _funnel_section() -> str:
    """Static placeholder markup; JS recomputes numbers from embedded data."""
    stages = [
        ("attempted", "Attempted"),
        ("qualified", "Qualified"),
        ("poc", "PoC found"),
        ("sent", "Emails sent"),
    ]
    rows = []
    for key, label in stages:
        rows.append(
            f'<div class="funnel-row" data-stage="{key}">'
            f'  <div class="funnel-label">{label}</div>'
            f'  <div class="funnel-bar"><div class="funnel-fill" style="width:0%"></div></div>'
            f'  <div class="funnel-count"><span class="count">0</span>'
            f'    <span class="pct muted">0%</span></div>'
            f'</div>'
        )
    return (
        '<section class="funnel">'
        '  <h2>Funnel</h2>'
        f'  {"".join(rows)}'
        '</section>'
    )


def _filter_bar(dates: list[str]) -> str:
    opts = ['<option value="all">All dates</option>']
    for d in dates:
        opts.append(f'<option value="{_esc(d)}">{_esc(d)}</option>')
    return (
        '<nav class="filter">'
        '  <label for="date-filter">Date:</label>'
        '  <select id="date-filter">'
        f'   {"".join(opts)}'
        '  </select>'
        '</nav>'
    )


def _emails_section(traces: list[tuple[str, str, dict]]) -> str:
    cards = [_email_card(d, dom, t) for d, dom, t in traces]
    cards = [c for c in cards if c]
    if not cards:
        return ('<section class="emails"><h2>Emails</h2>'
                '<p class="muted">No emails drafted yet.</p></section>')
    return (
        '<section class="emails">'
        '  <h2>Emails <span class="muted">· most recent first</span></h2>'
        f' {"".join(cards)}'
        '</section>'
    )


def _batch_section(date: str, batch_traces: list[tuple[str, dict]]) -> str:
    cards = [_domain_card(date, dom, t) for dom, t in batch_traces]
    return (
        f'<section class="batch" data-date="{_esc(date)}">'
        f'  <h2>{_esc(date)} <span class="muted">· {len(batch_traces)} domain(s)</span></h2>'
        f'  {"".join(cards)}'
        f'</section>'
    )


# ---- Main ------------------------------------------------------------------

def main() -> None:
    traces = _collect_traces()
    dates = sorted({d for d, _, _ in traces}, reverse=True)

    # Per-trace data for JS funnel recomputation
    js_data = [
        {"date": d, "domain": dom, **_classify(t)}
        for d, dom, t in traces
    ]

    # Group traces by date for per-batch sections
    by_date: dict[str, list[tuple[str, dict]]] = {}
    for d, dom, t in traces:
        by_date.setdefault(d, []).append((dom, t))

    batch_sections = "\n".join(_batch_section(d, by_date[d]) for d in dates)

    css = """
      *, *::before, *::after { box-sizing: border-box; }
      body { font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
             max-width: 960px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a;
             line-height: 1.55; background: #fcfbf7; }
      header.top h1 { margin: 0 0 0.25rem; font-weight: 600; letter-spacing: -0.02em; font-size: 1.6rem; }
      header.top p { color: #666; margin: 0; }

      /* Filter bar */
      nav.filter { display: flex; align-items: center; gap: 0.5rem;
                   margin: 1.5rem 0 1rem; padding: 0.5rem 0.9rem;
                   background: #fff; border: 1px solid #ebe8e0; border-radius: 8px; }
      nav.filter label { font-size: 0.85rem; color: #555; font-weight: 500; }
      nav.filter select { font: inherit; font-size: 0.9rem; border: 1px solid #d9d4c6;
                          background: #fff; padding: 0.25rem 0.5rem; border-radius: 4px;
                          color: #1a1a1a; }

      /* Funnel */
      section.funnel { background: #fff; border: 1px solid #ebe8e0; border-radius: 10px;
                       padding: 1rem 1.25rem; margin: 1rem 0; }
      section.funnel h2 { font-size: 1rem; font-weight: 500; color: #555;
                          margin: 0 0 0.75rem; }
      .funnel-row { display: grid; grid-template-columns: 110px 1fr 130px;
                    align-items: center; gap: 0.75rem; margin: 0.5rem 0; }
      .funnel-label { font-size: 0.9rem; color: #333; font-weight: 500; }
      .funnel-bar { background: #f3efe3; height: 24px; border-radius: 4px; overflow: hidden; }
      .funnel-fill { background: linear-gradient(90deg, #0a8f5c, #0ca672); height: 100%;
                     transition: width 0.35s ease; }
      .funnel-count { font-size: 0.9rem; color: #333; display: flex; align-items: baseline;
                      gap: 0.5rem; font-variant-numeric: tabular-nums; }
      .funnel-count .count { font-weight: 600; font-size: 1rem; }

      /* Shared section headings */
      section.batch h2, section.emails h2 { font-size: 1.05rem; font-weight: 500;
          color: #555; border-bottom: 1px solid #e5e2db; padding-bottom: 0.3rem;
          margin: 2rem 0 1rem; }
      .muted { color: #999; font-weight: 400; font-size: 0.9rem; }

      /* Emails */
      article.email-card { padding: 1rem 1.1rem; margin: 0.75rem 0; background: #fff;
                           border: 1px solid #ebe8e0; border-radius: 10px; }
      article.email-card header { display: flex; justify-content: space-between;
                                  align-items: baseline; gap: 1rem; }

      /* Domain cards */
      article.domain { padding: 1rem 1.1rem; margin: 0.75rem 0; background: #fff;
                       border: 1px solid #ece8de; border-radius: 10px;
                       box-shadow: 0 1px 0 rgba(0,0,0,0.02); }
      article.domain header { display: flex; justify-content: space-between;
                              align-items: baseline; gap: 1rem; }
      article.domain h3 { margin: 0; font-size: 1.05rem; font-weight: 600; }

      .status { padding: 0.15rem 0.55rem; border-radius: 4px; font-size: 0.8rem;
                font-weight: 500; letter-spacing: 0.01em; }
      .status-sent { background: #e3f3e0; color: #086b00; }
      .status-drop { background: #f6e4e4; color: #a30000; }
      .status-no-poc { background: #faf0d2; color: #7a5400; }
      .status-error { background: #333; color: #fff; }
      .status-other { background: #eee; color: #555; }
      .score { color: #777; font-size: 0.85rem; margin-left: 0.5rem;
               font-variant-numeric: tabular-nums; }
      .meta { color: #333; font-size: 0.9rem; margin: 0.5rem 0; }
      .reason { color: #a30000; font-size: 0.9rem; margin: 0.5rem 0; }
      details { margin-top: 0.75rem; }
      details summary { cursor: pointer; color: #666; font-size: 0.85rem; }
      details summary:hover { color: #000; }
      details h4 { margin: 0.9rem 0 0.25rem; font-size: 0.85rem; color: #555;
                   font-weight: 600; }
      details ul { margin: 0; padding-left: 1.25rem; }
      details li { font-size: 0.85rem; margin: 0.3rem 0; color: #333; }
      code { font-family: ui-monospace, Menlo, monospace; font-size: 0.85em;
             background: #f1ede2; padding: 1px 5px; border-radius: 3px; }
      .v-pass { color: #086b00; font-weight: 600; }
      .v-fail { color: #a30000; font-weight: 600; }
      .v-unknown { color: #7a5400; font-weight: 600; }
      pre.email { background: #faf8f1; border: 1px solid #ebe8e0; padding: 0.9rem;
                  border-radius: 6px; overflow-x: auto;
                  font-family: ui-monospace, Menlo, monospace; font-size: 0.82rem;
                  white-space: pre-wrap; margin: 0.6rem 0; color: #222; }
      a { color: #0a6; text-decoration: none; }
      a:hover { text-decoration: underline; }
      footer { color: #888; font-size: 0.75rem; margin: 3rem 0 1rem;
               text-align: center; }
    """

    js = """
      const DATA = __DATA__;

      function recomputeFunnel(dateFilter) {
        const rows = dateFilter === 'all' ? DATA : DATA.filter(d => d.date === dateFilter);
        const total = rows.length;
        const qualified = rows.filter(d => d.qualified).length;
        const pocFound  = rows.filter(d => d.poc_found).length;
        const sent      = rows.filter(d => d.sent).length;

        const stages = {
          attempted: total,
          qualified: qualified,
          poc: pocFound,
          sent: sent,
        };
        const denom = total || 1;
        Object.entries(stages).forEach(([key, count]) => {
          const row = document.querySelector('.funnel-row[data-stage="' + key + '"]');
          if (!row) return;
          const pct = Math.round((count / denom) * 100);
          row.querySelector('.count').textContent = count;
          row.querySelector('.pct').textContent = total ? pct + '%' : '—';
          row.querySelector('.funnel-fill').style.width = (total ? pct : 0) + '%';
        });
      }

      function applyFilter(val) {
        document.querySelectorAll('section.batch').forEach(el => {
          el.style.display = (val === 'all' || el.dataset.date === val) ? '' : 'none';
        });
        document.querySelectorAll('article.email-card').forEach(el => {
          el.style.display = (val === 'all' || el.dataset.date === val) ? '' : 'none';
        });
        recomputeFunnel(val);
      }

      document.addEventListener('DOMContentLoaded', () => {
        const sel = document.getElementById('date-filter');
        sel.addEventListener('change', e => applyFilter(e.target.value));
        applyFilter('all');
      });
    """
    js = js.replace("__DATA__", json.dumps(js_data))

    now = dt.datetime.now().isoformat(timespec="minutes")

    doc = (
        '<!doctype html>\n'
        '<html lang="en">\n'
        '<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<title>Sprinto Cold Outreach · Pipeline Dashboard</title>\n'
        f'<style>{css}</style>\n'
        '</head>\n'
        '<body>\n'
        '<header class="top">\n'
        '  <h1>Sprinto Cold Outreach</h1>\n'
        '  <p>Per-domain defensibility: what qualified, what dropped, which PoC, '
        'which email, source for every claim.</p>\n'
        '</header>\n'
        f'{_filter_bar(dates)}\n'
        f'{_funnel_section()}\n'
        f'{_emails_section(traces)}\n'
        f'{batch_sections}\n'
        f'<footer>Generated {_esc(now)} · static snapshot from <code>runs/</code></footer>\n'
        f'<script>{js}</script>\n'
        '</body>\n'
        '</html>\n'
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(doc)
    print(f"Wrote {OUT} ({len(doc):,} chars) · {len(traces)} traces across {len(dates)} date(s)")


if __name__ == "__main__":
    main()
