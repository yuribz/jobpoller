"""Render a self-contained static HTML digest (pure function, no I/O).

Job text comes from the open internet, so every dynamic value is HTML-escaped, links are
limited to http(s), nothing is interpolated into script, and a CSP forbids loading anything
external. The only script is a fixed block that reads data-* attributes.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from datetime import datetime
from urllib.parse import urlparse

from .queuefile import format_row
from .store import QueuedJob

_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#16181d;--muted:#5b6370;--line:#dfe3ea;--accent:#2457d6;
--strong:#0f7b3f;--strongbg:#dff3e7;--good:#8a5a00;--goodbg:#fdf0d0;--weak:#5b6370;--weakbg:#eceff3;
--flag:#a4262c}
@media (prefers-color-scheme:dark){:root{--bg:#101216;--card:#1a1d23;--ink:#e8eaee;--muted:#9aa3b2;
--line:#2c313a;--accent:#7aa2ff;--strong:#6fd09a;--strongbg:#173a27;--good:#f0c76a;--goodbg:#3d3010;
--weak:#9aa3b2;--weakbg:#262a31;--flag:#ff8a8f}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.45 system-ui,sans-serif}
main{max-width:860px;margin:0 auto;padding:16px}
h1{font-size:1.3rem;margin:0 0 4px}.sub{color:var(--muted);margin:0 0 12px;font-size:.9rem}
.controls{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin-bottom:16px}
.controls input[type=search]{flex:1 1 200px;padding:8px;border:1px solid var(--line);
border-radius:8px;background:var(--card);color:var(--ink);font:inherit}
.controls label{display:flex;gap:6px;align-items:center;font-size:.9rem}
.card{display:flex;gap:12px;background:var(--card);border:1px solid var(--line);
border-radius:12px;padding:12px;margin-bottom:12px}
[hidden]{display:none!important}
.badge{flex:none;min-width:52px;height:52px;border-radius:10px;display:grid;place-items:center;
font-weight:700;font-size:1.1rem;padding:0 6px;text-align:center}
.badge small{display:block;font-size:.65rem;font-weight:600;opacity:.8}
.strong{background:var(--strongbg);color:var(--strong)}.good{background:var(--goodbg);color:var(--good)}
.weak{background:var(--weakbg);color:var(--weak)}
.body{min-width:0;flex:1}.body h2{font-size:1.05rem;margin:0 0 2px;overflow-wrap:anywhere}
.meta{color:var(--muted);font-size:.85rem;margin:0 0 6px;overflow-wrap:anywhere}
.summary{margin:0 0 6px}.flags{margin:0 0 8px;padding-left:18px;color:var(--flag);font-size:.9rem}
.actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.actions a,.actions button{font:inherit;font-size:.9rem;padding:6px 12px;border-radius:8px;
border:1px solid var(--accent);color:var(--accent);background:transparent;text-decoration:none;cursor:pointer}
.chip{font-size:.75rem;padding:2px 8px;border-radius:99px;background:var(--weakbg);color:var(--muted)}
.empty{color:var(--muted);text-align:center;padding:32px 0}
"""

_JS = """
(function(){
var cards=[].slice.call(document.querySelectorAll('.card'));
var q=document.getElementById('q'),min=document.getElementById('min'),hide=document.getElementById('hide');
var minv=document.getElementById('minv'),count=document.getElementById('count');
function apply(){var t=q.value.trim().toLowerCase(),m=+min.value,h=hide.checked,n=0;minv.textContent=m;
cards.forEach(function(c){var ok=(+c.dataset.fit>=m)&&(!t||c.dataset.text.indexOf(t)>-1)
&&!(h&&c.dataset.status==='dismissed');c.hidden=!ok;if(ok)n++;});count.textContent=n+' shown';}
[q,min,hide].forEach(function(e){e.addEventListener('input',apply);});apply();
function copy(text,b){var label=b.textContent;
function done(){b.textContent='Copied';setTimeout(function(){b.textContent=label;},1500);}
function fallback(){var a=document.createElement('textarea');a.value=text;a.style.position='fixed';
a.style.opacity='0';document.body.appendChild(a);a.select();
try{document.execCommand('copy');done();}catch(e){}a.remove();}
if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(text).then(done,fallback);}
else{fallback();}}
document.addEventListener('click',function(e){var b=e.target.closest('button[data-row]');
if(b){copy(b.dataset.row,b);}});
})();
"""

_CSP = "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'"


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _safe_url(url: str) -> str | None:
    """Only absolute http(s) links are clickable; javascript:, data: etc. are dropped."""
    parts = urlparse(url.strip())
    return url.strip() if parts.scheme in ("http", "https") and parts.netloc else None


def _card(job: QueuedJob) -> str:
    fit = job.sort_key
    tier = "strong" if fit >= 80 else "good" if fit >= 60 else "weak"
    label = "fit" if job.fit is not None else "kw"
    status = f"discovered ({label} {fit})"
    searchable = " ".join(
        [job.title, job.company, job.location, job.source, job.summary, *job.red_flags]
    ).lower()
    link = _safe_url(job.url)
    apply_html = (
        f'<a href="{_esc(link)}" target="_blank" rel="noopener noreferrer">Apply</a>'
        if link
        else ""
    )
    flags = (
        '<ul class="flags">' + "".join(f"<li>{_esc(f)}</li>" for f in job.red_flags) + "</ul>"
        if job.red_flags
        else ""
    )
    summary = f'<p class="summary">{_esc(job.summary)}</p>' if job.summary else ""
    chip = f'<span class="chip">{_esc(job.status)}</span>' if job.status != "new" else ""
    row = format_row(None, job.title, job.company, job.url, status)
    meta = " · ".join(_esc(p) for p in (job.company, job.location or "location n/a", job.source))
    return (
        f'<article class="card" data-fit="{fit}" data-status="{_esc(job.status)}" '
        f'data-text="{_esc(searchable)}">'
        f'<div class="badge {tier}"><span><small>{label}</small>{fit}</span></div>'
        f'<div class="body"><h2>{_esc(job.title)}</h2>'
        f'<p class="meta">{meta} · #{job.id}</p>{summary}{flags}'
        f'<div class="actions">{apply_html}'
        f'<button type="button" data-row="{_esc(row)}">Copy queue row</button>{chip}</div>'
        "</div></article>"
    )


def render_digest(jobs: Sequence[QueuedJob], now: datetime) -> str:
    """Cards sorted best first. Dismissed jobs are included but hidden by default via a toggle."""
    ordered = sorted(jobs, key=lambda j: (-j.sort_key, j.id))
    cards = "\n".join(_card(j) for j in ordered) or '<p class="empty">Nothing queued.</p>'
    stamp = now.strftime("%Y-%m-%d %H:%M")
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta http-equiv="Content-Security-Policy" content="{_CSP}">'
        '<meta name="color-scheme" content="light dark"><title>jobpoller digest</title>'
        f"<style>{_CSS}</style></head><body><main>"
        f'<h1>jobpoller digest</h1><p class="sub">Generated {_esc(stamp)} · '
        f'{len(ordered)} jobs · <span id="count"></span></p>'
        '<div class="controls"><input id="q" type="search" placeholder="Filter text" '
        'aria-label="Filter text">'
        '<label>Min fit <input id="min" type="range" min="0" max="100" value="0"> '
        '<span id="minv">0</span></label>'
        '<label><input id="hide" type="checkbox" checked> Hide dismissed</label></div>'
        f"{cards}</main><script>{_JS}</script></body></html>\n"
    )
