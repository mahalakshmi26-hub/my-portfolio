"""
Meta Details Extractor — Flask dashboard
Built by Mahalakshmi Marimuthu · Digital Marketing Strategist & AI-Powered SEO Expert

Extracts H1, Meta Title, Meta Description & Meta Keywords from a batch of URLs
(up to 200 at once), flags missing on-page elements and title/description
lengths outside the recommended range, and reports an on-page health score.
"""

import io
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
from bs4 import BeautifulSoup
from flask import Flask, abort, jsonify, render_template_string, request, send_file

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config / limits
# ---------------------------------------------------------------------------
MAX_URLS = 200
FETCH_TIMEOUT = 15
WORKERS = 10
TITLE_MIN, TITLE_MAX = 50, 60
DESC_MIN, DESC_MAX = 120, 160

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}

# in-memory store for download exports (keeps the last 20 runs)
STORE = {}
STORE_LIMIT = 20


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------
def clean_urls(raw: str) -> list:
    urls = []
    for line in re.split(r"[\n,]+", raw):
        u = line.strip()
        if not u:
            continue
        if not u.startswith(("http://", "https://")):
            u = "https://" + u
        if u not in urls:
            urls.append(u)
    return urls


def extract_meta(url: str) -> dict:
    """Fetch one URL and pull out its key on-page SEO elements."""
    row = {
        "url": url, "status": "", "error": None,
        "h1": "", "h1_missing": True,
        "title": "", "title_len": 0, "title_missing": True, "title_len_flag": "",
        "description": "", "desc_len": 0, "desc_missing": True, "desc_len_flag": "",
        "keywords": "",
    }
    try:
        resp = requests.get(url, headers=HEADERS, timeout=FETCH_TIMEOUT, allow_redirects=True)
        row["status"] = str(resp.status_code)
        soup = BeautifulSoup(resp.text, "html.parser")

        h1 = soup.find("h1")
        if h1 and h1.get_text(strip=True):
            row["h1"] = h1.get_text(strip=True)
            row["h1_missing"] = False

        title = soup.find("title")
        if title and title.get_text(strip=True):
            row["title"] = title.get_text(strip=True)
            row["title_len"] = len(row["title"])
            row["title_missing"] = False
            if row["title_len"] < TITLE_MIN:
                row["title_len_flag"] = "short"
            elif row["title_len"] > TITLE_MAX:
                row["title_len_flag"] = "long"

        desc = soup.find("meta", attrs={"name": "description"})
        if desc and (desc.get("content") or "").strip():
            row["description"] = desc["content"].strip()
            row["desc_len"] = len(row["description"])
            row["desc_missing"] = False
            if row["desc_len"] < DESC_MIN:
                row["desc_len_flag"] = "short"
            elif row["desc_len"] > DESC_MAX:
                row["desc_len_flag"] = "long"

        kws = soup.find("meta", attrs={"name": "keywords"})
        if kws and (kws.get("content") or "").strip():
            row["keywords"] = kws["content"].strip()

    except requests.exceptions.Timeout:
        row["status"] = "Timeout"
        row["error"] = "Timeout"
    except requests.exceptions.RequestException as exc:
        row["status"] = "Error"
        row["error"] = str(exc)[:120]
    return row


def run_extraction(urls: list) -> list:
    results = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(extract_meta, u): u for u in urls}
        for fut in as_completed(futures):
            results.append(fut.result())
    order = {u: i for i, u in enumerate(urls)}
    results.sort(key=lambda r: order[r["url"]])
    return results


def summarize(results: list) -> dict:
    total = len(results)
    ok = sum(1 for r in results if r["status"] == "200")
    missing_title = sum(1 for r in results if r["title_missing"])
    missing_desc = sum(1 for r in results if r["desc_missing"])
    missing_h1 = sum(1 for r in results if r["h1_missing"])
    errors = sum(1 for r in results if r["error"])
    complete = sum(
        1 for r in results
        if not r["title_missing"] and not r["desc_missing"] and not r["h1_missing"]
    )
    health = round((complete / total) * 100) if total else 100
    return {
        "total": total,
        "ok": ok,
        "missing_title": missing_title,
        "missing_desc": missing_desc,
        "missing_h1": missing_h1,
        "errors": errors,
        "health": health,
    }


def build_export_df(results: list) -> "pd.DataFrame":
    rows = []
    for r in results:
        rows.append({
            "URL": r["url"],
            "Status": r["error"] or r["status"],
            "H1": r["h1"] or "Missing",
            "Meta Title": r["title"] or "Missing",
            "Title Length": r["title_len"],
            "Meta Description": r["description"] or "Missing",
            "Description Length": r["desc_len"],
            "Meta Keywords": r["keywords"] or "—",
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template_string(PAGE_TEMPLATE, max_urls=MAX_URLS)


@app.route("/api/extract", methods=["POST"])
def api_extract():
    data = request.get_json(force=True, silent=True) or {}
    raw_input = (data.get("input") or "").strip()

    if not raw_input:
        return jsonify({"error": "Please enter at least one URL."}), 400

    urls = clean_urls(raw_input)
    if not urls:
        return jsonify({"error": "No valid http(s) URLs found."}), 400

    truncated = len(urls) > MAX_URLS
    urls = urls[:MAX_URLS]

    try:
        results = run_extraction(urls)
    except Exception as exc:
        return jsonify({"error": f"Extraction failed: {exc}"}), 500

    token = uuid.uuid4().hex[:12]
    STORE[token] = results
    while len(STORE) > STORE_LIMIT:
        STORE.pop(next(iter(STORE)))

    summary = summarize(results)
    summary["truncated"] = truncated
    return jsonify({"summary": summary, "results": results, "token": token})


@app.route("/download/<token>/<fmt>")
def download(token, fmt):
    results = STORE.get(token)
    if results is None:
        abort(404)
    df = build_export_df(results)
    if fmt == "csv":
        buf = io.BytesIO(df.to_csv(index=False).encode("utf-8-sig"))
        return send_file(buf, as_attachment=True, download_name="meta_details.csv", mimetype="text/csv")
    if fmt == "xlsx":
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Meta Details")
        buf.seek(0)
        return send_file(
            buf, as_attachment=True, download_name="meta_details.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    abort(404)


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Frontend (single-file dashboard template)
# ---------------------------------------------------------------------------
PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Meta Details Extractor | SEO Tools by Mahalakshmi</title>
<meta name="description" content="Free bulk meta tag extractor. Pull H1, meta title, description and keywords from up to 200 URLs at once, with character counts and an on-page health score.">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%237c6af7'/%3E%3Ctext x='50%25' y='50%25' dominant-baseline='central' text-anchor='middle' font-family='Georgia%2C serif' font-size='18' font-weight='900' fill='white'%3EM%3C/text%3E%3C/svg%3E">
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;600;700;800;900&family=Inter:wght@300;400;500;600&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  :root {
    --bg: #0a0a0f; --surface: #12121a; --surface2: #1a1a26;
    --accent: #7c6af7; --accent2: #f7a26a; --accent3: #6af7c8;
    --danger: #f76a6a; --text: #e8e8f0; --muted: #8888a8;
    --border: rgba(124,106,247,0.18); --card: rgba(18,18,28,0.85);
  }
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); font-size: 15px; line-height: 1.6; }
  a { color: inherit; }

  .layout { display: flex; min-height: 100vh; }

  /* SIDEBAR */
  .sidebar { width: 240px; flex-shrink: 0; background: var(--surface); border-right: 1px solid var(--border); padding: 1.6rem 1.2rem; position: sticky; top: 0; height: 100vh; display: flex; flex-direction: column; gap: 2rem; }
  .brand { display: flex; align-items: center; gap: 0.55rem; text-decoration: none; }
  .brand-mark { width: 30px; height: 30px; border-radius: 8px; background: var(--accent); display: flex; align-items: center; justify-content: center; font-family: 'Sora', sans-serif; font-weight: 900; font-size: 1rem; color: #fff; flex-shrink: 0; }
  .brand-text { font-family: 'Sora', sans-serif; font-weight: 700; font-size: 0.95rem; }
  .sidebar-section { display: flex; flex-direction: column; gap: 0.3rem; }
  .sidebar-label { font-family: 'DM Mono', monospace; font-size: 0.65rem; letter-spacing: 0.14em; text-transform: uppercase; color: var(--muted); margin-bottom: 0.5rem; }
  .sidebar-link { display: flex; align-items: center; gap: 0.6rem; padding: 0.55rem 0.7rem; border-radius: 8px; font-size: 0.85rem; color: var(--muted); text-decoration: none; transition: all 0.15s; }
  .sidebar-link:hover, .sidebar-link.active { background: rgba(124,106,247,0.12); color: var(--text); }
  .sidebar-limits { margin-top: auto; background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 0.9rem; font-size: 0.72rem; color: var(--muted); line-height: 1.6; }
  .sidebar-limits strong { color: var(--text); }

  /* MAIN */
  .main { flex: 1; padding: 2.2rem 3rem 4rem; max-width: 1180px; }
  .page-head { margin-bottom: 1.8rem; }
  .page-eyebrow { font-family: 'DM Mono', monospace; font-size: 0.68rem; letter-spacing: 0.16em; text-transform: uppercase; color: var(--accent3); margin-bottom: 0.5rem; }
  .page-title { font-family: 'Sora', sans-serif; font-size: 1.7rem; font-weight: 800; margin-bottom: 0.4rem; }
  .page-sub { color: var(--muted); font-size: 0.92rem; max-width: 640px; }

  /* SCAN CARD */
  .scan-card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 1.6rem; margin-bottom: 2rem; }
  .scan-input { width: 100%; background: var(--surface2); border: 1px solid var(--border); border-radius: 8px; color: var(--text); padding: 0.75rem 0.9rem; font-family: 'DM Mono', monospace; font-size: 0.85rem; resize: vertical; min-height: 150px; }
  .scan-input:focus { outline: none; border-color: var(--accent); }
  .scan-hint { font-size: 0.74rem; color: var(--muted); margin-top: 0.5rem; font-family: 'DM Mono', monospace; }
  .scan-actions { margin-top: 1.2rem; display: flex; align-items: center; gap: 1rem; }
  .btn-primary { background: var(--accent); color: #fff; border: none; padding: 0.7rem 1.7rem; border-radius: 8px; font-family: 'Inter', sans-serif; font-weight: 600; font-size: 0.88rem; cursor: pointer; transition: background 0.2s, transform 0.2s; }
  .btn-primary:hover { background: #6a58e8; transform: translateY(-1px); }
  .btn-primary:disabled { opacity: 0.55; cursor: not-allowed; transform: none; }
  .spinner { width: 16px; height: 16px; border: 2px solid rgba(255,255,255,0.35); border-top-color: #fff; border-radius: 50%; animation: spin 0.7s linear infinite; display: inline-block; vertical-align: -3px; margin-right: 0.5rem; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .scan-status { font-size: 0.82rem; color: var(--muted); }
  .error-box { margin-top: 1rem; background: rgba(247,106,106,0.1); border: 1px solid rgba(247,106,106,0.3); color: #ff9d9d; padding: 0.7rem 1rem; border-radius: 8px; font-size: 0.85rem; display: none; }

  /* RESULTS */
  #results { display: none; }
  .summary-row { display: grid; grid-template-columns: 200px repeat(5, 1fr); gap: 1rem; margin-bottom: 1.6rem; }
  .health-card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 1.2rem; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 0.4rem; }
  .health-ring-wrap { position: relative; width: 100px; height: 100px; }
  .health-ring-wrap svg { transform: rotate(-90deg); }
  .health-ring-bg { fill: none; stroke: var(--surface2); stroke-width: 9; }
  .health-ring-fg { fill: none; stroke: var(--accent3); stroke-width: 9; stroke-linecap: round; transition: stroke-dashoffset 0.6s ease; }
  .health-score { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; font-family: 'Sora', sans-serif; font-size: 1.3rem; font-weight: 800; }
  .health-label { font-size: 0.7rem; color: var(--muted); font-family: 'DM Mono', monospace; text-transform: uppercase; letter-spacing: 0.08em; }
  .stat-card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 1.1rem 1.2rem; display: flex; flex-direction: column; justify-content: center; gap: 0.3rem; }
  .stat-num { font-family: 'Sora', sans-serif; font-size: 1.6rem; font-weight: 800; }
  .stat-label { font-size: 0.75rem; color: var(--muted); }
  .stat-card.warn .stat-num { color: var(--accent2); }
  .stat-card.ok .stat-num { color: var(--accent3); }
  .stat-card.error .stat-num { color: var(--danger); }

  .chart-row { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1.6rem; }
  .chart-card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 1.2rem; }
  .chart-card h3 { font-family: 'Sora', sans-serif; font-size: 0.85rem; font-weight: 700; margin-bottom: 0.9rem; }
  .chart-card canvas { max-height: 190px; }

  .results-card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 1.2rem; }
  .results-head { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 0.8rem; margin-bottom: 1rem; }
  .filter-row { display: flex; gap: 0.5rem; flex-wrap: wrap; }
  .filter-btn { background: var(--surface2); border: 1px solid var(--border); color: var(--muted); padding: 0.4rem 0.85rem; border-radius: 100px; font-size: 0.76rem; font-family: 'DM Mono', monospace; cursor: pointer; transition: all 0.15s; }
  .filter-btn.active { background: var(--accent); border-color: var(--accent); color: #fff; }
  .export-row { display: flex; gap: 0.6rem; }
  .btn-export { background: var(--surface2); border: 1px solid var(--border); color: var(--text); padding: 0.45rem 1rem; border-radius: 8px; font-size: 0.78rem; font-weight: 600; cursor: pointer; text-decoration: none; display: inline-flex; align-items: center; }
  .btn-export:hover { border-color: var(--accent); color: var(--accent); }

  table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
  thead th { text-align: left; padding: 0.6rem 0.7rem; color: var(--muted); font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.06em; font-family: 'DM Mono', monospace; border-bottom: 1px solid var(--border); }
  tbody td { padding: 0.65rem 0.7rem; border-bottom: 1px solid rgba(255,255,255,0.04); vertical-align: top; }
  tbody tr:hover { background: rgba(124,106,247,0.05); }
  .url-cell { max-width: 260px; overflow-wrap: anywhere; }
  .url-cell a { text-decoration: none; color: var(--text); }
  .url-cell a:hover { color: var(--accent); }
  .cell-text { max-width: 260px; overflow-wrap: anywhere; }
  .len-badge { display: inline-block; font-family: 'DM Mono', monospace; font-size: 0.66rem; color: var(--muted); margin-top: 0.2rem; }
  .len-badge.flag { color: var(--accent2); }
  .badge { display: inline-block; font-family: 'DM Mono', monospace; font-size: 0.68rem; padding: 0.22rem 0.6rem; border-radius: 100px; font-weight: 600; }
  .badge.ok { background: rgba(106,247,200,0.12); color: var(--accent3); border: 1px solid rgba(106,247,200,0.25); }
  .badge.missing { background: rgba(247,106,106,0.12); color: var(--danger); border: 1px solid rgba(247,106,106,0.3); }
  .empty-state { text-align: center; padding: 3rem 1rem; color: var(--muted); font-size: 0.88rem; }

  @media (max-width: 900px) {
    .sidebar { display: none; }
    .main { padding: 1.6rem 1.2rem 3rem; }
    .summary-row { grid-template-columns: 1fr 1fr; }
    .chart-row { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<div class="layout">

  <aside class="sidebar">
    <a href="https://mahalakshmi26-hub.github.io/my-portfolio" class="brand">
      <span class="brand-mark">M</span>
      <span class="brand-text">SEO Tools</span>
    </a>
    <div class="sidebar-section">
      <div class="sidebar-label">Tool</div>
      <a href="#" class="sidebar-link active">🔍 Meta Details Extractor</a>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-label">Navigate</div>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="sidebar-link">← All Tools</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio" class="sidebar-link">Portfolio Home</a>
    </div>
    <div class="sidebar-limits">
      Extracts on-page SEO data from up to <strong>{{ max_urls }} URLs</strong> per run, fetched in parallel so results stay fast.
    </div>
  </aside>

  <main class="main">
    <div class="page-head">
      <div class="page-eyebrow">// seo tool</div>
      <div class="page-title">Meta Details Extractor</div>
      <div class="page-sub">Pull H1, Meta Title, Meta Description and Meta Keywords from a batch of URLs — with character counts, missing-tag flags, and an on-page health score.</div>
    </div>

    <div class="scan-card">
      <label class="scan-hint" for="urlsInput" style="display:block;margin-bottom:0.5rem;">Paste URLs (one per line, or comma-separated)</label>
      <textarea id="urlsInput" class="scan-input" placeholder="https://example.com&#10;https://example.com/page-2&#10;example.com/page-3"></textarea>
      <div class="scan-hint">https:// is added automatically if you skip it — duplicates are removed.</div>

      <div class="scan-actions">
        <button class="btn-primary" id="extractBtn" onclick="startExtract()">Extract Details</button>
        <span class="scan-status" id="scanStatus"></span>
      </div>
      <div class="error-box" id="errorBox"></div>
    </div>

    <div id="results">
      <div class="summary-row">
        <div class="health-card">
          <div class="health-ring-wrap">
            <svg width="100" height="100" viewBox="0 0 100 100">
              <circle class="health-ring-bg" cx="50" cy="50" r="42"></circle>
              <circle class="health-ring-fg" id="healthRing" cx="50" cy="50" r="42" stroke-dasharray="264" stroke-dashoffset="264"></circle>
            </svg>
            <div class="health-score" id="healthScoreText">0%</div>
          </div>
          <div class="health-label">On-Page Health</div>
        </div>
        <div class="stat-card"><div class="stat-num" id="statTotal">0</div><div class="stat-label">URLs processed</div></div>
        <div class="stat-card ok"><div class="stat-num" id="statOk">0</div><div class="stat-label">Successful (200)</div></div>
        <div class="stat-card warn"><div class="stat-num" id="statMissingTitle">0</div><div class="stat-label">Missing titles</div></div>
        <div class="stat-card warn"><div class="stat-num" id="statMissingDesc">0</div><div class="stat-label">Missing descriptions</div></div>
        <div class="stat-card error"><div class="stat-num" id="statMissingH1">0</div><div class="stat-label">Missing H1s</div></div>
      </div>

      <div class="chart-row">
        <div class="chart-card">
          <h3>On-Page Element Coverage</h3>
          <canvas id="coverageChart"></canvas>
        </div>
        <div class="chart-card">
          <h3>Fetch Status</h3>
          <canvas id="statusChart"></canvas>
        </div>
      </div>

      <div class="results-card">
        <div class="results-head">
          <div class="filter-row">
            <button class="filter-btn active" data-filter="all" onclick="setFilter('all')">All</button>
            <button class="filter-btn" data-filter="missing_title" onclick="setFilter('missing_title')">Missing Title</button>
            <button class="filter-btn" data-filter="missing_desc" onclick="setFilter('missing_desc')">Missing Description</button>
            <button class="filter-btn" data-filter="missing_h1" onclick="setFilter('missing_h1')">Missing H1</button>
            <button class="filter-btn" data-filter="errors" onclick="setFilter('errors')">Errors</button>
          </div>
          <div class="export-row">
            <a class="btn-export" onclick="exportCsv()">⬇ Export CSV</a>
            <a class="btn-export" id="excelBtn" onclick="downloadExcel()">⬇ Download Excel</a>
          </div>
        </div>
        <div style="overflow-x:auto;">
          <table>
            <thead>
              <tr><th>URL</th><th>Status</th><th>H1</th><th>Meta Title</th><th>Meta Description</th><th>Meta Keywords</th></tr>
            </thead>
            <tbody id="resultsBody"></tbody>
          </table>
        </div>
        <div class="empty-state" id="emptyState" style="display:none;">No URLs match this filter.</div>
      </div>
    </div>
  </main>
</div>

<script>
let currentFilter = 'all';
let allResults = [];
let currentToken = null;
let coverageChartInstance = null;
let statusChartInstance = null;

function setFilter(filter) {
  currentFilter = filter;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.toggle('active', b.dataset.filter === filter));
  renderTable();
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : s;
  return d.innerHTML;
}

async function startExtract() {
  const input = document.getElementById('urlsInput').value.trim();
  const errorBox = document.getElementById('errorBox');
  errorBox.style.display = 'none';

  if (!input) {
    errorBox.textContent = 'Please enter at least one URL.';
    errorBox.style.display = 'block';
    return;
  }

  const btn = document.getElementById('extractBtn');
  const status = document.getElementById('scanStatus');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Extracting…';
  status.textContent = 'Fetching pages in parallel — this can take a little while for larger batches.';
  document.getElementById('results').style.display = 'none';

  try {
    const resp = await fetch('/api/extract', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ input: input })
    });
    const data = await resp.json();

    if (!resp.ok) {
      errorBox.textContent = data.error || 'Something went wrong. Please try again.';
      errorBox.style.display = 'block';
      return;
    }

    allResults = data.results;
    currentToken = data.token;
    renderSummary(data.summary);
    renderCharts(data.summary);
    currentFilter = 'all';
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.toggle('active', b.dataset.filter === 'all'));
    renderTable();
    document.getElementById('results').style.display = 'block';
    status.textContent = data.summary.truncated
      ? `Processed the first ${data.summary.total} URLs (limit is {{ max_urls }} per run).`
      : `Processed ${data.summary.total} URL(s).`;
  } catch (err) {
    errorBox.textContent = 'Network error — please try again in a moment.';
    errorBox.style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Extract Details';
  }
}

function renderSummary(summary) {
  document.getElementById('statTotal').textContent = summary.total;
  document.getElementById('statOk').textContent = summary.ok;
  document.getElementById('statMissingTitle').textContent = summary.missing_title;
  document.getElementById('statMissingDesc').textContent = summary.missing_desc;
  document.getElementById('statMissingH1').textContent = summary.missing_h1;
  document.getElementById('healthScoreText').textContent = summary.health + '%';

  const circumference = 264;
  const offset = circumference - (summary.health / 100) * circumference;
  const ring = document.getElementById('healthRing');
  ring.style.strokeDashoffset = offset;
  ring.style.stroke = summary.health >= 90 ? '#6af7c8' : summary.health >= 70 ? '#f7a26a' : '#f76a6a';
}

function renderCharts(summary) {
  const coverageCtx = document.getElementById('coverageChart');
  const statusCtx = document.getElementById('statusChart');

  if (coverageChartInstance) coverageChartInstance.destroy();
  if (statusChartInstance) statusChartInstance.destroy();

  const chartFont = { family: 'Inter', size: 11 };

  coverageChartInstance = new Chart(coverageCtx, {
    type: 'bar',
    data: {
      labels: ['H1', 'Meta Title', 'Meta Description'],
      datasets: [
        { label: 'Present', data: [summary.total - summary.missing_h1, summary.total - summary.missing_title, summary.total - summary.missing_desc], backgroundColor: '#6af7c8', borderRadius: 6 },
        { label: 'Missing', data: [summary.missing_h1, summary.missing_title, summary.missing_desc], backgroundColor: '#f76a6a', borderRadius: 6 }
      ]
    },
    options: {
      scales: {
        x: { stacked: true, ticks: { color: '#c8c8e0', font: chartFont }, grid: { display: false } },
        y: { stacked: true, ticks: { color: '#c8c8e0', font: chartFont, precision: 0 }, grid: { color: 'rgba(255,255,255,0.06)' } }
      },
      plugins: { legend: { position: 'bottom', labels: { color: '#c8c8e0', font: chartFont, padding: 12 } } }
    }
  });

  const errors = summary.errors || 0;
  const otherStatus = Math.max(summary.total - summary.ok - errors, 0);

  statusChartInstance = new Chart(statusCtx, {
    type: 'doughnut',
    data: {
      labels: ['200 OK', 'Other status', 'Errors / timeouts'],
      datasets: [{ data: [summary.ok, otherStatus, errors], backgroundColor: ['#6af7c8', '#f7a26a', '#f76a6a'], borderWidth: 0 }]
    },
    options: { plugins: { legend: { position: 'bottom', labels: { color: '#c8c8e0', font: chartFont, padding: 12 } } }, cutout: '65%' }
  });
}

function matchesFilter(r, filter) {
  if (filter === 'all') return true;
  if (filter === 'missing_title') return r.title_missing;
  if (filter === 'missing_desc') return r.desc_missing;
  if (filter === 'missing_h1') return r.h1_missing;
  if (filter === 'errors') return !!r.error;
  return true;
}

function lenBadge(len, flag) {
  if (!len) return '';
  return `<span class="len-badge ${flag ? 'flag' : ''}">${len} chars${flag ? ' (' + flag + ')' : ''}</span>`;
}

function renderTable() {
  const tbody = document.getElementById('resultsBody');
  const emptyState = document.getElementById('emptyState');
  const filtered = allResults.filter(r => matchesFilter(r, currentFilter));

  tbody.innerHTML = '';
  emptyState.style.display = filtered.length ? 'none' : 'block';

  filtered.forEach(r => {
    const tr = document.createElement('tr');
    const statusLabel = r.error ? r.error : (r.status || '—');

    tr.innerHTML = `
      <td class="url-cell"><a href="${r.url}" target="_blank" rel="noopener">${escapeHtml(r.url)}</a></td>
      <td>${escapeHtml(statusLabel)}</td>
      <td class="cell-text">${r.h1_missing ? '<span class="badge missing">Missing</span>' : escapeHtml(r.h1)}</td>
      <td class="cell-text">${r.title_missing ? '<span class="badge missing">Missing</span>' : escapeHtml(r.title) + '<br>' + lenBadge(r.title_len, r.title_len_flag)}</td>
      <td class="cell-text">${r.desc_missing ? '<span class="badge missing">Missing</span>' : escapeHtml(r.description) + '<br>' + lenBadge(r.desc_len, r.desc_len_flag)}</td>
      <td class="cell-text">${r.keywords ? escapeHtml(r.keywords) : '—'}</td>
    `;
    tbody.appendChild(tr);
  });
}

function exportCsv() {
  if (!allResults.length) return;
  const rows = [['URL', 'Status', 'H1', 'Meta Title', 'Title Length', 'Meta Description', 'Description Length', 'Meta Keywords']];
  allResults.forEach(r => {
    rows.push([
      r.url, r.error || r.status, r.h1_missing ? 'Missing' : r.h1,
      r.title_missing ? 'Missing' : r.title, r.title_len,
      r.desc_missing ? 'Missing' : r.description, r.desc_len,
      r.keywords || ''
    ]);
  });
  const csv = rows.map(row => row.map(cell => `"${String(cell).replace(/"/g, '""')}"`).join(',')).join('\\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = 'meta_details.csv';
  link.click();
}

function downloadExcel() {
  if (!currentToken) return;
  window.location.href = `/download/${currentToken}/xlsx`;
}
</script>
</body>
</html>"""

if __name__ == "__main__":
    app.run(debug=True, port=5001)
