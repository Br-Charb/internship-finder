"""Build docs/index.html (your job page) and the table at the top of README.md."""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Summer 2027 aerospace internships</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🚀</text></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Barlow+Semi+Condensed:wght@500;600;700&family=Barlow:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#E6EBEF; --panel:#FFFFFF; --ink:#16212B; --muted:#56636F; --line:#C7D0D8;
  --strong:#1D7A4E; --good:#2B5C9C; --look:#94690F; --warn:#A3442D;
  --chip:#EEF2F5; --focus:#2B5C9C;
  --cond:"Barlow Semi Condensed","Arial Narrow",Arial,sans-serif;
  --body:Barlow,"Segoe UI",Roboto,Arial,sans-serif;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0F171E; --panel:#18232C; --ink:#E3E9EE; --muted:#93A1AD; --line:#2B3843;
  --strong:#4FBF88; --good:#6F9EE0; --look:#D9A93F; --warn:#E07A5F; --chip:#212E38; --focus:#6F9EE0;
}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 var(--body)}
a{color:inherit}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.wrap{max-width:1040px;margin:0 auto;padding:28px 18px 64px}
header h1{font:700 clamp(28px,5vw,42px)/1.05 var(--cond);letter-spacing:-.01em;margin:0 0 8px}
header p{margin:0;color:var(--muted)}
.counts{display:flex;flex-wrap:wrap;gap:6px 18px;margin-top:14px;font:600 15px/1.3 var(--cond)}
.counts b{font-size:20px;margin-right:4px}
.controls{position:sticky;top:0;z-index:5;background:var(--bg);padding:14px 0 12px;margin-top:18px;border-bottom:1px solid var(--line);display:grid;gap:10px}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
input[type=search]{flex:1 1 240px;min-width:0;font:inherit;padding:9px 12px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink)}
select{font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--ink)}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:6px;overflow:hidden;background:var(--panel)}
.seg button{font:600 14px/1 var(--cond);padding:9px 12px;border:0;background:transparent;color:var(--muted);cursor:pointer}
.seg button[aria-pressed=true]{background:var(--ink);color:var(--bg)}
label.tog{display:inline-flex;gap:6px;align-items:center;font-size:14px;color:var(--muted);cursor:pointer}
.gapbox{margin:18px 0 6px;background:var(--panel);border:1px solid var(--line);border-radius:8px}
.gapbox summary{cursor:pointer;padding:12px 16px;font:600 17px/1.3 var(--cond)}
.gapbox .inner{padding:0 16px 16px;display:grid;gap:18px;grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}
.gapbox h3{font:600 15px/1.3 var(--cond);margin:0 0 8px;color:var(--muted)}
.bar{display:grid;grid-template-columns:150px 1fr 40px;gap:8px;align-items:center;font-size:14px;margin:4px 0}
.bar i{display:block;height:8px;border-radius:4px;background:var(--line)}
.bar i span{display:block;height:100%;border-radius:4px}
.bar em{font-style:normal;color:var(--muted);text-align:right}
#list{display:grid;gap:10px;margin-top:16px}
.strip{display:grid;grid-template-columns:76px 1fr auto;background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:hidden}
.strip.done{opacity:.55}
.score{display:flex;flex-direction:column;align-items:center;justify-content:center;color:#fff;padding:10px 4px;font-family:var(--cond)}
.score b{font-size:30px;line-height:1}
.score small{font-size:12px;font-weight:600;opacity:.92;margin-top:2px;text-align:center}
.t-strong .score{background:var(--strong)} .t-good .score{background:var(--good)} .t-look .score{background:var(--look)}
@media (prefers-color-scheme:dark){.score{color:#0F171E}}
.main{padding:12px 16px;min-width:0}
.co{font:600 14px/1.3 var(--cond);color:var(--muted)}
.title{font:600 20px/1.2 var(--cond);margin:2px 0 4px}
.meta{font-size:14px;color:var(--muted);display:flex;flex-wrap:wrap;gap:2px 14px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.chip{font-size:13px;padding:2px 8px;border-radius:4px;background:var(--chip);border:1px solid transparent}
.chip.good{border-color:var(--strong);color:var(--strong)}
.chip.warn{border-color:var(--warn);color:var(--warn)}
.chip.new{background:var(--ink);color:var(--bg);font-weight:600}
details.why{margin-top:8px}
details.why summary{cursor:pointer;font-size:14px;font-weight:600;color:var(--good)}
.why dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 12px;margin:8px 0 0;font-size:14px}
.why dt{color:var(--muted)} .why dd{margin:0}
.ai{margin-top:8px;padding:8px 10px;border-left:3px solid var(--good);background:var(--chip);font-size:14px}
.acts{display:flex;flex-direction:column;gap:8px;padding:12px 14px 12px 0;justify-content:center}
.btn{font:600 15px/1 var(--cond);text-decoration:none;text-align:center;padding:10px 16px;border-radius:6px;border:1px solid var(--ink);background:var(--ink);color:var(--bg);white-space:nowrap;cursor:pointer}
.btn.ghost{background:transparent;color:var(--ink);border-color:var(--line);font-weight:500}
.empty{padding:40px 20px;text-align:center;background:var(--panel);border:1px dashed var(--line);border-radius:8px;color:var(--muted)}
footer{margin-top:36px;font-size:14px;color:var(--muted)}
footer details summary{cursor:pointer;font-weight:600}
footer table{border-collapse:collapse;margin-top:8px;width:100%;font-size:13px}
footer td,footer th{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line);vertical-align:top}
.tbl{overflow-x:auto}
@media (max-width:640px){
  .strip{grid-template-columns:58px 1fr}
  .score b{font-size:24px}
  .acts{grid-column:1/-1;flex-direction:row;padding:0 12px 12px}
  .acts .btn{flex:1}
  .bar{grid-template-columns:120px 1fr 36px}
}
@media (prefers-reduced-motion:no-preference){.strip{transition:opacity .2s}}
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>Summer 2027 aerospace internships</h1>
  <p id="sub"></p>
  <div class="counts" id="counts"></div>
</header>

<details class="gapbox" id="gapbox">
  <summary>What these postings ask for</summary>
  <div class="inner">
    <div><h3>Skills you have that come up most</h3><div id="have"></div></div>
    <div><h3>Skills not on your resume yet</h3><div id="need"></div></div>
  </div>
</details>

<div class="controls">
  <div class="row">
    <input type="search" id="q" placeholder="Search company, title, location, skill" aria-label="Search">
    <select id="sort" aria-label="Sort">
      <option value="score">Best fit first</option>
      <option value="new">Newest first</option>
      <option value="company">Company A to Z</option>
    </select>
  </div>
  <div class="row">
    <div class="seg" role="group" aria-label="Fit">
      <button data-tier="all" aria-pressed="true">All</button>
      <button data-tier="strong" aria-pressed="false">Strong fit</button>
      <button data-tier="good" aria-pressed="false">Good fit</button>
      <button data-tier="look" aria-pressed="false">Worth a look</button>
    </div>
    <label class="tog"><input type="checkbox" id="onlyNew"> New this week</label>
    <label class="tog"><input type="checkbox" id="onlySummer"> Says Summer 2027</label>
    <label class="tog"><input type="checkbox" id="hideDone" checked> Hide applied and dismissed</label>
  </div>
</div>

<div id="list"></div>

<footer>
  <p id="foot"></p>
  <details>
    <summary>Sources checked on this run</summary>
    <div class="tbl"><table id="src"></table></div>
  </details>
</footer>
</div>

<script id="data" type="application/json">__DATA__</script>
<script>
(function(){
const D = JSON.parse(document.getElementById('data').textContent);
const store = {
  get(){ try { return JSON.parse(localStorage.getItem('ajf-status') || '{}'); } catch(e){ return {}; } },
  set(v){ try { localStorage.setItem('ajf-status', JSON.stringify(v)); } catch(e){} }
};
let status = store.get();
let tier = 'all';
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = iso => { if(!iso) return ''; const d = new Date(iso + 'T12:00:00'); return d.toLocaleDateString(undefined,{month:'short',day:'numeric'}); };
const TIER = {strong:'Strong fit', good:'Good fit', look:'Worth a look'};
const weekAgo = new Date(Date.now() - 7*864e5).toISOString().slice(0,10);

$('sub').textContent = D.sources.length ? `Scored against ${D.name}'s resume. Updated ${D.updated_label}.` : 'Waiting for the first scan.';
const c = D.counts;
$('counts').innerHTML = `<span><b>${c.total}</b>matches</span><span><b>${c.strong}</b>strong fits</span><span><b>${c.new_today}</b>new today</span>`;

function bars(list, color){
  if(!list.length) return '<p style="color:var(--muted);font-size:14px;margin:0">Not enough descriptions yet.</p>';
  return list.map(g => `<div class="bar"><span>${esc(g.name)}</span><i><span style="width:${g.pct}%;background:${color}"></span></i><em>${g.pct}%</em></div>`).join('');
}
$('have').innerHTML = bars(D.have, 'var(--strong)');
$('need').innerHTML = bars(D.need, 'var(--warn)');

$('src').innerHTML = '<tr><th>Source</th><th>Type</th><th>Result</th></tr>' + D.sources.map(s =>
  `<tr><td>${esc(s.name)}</td><td>${esc(s.kind)}</td><td>${s.ok ? s.count + ' postings read' : 'Failed: ' + esc(s.error)}</td></tr>`).join('');
const bad = D.sources.filter(s => !s.ok && !String(s.error).startsWith('skipped')).length;
$('foot').textContent = `${D.sources.length} sources checked` + (bad ? `, ${bad} failed (see below; fix or remove them in sources.toml).` : '.') + ' Scores are keyword-based estimates. Always read the posting before applying.';

function card(j){
  const st = status[j.key];
  const chips = [];
  if(j.first_seen >= D.today) chips.push('<span class="chip new">New</span>');
  for(const f of j.flags) chips.push(`<span class="chip ${f.kind === 'info' ? '' : f.kind}">${esc(f.label)}</span>`);
  const rows = [];
  if(j.matched.length) rows.push(`<dt>Your skills it asks for</dt><dd>${esc(j.matched.join(', '))}</dd>`);
  if(j.related.length) rows.push(`<dt>Transferable</dt><dd>${esc(j.related.join(', '))}</dd>`);
  if(j.gaps.length) rows.push(`<dt>Not on your resume</dt><dd>${esc(j.gaps.join(', '))}</dd>`);
  rows.push(`<dt>Score breakdown</dt><dd>Aerospace ${j.parts.aerospace}/40, skills ${j.parts.skills}/35, eligibility ${j.parts.eligibility}/25</dd>`);
  rows.push(`<dt>Found via</dt><dd>${esc(j.source)}</dd>`);
  const ai = j.ai ? `<div class="ai"><b>Claude's take (${esc(j.ai.fit)}):</b> ${esc(j.ai.summary)}${j.ai.talking_point ? '<br><b>Lead with:</b> ' + esc(j.ai.talking_point) : ''}</div>` : '';
  const meta = [j.location && `<span>${esc(j.location)}</span>`, j.posted && `<span>Posted ${fmt(j.posted)}</span>`, `<span>Found ${fmt(j.first_seen)}</span>`].filter(Boolean).join('');
  return `<article class="strip t-${j.tier} ${st ? 'done' : ''}" data-key="${esc(j.key)}">
    <div class="score" title="Fit score out of 100"><b>${j.score}</b><small>${TIER[j.tier]}</small></div>
    <div class="main">
      <div class="co">${esc(j.company)}</div>
      <h2 class="title">${esc(j.title)}</h2>
      <div class="meta">${meta}</div>
      <div class="chips">${chips.join('')}</div>
      ${ai}
      <details class="why"><summary>Why this score</summary><dl>${rows.join('')}</dl></details>
    </div>
    <div class="acts">
      <a class="btn" href="${esc(j.url)}" target="_blank" rel="noopener">Apply</a>
      <button class="btn ghost" data-act="applied">${st === 'applied' ? 'Applied ✓' : 'Mark applied'}</button>
      <button class="btn ghost" data-act="dismissed">${st === 'dismissed' ? 'Dismissed' : 'Dismiss'}</button>
    </div>
  </article>`;
}

function render(){
  const q = $('q').value.trim().toLowerCase();
  const hide = $('hideDone').checked, onlyNew = $('onlyNew').checked, onlySummer = $('onlySummer').checked;
  let jobs = D.jobs.filter(j => {
    if(tier !== 'all' && j.tier !== tier) return false;
    if(hide && status[j.key]) return false;
    if(onlyNew && j.first_seen < weekAgo) return false;
    if(onlySummer && j.season !== 'match') return false;
    if(q){
      const hay = [j.company, j.title, j.location, j.matched.join(' '), j.gaps.join(' ')].join(' ').toLowerCase();
      if(!q.split(/\s+/).every(w => hay.includes(w))) return false;
    }
    return true;
  });
  const s = $('sort').value;
  jobs.sort(s === 'new' ? (a,b) => (b.first_seen + (b.posted||'')).localeCompare(a.first_seen + (a.posted||'')) || b.score - a.score
         : s === 'company' ? (a,b) => a.company.localeCompare(b.company) || b.score - a.score
         : (a,b) => b.score - a.score);
  if(!D.jobs.length){
    $('list').innerHTML = `<div class="empty"><p><b>No scan results yet.</b></p><p>In your GitHub repo, open the Actions tab, choose “Daily job scan”, and click “Run workflow”. This page fills in about five minutes later.</p></div>`;
    return;
  }
  $('list').innerHTML = jobs.length ? jobs.map(card).join('') :
    '<div class="empty">No postings match these filters. Clear the search or choose “All”.</div>';
}

document.querySelectorAll('.seg button').forEach(b => b.addEventListener('click', () => {
  tier = b.dataset.tier;
  document.querySelectorAll('.seg button').forEach(x => x.setAttribute('aria-pressed', x === b));
  render();
}));
['q','sort','onlyNew','onlySummer','hideDone'].forEach(id => $(id).addEventListener(id === 'q' ? 'input' : 'change', render));
$('list').addEventListener('click', e => {
  const btn = e.target.closest('button[data-act]');
  if(!btn) return;
  const key = btn.closest('.strip').dataset.key;
  status[key] = status[key] === btn.dataset.act ? undefined : btn.dataset.act;
  if(!status[key]) delete status[key];
  store.set(status);
  render();
});
render();
})();
</script>
</body>
</html>
"""


def _pct_list(counter: dict, total: int, top: int = 8) -> list[dict]:
    if not total:
        return []
    items = sorted(counter.items(), key=lambda kv: -kv[1])[:top]
    return [{"name": k, "count": v, "pct": round(100 * v / total)} for k, v in items if v]


def build_page(jobs: list[dict], sources: list[dict], profile: dict, now: dt.datetime) -> str:
    with_desc = [j for j in jobs if j.get("has_description")]
    have, need = {}, {}
    for j in with_desc:
        for s in j["matched"]:
            have[s] = have.get(s, 0) + 1
        for g in j["gaps"]:
            need[g] = need.get(g, 0) + 1
    today = now.date().isoformat()
    page_jobs = [{k: j.get(k) for k in (
        "key", "company", "title", "location", "url", "source", "posted", "first_seen",
        "score", "tier", "parts", "matched", "related", "gaps", "flags", "season", "ai")} for j in jobs]
    try:
        from zoneinfo import ZoneInfo
        eastern = now.astimezone(ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001 - label only
        eastern = now - dt.timedelta(hours=4)
    data = {
        "name": profile["student"].get("name", "you"),
        # %-d / %-I are Linux-only, so build the unpadded parts by hand
        "updated_label": (f"{eastern:%b} {eastern.day}, {eastern.year} at "
                          f"{eastern.hour % 12 or 12}:{eastern:%M %p} ET") if jobs or sources else "never",
        "today": today,
        "jobs": page_jobs,
        "have": _pct_list(have, len(with_desc)),
        "need": _pct_list(need, len(with_desc)),
        "sources": sources,
        "counts": {
            "total": len(jobs),
            "strong": sum(1 for j in jobs if j["tier"] == "strong"),
            "new_today": sum(1 for j in jobs if j.get("first_seen") == today),
        },
    }
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.replace("__DATA__", blob)


def update_readme(path: Path, jobs: list[dict], page_url: str, now: dt.datetime, top: int = 25) -> None:
    start, end = "<!-- JOBS:START -->", "<!-- JOBS:END -->"
    text = path.read_text(encoding="utf-8")
    if start not in text or end not in text:
        return
    today = now.date().isoformat()
    lines = [
        f"**{len(jobs)} matches** as of {now:%b} {now.day}, {now.year}. "
        f"Full list with filters: **[open the job page]({page_url})**",
        "",
        "| Fit | Company | Role | Location | Apply |",
        "|---|---|---|---|---|",
    ]
    for j in sorted(jobs, key=lambda j: -j["score"])[:top]:
        new = " 🆕" if j.get("first_seen") == today else ""
        loc = re.sub(r"\s+", " ", (j.get("location") or ""))[:40]
        title = (j["title"] or "").replace("|", "/")
        lines.append(f"| {j['score']} | {j['company'].replace('|', '/')} | {title}{new} | {loc} | [Apply]({j['url']}) |")
    if not jobs:
        lines.append("| | No matches yet | Run the workflow from the Actions tab | | |")
    block = start + "\n" + "\n".join(lines) + "\n" + end
    text = re.sub(re.escape(start) + r".*?" + re.escape(end), lambda _: block, text, flags=re.S)
    path.write_text(text, encoding="utf-8")
