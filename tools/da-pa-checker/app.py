"""
DA PA Checker (Domain Authority Checker)  -  Flask app
Check the authority score of any website and compare it with competitors.

  * DA (Domain Authority) 0-100 (from the free Open PageRank API, built on Common Crawl link data)
  * Global rank, referring domains and 12-month trend
  * Compare up to 20 domains side by side, with tier + link-prospect verdict
  * Paste messy URLs - https://, www and paths are cleaned automatically
  * CSV export

Set your free Open PageRank key before running:
    Windows (cmd):   set OPR_API_KEY=opr_live_xxxxxxxx
    Vercel:          Settings -> Environment Variables -> OPR_API_KEY

Built by Mahalakshmi Marimuthu - Digital Marketing Strategist & AI-Powered SEO Expert
"""
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

OPR_URL = os.environ.get("OPR_API_URL", "https://openpagerank.keywordseverywhere.com/v1/domains/bulk")
MAX_DOMAINS = 20
TIMEOUT = 20
DOMAIN_RX = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def clean_domain(raw):
    """'https://www.Example.com/blog?x=1' -> 'example.com'. Returns '' if not a domain."""
    s = (raw or "").strip().strip("\"'<>").lower()
    if not s:
        return ""
    if "://" not in s:
        s = "http://" + s
    try:
        host = urlparse(s).hostname or ""
    except ValueError:
        return ""
    host = host.strip(".")
    if host.startswith("www."):
        host = host[4:]
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    return host if DOMAIN_RX.match(host) else ""


def tier(score):
    if score is None:
        return ["No data", "none"]
    if score >= 80:
        return ["Excellent", "ok"]
    if score >= 60:
        return ["Strong", "ok"]
    if score >= 40:
        return ["Good", "purple"]
    if score >= 20:
        return ["Fair", "warn"]
    return ["Low", "error"]


def verdict(score):
    if score is None:
        return ["Not enough link data", "none"]
    if score >= 40:
        return ["Strong link prospect", "ok"]
    if score >= 20:
        return ["Average prospect", "warn"]
    return ["Weak prospect", "error"]


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_item(item):
    """Normalise one Open PageRank result into what the page needs."""
    opr = num(item.get("open_page_rank"))
    found = item.get("found", opr is not None)
    if not found:
        opr = None
    score = None if opr is None else int(round(opr * 10))

    hist = []
    for h in item.get("history") or []:
        v = num(h.get("open_page_rank"))
        if h.get("date") and v is not None:
            hist.append({"date": str(h["date"])[:7], "score": round(v * 10, 1),
                         "estimated": bool(h.get("estimated"))})
    hist.sort(key=lambda x: x["date"])
    hist = hist[-13:]  # last 12 months + current

    change = None
    if score is not None and len(hist) >= 2:
        change = round(hist[-1]["score"] - hist[0]["score"], 1)

    rank = item.get("rank")
    try:
        rank = int(rank) if rank not in (None, "") else None
    except (TypeError, ValueError):
        rank = None
    refs = item.get("referring_domains")
    try:
        refs = int(refs) if refs not in (None, "") else None
    except (TypeError, ValueError):
        refs = None

    return {
        "domain": item.get("domain", ""),
        "found": score is not None,
        "score": score,
        "opr": None if opr is None else round(opr, 2),
        "rank": rank,
        "referring_domains": refs,
        "history": hist,
        "change": change,
        "tier": tier(score),
        "verdict": verdict(score),
    }


def fetch_opr(domains, key):
    r = requests.post(
        OPR_URL,
        json={"domains": domains, "include_history": True},
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "Accept": "application/json"},
        timeout=TIMEOUT,
    )
    if r.status_code in (401, 403):
        raise RuntimeError("The Open PageRank API key was rejected. Please check the OPR_API_KEY value.")
    if r.status_code == 429:
        raise RuntimeError("Too many checks right now (free API limit). Please wait a minute and try again.")
    if r.status_code >= 400:
        raise RuntimeError(f"The authority data service returned an error ({r.status_code}). Please try again shortly.")
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError("The authority data service sent an unreadable reply. Please try again.")
    return data


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/api/check", methods=["POST"])
def api_check():
    body = request.get_json(silent=True) or {}
    raw = [str(d) for d in (body.get("domains") or []) if str(d).strip()]
    if not raw:
        return jsonify({"ok": False, "error": "Please enter a website or domain, e.g. yourwebsite.com"}), 400

    seen, domains, invalid = set(), [], []
    for d in raw:
        c = clean_domain(d)
        if not c:
            invalid.append(d.strip())
        elif c not in seen:
            seen.add(c)
            domains.append(c)
    if not domains:
        return jsonify({"ok": False, "error": "That doesn't look like a valid domain. Try something like yourwebsite.com"}), 400
    truncated = len(domains) > MAX_DOMAINS
    domains = domains[:MAX_DOMAINS]

    key = os.environ.get("OPR_API_KEY", "").strip()
    if not key:
        return jsonify({"ok": False, "error": "The tool isn't set up yet: the OPR_API_KEY environment variable is missing."}), 500

    try:
        data = fetch_opr(domains, key)
    except requests.RequestException:
        return jsonify({"ok": False, "error": "Couldn't reach the authority data service. Please try again."}), 502
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    by_domain = {}
    for item in data.get("results") or []:
        p = parse_item(item)
        by_domain[clean_domain(p["domain"]) or p["domain"]] = p

    results = []
    for d in domains:
        p = by_domain.get(d) or parse_item({"domain": d, "found": False})
        p["domain"] = d
        results.append(p)

    invalid += [str(x) for x in (data.get("invalid") or [])]
    return jsonify({
        "ok": True,
        "results": results,
        "invalid": invalid,
        "truncated": truncated,
        "as_of": data.get("as_of"),
        "checked_at": datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC"),
    })


@app.route("/")
def home():
    return Response(PAGE, mimetype="text/html")


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DA PA Checker – Free Bulk Domain Authority (DA) Checker</title>
<meta name="description" content="Free DA PA Checker: check the Domain Authority (DA) of up to 20 websites at once, with global rank, referring domains, 12-month DA trend and a side-by-side comparison.">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%237c6af7'/%3E%3Ctext x='50%25' y='50%25' dominant-baseline='central' text-anchor='middle' font-family='Georgia%2C serif' font-size='18' font-weight='900' fill='white'%3EM%3C/text%3E%3C/svg%3E">
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@600;700;800&family=Inter:wght@400;500;600;700&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root{
    --bg:#0a0a0f;--surface:#12121a;--surface2:#171722;--border:#23233a;
    --accent:#7c6af7;--accent-h:#6a58e8;--mint:#6af7c8;--orange:#f7a26a;
    --red:#f76a6a;--text:#e8e8f0;--muted:#8888a8;
  }
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);font-size:15px;line-height:1.6}
  a{color:var(--accent);text-decoration:none}

  .shell{display:flex;min-height:100vh}
  .sidebar{width:230px;background:var(--surface);border-right:1px solid var(--border);padding:1.4rem 1rem;position:fixed;top:0;bottom:0;display:flex;flex-direction:column;overflow-y:auto}
  .main{flex:1;margin-left:230px;padding:1.6rem 2rem 3rem;max-width:1400px;min-width:0}
  .brand{display:flex;align-items:center;gap:.6rem;font-family:'Sora',sans-serif;font-weight:800;font-size:.95rem;margin-bottom:2rem;color:var(--text);text-decoration:none}
  .brand:hover .brand-text{color:var(--accent)}
  .brand-mark{width:30px;height:30px;border-radius:8px;background:var(--accent);display:flex;align-items:center;justify-content:center;color:#fff;font-size:1rem}
  .sidebar-section{margin-bottom:1.4rem}
  .sidebar-label{font-family:'DM Mono',monospace;font-size:.62rem;letter-spacing:.16em;text-transform:uppercase;color:var(--muted);margin-bottom:.5rem;padding-left:.3rem}
  .sidebar-link{display:flex;align-items:center;gap:.6rem;padding:.55rem .8rem;border-radius:8px;color:var(--muted);font-size:.85rem;font-weight:500;margin-bottom:.2rem;border:1px solid transparent;text-decoration:none}
  .sidebar-link.active{background:rgba(124,106,247,.14);border:1px solid rgba(124,106,247,.4);color:var(--accent);font-weight:700}
  .sidebar-link:hover{color:var(--text)}
  .sidebar-link.active:hover{color:var(--accent)}
  .side-note{font-size:.7rem;color:var(--muted);line-height:1.5;background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:.7rem .8rem}
  .side-note b{color:var(--text)}
  .sidebar-footer{margin-top:auto;font-size:.68rem;color:var(--muted);line-height:1.5;padding-top:1rem}
  .credit-name{color:var(--mint);font-weight:700;text-decoration:none}
  .credit-name:hover{text-decoration:underline}
  .sidebar-social{display:flex;gap:.5rem;margin-top:.7rem}
  .sidebar-social a{width:28px;height:28px;border-radius:6px;background:rgba(106,247,200,.08);border:1px solid rgba(106,247,200,.35);color:var(--mint);display:flex;align-items:center;justify-content:center;font-family:'DM Mono',monospace;font-size:.7rem;text-decoration:none;transition:background .2s}
  .sidebar-social a:hover{background:rgba(106,247,200,.18)}

  .topbar{margin-bottom:1.2rem}
  .topbar h1{font-family:'Sora',sans-serif;font-size:1.25rem;font-weight:700}
  .topbar h1 span{color:var(--accent)}
  .crumb{font-family:'DM Mono',monospace;font-size:.68rem;color:var(--muted);letter-spacing:.12em;text-transform:uppercase}

  .card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.2rem 1.4rem}
  label.lbl{font-size:.8rem;color:var(--muted);display:block;margin-bottom:.5rem}
  input[type=text],textarea{width:100%;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:.75rem 1rem;font-family:'DM Mono',monospace;font-size:.82rem}
  textarea{resize:vertical}
  input:focus,textarea:focus{outline:none;border-color:var(--accent)}
  .btn{display:inline-flex;align-items:center;gap:.5rem;background:var(--accent);color:#fff;border:none;border-radius:8px;padding:.7rem 1.5rem;font-size:.85rem;font-weight:600;cursor:pointer;font-family:'Inter',sans-serif;white-space:nowrap}
  .btn:hover{background:var(--accent-h)}
  .btn:disabled{opacity:.5;cursor:wait}
  .btn.ghost{background:var(--surface);border:1px solid var(--border);color:var(--text);font-weight:500;padding:.55rem 1.1rem;font-size:.8rem}
  .btn.ghost:hover{border-color:var(--accent)}
  .row-inline{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center}
  .row-inline input{flex:1;min-width:240px}
  details.adv{margin-top:.9rem}
  details.adv summary{cursor:pointer;font-size:.78rem;color:var(--muted);font-family:'DM Mono',monospace}
  details.adv summary:hover{color:var(--text)}
  .adv-body{margin-top:.8rem}
  .hint{font-size:.72rem;color:var(--muted);margin-top:.5rem;font-family:'DM Mono',monospace}
  .err{font-size:.8rem;color:var(--red);margin-top:.6rem}
  .spinner{display:none;margin-top:.8rem;font-size:.78rem;color:var(--muted);font-family:'DM Mono',monospace}
  .spinner.show{display:block}

  .overview{display:grid;grid-template-columns:220px 1fr 1.2fr;gap:1rem;margin:1.4rem 0}
  .chart-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.1rem 1.2rem;min-width:0}
  .chart-card h3{font-family:'DM Mono',monospace;font-size:.66rem;letter-spacing:.12em;font-weight:500;margin-bottom:.8rem;color:var(--muted);text-transform:uppercase}
  .ring-wrap{position:relative;width:160px;height:160px;margin:0 auto}
  .ring-center{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
  .ring-center .score{font-family:'Sora',sans-serif;font-size:2.1rem;font-weight:800}
  .ring-center .sub{font-size:.6rem;font-family:'DM Mono',monospace;color:var(--muted);text-transform:uppercase;letter-spacing:.1em}
  .ring-dom{text-align:center;font-family:'DM Mono',monospace;font-size:.72rem;margin-top:.6rem;overflow-wrap:anywhere}
  .chart-box{position:relative;height:210px}

  .metrics{display:grid;grid-template-columns:1fr 1fr;gap:.7rem}
  .metric{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:.7rem .9rem;min-width:0}
  .metric .k{font-family:'DM Mono',monospace;font-size:.6rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
  .metric .v{font-family:'Sora',sans-serif;font-size:1.05rem;font-weight:700;margin-top:.15rem;overflow-wrap:anywhere}
  .mint{color:var(--mint)} .orange{color:var(--orange)} .red{color:var(--red)} .purple{color:var(--accent)} .muted{color:var(--muted)}

  .badge{display:inline-flex;align-items:center;gap:.3rem;font-size:.66rem;font-family:'DM Mono',monospace;padding:.22rem .6rem;border-radius:100px;white-space:nowrap;border:1px solid}
  .badge.ok{background:rgba(106,247,200,.1);border-color:rgba(106,247,200,.3);color:var(--mint)}
  .badge.purple{background:rgba(124,106,247,.12);border-color:rgba(124,106,247,.4);color:var(--accent)}
  .badge.warn{background:rgba(247,162,106,.1);border-color:rgba(247,162,106,.35);color:var(--orange)}
  .badge.error{background:rgba(247,106,106,.1);border-color:rgba(247,106,106,.35);color:var(--red)}
  .badge.none{background:rgba(136,136,168,.1);border-color:rgba(136,136,168,.3);color:var(--muted)}

  .sec-title{font-family:'Sora',sans-serif;font-size:1rem;font-weight:700;margin:2rem 0 .9rem;display:flex;align-items:center;gap:.6rem;flex-wrap:wrap}
  .sec-title .count{font-family:'DM Mono',monospace;font-size:.7rem;color:var(--muted);font-weight:400}
  .sec-title .btn{margin-left:auto}

  .tbl-wrap{overflow-x:auto;background:var(--surface);border:1px solid var(--border);border-radius:12px}
  table{width:100%;border-collapse:collapse;font-size:.8rem}
  th{font-family:'DM Mono',monospace;font-size:.6rem;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);text-align:left;padding:.65rem .8rem;border-bottom:1px solid var(--border);white-space:nowrap}
  td{padding:.6rem .8rem;border-bottom:1px solid var(--border);vertical-align:middle}
  tr:last-child td{border-bottom:none}
  tbody tr:hover td{background:rgba(124,106,247,.05)}
  tr.pick{cursor:pointer}
  tr.pick.sel td{background:rgba(124,106,247,.09)}
  td.mono{font-family:'DM Mono',monospace;font-size:.74rem;word-break:break-all}
  .sc{display:flex;align-items:center;gap:.6rem;min-width:130px}
  .sc b{font-family:'Sora',sans-serif;width:28px}
  .sc-bar{flex:1;height:6px;background:var(--surface2);border-radius:100px;overflow:hidden}
  .sc-bar i{display:block;height:100%;border-radius:100px}

  .about{margin-top:2rem;font-size:.8rem;color:var(--muted);line-height:1.65}
  .about b{color:var(--text)}
  .about ul{margin:.5rem 0 0 1.1rem}

  @media(max-width:1100px){.overview{grid-template-columns:220px 1fr}.overview .wide{grid-column:1/-1}}
  @media(max-width:960px){.overview{grid-template-columns:1fr}.sidebar{display:none}.main{margin-left:0;padding:1.2rem 1rem}}
</style>
</head>
<body>
<div class="shell">

  <aside class="sidebar">
    <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="brand">
      <span class="brand-mark">M</span>
      <span class="brand-text">SEO Tools</span>
    </a>
    <div class="sidebar-section">
      <div class="sidebar-label">Tool</div>
      <a href="#" class="sidebar-link active">🏆&nbsp; DA PA Checker</a>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-label">Navigate</div>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="sidebar-link">🧰&nbsp; All My Tools</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio" class="sidebar-link">👩‍💻&nbsp; Portfolio</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/blog/index.html" class="sidebar-link">📝&nbsp; Blog Posts</a>
    </div>
    <div class="side-note"><b>DA source:</b> Open PageRank (Common Crawl link data), shown on a 0–100 scale. Up to 20 sites per check.</div>
    <div class="sidebar-footer">
      <p>Built by <a href="https://mahalakshmi26-hub.github.io/my-portfolio" class="credit-name">Mahalakshmi Marimuthu</a><br>Digital Marketing Strategist &amp; AI-Powered SEO Expert</p>
      <div class="sidebar-social">
        <a href="https://linkedin.com/in/mahalakshmimarimuthu" target="_blank" title="LinkedIn">in</a>
        <a href="https://www.instagram.com/mahapravin26/" target="_blank" title="Instagram">IG</a>
        <a href="mailto:mahalakshmi.digitalpro@gmail.com" title="Email">✉</a>
      </div>
    </div>
  </aside>

  <main class="main">
    <div class="topbar">
      <div class="crumb">// seo tool · free</div>
      <h1>🏆 DA PA <span>Checker</span></h1>
    </div>

    <form class="card" id="form">
      <label class="lbl" for="sites">Websites or URLs — one per line, up to 20</label>
      <textarea id="sites" rows="5" placeholder="yourwebsite.com&#10;competitor-one.com&#10;https://www.competitor-two.com/blog"></textarea>
      <div class="row-inline" style="margin-top:.8rem">
        <button class="btn" type="submit" id="runBtn">🔍 Check DA</button>
        <span class="hint" style="margin:0" id="lineCount">0 / 20</span>
      </div>
      <div class="hint">Paste full URLs if you like — https://, www and page paths are removed automatically.</div>
      <div class="spinner" id="sp">Looking up authority data…</div>
      <div class="err" id="err"></div>
    </form>

    <div id="results" style="display:none">
      <div class="overview">
        <div class="chart-card">
          <h3>DA · Domain Authority</h3>
          <div class="ring-wrap" id="ring"></div>
          <div class="ring-dom" id="ringDom"></div>
        </div>
        <div class="chart-card">
          <h3>At a glance</h3>
          <div class="metrics" id="metrics"></div>
        </div>
        <div class="chart-card wide">
          <h3 id="chartTitle">DA · 12-month trend</h3>
          <div class="chart-box"><canvas id="chart"></canvas></div>
        </div>
      </div>

      <div class="sec-title">📊 Results <span class="count" id="count"></span>
        <button type="button" class="btn ghost" id="csvBtn">⬇ Download CSV</button></div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>#</th><th>Domain</th><th>DA</th><th>Tier</th><th>Link prospect</th><th>Global rank</th><th>Referring domains</th><th>DA change (12 mo)</th></tr></thead>
          <tbody id="body"></tbody>
        </table>
      </div>
      <div class="hint" id="tblHint"></div>

      <div class="about">
        <b>About DA.</b> DA (Domain Authority) here is a free, transparent score: the <b>Open PageRank</b> value (0–10, built from Common Crawl link data) shown on a 0–100 scale.
        It's not Moz's own DA, and Google doesn't use any third-party authority score for ranking. DA is measured per domain, so a page URL is scored by its domain —
        page-level PA (Page Authority) needs paid data, so it isn't shown. Use DA to compare sites and vet link prospects, not as a ranking guarantee.
        <ul>
          <li>DA tiers: <b>80–100</b> Excellent · <b>60–79</b> Strong · <b>40–59</b> Good · <b>20–39</b> Fair · <b>0–19</b> Low</li>
        </ul>
      </div>
    </div>
  </main>
</div>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtN = n => n == null ? '—' : Number(n).toLocaleString('en-US');
const colOf = s => s == null ? '#8888a8' : s >= 60 ? '#6af7c8' : s >= 40 ? '#7c6af7' : s >= 20 ? '#f7a26a' : '#f76a6a';
let DATA = null, ROWS = [], SEL = 0, CH = null;

$('form').addEventListener('submit', async e => {
  e.preventDefault();
  const list = $('sites').value.split(/[\n,]+/).map(s => s.trim()).filter(Boolean);
  $('err').textContent = '';
  if (!list.length) { $('err').textContent = 'Please enter at least one website or URL.'; return; }
  $('sp').classList.add('show'); $('runBtn').disabled = true;
  try {
    const r = await fetch('/api/check', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ domains: list }) });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'Something went wrong.');
    DATA = j;
    ROWS = j.results.slice().sort((a, b) => (b.score ?? -1) - (a.score ?? -1));
    SEL = 0;
    render();
    $('results').style.display = '';
    const notes = [];
    if (j.truncated) notes.push('Only the first 20 sites were checked.');
    if (j.invalid && j.invalid.length) notes.push('Skipped (not a valid domain): ' + j.invalid.join(', '));
    $('err').textContent = notes.join(' ');
  } catch (err) {
    $('err').textContent = err.message || 'Could not reach the server — please try again.';
  } finally {
    $('sp').classList.remove('show'); $('runBtn').disabled = false;
  }
});

$('sites').addEventListener('input', () => {
  const n = $('sites').value.split(/[\n,]+/).map(s => s.trim()).filter(Boolean).length;
  $('lineCount').textContent = `${n} / 20`; $('lineCount').style.color = n > 20 ? 'var(--red)' : '';
});

function ring(sc) {
  const col = colOf(sc), v = sc || 0;
  return `<svg viewBox="0 0 160 160" width="160" height="160">
      <circle cx="80" cy="80" r="68" fill="none" stroke="#23233a" stroke-width="12"/>
      <circle cx="80" cy="80" r="68" fill="none" stroke="${col}" stroke-width="12" stroke-linecap="${v > 0 ? 'round' : 'butt'}"
        stroke-dasharray="${(427.26 * v / 100).toFixed(2)} 427.26" transform="rotate(-90 80 80)"/></svg>
    <div class="ring-center"><div class="score" style="color:${col}">${sc == null ? '—' : sc}</div><div class="sub">DA / 100</div></div>`;
}

function changeHtml(c) {
  if (c == null) return '<span class="muted">—</span>';
  if (c === 0) return '<span class="muted">0</span>';
  return c > 0 ? `<span class="mint">▲ ${c}</span>` : `<span class="red">▼ ${Math.abs(c)}</span>`;
}

function render() {
  const r = ROWS[SEL];
  $('ring').innerHTML = ring(r.score);
  $('ringDom').innerHTML = esc(r.domain);
  $('metrics').innerHTML = [
    ['Tier', r.tier[0], r.tier[1] === 'ok' ? 'mint' : r.tier[1] === 'purple' ? 'purple' : r.tier[1] === 'warn' ? 'orange' : r.tier[1] === 'error' ? 'red' : 'muted'],
    ['Open PageRank', r.opr == null ? '—' : r.opr + ' / 10', 'purple'],
    ['Global rank', r.rank == null ? '—' : '#' + fmtN(r.rank), ''],
    ['Referring domains', fmtN(r.referring_domains), ''],
    ['DA change (12 mo)', r.change == null ? '—' : (r.change > 0 ? '+' : '') + r.change, r.change > 0 ? 'mint' : r.change < 0 ? 'red' : 'muted'],
    ['Link prospect', r.verdict[0], r.verdict[1] === 'ok' ? 'mint' : r.verdict[1] === 'warn' ? 'orange' : r.verdict[1] === 'error' ? 'red' : 'muted'],
  ].map(([k, v, cl]) => `<div class="metric"><div class="k">${k}</div><div class="v ${cl}">${esc(v)}</div></div>`).join('');

  $('count').textContent = `${ROWS.length} domain${ROWS.length > 1 ? 's' : ''} · checked ${DATA.checked_at}${DATA.as_of ? ' · data as of ' + DATA.as_of : ''}`;
  $('body').innerHTML = ROWS.map((x, i) => `<tr class="pick ${i === SEL ? 'sel' : ''}" data-i="${i}">
      <td class="muted">${i + 1}</td>
      <td class="mono">${esc(x.domain)}</td>
      <td><div class="sc"><b style="color:${colOf(x.score)}">${x.score == null ? '—' : x.score}</b><div class="sc-bar"><i style="width:${x.score || 0}%;background:${colOf(x.score)}"></i></div></div></td>
      <td><span class="badge ${x.tier[1]}">${esc(x.tier[0])}</span></td>
      <td><span class="badge ${x.verdict[1]}">${esc(x.verdict[0])}</span></td>
      <td>${x.rank == null ? '—' : '#' + fmtN(x.rank)}</td>
      <td>${fmtN(x.referring_domains)}</td>
      <td>${changeHtml(x.change)}</td></tr>`).join('');
  $('tblHint').textContent = ROWS.length > 1 ? 'Click a row to see its score and trend above.' : '';
  document.querySelectorAll('tr.pick').forEach(tr => tr.addEventListener('click', () => {
    SEL = +tr.dataset.i; render(); window.scrollTo({ top: 0, behavior: 'smooth' });
  }));
  drawChart();
}

function drawChart() {
  if (typeof Chart === 'undefined') return;
  if (CH) CH.destroy();
  const grid = { color: '#23233a' }, ticks = { color: '#8888a8', font: { family: 'DM Mono', size: 10 } };
  if (ROWS.length > 1) {
    $('chartTitle').textContent = 'DA comparison';
    CH = new Chart($('chart'), {
      type: 'bar',
      data: { labels: ROWS.map(x => x.domain), datasets: [{ data: ROWS.map(x => x.score || 0),
        backgroundColor: ROWS.map((x, i) => i === SEL ? colOf(x.score) : colOf(x.score) + '66'), borderRadius: 5, maxBarThickness: 22 }] },
      options: { indexAxis: 'y', maintainAspectRatio: false, plugins: { legend: { display: false } },
        scales: { x: { min: 0, max: 100, grid, ticks }, y: { grid: { display: false }, ticks } },
        onClick: (e, el) => { if (el.length) { SEL = el[0].index; render(); } } }
    });
  } else {
    const h = ROWS[0].history || [];
    $('chartTitle').textContent = 'DA · 12-month trend';
    CH = new Chart($('chart'), {
      type: 'line',
      data: { labels: h.map(p => p.date), datasets: [{ data: h.map(p => p.score), borderColor: '#7c6af7',
        backgroundColor: 'rgba(124,106,247,.12)', fill: true, tension: .3, pointRadius: 3,
        pointBackgroundColor: h.map(p => p.estimated ? '#8888a8' : '#7c6af7') }] },
      options: { maintainAspectRatio: false, plugins: { legend: { display: false },
          tooltip: { callbacks: { label: c => ' ' + c.parsed.y + (h[c.dataIndex].estimated ? ' (estimated)' : '') } } },
        scales: { y: { suggestedMin: 0, suggestedMax: 100, grid, ticks }, x: { grid: { display: false }, ticks } } }
    });
  }
}

$('csvBtn').addEventListener('click', () => {
  if (!ROWS.length) return;
  const q = v => '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"';
  const out = [['Domain', 'DA - Domain Authority (0-100)', 'Open PageRank (0-10)', 'Tier', 'Link prospect', 'Global rank', 'Referring domains', '12-month change'].map(q).join(',')];
  ROWS.forEach(x => out.push([x.domain, x.score, x.opr, x.tier[0], x.verdict[0], x.rank, x.referring_domains, x.change].map(q).join(',')));
  const blob = new Blob(['﻿' + out.join('\r\n')], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
  a.download = `da-pa-check-${ROWS.length === 1 ? ROWS[0].domain : 'comparison'}.csv`; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
});
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(debug=True, port=5000)
