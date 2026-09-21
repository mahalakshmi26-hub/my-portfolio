"""
Meta Tags Analyzer
Fetch a single URL and report its meta title, description, keywords,
viewport, robots, canonical, Open Graph tags, H1, on-page URL count and
page size — with character-count checks and a search-result preview.
Flask app | by Mahalakshmi Marimuthu
"""

import csv
import io
import re
import uuid

import requests
from bs4 import BeautifulSoup
from flask import Flask, abort, render_template_string, request, send_file

app = Flask(__name__)

# in-memory store for CSV export (keeps the last 20 runs)
STORE = {}
STORE_LIMIT = 20

FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}

TITLE_MIN, TITLE_MAX = 50, 60
DESC_MIN, DESC_MAX = 80, 160
URL_COUNT_WARN = 100
PAGE_SIZE_WARN = 500_000  # bytes


# ---------------------------------------------------------------- helpers
def normalize_url(raw: str) -> str:
    u = raw.strip()
    if not u:
        return u
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u


def length_status(n: int, lo: int, hi: int) -> tuple:
    """Return (status, message) for a character count against an ideal band."""
    if n == 0:
        return "error", "Missing"
    if lo <= n <= hi:
        return "ok", f"Good length ({n} characters)"
    if n < lo:
        return "warn", f"Shorter than recommended ({n} characters — aim for {lo}–{hi})"
    return "warn", f"Longer than recommended ({n} characters — aim for {lo}–{hi}), may get truncated in search results"


# ---------------------------------------------------------------- core logic
def analyze_url(url: str) -> dict:
    result = {"url": url, "fetch_ok": False, "error": None}
    try:
        resp = requests.get(url, headers=FETCH_HEADERS, timeout=15, allow_redirects=True)
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        result["error"] = "The request timed out — the site may be slow or blocking automated requests."
        return result
    except requests.exceptions.RequestException as e:
        result["error"] = f"Could not fetch this page ({type(e).__name__})."
        return result

    result["fetch_ok"] = True
    result["status_code"] = resp.status_code
    result["final_url"] = resp.url
    page_bytes = len(resp.content)

    soup = BeautifulSoup(resp.text, "html.parser")

    # --- title
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""
    t_status, t_msg = length_status(len(title), TITLE_MIN, TITLE_MAX)
    result["title"] = {"value": title, "len": len(title), "status": t_status, "msg": t_msg}

    # --- description
    desc_tag = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    desc = desc_tag["content"].strip() if desc_tag and desc_tag.get("content") else ""
    d_status, d_msg = length_status(len(desc), DESC_MIN, DESC_MAX)
    result["description"] = {"value": desc, "len": len(desc), "status": d_status, "msg": d_msg}

    # --- keywords
    kw_tag = soup.find("meta", attrs={"name": re.compile("^keywords$", re.I)})
    keywords = kw_tag["content"].strip() if kw_tag and kw_tag.get("content") else ""
    result["keywords"] = {
        "value": keywords or "Not found",
        "status": "ok" if keywords else "info",
        "msg": "Detected" if keywords else "Not found (this tag carries little SEO weight today, but worth having)",
    }

    # --- viewport
    vp_tag = soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)})
    viewport = vp_tag["content"].strip() if vp_tag and vp_tag.get("content") else ""
    result["viewport"] = {
        "value": viewport or "Not set",
        "status": "ok" if viewport else "warn",
        "msg": "Responsive viewport is set" if viewport else "Missing — page may not be flagged mobile-friendly",
    }

    # --- robots
    robots_tag = soup.find("meta", attrs={"name": re.compile("^robots$", re.I)})
    robots = robots_tag["content"].strip() if robots_tag and robots_tag.get("content") else ""
    if robots and "noindex" in robots.lower():
        r_status, r_msg = "error", "This page is blocked from search results"
    elif robots:
        r_status, r_msg = "ok", "Explicitly set"
    else:
        r_status, r_msg = "ok", "Not set (defaults to index, follow)"
    result["robots"] = {"value": robots or "index, follow (default)", "status": r_status, "msg": r_msg}

    # --- canonical
    canon_tag = soup.find("link", attrs={"rel": re.compile("^canonical$", re.I)})
    canonical = canon_tag["href"].strip() if canon_tag and canon_tag.get("href") else ""
    result["canonical"] = {
        "value": canonical or "Not set",
        "status": "ok" if canonical else "warn",
        "msg": "Set" if canonical else "Missing — add one to avoid duplicate-content issues",
    }

    # --- open graph
    og_tags = soup.find_all("meta", attrs={"property": re.compile("^og:", re.I)})
    og = {t.get("property", "").lower(): t.get("content", "") for t in og_tags if t.get("content")}
    result["og"] = {
        "present": bool(og),
        "tags": og,
        "status": "ok" if og else "warn",
        "msg": f"{len(og)} Open Graph tag(s) found" if og else "No Open Graph tags found — links may look plain when shared on social media",
    }

    # --- H1
    h1_tags = soup.find_all("h1")
    h1_count = len(h1_tags)
    if h1_count == 0:
        h_status, h_msg = "warn", "No H1 found on the page"
    elif h1_count == 1:
        h_status, h_msg = "ok", (h1_tags[0].get_text(strip=True) or "(empty H1 tag)")
    else:
        h_status, h_msg = "warn", f"{h1_count} H1 tags found — use only one per page"
    result["h1"] = {"count": h1_count, "status": h_status, "msg": h_msg}

    # --- url count referenced on the page (href + src attributes)
    href_count = len(soup.find_all(href=True))
    src_count = len(soup.find_all(src=True))
    url_count = href_count + src_count
    result["url_count"] = {
        "value": url_count,
        "status": "warn" if url_count > URL_COUNT_WARN else "ok",
        "msg": (
            f"This page references {url_count} URLs (links + assets). Some search engines have "
            f"trouble crawling pages with more than {URL_COUNT_WARN}."
            if url_count > URL_COUNT_WARN
            else f"{url_count} URLs referenced — within a healthy range"
        ),
    }

    # --- page size
    result["page_size"] = {
        "value": page_bytes,
        "status": "warn" if page_bytes > PAGE_SIZE_WARN else "ok",
        "msg": (
            f"{page_bytes / 1024:.0f} KB — on the heavier side, which can slow load times"
            if page_bytes > PAGE_SIZE_WARN
            else f"{page_bytes / 1024:.0f} KB"
        ),
    }

    # --- overall score
    checks = [
        result["title"]["status"] == "ok",
        result["description"]["status"] == "ok",
        result["viewport"]["status"] == "ok",
        result["robots"]["status"] == "ok",
        result["canonical"]["status"] == "ok",
        result["og"]["status"] == "ok",
        result["h1"]["status"] == "ok",
        result["url_count"]["status"] == "ok",
    ]
    result["score"] = round(100 * sum(checks) / len(checks))
    result["counts"] = {"ok": sum(1 for c in checks if c), "issue": sum(1 for c in checks if not c)}

    return result


def build_csv(r: dict) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Tag", "Value", "Status", "Note"])
    rows = [
        ("Final URL", r["final_url"], "", ""),
        ("Status Code", r["status_code"], "", ""),
        ("Meta Title", r["title"]["value"], r["title"]["status"], r["title"]["msg"]),
        ("Meta Description", r["description"]["value"], r["description"]["status"], r["description"]["msg"]),
        ("Meta Keywords", r["keywords"]["value"], r["keywords"]["status"], r["keywords"]["msg"]),
        ("Meta Viewport", r["viewport"]["value"], r["viewport"]["status"], r["viewport"]["msg"]),
        ("Meta Robots", r["robots"]["value"], r["robots"]["status"], r["robots"]["msg"]),
        ("Canonical", r["canonical"]["value"], r["canonical"]["status"], r["canonical"]["msg"]),
        ("Open Graph", "Yes" if r["og"]["present"] else "No", r["og"]["status"], r["og"]["msg"]),
        ("H1 Tag", r["h1"]["msg"], r["h1"]["status"], f"{r['h1']['count']} H1 tag(s)"),
        ("URLs on Page", r["url_count"]["value"], r["url_count"]["status"], r["url_count"]["msg"]),
        ("Page Size", f"{r['page_size']['value']} bytes", r["page_size"]["status"], r["page_size"]["msg"]),
        ("SEO Health Score", f"{r['score']}%", "", ""),
    ]
    writer.writerows(rows)
    return buf.getvalue()


# ---------------------------------------------------------------- template
PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Meta Tags Analyzer</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%237c6af7'/%3E%3Ctext x='50%25' y='50%25' dominant-baseline='central' text-anchor='middle' font-family='Georgia%2C serif' font-size='18' font-weight='900' fill='white'%3EM%3C/text%3E%3C/svg%3E">
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@600;700;800&family=Inter:wght@400;500;600&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root {
    --bg:#0a0a0f; --surface:#12121a; --surface2:#171722; --border:#23233a;
    --accent:#7c6af7; --accent-h:#6a58e8; --mint:#6af7c8; --orange:#f7a26a;
    --red:#f76a7c; --text:#e8e8f0; --muted:#8888a8;
  }
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);font-size:15px;line-height:1.6}
  a{color:var(--accent);text-decoration:none}

  /* layout */
  .shell{display:flex;min-height:100vh}
  .sidebar{width:230px;background:var(--surface);border-right:1px solid var(--border);padding:1.4rem 1rem;position:fixed;top:0;bottom:0;display:flex;flex-direction:column}
  .main{flex:1;margin-left:230px;padding:1.6rem 2rem 3rem;max-width:1400px}
  .brand{display:flex;align-items:center;gap:.6rem;font-family:'Sora',sans-serif;font-weight:800;font-size:.95rem;margin-bottom:2rem;color:var(--text);text-decoration:none}
  .brand:hover .brand-text{color:var(--accent)}
  .brand-mark{width:30px;height:30px;border-radius:8px;background:var(--accent);display:flex;align-items:center;justify-content:center;color:#fff;font-size:1rem}
  .sidebar-section{margin-bottom:1.4rem}
  .sidebar-label{font-family:'DM Mono',monospace;font-size:.62rem;letter-spacing:.16em;text-transform:uppercase;color:var(--muted);margin-bottom:.5rem;padding-left:.3rem}
  .sidebar-link{display:flex;align-items:center;gap:.6rem;padding:.55rem .8rem;border-radius:8px;color:var(--muted);font-size:.85rem;font-weight:500;margin-bottom:.2rem;border:1px solid transparent;text-decoration:none}
  .sidebar-link.active{background:rgba(124,106,247,.14);border:1px solid rgba(124,106,247,.4);color:var(--accent);font-weight:700}
  .sidebar-link:hover{color:var(--text)}
  .sidebar-link.active:hover{color:var(--accent)}
  .sidebar-footer{margin-top:auto;font-size:.68rem;color:var(--muted);line-height:1.5}
  .credit-name{color:var(--mint);font-weight:700;text-decoration:none}
  .credit-name:hover{text-decoration:underline}
  .sidebar-social{display:flex;gap:.5rem;margin-top:.7rem}
  .sidebar-social a{width:28px;height:28px;border-radius:6px;background:rgba(106,247,200,.08);border:1px solid rgba(106,247,200,.35);color:var(--mint);display:flex;align-items:center;justify-content:center;font-family:'DM Mono',monospace;font-size:.7rem;text-decoration:none;transition:background .2s}
  .sidebar-social a:hover{background:rgba(106,247,200,.18)}

  .topbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:1.4rem;flex-wrap:wrap;gap:.8rem}
  .topbar h1{font-family:'Sora',sans-serif;font-size:1.25rem;font-weight:700}
  .topbar h1 span{color:var(--accent)}
  .crumb{font-family:'DM Mono',monospace;font-size:.68rem;color:var(--muted);letter-spacing:.12em;text-transform:uppercase}

  /* cards */
  .card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.2rem 1.4rem}
  input[type=text]{width:100%;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:.75rem 1rem;font-family:'DM Mono',monospace;font-size:.85rem}
  input[type=text]:focus{outline:none;border-color:var(--accent)}
  .btn{display:inline-flex;align-items:center;gap:.5rem;background:var(--accent);color:#fff;border:none;border-radius:8px;padding:.6rem 1.5rem;font-size:.85rem;font-weight:600;cursor:pointer;font-family:'Inter',sans-serif;margin-top:.8rem}
  .btn:hover{background:var(--accent-h)}
  .hint{font-size:.72rem;color:var(--muted);margin-top:.5rem;font-family:'DM Mono',monospace}
  .form-row{display:flex;gap:.7rem;flex-wrap:wrap}
  .form-row input{flex:1;min-width:240px}

  /* metric row */
  .metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1rem;margin:1.4rem 0}
  .metric{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1rem 1.2rem}
  .metric .k{font-family:'DM Mono',monospace;font-size:.66rem;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
  .metric .v{font-family:'Sora',sans-serif;font-size:1.6rem;font-weight:800;margin-top:.2rem}
  .metric .v.purple{color:var(--accent)} .metric .v.mint{color:var(--mint)}
  .metric .v.orange{color:var(--orange)} .metric .v.red{color:var(--red)}

  /* charts row */
  .charts{display:grid;grid-template-columns:280px 1fr 1fr;gap:1rem;margin-bottom:1.4rem}
  .chart-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.1rem 1.2rem}
  .chart-card h3{font-family:'Sora',sans-serif;font-size:.82rem;font-weight:700;margin-bottom:.8rem;color:var(--muted)}
  .ring-wrap{position:relative;width:170px;height:170px;margin:0 auto}
  .ring-center{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
  .ring-center .score{font-family:'Sora',sans-serif;font-size:2rem;font-weight:800}
  .ring-center .lbl{font-size:.62rem;font-family:'DM Mono',monospace;color:var(--muted);text-transform:uppercase;letter-spacing:.12em}

  /* content length bars */
  .len-row{margin-bottom:1.1rem}
  .len-row:last-child{margin-bottom:0}
  .len-row .len-label{display:flex;justify-content:space-between;font-family:'DM Mono',monospace;font-size:.68rem;color:var(--muted);margin-bottom:.4rem}
  .len-track{position:relative;height:10px;border-radius:6px;background:var(--surface2);overflow:hidden}
  .len-ideal{position:absolute;top:0;bottom:0;background:rgba(106,247,200,.18)}
  .len-fill{position:absolute;top:0;bottom:0;left:0;border-radius:6px}
  .len-fill.ok{background:var(--mint)} .len-fill.warn{background:var(--orange)} .len-fill.error{background:var(--red)}

  /* table */
  .tbl-wrap{overflow-x:auto;background:var(--surface);border:1px solid var(--border);border-radius:12px;margin-bottom:1.4rem}
  table{width:100%;border-collapse:collapse;font-size:.8rem;min-width:640px}
  th{font-family:'DM Mono',monospace;font-size:.62rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);text-align:left;padding:.7rem .9rem;border-bottom:1px solid var(--border);white-space:nowrap}
  td{padding:.65rem .9rem;border-bottom:1px solid var(--border);vertical-align:top}
  tr:last-child td{border-bottom:none}
  tr:hover td{background:rgba(124,106,247,.05)}
  td.tag{font-weight:600;white-space:nowrap}
  td.val{max-width:420px;word-break:break-word;font-family:'DM Mono',monospace;font-size:.74rem;color:#c9c9d9}
  td.note{color:var(--muted);font-size:.74rem;max-width:320px}
  .badge{display:inline-block;font-size:.64rem;font-family:'DM Mono',monospace;padding:.18rem .55rem;border-radius:100px;white-space:nowrap}
  .badge.ok{background:rgba(106,247,200,.1);border:1px solid rgba(106,247,200,.3);color:var(--mint)}
  .badge.warn{background:rgba(247,162,106,.1);border:1px solid rgba(247,162,106,.35);color:var(--orange)}
  .badge.error{background:rgba(247,106,124,.1);border:1px solid rgba(247,106,124,.35);color:var(--red)}
  .badge.info{background:rgba(136,136,168,.1);border:1px solid rgba(136,136,168,.3);color:var(--muted)}

  /* search preview (mimics a real result) */
  .sec-title{font-family:'Sora',sans-serif;font-size:1rem;font-weight:700;margin:1.8rem 0 .9rem}
  .serp-card{background:#fff;border-radius:12px;padding:1.2rem 1.4rem;max-width:640px}
  .serp-url{color:#202124;font-size:.8rem;font-family:arial,sans-serif}
  .serp-title{color:#1a0dab;font-size:1.15rem;font-family:arial,sans-serif;margin:.15rem 0 .3rem;line-height:1.3}
  .serp-desc{color:#4d5156;font-size:.85rem;font-family:arial,sans-serif;line-height:1.5}

  .dl-row{display:flex;gap:.8rem;margin-top:1.4rem;flex-wrap:wrap}
  .dl-row a{display:inline-flex;align-items:center;gap:.5rem;background:var(--surface);border:1px solid var(--border);color:var(--text);padding:.55rem 1.2rem;border-radius:8px;font-size:.8rem;font-weight:500}
  .dl-row a:hover{border-color:var(--accent)}

  .spinner{display:none;margin-top:.8rem;font-size:.78rem;color:var(--muted);font-family:'DM Mono',monospace}
  .spinner.show{display:block}

  @media(max-width:960px){.charts{grid-template-columns:1fr}.sidebar{display:none}.main{margin-left:0;padding:1.2rem}}
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
      <a href="#" class="sidebar-link active">📋&nbsp; Meta Tags Analyzer</a>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-label">Navigate</div>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="sidebar-link">🧰&nbsp; All My Tools</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio" class="sidebar-link">👩‍💻&nbsp; Portfolio</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/blog/index.html" class="sidebar-link">📝&nbsp; Blog Posts</a>
    </div>
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
      <div>
        <div class="crumb">// seo tool · free</div>
        <h1>📋 Meta Tags <span>Analyzer</span></h1>
      </div>
    </div>

    <form class="card" method="POST" action="/analyze" onsubmit="document.getElementById('sp').classList.add('show')">
      <label style="font-size:.8rem;color:var(--muted);display:block;margin-bottom:.5rem">Enter a page URL to analyze</label>
      <div class="form-row">
        <input type="text" name="url" placeholder="https://example.com/page" value="{{ raw_input or '' }}">
        <button class="btn" type="submit">🔍 Analyze</button>
      </div>
      <div class="spinner" id="sp">Fetching and analyzing the page…</div>
      {% if error %}<div class="hint" style="color:var(--red)">{{ error }}</div>{% endif %}
    </form>

    {% if r %}
    <div class="sec-title">Meta tags report for: <span style="color:var(--accent);font-family:'DM Mono',monospace;font-size:.85rem">{{ r.final_url }}</span></div>

    <!-- metrics -->
    <div class="metrics">
      <div class="metric"><div class="k">SEO Health Score</div><div class="v {{ 'mint' if r.score >= 80 else 'orange' if r.score >= 50 else 'red' }}">{{ r.score }}%</div></div>
      <div class="metric"><div class="k">Status Code</div><div class="v purple">{{ r.status_code }}</div></div>
      <div class="metric"><div class="k">Page Size</div><div class="v {{ r.page_size.status }}">{{ (r.page_size.value / 1024) | round(0, 'floor') | int }} KB</div></div>
      <div class="metric"><div class="k">URLs on Page</div><div class="v {{ 'orange' if r.url_count.status == 'warn' else 'mint' }}">{{ r.url_count.value }}</div></div>
    </div>

    <!-- charts -->
    <div class="charts">
      <div class="chart-card">
        <h3>SEO HEALTH SCORE</h3>
        <div class="ring-wrap">
          <canvas id="ring"></canvas>
          <div class="ring-center">
            <div class="score" style="color:{{ '#6af7c8' if r.score >= 80 else '#f7a26a' if r.score >= 50 else '#f76a7c' }}">{{ r.score }}%</div>
            <div class="lbl">{{ r.counts.ok }} of {{ r.counts.ok + r.counts.issue }} checks passed</div>
          </div>
        </div>
      </div>
      <div class="chart-card">
        <h3>CHECK BREAKDOWN</h3>
        <canvas id="donut" height="170"></canvas>
      </div>
      <div class="chart-card">
        <h3>CONTENT LENGTH CHECK</h3>
        <div class="len-row">
          <div class="len-label"><span>Meta Title</span><span>{{ r.title.len }} / 60 chars</span></div>
          <div class="len-track">
            <div class="len-ideal" style="left:{{ (50/90*100) | round(1) }}%;width:{{ (10/90*100) | round(1) }}%"></div>
            <div class="len-fill {{ r.title.status }}" style="width:{{ [r.title.len, 90] | min / 90 * 100 }}%"></div>
          </div>
        </div>
        <div class="len-row">
          <div class="len-label"><span>Meta Description</span><span>{{ r.description.len }} / 160 chars</span></div>
          <div class="len-track">
            <div class="len-ideal" style="left:{{ (80/220*100) | round(1) }}%;width:{{ (80/220*100) | round(1) }}%"></div>
            <div class="len-fill {{ r.description.status }}" style="width:{{ [r.description.len, 220] | min / 220 * 100 }}%"></div>
          </div>
        </div>
        <div class="hint" style="margin-top:1rem">Shaded band = ideal length range</div>
      </div>
    </div>

    <!-- analysis table -->
    <div class="sec-title">Meta Tags Analysis</div>
    <div class="tbl-wrap">
      <table>
        <thead><tr><th>Tag</th><th>Value</th><th>Status</th><th>Note</th></tr></thead>
        <tbody>
          <tr><td class="tag">Meta Title</td><td class="val">{{ r.title.value or '—' }}</td><td><span class="badge {{ r.title.status }}">{{ r.title.status }}</span></td><td class="note">{{ r.title.msg }}</td></tr>
          <tr><td class="tag">Meta Description</td><td class="val">{{ r.description.value or '—' }}</td><td><span class="badge {{ r.description.status }}">{{ r.description.status }}</span></td><td class="note">{{ r.description.msg }}</td></tr>
          <tr><td class="tag">Meta Keywords</td><td class="val">{{ r.keywords.value }}</td><td><span class="badge {{ r.keywords.status }}">{{ r.keywords.status }}</span></td><td class="note">{{ r.keywords.msg }}</td></tr>
          <tr><td class="tag">Meta Viewport</td><td class="val">{{ r.viewport.value }}</td><td><span class="badge {{ r.viewport.status }}">{{ r.viewport.status }}</span></td><td class="note">{{ r.viewport.msg }}</td></tr>
          <tr><td class="tag">Meta Robots</td><td class="val">{{ r.robots.value }}</td><td><span class="badge {{ r.robots.status }}">{{ r.robots.status }}</span></td><td class="note">{{ r.robots.msg }}</td></tr>
          <tr><td class="tag">Canonical URL</td><td class="val">{{ r.canonical.value }}</td><td><span class="badge {{ r.canonical.status }}">{{ r.canonical.status }}</span></td><td class="note">{{ r.canonical.msg }}</td></tr>
          <tr><td class="tag">Open Graph</td><td class="val">{{ r.og.tags.keys() | join(', ') if r.og.present else 'None found' }}</td><td><span class="badge {{ r.og.status }}">{{ r.og.status }}</span></td><td class="note">{{ r.og.msg }}</td></tr>
          <tr><td class="tag">H1 Tag</td><td class="val">{{ r.h1.msg }}</td><td><span class="badge {{ r.h1.status }}">{{ r.h1.status }}</span></td><td class="note">{{ r.h1.count }} H1 tag(s) found</td></tr>
          <tr><td class="tag">URLs on Page</td><td class="val">{{ r.url_count.value }}</td><td><span class="badge {{ r.url_count.status }}">{{ r.url_count.status }}</span></td><td class="note">{{ r.url_count.msg }}</td></tr>
          <tr><td class="tag">Page Size</td><td class="val">{{ (r.page_size.value / 1024) | round(1) }} KB</td><td><span class="badge {{ r.page_size.status }}">{{ r.page_size.status }}</span></td><td class="note">{{ r.page_size.msg }}</td></tr>
        </tbody>
      </table>
    </div>

    <!-- search preview -->
    <div class="sec-title">Your Page on a Search Engine Result</div>
    <div class="serp-card">
      <div class="serp-url">{{ r.final_url }}</div>
      <div class="serp-title">{{ r.title.value[:60] }}{% if r.title.len > 60 %}…{% endif %}</div>
      <div class="serp-desc">{{ r.description.value[:160] if r.description.value else 'No meta description found — search engines will pull text from the page instead.' }}{% if r.description.len > 160 %}…{% endif %}</div>
    </div>

    <div class="dl-row">
      <a href="/download/{{ token }}/csv">⬇️ Download Report (CSV)</a>
    </div>
    {% endif %}
    {% if r and not r.fetch_ok %}{% endif %}

    <script>
    {% if r and r.fetch_ok %}
      const P = { score: {{ r.score }}, ok: {{ r.counts.ok }}, issue: {{ r.counts.issue }} };
      const MUTED = '#8888a8';
      new Chart(document.getElementById('ring'), {
        type: 'doughnut',
        data: { datasets: [{ data: [P.score, 100 - P.score],
          backgroundColor: [P.score >= 80 ? '#6af7c8' : P.score >= 50 ? '#f7a26a' : '#f76a7c', '#23233a'],
          borderWidth: 0, cutout: '78%' }] },
        options: { plugins: { legend: { display: false }, tooltip: { enabled: false } } }
      });
      new Chart(document.getElementById('donut'), {
        type: 'doughnut',
        data: { labels: ['Checks Passed', 'Needs Attention'],
          datasets: [{ data: [P.ok, P.issue], borderWidth: 0, cutout: '60%',
            backgroundColor: ['#6af7c8', '#f7a26a'] }] },
        options: { plugins: { legend: { position: 'right', labels: { color: MUTED, boxWidth: 10, font: { size: 11 } } } } }
      });
    {% endif %}
    </script>
  </main>
</div>
</body>
</html>
"""


# ---------------------------------------------------------------- routes
@app.route("/", methods=["GET"])
def home():
    return render_template_string(PAGE, r=None, error=None, raw_input="")


@app.route("/analyze", methods=["POST"])
def analyze():
    raw = request.form.get("url", "")
    url = normalize_url(raw)
    if not url:
        return render_template_string(PAGE, r=None, error="Please enter a URL.", raw_input=raw)

    result = analyze_url(url)
    if not result["fetch_ok"]:
        return render_template_string(PAGE, r=None, error=result["error"], raw_input=raw)

    token = uuid.uuid4().hex[:12]
    STORE[token] = result
    while len(STORE) > STORE_LIMIT:
        STORE.pop(next(iter(STORE)))

    return render_template_string(PAGE, r=result, error=None, raw_input=raw, token=token)


@app.route("/download/<token>/csv")
def download(token):
    result = STORE.get(token)
    if result is None:
        abort(404)
    csv_text = build_csv(result)
    buf = io.BytesIO(csv_text.encode("utf-8-sig"))
    return send_file(buf, as_attachment=True, download_name="meta_tags_report.csv", mimetype="text/csv")


if __name__ == "__main__":
    app.run(debug=True, port=8501)
