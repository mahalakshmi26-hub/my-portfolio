import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, Response, jsonify, request

from lookup import check_domain, normalize

app = Flask(__name__)

MAX_DOMAINS = 25          # per request
HOURLY_LIMIT = 150        # lookups per visitor per hour
_hits = defaultdict(deque)


def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else request.remote_addr) or "unknown"


def _within_limit(ip, n):
    now = time.time()
    q = _hits[ip]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) + n > HOURLY_LIMIT:
        return False
    q.extend([now] * n)
    return True


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.post("/api/check")
def api_check():
    body = request.get_json(silent=True) or {}
    raw = body.get("domains", [])
    if isinstance(raw, str):
        raw = raw.replace(",", "\n").splitlines()
    raw = [str(x).strip() for x in raw if str(x).strip()]
    if not raw:
        return jsonify(error="Enter at least one domain."), 400

    seen, jobs, results, skipped = set(), [], [], []
    for item in raw:
        dom, note = normalize(item)
        if not dom:
            skipped.append({"input": item, "reason": note})
            continue
        if dom in seen:
            continue
        seen.add(dom)
        jobs.append((dom, item, note))

    if len(jobs) > MAX_DOMAINS:
        skipped += [{"input": j[1], "reason": f"over the {MAX_DOMAINS}-domain limit per run"} for j in jobs[MAX_DOMAINS:]]
        jobs = jobs[:MAX_DOMAINS]

    if jobs and not _within_limit(_client_ip(), len(jobs)):
        return jsonify(error="Hourly limit reached (150 lookups). Please try again later."), 429

    if jobs:
        with ThreadPoolExecutor(max_workers=5) as ex:
            results = list(ex.map(lambda j: check_domain(j[0], j[1], j[2]), jobs))
    return jsonify(results=results, skipped=skipped)


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Domain Age Checker | Mahalakshmi Marimuthu</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Inter:wght@400;500;600;700&family=Sora:wght@600;700;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0a0f;--surface:#12121a;--surface2:#14141c;--line:#23233a;--accent:#7c6af7;--accent-h:#6a58e8;--accent2:#f7a26a;--accent3:#6af7c8;--danger:#f76a6a;--text:#e8e8f0;--muted:#8888a8}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:Inter,sans-serif;display:flex;min-height:100vh;font-size:14px}
a{color:inherit;text-decoration:none}
.sidebar{width:240px;flex-shrink:0;background:var(--surface);border-right:1px solid var(--line);padding:22px 16px;display:flex;flex-direction:column;position:sticky;top:0;height:100vh}
.brand{display:flex;align-items:center;gap:10px;margin-bottom:26px}
.brand-mark{width:32px;height:32px;border-radius:9px;background:var(--accent);display:grid;place-items:center;font:800 15px Sora,sans-serif;color:#fff}
.brand-text{font:700 15px Sora,sans-serif}
.sidebar-section{margin-bottom:20px}
.sidebar-label{font:500 10px 'DM Mono',monospace;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);margin:0 8px 8px}
.sidebar-link{display:block;padding:9px 10px;border-radius:8px;color:var(--muted);border:1px solid transparent;margin-bottom:3px;font-weight:500}
.sidebar-link:hover{color:var(--text);background:rgba(255,255,255,.04)}
.sidebar-link.active{background:rgba(124,106,247,.14);border-color:rgba(124,106,247,.4);color:var(--accent);font-weight:700}
.info-box{background:var(--surface2);border:1px solid var(--line);border-radius:10px;padding:12px;font-size:12px;color:var(--muted);line-height:1.55}
.info-box b{color:var(--text)}
.sidebar-footer{margin-top:auto;font-size:11.5px;color:var(--muted);line-height:1.5}
.credit-name{color:var(--accent3);font-weight:700}
.sidebar-social{display:flex;gap:6px;margin-top:10px}
.sidebar-social a{width:28px;height:28px;border-radius:7px;display:grid;place-items:center;font:500 11px 'DM Mono',monospace;background:rgba(106,247,200,.08);border:1px solid rgba(106,247,200,.35);color:var(--accent3)}
.sidebar-social a:hover{background:rgba(106,247,200,.18)}
main{flex:1;min-width:0;padding:32px 36px 60px;max-width:1300px}
h1{font:800 26px Sora,sans-serif;margin-bottom:6px}
.sub{color:var(--muted);margin-bottom:22px;max-width:720px;line-height:1.55}
.card{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:20px}
textarea{width:100%;min-height:120px;resize:vertical;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:10px;padding:12px 14px;font:13.5px 'DM Mono',monospace;outline:none}
textarea:focus{border-color:var(--accent)}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:12px}
.btn{background:var(--accent);color:#fff;border:0;border-radius:9px;padding:11px 20px;font:600 14px Inter,sans-serif;cursor:pointer}
.btn:hover{background:var(--accent-h)}.btn:disabled{opacity:.55;cursor:wait}
.btn.ghost{background:transparent;border:1px solid var(--line);color:var(--muted)}
.btn.ghost:hover{color:var(--text);border-color:var(--accent)}
.hint{color:var(--muted);font-size:12px}
.chip{background:var(--surface2);border:1px solid var(--line);border-radius:20px;padding:4px 11px;font:12px 'DM Mono',monospace;color:var(--muted);cursor:pointer}
.chip:hover{color:var(--accent3);border-color:var(--accent3)}
#status{margin-top:14px;color:var(--muted);font-size:13px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:22px 0 16px}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.tile .k{font:500 10px 'DM Mono',monospace;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.tile .v{font:700 22px Sora,sans-serif;margin-top:6px}
.tile .s{font-size:12px;color:var(--muted);margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.toolbar{display:flex;gap:10px;align-items:center;margin-bottom:10px;flex-wrap:wrap}
.toolbar input{background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:8px 12px;font:13px Inter,sans-serif;outline:none;min-width:220px}
.toolbar .spacer{flex:1}
.tablewrap{overflow-x:scroll;overflow-y:hidden;max-width:100%;border:1px solid var(--line);border-radius:12px;background:var(--surface);scrollbar-color:var(--accent) var(--surface2);scrollbar-width:auto}
.tablewrap::-webkit-scrollbar{height:14px}.tablewrap::-webkit-scrollbar-track{background:var(--surface2)}.tablewrap::-webkit-scrollbar-thumb{background:var(--accent);border-radius:8px;border:3px solid var(--surface2)}
table{width:100%;border-collapse:collapse;min-width:1150px}
th{position:sticky;top:0;text-align:left;font:500 10.5px 'DM Mono',monospace;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);padding:12px 14px;border-bottom:1px solid var(--line);background:var(--surface2);cursor:pointer;white-space:nowrap}
th:hover{color:var(--text)}
td{padding:12px 14px;border-bottom:1px solid var(--line);vertical-align:top}
tr.data:hover td{background:rgba(255,255,255,.02)}
tr:last-child td{border-bottom:0}
.dom{font-weight:600;cursor:pointer}
.dom small{display:block;font-weight:400;color:var(--muted);font-size:11.5px;margin-top:2px}
.mono{font-family:'DM Mono',monospace;font-size:12.5px;white-space:nowrap}
.left{display:block;font:11.5px Inter,sans-serif;color:var(--muted);margin-top:2px}.left.soon{color:var(--accent2)}.left.gone{color:var(--danger)}
.badge{display:inline-block;padding:3px 9px;border-radius:20px;font:600 11px Inter,sans-serif;border:1px solid}
.b-new{color:var(--accent2);border-color:rgba(247,162,106,.45);background:rgba(247,162,106,.1)}
.b-young{color:#e6d36a;border-color:rgba(230,211,106,.4);background:rgba(230,211,106,.08)}
.b-est{color:var(--accent3);border-color:rgba(106,247,200,.4);background:rgba(106,247,200,.08)}
.b-aged{color:var(--accent);border-color:rgba(124,106,247,.5);background:rgba(124,106,247,.12)}
.b-err{color:var(--danger);border-color:rgba(247,106,106,.45);background:rgba(247,106,106,.1)}
.muted{color:var(--muted)}
tr.detail td{background:var(--surface2);font-size:12.5px;line-height:1.7;color:var(--muted)}
tr.detail b{color:var(--text)}
.note{color:var(--accent2);margin-top:4px}
.explain{margin-top:28px;display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}
.explain .card h3{font:700 14px Sora,sans-serif;margin-bottom:8px}
.explain p,.explain li{color:var(--muted);line-height:1.6;font-size:13px}
.explain ol{padding-left:18px}
.hidden{display:none}
@media(max-width:820px){body{flex-direction:column}.sidebar{width:100%;height:auto;position:static;flex-direction:row;flex-wrap:wrap;gap:10px}.sidebar-footer{display:none}main{padding:22px 16px}}
</style>
</head>
<body>
<aside class="sidebar">
  <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="brand">
    <span class="brand-mark">M</span><span class="brand-text">SEO Tools</span>
  </a>
  <div class="sidebar-section">
    <div class="sidebar-label">Tool</div>
    <a href="#" class="sidebar-link active">&#128197; Domain Age Checker</a>
  </div>
  <div class="sidebar-section">
    <div class="sidebar-label">Navigate</div>
    <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="sidebar-link">&#129520; All My Tools</a>
    <a href="https://mahalakshmi26-hub.github.io/my-portfolio" class="sidebar-link">&#128105;&#8205;&#128187; Portfolio</a>
    <a href="https://mahalakshmi26-hub.github.io/my-portfolio/blog/index.html" class="sidebar-link">&#128221; Blog Posts</a>
  </div>
  <div class="info-box"><b>Limits</b><br>Up to 25 domains per run. Data comes from the domain registry (RDAP), so it is the same date the registrar holds.</div>
  <div class="sidebar-footer">
    <p>Built by <a href="https://mahalakshmi26-hub.github.io/my-portfolio" class="credit-name">Mahalakshmi Marimuthu</a><br>Digital Marketing Strategist &amp; AI-Powered SEO Expert</p>
    <div class="sidebar-social">
      <a href="https://linkedin.com/in/mahalakshmimarimuthu" target="_blank" title="LinkedIn">in</a>
      <a href="https://www.instagram.com/mahapravin26/" target="_blank" title="Instagram">IG</a>
      <a href="mailto:mahalakshmi.digitalpro@gmail.com" title="Email">&#9993;</a>
    </div>
  </div>
</aside>

<main>
  <h1>Domain Age Checker</h1>
  <p class="sub">Find out exactly when a domain was registered, how old it is, when it expires and who the registrar is. Paste one domain or a list, one per line. Results come straight from the registry, with the Wayback Machine as a cross-check.</p>

  <div class="card">
    <textarea id="input" placeholder="example.com&#10;bankbazaar.com&#10;https://www.paisabazaar.com/personal-loan/"></textarea>
    <div class="row">
      <button class="btn" id="go">Check domain age</button>
      <button class="btn ghost" id="clear">Clear</button>
      <span class="hint">Try:</span>
      <span class="chip" data-d="google.com">google.com</span>
      <span class="chip" data-d="bankbazaar.com">bankbazaar.com</span>
      <span class="chip" data-d="bajajfinserv.in">bajajfinserv.in</span>
    </div>
    <div id="status"></div>
  </div>

  <section id="results" class="hidden">
    <div class="tiles" id="tiles"></div>
    <div class="toolbar">
      <input id="filter" placeholder="Filter results...">
      <span class="spacer"></span>
      <button class="btn ghost" id="csv">Export CSV</button>
    </div>
    <div class="tablewrap">
      <table>
        <thead><tr>
          <th data-k="domain">Domain</th><th data-k="created">Registered</th><th data-k="age_days">Age</th>
          <th data-k="trust">Bracket</th><th data-k="expires">Expires</th><th data-k="registrar">Registrar</th>
          <th data-k="wayback_first">Wayback first seen</th>
        </tr></thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
    <p class="hint" style="margin-top:10px">Scroll the table sideways to see every column. Click a domain to see nameservers, status flags and notes (including why a Wayback date is missing).</p>
  </section>

  <div class="explain">
    <div class="card"><h3>How to use it</h3><ol>
      <li>Paste one or more domains or URLs (subdomains are trimmed to the registered domain).</li>
      <li>Click <b>Check domain age</b>.</li>
      <li>Sort or filter the table, open a row for details, and export to CSV.</li></ol></div>
    <div class="card"><h3>How accurate is it?</h3><p>The registration date is read from the registry's own RDAP record, the official successor to WHOIS, so it is authoritative. If a TLD has no RDAP service, the tool falls back to WHOIS and labels the source. Privacy protection hides owner details, not dates.</p></div>
    <div class="card"><h3>Reading the result</h3><p>Age is counted from the <b>current</b> registration. If a domain was dropped and re-registered, the registry date resets, so the tool flags it when the Wayback Machine saw the site earlier. Domain age alone is not a Google ranking factor; use it as context when vetting backlinks, competitors or expired domains.</p></div>
  </div>
</main>

<script>
let DATA=[], sortKey=null, sortDir=1;
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

document.querySelectorAll('.chip').forEach(c=>c.onclick=()=>{const t=$('input');t.value=(t.value.trim()?t.value.trim()+'\n':'')+c.dataset.d});
$('clear').onclick=()=>{$('input').value='';$('status').textContent=''};
$('go').onclick=run;

async function run(){
  const text=$('input').value.trim();
  if(!text){$('status').textContent='Enter at least one domain.';return}
  $('go').disabled=true;$('status').textContent='Checking registry records... bulk runs can take up to a minute.';
  try{
    const r=await fetch('/api/check',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({domains:text.split(/[\n,]+/)})});
    const j=await r.json();
    if(!r.ok){$('status').textContent=j.error||'Something went wrong.';return}
    DATA=j.results;sortKey=null;
    let msg=`Done: ${DATA.length} domain${DATA.length!==1?'s':''} checked.`;
    if(j.skipped&&j.skipped.length)msg+=` Skipped: ${j.skipped.map(s=>esc(s.input)+' ('+esc(s.reason)+')').join(', ')}.`;
    $('status').innerHTML=msg;
    render();
  }catch(e){$('status').textContent='Network error. If the server was asleep, wait 30 seconds and try again.'}
  finally{$('go').disabled=false}
}

function leftTxt(d){const n=d.expires_in_days;if(n==null)return '';
  if(n<0)return '<span class="left gone">expired</span>';
  const y=Math.floor(n/365),m=Math.floor((n%365)/30);
  const t=y?`${y}y ${m}m left`:(m?`${m}m left`:`${n}d left`);
  return `<span class="left${n<=60?' soon':''}">${t}</span>`}
function wb(d){return '<td class="mono">'+(d.wayback_first?esc(d.wayback_first):'<span class="muted" title="Open the row to see why">not available</span>')+'</td>'}
function badge(t){if(!t)return '';const c=t.startsWith('New')?'b-new':t.startsWith('Young')?'b-young':t.startsWith('Est')?'b-est':'b-aged';return `<span class="badge ${c}">${esc(t)}</span>`}

function render(){
  $('results').classList.toggle('hidden',!DATA.length);
  const ok=DATA.filter(d=>d.ok&&d.age_days!=null);
  const oldest=ok.slice().sort((a,b)=>b.age_days-a.age_days)[0], newest=ok.slice().sort((a,b)=>a.age_days-b.age_days)[0];
  const failed=DATA.filter(d=>!d.ok).length, soon=DATA.filter(d=>d.expires_in_days!=null&&d.expires_in_days<=60).length;
  $('tiles').innerHTML=[
    ['Domains checked',DATA.length,failed?failed+' not resolved':'all resolved'],
    ['Oldest',oldest?oldest.age_text.split(',')[0]:'-',oldest?oldest.domain:''],
    ['Newest',newest?newest.age_text.split(',')[0]:'-',newest?newest.domain:''],
    ['Expiring in 60 days',soon,soon?'check renewals':'none']
  ].map(t=>`<div class="tile"><div class="k">${t[0]}</div><div class="v">${esc(t[1])}</div><div class="s">${esc(t[2])}</div></div>`).join('');
  const q=$('filter').value.toLowerCase();
  let rows=DATA.filter(d=>!q||JSON.stringify(d).toLowerCase().includes(q));
  if(sortKey)rows.sort((a,b)=>{let x=a[sortKey],y=b[sortKey];if(x==null)return 1;if(y==null)return -1;return (x>y?1:x<y?-1:0)*sortDir});
  $('rows').innerHTML=rows.map((d,i)=>{
    const main=d.ok?`<td class="mono">${esc(d.created||'Not published')}</td><td style="white-space:nowrap"><b>${esc(d.age_text||'-')}</b></td><td>${badge(d.trust)}</td><td class="mono">${esc(d.expires||'-')}${leftTxt(d)}</td><td>${esc(d.registrar||'-')}</td>${wb(d)}`
      :`<td colspan="5"><span class="badge b-err">Not found</span> <span class="muted">${esc(d.error)}</span></td>${wb(d)}`;
    const det=`<tr class="detail hidden" id="det${i}"><td colspan="7"><b>Source:</b> ${esc(d.source||'-')} &nbsp; <b>Last changed:</b> ${esc(d.updated||'-')} &nbsp; <b>Status:</b> ${esc((d.status||[]).join(', ')||'-')}<br><b>Nameservers:</b> ${esc((d.nameservers||[]).join(', ')||'-')}${(d.notes||[]).map(n=>`<div class="note">&#9432; ${esc(n)}</div>`).join('')}</td></tr>`;
    return `<tr class="data"><td class="dom" onclick="document.getElementById('det${i}').classList.toggle('hidden')">${esc(d.domain)}${d.input&&d.input!==d.domain?`<small>from ${esc(d.input)}</small>`:''}</td>${main}</tr>${det}`}).join('');
}
$('filter').oninput=render;
document.querySelectorAll('th').forEach(th=>th.onclick=()=>{const k=th.dataset.k;sortDir=sortKey===k?-sortDir:1;sortKey=k;render()});

$('csv').onclick=()=>{
  const cols=['domain','created','age_days','age_text','trust','expires','updated','registrar','wayback_first','source','nameservers','status','notes','error'];
  const cell=v=>{v=Array.isArray(v)?v.join(' | '):(v==null?'':v);return '"'+String(v).replace(/"/g,'""')+'"'};
  const csv=[cols.join(',')].concat(DATA.map(d=>cols.map(c=>cell(d[c])).join(','))).join('\n');
  const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));a.download='domain-age-report.csv';a.click();
};
</script>
</body>
</html>
"""


if __name__ == "__main__":
    import os
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False)
