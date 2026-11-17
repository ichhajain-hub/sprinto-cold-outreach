"""Generate a static HTML dashboard from runs/<date>/*.json traces.

Output: public/index.html — single self-contained file (CSS inlined, data baked
in at build time). Vercel serves it as a plain static site; no backend.

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


def _email_block(subject: str, body: str, poc: dict) -> str:
    header = (f"Subject: {subject}\n\n"
              f"Intended recipient: {poc.get('name','?')} "
              f"<{poc.get('email','?')}>, {poc.get('title','?')}")
    return (f'<pre class="email">{_esc(header)}\n\n{_esc(body)}</pre>')


def _domain_card(domain: str, trace: dict) -> str:
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

    # Gates
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

    # Signals
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

    # PoC + email
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

    email_html = ""
    if em.get("subject"):
        email_html = _email_block(em.get("subject", ""), em.get("body", ""), poc)

    # Drop reason
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
        f'  {email_html}'
        f'  <details><summary>Gates &amp; signals</summary>'
        f'    <h4>Gates</h4>{gates_html}'
        f'    <h4>Signals</h4>{sig_html}'
        f'  </details>'
        f'</article>'
    )


def _batch_block(date: str) -> str:
    traces = sorted((RUNS / date).glob("*.json"))
    if not traces:
        return ""
    cards = []
    for p in traces:
        try:
            trace = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        cards.append(_domain_card(p.stem, trace))
    return (f'<section class="batch">'
            f'<h2>{_esc(date)} <span class="count">· {len(cards)} domain(s)</span></h2>'
            f'{"".join(cards)}'
            f'</section>')


def main() -> None:
    dates = []
    if RUNS.exists():
        dates = sorted((p.name for p in RUNS.iterdir() if p.is_dir()), reverse=True)
    body = "\n".join(_batch_block(d) for d in dates) or "<p>No runs yet.</p>"

    now = dt.datetime.now().isoformat(timespec="minutes")

    css = """
      *, *::before, *::after { box-sizing: border-box; }
      body { font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
             max-width: 960px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a;
             line-height: 1.55; background: #fcfbf7; }
      header.top h1 { margin: 0 0 0.25rem; font-weight: 600; letter-spacing: -0.02em; font-size: 1.6rem; }
      header.top p { color: #666; margin: 0; }
      section.batch { margin: 2.25rem 0; }
      section.batch h2 { font-size: 1.05rem; font-weight: 500; color: #555;
                         border-bottom: 1px solid #e5e2db; padding-bottom: 0.3rem;
                         margin-bottom: 1rem; }
      .count { color: #999; font-weight: 400; font-size: 0.9rem; }
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
        f'{body}\n'
        f'<footer>Generated {_esc(now)} · static snapshot from <code>runs/</code></footer>\n'
        '</body>\n'
        '</html>\n'
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(doc)
    print(f"Wrote {OUT} ({len(doc):,} chars)")


if __name__ == "__main__":
    main()
