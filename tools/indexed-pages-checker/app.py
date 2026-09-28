"""
Indexed Pages Checker  -  Flask app
How many pages of a website has Google indexed?

  * Enter a domain -> get Google's indexed-page count (the "site:" search estimate)
  * Enter a URL with a folder (e.g. site.com/blog/) -> count for just that section
  * "Verify on Google" button opens the same site: search so anyone can cross-check
  * Shows a sample of the indexed pages Google returns
  * Results cached for 6 hours to save the free API quota

Set your free SerpApi key before running (free plan = 250 searches / month):
    Windows (cmd):   set SERPAPI_KEY=your_key_here
    Vercel:          Settings -> Environment Variables -> SERPAPI_KEY

Built by Mahalakshmi Marimuthu - Digital Marketing Strategist & AI-Powered SEO Expert
"""
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote_plus, urlparse

import requests
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

SERPAPI_URL = os.environ.get("SERPAPI_URL", "https://serpapi.com/search.json")
TIMEOUT = 30
CACHE_SECONDS = 6 * 60 * 60
GOOGLE_DOMAIN = "google.co.in"
DOMAIN_RX = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
NO_RESULTS_HINTS = ("hasn't returned any results", "has not returned any results", "no results")

_cache = {}  # query -> (timestamp, payload)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def clean_target(raw):
    """
    'https://www.Example.com/blog/?x=1' -> ('example.com', '/blog/')
    Returns ('', '') if it isn't a valid domain.
    """
    s = (raw or "").strip().strip("\"'<>").lower()
    if s.startswith("site:"):
        s = s[5:].strip()
    if not s:
        return "", ""
    if "://" not in s:
        s = "http://" + s
    try:
        p = urlparse(s)
        host = p.hostname or ""
    except ValueError:
        return "", ""
    host = host.strip(".")
    if host.startswith("www."):
        host = host[4:]
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return "", ""
    if not DOMAIN_RX.match(host):
        return "", ""
    path = (p.path or "").strip()
    if path in ("", "/"):
        path = ""
    return host, path


def size_band(n):
    if n is None:
        return ["Unknown", "none"]
    if n == 0:
        return ["Not indexed", "error"]
    if n < 100:
        return ["Small · under 100", "warn"]
    if n < 10_000:
        return ["Medium · 100–10K", "purple"]
    if n < 1_000_000:
        return ["Large · 10K–1M", "ok"]
    return ["Very large · 1M+", "ok"]


def to_int(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    digits = re.sub(r"[^\d]", "", str(v))
    return int(digits) if digits else None


def fetch_serpapi(query, key):
    r = requests.get(
        SERPAPI_URL,
        params={
            "engine": "google",
            "q": query,
            "google_domain": GOOGLE_DOMAIN,
            "gl": "in",
            "hl": "en",
            "num": 10,
            "api_key": key,
        },
        timeout=TIMEOUT,
    )
    try:
        data = r.json()
    except ValueError:
        data = {}
    err = str(data.get("error") or "")
    if err and any(h in err.lower() for h in NO_RESULTS_HINTS):
        return {"search_information": {"total_results": 0}, "organic_results": []}
    if r.status_code in (401, 403) or "invalid api key" in err.lower():
        raise RuntimeError("The SerpApi key was rejected. Please check the SERPAPI_KEY value.")
    if r.status_code == 429 or "run out of searches" in err.lower():
        raise RuntimeError("The free monthly search limit has been used up. Please try again next month.")
    if r.status_code >= 400 or err:
        raise RuntimeError(f"The search data service returned an error ({err or r.status_code}). Please try again shortly.")
    return data


def build_result(domain, path, data):
    info = data.get("search_information") or {}
    total = to_int(info.get("total_results"))
    organic = data.get("organic_results") or []
    sample = [{"title": o.get("title") or "", "link": o.get("link") or ""} for o in organic if o.get("link")][:10]

    exact = total is not None
    if total is None:
        # Google didn't show a count; the best we can say is "at least" the pages it listed.
        total = len(sample) if sample else 0

    query = f"site:{domain}{path}"
    return {
        "domain": domain,
        "path": path,
        "query": query,
        "count": total,
        "has_count": exact,
        "band": size_band(total),
        "scope": "Section only" if path else "Whole site (incl. subdomains)",
        "sample": sample,
        "google_url": f"https://www.{GOOGLE_DOMAIN}/search?q={quote_plus(query)}",
        "checked_at": datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC"),
    }


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/api/check", methods=["POST"])
def api_check():
    body = request.get_json(silent=True) or {}
    raw = str(body.get("site") or "").strip()
    if not raw:
        return jsonify({"ok": False, "error": "Please enter a website, e.g. yourwebsite.com"}), 400

    domain, path = clean_target(raw)
    if not domain:
        return jsonify({"ok": False, "error": "That doesn't look like a valid website. Try something like yourwebsite.com"}), 400

    query = f"site:{domain}{path}"
    hit = _cache.get(query)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        out = dict(hit[1])
        out["cached"] = True
        return jsonify(out)

    key = os.environ.get("SERPAPI_KEY", "").strip()
    if not key:
        return jsonify({"ok": False, "error": "The tool isn't set up yet: the SERPAPI_KEY environment variable is missing."}), 500

    try:
        data = fetch_serpapi(query, key)
    except requests.RequestException:
        return jsonify({"ok": False, "error": "Couldn't reach the search data service. Please try again."}), 502
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    out = {"ok": True, "cached": False, **build_result(domain, path, data)}
    _cache[query] = (time.time(), out)
    return jsonify(out)


@app.route("/")
def home():
    return Response(PAGE, mimetype="text/html")


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Indexed Pages Checker – How Many Pages Has Google Indexed?</title>
<meta name="description" content="Free Indexed Pages Checker: enter any website and see how many of its pages Google has indexed, check a single section, and verify it on Google in one click.">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%237c6af7'/%3E%3Ctext x='50%25' y='50%25' dominant-baseline='central' text-anchor='middle' font-family='Georgia%2C serif' font-size='18' font-weight='900' fill='white'%3EM%3C/text%3E%3C/svg%3E">
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@600;700;800&family=Inter:wght@400;500;600;700&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
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
  .main{flex:1;margin-left:230px;padding:1.6rem 2rem 3rem;max-width:1200px;min-width:0}
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
  input[type=text]{width:100%;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:.75rem 1rem;font-family:'DM Mono',monospace;font-size:.85rem}
  input:focus{outline:none;border-color:var(--accent)}
  .btn{display:inline-flex;align-items:center;gap:.5rem;background:var(--accent);color:#fff;border:none;border-radius:8px;padding:.72rem 1.5rem;font-size:.85rem;font-weight:600;cursor:pointer;font-family:'Inter',sans-serif;white-space:nowrap;text-decoration:none}
  .btn:hover{background:var(--accent-h)}
  .btn:disabled{opacity:.5;cursor:wait}
  .btn.ghost{background:var(--surface2);border:1px solid var(--border);color:var(--text);font-weight:500;padding:.6rem 1.1rem;font-size:.8rem}
  .btn.ghost:hover{border-color:var(--accent)}
  .row-inline{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center}
  .row-inline input{flex:1;min-width:240px}
  .hint{font-size:.72rem;color:var(--muted);margin-top:.6rem;font-family:'DM Mono',monospace}
  .err{font-size:.8rem;color:var(--red);margin-top:.6rem}
  .spinner{display:none;margin-top:.8rem;font-size:.78rem;color:var(--muted);font-family:'DM Mono',monospace}
  .spinner.show{display:block}

  .overview{display:grid;grid-template-columns:1.1fr 1fr;gap:1rem;margin:1.4rem 0}
  .panel{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.3rem 1.4rem;min-width:0}
  .panel h3{font-family:'DM Mono',monospace;font-size:.66rem;letter-spacing:.12em;font-weight:500;margin-bottom:.8rem;color:var(--muted);text-transform:uppercase}
  .big{font-family:'Sora',sans-serif;font-size:3.2rem;font-weight:800;line-height:1.1;color:var(--mint);overflow-wrap:anywhere}
  .big small{font-size:1.2rem;color:var(--muted);font-weight:600;margin-right:.2rem}
  .big-sub{font-size:.85rem;color:var(--muted);margin-top:.3rem}
  .big-sub b{color:var(--text);font-family:'DM Mono',monospace;font-weight:500;overflow-wrap:anywhere}
  .actions{display:flex;gap:.6rem;flex-wrap:wrap;margin-top:1.1rem}

  .metrics{display:grid;grid-template-columns:1fr 1fr;gap:.7rem}
  .metric{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:.7rem .9rem;min-width:0}
  .metric .k{font-family:'DM Mono',monospace;font-size:.6rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
  .metric .v{font-family:'Sora',sans-serif;font-size:.95rem;font-weight:700;margin-top:.15rem;overflow-wrap:anywhere}

  .badge{display:inline-flex;align-items:center;gap:.3rem;font-size:.66rem;font-family:'DM Mono',monospace;padding:.22rem .6rem;border-radius:100px;white-space:nowrap;border:1px solid}
  .badge.ok{background:rgba(106,247,200,.1);border-color:rgba(106,247,200,.3);color:var(--mint)}
  .badge.purple{background:rgba(124,106,247,.12);border-color:rgba(124,106,247,.4);color:var(--accent)}
  .badge.warn{background:rgba(247,162,106,.1);border-color:rgba(247,162,106,.35);color:var(--orange)}
  .badge.error{background:rgba(247,106,106,.1);border-color:rgba(247,106,106,.35);color:var(--red)}
  .badge.none{background:rgba(136,136,168,.1);border-color:rgba(136,136,168,.3);color:var(--muted)}

  .sec-title{font-family:'Sora',sans-serif;font-size:1rem;font-weight:700;margin:1.8rem 0 .9rem;display:flex;align-items:center;gap:.6rem;flex-wrap:wrap}
  .sec-title .count{font-family:'DM Mono',monospace;font-size:.7rem;color:var(--muted);font-weight:400}
  .tbl-wrap{overflow-x:auto;background:var(--surface);border:1px solid var(--border);border-radius:12px}
  table{width:100%;border-collapse:collapse;font-size:.8rem}
  th{font-family:'DM Mono',monospace;font-size:.6rem;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);text-align:left;padding:.65rem .8rem;border-bottom:1px solid var(--border);white-space:nowrap}
  td{padding:.6rem .8rem;border-bottom:1px solid var(--border);vertical-align:top}
  tr:last-child td{border-bottom:none}
  tbody tr:hover td{background:rgba(124,106,247,.05)}
  td.mono{font-family:'DM Mono',monospace;font-size:.72rem;word-break:break-all}
  .muted{color:var(--muted)}

  .about{margin-top:2rem;font-size:.8rem;color:var(--muted);line-height:1.65}
  .about b{color:var(--text)}
  .about ul{margin:.5rem 0 0 1.1rem}

  @media(max-width:960px){.overview{grid-template-columns:1fr}.sidebar{display:none}.main{margin-left:0;padding:1.2rem 1rem}.big{font-size:2.6rem}}
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
      <a href="#" class="sidebar-link active">📚&nbsp; Indexed Pages Checker</a>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-label">Navigate</div>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="sidebar-link">🧰&nbsp; All My Tools</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio" class="sidebar-link">👩‍💻&nbsp; Portfolio</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/blog/index.html" class="sidebar-link">📝&nbsp; Blog Posts</a>
    </div>
    <div class="side-note"><b>How it counts:</b> Google's own <b>site:</b> search estimate from google.co.in. Add a folder (e.g. /blog/) to count just that section.</div>
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
      <h1>📚 Indexed Pages <span>Checker</span></h1>
    </div>

    <form class="card" id="form">
      <label class="lbl" for="site">Website (or a section URL)</label>
      <div class="row-inline">
        <input type="text" id="site" placeholder="yourwebsite.com   or   yourwebsite.com/blog/" autocomplete="off">
        <button class="btn" type="submit" id="runBtn">🔍 Check Indexed Pages</button>
      </div>
      <div class="hint">https:// and www are removed automatically · keep a folder path to count only that section</div>
      <div class="spinner" id="sp">Asking Google how many pages it has indexed…</div>
      <div class="err" id="err"></div>
    </form>

    <div id="results" style="display:none">
      <div class="overview">
        <div class="panel">
          <h3>Pages indexed on Google</h3>
          <div class="big" id="big"></div>
          <div class="big-sub" id="bigSub"></div>
          <div class="actions">
            <a class="btn" id="verify" target="_blank" rel="noopener">🔎 Verify on Google</a>
            <button type="button" class="btn ghost" id="copyBtn">📋 Copy result</button>
          </div>
        </div>
        <div class="panel">
          <h3>Details</h3>
          <div class="metrics" id="metrics"></div>
        </div>
      </div>

      <div id="sampleWrap">
        <div class="sec-title">📄 Sample of indexed pages <span class="count" id="sampleCount"></span></div>
        <div class="tbl-wrap">
          <table>
            <thead><tr><th>#</th><th>Page title</th><th>URL</th></tr></thead>
            <tbody id="body"></tbody>
          </table>
        </div>
      </div>

      <div class="about">
        <b>About this number.</b> It's the count Google itself shows for a <b>site:</b> search — the same number you'd see on Google.
        Google calls this an <b>estimate</b>, so it can differ slightly from day to day and from Search Console.
        For the exact figure on a site you own, open <b>Google Search Console → Indexing → Pages</b>.
        <ul>
          <li>Whole domain checks include subdomains (e.g. blog.yoursite.com).</li>
          <li>Add a folder to count one section, e.g. <b>yoursite.com/blog/</b>.</li>
          <li>Results are cached for 6 hours.</li>
        </ul>
      </div>
    </div>
  </main>
</div>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtN = n => n == null ? '—' : Number(n).toLocaleString('en-IN');
let R = null;

$('form').addEventListener('submit', async e => {
  e.preventDefault();
  const site = $('site').value.trim();
  $('err').textContent = '';
  if (!site) { $('err').textContent = 'Please enter a website, e.g. yourwebsite.com'; return; }
  $('sp').classList.add('show'); $('runBtn').disabled = true;
  try {
    const r = await fetch('/api/check', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ site }) });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'Something went wrong.');
    R = j; render(); $('results').style.display = '';
  } catch (err) {
    $('err').textContent = err.message || 'Could not reach the server — please try again.';
  } finally {
    $('sp').classList.remove('show'); $('runBtn').disabled = false;
  }
});

function render() {
  const r = R;
  const prefix = r.count === 0 ? '' : (r.has_count ? '<small>≈</small>' : '<small>at least</small>');
  $('big').innerHTML = prefix + fmtN(r.count);
  $('big').style.color = r.count === 0 ? 'var(--red)' : 'var(--mint)';
  $('bigSub').innerHTML = r.count === 0
    ? `Google shows <b>no indexed pages</b> for <b>${esc(r.domain + r.path)}</b>.`
    : `pages indexed for <b>${esc(r.domain + r.path)}</b>` + (r.has_count ? '' : ' · Google didn\'t show a total, so this is the number of pages it listed');
  $('verify').href = r.google_url;
  $('metrics').innerHTML = [
    ['Website', esc(r.domain)],
    ['Scope', esc(r.scope)],
    ['Size', `<span class="badge ${r.band[1]}">${esc(r.band[0])}</span>`],
    ['Search used', `<span style="font-family:'DM Mono',monospace;font-size:.8rem">${esc(r.query)}</span>`],
    ['Google', 'google.co.in'],
    ['Checked', esc(r.checked_at) + (r.cached ? ' <span class="muted" style="font-size:.7rem">(cached)</span>' : '')],
  ].map(([k, v]) => `<div class="metric"><div class="k">${k}</div><div class="v">${v}</div></div>`).join('');

  const s = r.sample || [];
  $('sampleWrap').style.display = s.length ? '' : 'none';
  $('sampleCount').textContent = s.length ? `first ${s.length} results Google returned` : '';
  $('body').innerHTML = s.map((x, i) => `<tr><td class="muted">${i + 1}</td><td>${esc(x.title)}</td>
      <td class="mono"><a href="${esc(x.link)}" target="_blank" rel="noopener">${esc(x.link)}</a></td></tr>`).join('');
}

$('copyBtn').addEventListener('click', async () => {
  if (!R) return;
  const txt = `${R.domain}${R.path}: ${R.has_count || R.count === 0 ? '~' : 'at least '}${fmtN(R.count)} pages indexed on Google (${R.query}, checked ${R.checked_at})`;
  try { await navigator.clipboard.writeText(txt); $('copyBtn').textContent = '✅ Copied'; }
  catch (e) { $('copyBtn').textContent = '⚠ Copy failed'; }
  setTimeout(() => { $('copyBtn').textContent = '📋 Copy result'; }, 1600);
});
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(debug=True, port=5000)
