"""
Link Analyser — on-page link analysis tool.
Given a URL, fetches that single page and reports on the links found IN it:
outbound link count, internal vs external split, dofollow vs nofollow split,
and a full filterable list. Pure on-page analysis — no backlink discovery,
no third-party/paid API.

Built by Mahalakshmi Marimuthu.
"""

import os
import time
import re
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify

app = Flask(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT = 15
RETRY_STATUS_CODES = {403, 429, 503}
NON_WEB_SCHEMES = {"mailto", "tel", "javascript", "sms", "whatsapp", "fax"}


def normalize_host(netloc: str) -> str:
    """Lowercase a netloc and strip a leading www. and any port for comparison."""
    host = netloc.lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def normalize_input_url(raw_url: str) -> str:
    raw_url = raw_url.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", raw_url):
        raw_url = "https://" + raw_url
    return raw_url


def fetch_page(url: str) -> requests.Response:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    last_exc = None
    for attempt in range(2):
        try:
            resp = requests.get(
                url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True
            )
            if resp.status_code in RETRY_STATUS_CODES and attempt == 0:
                time.sleep(1.2)
                continue
            return resp
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(0.8)
                continue
    raise last_exc


def classify_link(href: str, base_url: str, page_host: str):
    """Return (resolved_url, category, note) for a single href.

    category is one of: internal, external, other
    """
    href = (href or "").strip()
    if not href:
        return None, None, None

    # Same-page jump link, e.g. "#section"
    if href.startswith("#"):
        return href, "other", "same-page anchor"

    # Scheme-based non-web links
    scheme_match = re.match(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):", href)
    if scheme_match and scheme_match.group(1).lower() in NON_WEB_SCHEMES:
        return href, "other", scheme_match.group(1).lower()

    resolved = urljoin(base_url, href)
    parsed = urlparse(resolved)

    if parsed.scheme not in ("http", "https"):
        return resolved, "other", parsed.scheme or "unknown"

    link_host = normalize_host(parsed.netloc)
    if not link_host:
        return resolved, "other", "no-host"

    category = "internal" if link_host == page_host else "external"
    return resolved, category, None


def anchor_label(a_tag) -> str:
    text = a_tag.get_text(strip=True)
    if text:
        return text[:200]
    img = a_tag.find("img")
    if img is not None:
        alt = (img.get("alt") or "").strip()
        if alt:
            return f"[image: {alt[:180]}]"
        return "[image, no alt text]"
    return "[no anchor text]"


def analyze_url(raw_url: str) -> dict:
    url = normalize_input_url(raw_url)
    parsed_input = urlparse(url)
    if not parsed_input.netloc:
        raise ValueError("That doesn't look like a valid URL.")

    resp = fetch_page(url)

    if resp.status_code >= 400:
        raise ValueError(
            f"The page returned HTTP {resp.status_code}, so it couldn't be analyzed. "
            f"If you're sure the URL is correct, the site may be blocking automated requests."
        )

    content_type = resp.headers.get("Content-Type", "")
    if "html" not in content_type.lower() and resp.text[:200].strip()[:15].lower() != "<!doctype html":
        raise ValueError(
            f"That URL didn't return an HTML page (content-type: {content_type or 'unknown'})."
        )

    final_url = resp.url
    page_host = normalize_host(urlparse(final_url).netloc)

    soup = BeautifulSoup(resp.text, "html.parser")
    anchors = soup.find_all("a")

    links = []
    seen_external_hosts = set()

    for a in anchors:
        href = a.get("href")
        if href is None:
            continue
        resolved, category, note = classify_link(href, final_url, page_host)
        if resolved is None:
            continue

        rel_attr = a.get("rel") or []
        if isinstance(rel_attr, str):
            rel_attr = rel_attr.split()
        rel_lower = [r.lower() for r in rel_attr]

        is_nofollow = "nofollow" in rel_lower
        follow = None
        if category in ("internal", "external"):
            follow = "nofollow" if is_nofollow else "dofollow"

        entry = {
            "anchor_text": anchor_label(a),
            "url": resolved,
            "category": category,          # internal | external | other
            "follow": follow,               # dofollow | nofollow | None
            "rel": " ".join(rel_lower) if rel_lower else "",
            "sponsored": "sponsored" in rel_lower,
            "ugc": "ugc" in rel_lower,
            "target_blank": (a.get("target") or "").lower() == "_blank",
            "note": note,
        }
        links.append(entry)

        if category == "external":
            seen_external_hosts.add(normalize_host(urlparse(resolved).netloc))

    web_links = [l for l in links if l["category"] in ("internal", "external")]
    internal_links = [l for l in web_links if l["category"] == "internal"]
    external_links = [l for l in web_links if l["category"] == "external"]
    dofollow_links = [l for l in web_links if l["follow"] == "dofollow"]
    nofollow_links = [l for l in web_links if l["follow"] == "nofollow"]
    other_links = [l for l in links if l["category"] == "other"]

    total_web = len(web_links)
    dofollow_ratio = round((len(dofollow_links) / total_web) * 100, 1) if total_web else 0.0

    return {
        "input_url": raw_url,
        "analyzed_url": final_url,
        "page_host": page_host,
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "summary": {
            "total_links_found": len(links),
            "outbound_web_links": total_web,
            "internal_count": len(internal_links),
            "external_count": len(external_links),
            "dofollow_count": len(dofollow_links),
            "nofollow_count": len(nofollow_links),
            "other_count": len(other_links),
            "unique_external_domains": len(seen_external_hosts),
            "dofollow_ratio": dofollow_ratio,
        },
        "links": links,
    }


@app.route("/")
def index():
    return HTML_PAGE


@app.route("/analyze", methods=["POST"])
def analyze():
    data = request.get_json(silent=True) or {}
    raw_url = (data.get("url") or "").strip()
    if not raw_url:
        return jsonify({"error": "Please enter a URL to analyze."}), 400

    try:
        result = analyze_url(raw_url)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except requests.exceptions.Timeout:
        return jsonify({"error": "The page took too long to respond (timed out after 15s)."}), 504
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Couldn't connect to that URL. Check it's correct and publicly reachable."}), 502
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Couldn't fetch that page ({e.__class__.__name__})."}), 502
    except Exception as e:
        return jsonify({"error": f"Something went wrong while analyzing that page: {e}"}), 500

    return jsonify(result)


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Link Analyser | SEO Tools by Mahalakshmi</title>
<meta name="description" content="Free on-page link analyser — paste any URL and see its outbound links broken down by internal vs external and dofollow vs nofollow.">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%237c6af7'/%3E%3Ctext x='50%25' y='50%25' dominant-baseline='central' text-anchor='middle' font-family='Georgia%2C serif' font-size='18' font-weight='900' fill='white'%3EM%3C/text%3E%3C/svg%3E">
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;600;700;800;900&family=Inter:wght@300;400;500;600&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0a0a0f; --surface: #12121a; --surface2: #14141c; --surface3: #1a1a26;
    --accent: #7c6af7; --accent-hover: #6a58e8; --accent2: #f7a26a; --accent3: #6af7c8;
    --danger: #f76a6a; --text: #e8e8f0; --muted: #8888a8; --border: rgba(124,106,247,0.18);
  }
  * , *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); font-size: 15px; line-height: 1.6; }
  a { color: inherit; }

  .layout { display: flex; min-height: 100vh; }

  /* SIDEBAR */
  .sidebar { width: 260px; flex-shrink: 0; background: var(--surface); border-right: 1px solid var(--border); display: flex; flex-direction: column; padding: 1.5rem 1.1rem; position: sticky; top: 0; height: 100vh; overflow-y: auto; }
  .brand { display: flex; align-items: center; gap: 0.6rem; text-decoration: none; margin-bottom: 2rem; }
  .brand-mark { width: 30px; height: 30px; border-radius: 8px; background: var(--accent); display: flex; align-items: center; justify-content: center; font-family: 'Sora', sans-serif; font-weight: 900; color: #fff; flex-shrink: 0; }
  .brand-text { font-family: 'Sora', sans-serif; font-weight: 700; font-size: 0.98rem; color: var(--text); }
  .sidebar-section { margin-bottom: 1.6rem; }
  .sidebar-label { font-family: 'DM Mono', monospace; font-size: 0.66rem; letter-spacing: 0.14em; text-transform: uppercase; color: var(--muted); margin-bottom: 0.6rem; padding: 0 0.6rem; }
  .sidebar-link { display: flex; align-items: center; gap: 0.55rem; padding: 0.55rem 0.7rem; border-radius: 7px; text-decoration: none; color: var(--muted); font-size: 0.87rem; font-weight: 500; border: 1px solid transparent; margin-bottom: 0.15rem; transition: all 0.15s; }
  .sidebar-link:hover { color: var(--text); background: rgba(255,255,255,0.03); }
  .sidebar-link.active { background: rgba(124,106,247,0.14); border: 1px solid rgba(124,106,247,0.4); color: var(--accent); font-weight: 700; }
  .sidebar-info { background: var(--surface2); border: 1px solid var(--border); border-radius: 10px; padding: 0.9rem; font-size: 0.76rem; color: var(--muted); line-height: 1.6; margin-bottom: 1.6rem; }
  .sidebar-info strong { color: var(--text); }
  .sidebar-footer { margin-top: auto; padding-top: 1.2rem; border-top: 1px solid var(--border); font-size: 0.74rem; color: var(--muted); line-height: 1.6; }
  .credit-name { color: var(--accent3); font-weight: 700; text-decoration: none; }
  .sidebar-social { display: flex; gap: 0.5rem; margin-top: 0.7rem; }
  .sidebar-social a { width: 28px; height: 28px; border-radius: 6px; background: rgba(106,247,200,0.08); border: 1px solid rgba(106,247,200,0.35); color: var(--accent3); display: flex; align-items: center; justify-content: center; text-decoration: none; font-family: 'DM Mono', monospace; font-size: 0.72rem; transition: all 0.15s; }
  .sidebar-social a:hover { background: rgba(106,247,200,0.18); }

  /* MAIN */
  .main { flex: 1; padding: 2.2rem 2.5rem 4rem; max-width: 1200px; }
  .page-title { font-family: 'Sora', sans-serif; font-size: 1.7rem; font-weight: 800; margin-bottom: 0.35rem; }
  .page-subtitle { color: var(--muted); font-size: 0.92rem; margin-bottom: 1.8rem; max-width: 620px; }

  .search-card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 1.3rem; display: flex; gap: 0.7rem; margin-bottom: 1.8rem; }
  .search-card input { flex: 1; background: var(--surface2); border: 1px solid var(--border); border-radius: 8px; padding: 0.7rem 0.9rem; color: var(--text); font-size: 0.92rem; font-family: 'Inter', sans-serif; }
  .search-card input:focus { outline: none; border-color: var(--accent); }
  .search-card button { background: var(--accent); color: #fff; border: none; border-radius: 8px; padding: 0.7rem 1.4rem; font-size: 0.9rem; font-weight: 600; cursor: pointer; font-family: 'Inter', sans-serif; transition: background 0.15s; white-space: nowrap; }
  .search-card button:hover { background: var(--accent-hover); }
  .search-card button:disabled { opacity: 0.6; cursor: not-allowed; }

  .status-line { font-family: 'DM Mono', monospace; font-size: 0.78rem; color: var(--muted); margin-bottom: 1.2rem; min-height: 1.2em; }
  .status-line.error { color: var(--danger); }
  .status-line a { color: var(--accent3); text-decoration: underline; }

  .hidden { display: none !important; }

  /* STATS */
  .stats-row { display: grid; grid-template-columns: repeat(6, 1fr); gap: 0.9rem; margin-bottom: 1.6rem; }
  .stat-card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 1rem; }
  .stat-value { font-family: 'Sora', sans-serif; font-size: 1.55rem; font-weight: 800; }
  .stat-label { font-size: 0.72rem; color: var(--muted); margin-top: 0.25rem; }
  .stat-card.internal .stat-value { color: var(--accent); }
  .stat-card.external .stat-value { color: var(--accent2); }
  .stat-card.dofollow .stat-value { color: var(--accent3); }
  .stat-card.nofollow .stat-value { color: var(--text); }

  /* PANELS */
  .panels-row { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 1rem; margin-bottom: 1.6rem; }
  .panel { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 1.2rem; }
  .panel-title { font-family: 'Sora', sans-serif; font-size: 0.85rem; font-weight: 700; margin-bottom: 0.9rem; }

  .ring-wrap { display: flex; align-items: center; justify-content: center; gap: 1.1rem; }
  .ring-center-label { text-align: center; }
  .ring-center-value { font-family: 'Sora', sans-serif; font-size: 1.5rem; font-weight: 800; color: var(--accent3); }
  .ring-center-sub { font-size: 0.68rem; color: var(--muted); margin-top: 0.15rem; }
  .ring-legend { font-size: 0.76rem; color: var(--muted); line-height: 1.8; }
  .ring-legend span.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 0.4rem; }

  .chart-box { position: relative; height: 160px; }

  /* TABLE */
  .table-card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 1.3rem; }
  .table-header { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 0.8rem; margin-bottom: 1.1rem; }
  .table-title { font-family: 'Sora', sans-serif; font-size: 0.95rem; font-weight: 700; }
  .filter-row { display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: center; }
  .filter-btn { background: var(--surface2); border: 1px solid var(--border); color: var(--muted); font-size: 0.76rem; padding: 0.4rem 0.85rem; border-radius: 100px; cursor: pointer; font-family: 'DM Mono', monospace; transition: all 0.15s; }
  .filter-btn:hover { color: var(--text); }
  .filter-btn.active { background: rgba(124,106,247,0.16); border-color: var(--accent); color: var(--accent); }
  .table-search { background: var(--surface2); border: 1px solid var(--border); border-radius: 8px; padding: 0.45rem 0.8rem; color: var(--text); font-size: 0.82rem; font-family: 'Inter', sans-serif; width: 200px; }
  .table-search:focus { outline: none; border-color: var(--accent); }
  .export-btn { background: var(--surface2); border: 1px solid var(--border); color: var(--text); font-size: 0.78rem; padding: 0.45rem 0.9rem; border-radius: 8px; cursor: pointer; font-family: 'Inter', sans-serif; font-weight: 600; }
  .export-btn:hover { border-color: var(--accent3); color: var(--accent3); }

  .link-table-wrap { max-height: 520px; overflow-y: auto; border: 1px solid var(--border); border-radius: 8px; }
  table.link-table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
  table.link-table thead th { position: sticky; top: 0; background: var(--surface2); text-align: left; padding: 0.6rem 0.8rem; font-family: 'DM Mono', monospace; font-size: 0.68rem; letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted); border-bottom: 1px solid var(--border); }
  table.link-table td { padding: 0.6rem 0.8rem; border-bottom: 1px solid rgba(124,106,247,0.08); vertical-align: top; }
  table.link-table tr:last-child td { border-bottom: none; }
  table.link-table td.anchor-cell { max-width: 220px; }
  table.link-table td.url-cell { max-width: 340px; word-break: break-all; color: var(--muted); font-family: 'DM Mono', monospace; font-size: 0.74rem; }
  table.link-table td.url-cell a { color: var(--muted); text-decoration: none; }
  table.link-table td.url-cell a:hover { color: var(--accent3); text-decoration: underline; }

  .badge { display: inline-block; font-family: 'DM Mono', monospace; font-size: 0.68rem; padding: 0.2rem 0.55rem; border-radius: 100px; white-space: nowrap; }
  .badge.internal { background: rgba(124,106,247,0.12); border: 1px solid rgba(124,106,247,0.3); color: var(--accent); }
  .badge.external { background: rgba(247,162,106,0.12); border: 1px solid rgba(247,162,106,0.3); color: var(--accent2); }
  .badge.dofollow { background: rgba(106,247,200,0.12); border: 1px solid rgba(106,247,200,0.3); color: var(--accent3); }
  .badge.nofollow { background: rgba(136,136,168,0.14); border: 1px solid rgba(136,136,168,0.35); color: var(--muted); }

  .empty-state { text-align: center; padding: 3.5rem 1rem; color: var(--muted); }
  .empty-state-icon { font-size: 2.2rem; margin-bottom: 0.8rem; }
  .spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid rgba(124,106,247,0.3); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.7s linear infinite; vertical-align: middle; margin-right: 0.4rem; }
  @keyframes spin { to { transform: rotate(360deg); } }

  @media (max-width: 1100px) {
    .stats-row { grid-template-columns: repeat(3, 1fr); }
    .panels-row { grid-template-columns: 1fr; }
  }
  @media (max-width: 800px) {
    .sidebar { display: none; }
    .main { padding: 1.5rem 1.2rem 3rem; }
    .stats-row { grid-template-columns: repeat(2, 1fr); }
  }
</style>
</head>
<body>

<div class="layout">
  <aside class="sidebar">
    <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="brand">
      <span class="brand-mark">M</span>
      <span class="brand-text">SEO Tools</span>
    </a>
    <div class="sidebar-section">
      <div class="sidebar-label">Tool</div>
      <a href="#" class="sidebar-link active">🔗 Link Analyser</a>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-label">Navigate</div>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="sidebar-link">🧰 All My Tools</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio" class="sidebar-link">👩‍💻 Portfolio</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/blog/index.html" class="sidebar-link">📝 Blog Posts</a>
    </div>
    <div class="sidebar-info">
      <strong>What this checks:</strong> only the links found on the one page you enter — internal vs external, dofollow vs nofollow. It's on-page link analysis, not a backlink index, so it can't tell you who links <em>to</em> you.
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
    <h1 class="page-title">Link Analyser</h1>
    <p class="page-subtitle">Paste any URL and see every link on that page broken down by internal vs external, and dofollow vs nofollow — useful for auditing your own pages or checking how a competitor structures internal linking.</p>

    <div class="search-card">
      <input type="text" id="urlInput" placeholder="e.g. bankbazaar.com/personal-loan.html" autocomplete="off">
      <button id="analyzeBtn" onclick="runAnalysis()">Analyze Page</button>
    </div>
    <div class="status-line" id="statusLine"></div>

    <div id="resultsWrap" class="hidden">
      <div class="stats-row">
        <div class="stat-card">
          <div class="stat-value" id="statTotal">0</div>
          <div class="stat-label">Total Links Found</div>
        </div>
        <div class="stat-card internal">
          <div class="stat-value" id="statInternal">0</div>
          <div class="stat-label">Internal Links</div>
        </div>
        <div class="stat-card external">
          <div class="stat-value" id="statExternal">0</div>
          <div class="stat-label">External Links</div>
        </div>
        <div class="stat-card dofollow">
          <div class="stat-value" id="statDofollow">0</div>
          <div class="stat-label">Dofollow</div>
        </div>
        <div class="stat-card nofollow">
          <div class="stat-value" id="statNofollow">0</div>
          <div class="stat-label">Nofollow</div>
        </div>
        <div class="stat-card">
          <div class="stat-value" id="statDomains">0</div>
          <div class="stat-label">External Domains</div>
        </div>
      </div>

      <div class="panels-row">
        <div class="panel">
          <div class="panel-title">Dofollow Ratio</div>
          <div class="ring-wrap">
            <div class="chart-box" style="height:120px;width:120px;">
              <canvas id="ratioRing"></canvas>
            </div>
            <div class="ring-center-label">
              <div class="ring-center-value" id="ratioValue">0%</div>
              <div class="ring-center-sub">of outbound links</div>
            </div>
          </div>
        </div>
        <div class="panel">
          <div class="panel-title">Internal vs External</div>
          <div class="chart-box"><canvas id="ieChart"></canvas></div>
        </div>
        <div class="panel">
          <div class="panel-title">Dofollow vs Nofollow</div>
          <div class="chart-box"><canvas id="dnChart"></canvas></div>
        </div>
      </div>

      <div class="table-card">
        <div class="table-header">
          <div class="table-title">All Links <span id="tableCount" style="color:var(--muted);font-weight:400;"></span></div>
          <div class="filter-row">
            <button class="filter-btn active" data-filter="all" onclick="setFilter('all')">All</button>
            <button class="filter-btn" data-filter="internal" onclick="setFilter('internal')">Internal</button>
            <button class="filter-btn" data-filter="external" onclick="setFilter('external')">External</button>
            <button class="filter-btn" data-filter="dofollow" onclick="setFilter('dofollow')">Dofollow</button>
            <button class="filter-btn" data-filter="nofollow" onclick="setFilter('nofollow')">Nofollow</button>
            <input type="text" class="table-search" id="tableSearch" placeholder="Search anchor text or URL…" oninput="renderTable()">
            <button class="export-btn" onclick="exportCsv()">⬇ Export CSV</button>
          </div>
        </div>
        <div class="link-table-wrap">
          <table class="link-table">
            <thead>
              <tr>
                <th>Anchor Text</th>
                <th>URL</th>
                <th>Type</th>
                <th>Follow</th>
              </tr>
            </thead>
            <tbody id="tableBody"></tbody>
          </table>
        </div>
      </div>
    </div>

    <div id="emptyState" class="empty-state">
      <div class="empty-state-icon">🔗</div>
      <p>Enter a URL above and click <strong>Analyze Page</strong> to see its link breakdown.</p>
    </div>
  </main>
</div>

<script>
let currentData = null;
let currentFilter = 'all';
let ringChart, ieChart, dnChart;

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

async function runAnalysis() {
  const input = document.getElementById('urlInput').value.trim();
  const statusLine = document.getElementById('statusLine');
  const btn = document.getElementById('analyzeBtn');
  if (!input) {
    statusLine.textContent = 'Please enter a URL first.';
    statusLine.classList.add('error');
    return;
  }
  statusLine.classList.remove('error');
  statusLine.innerHTML = '<span class="spinner"></span> Fetching and analyzing the page…';
  btn.disabled = true;
  btn.textContent = 'Analyzing…';

  try {
    const res = await fetch('/analyze', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url: input})
    });
    const data = await res.json();
    if (!res.ok) {
      statusLine.textContent = data.error || 'Something went wrong.';
      statusLine.classList.add('error');
      document.getElementById('resultsWrap').classList.add('hidden');
      document.getElementById('emptyState').classList.remove('hidden');
      return;
    }
    currentData = data;
    statusLine.classList.remove('error');
    statusLine.innerHTML = `Analyzed <a href="${escapeHtml(data.analyzed_url)}" target="_blank">${escapeHtml(data.analyzed_url)}</a> · ${data.fetched_at}`;
    document.getElementById('emptyState').classList.add('hidden');
    document.getElementById('resultsWrap').classList.remove('hidden');
    currentFilter = 'all';
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.toggle('active', b.dataset.filter === 'all'));
    document.getElementById('tableSearch').value = '';
    renderResults();
  } catch (err) {
    statusLine.textContent = 'Network error — could not reach the analyzer.';
    statusLine.classList.add('error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Analyze Page';
  }
}

document.getElementById('urlInput').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') runAnalysis();
});

function renderResults() {
  const s = currentData.summary;
  document.getElementById('statTotal').textContent = s.total_links_found;
  document.getElementById('statInternal').textContent = s.internal_count;
  document.getElementById('statExternal').textContent = s.external_count;
  document.getElementById('statDofollow').textContent = s.dofollow_count;
  document.getElementById('statNofollow').textContent = s.nofollow_count;
  document.getElementById('statDomains').textContent = s.unique_external_domains;
  document.getElementById('ratioValue').textContent = s.dofollow_ratio + '%';

  drawRing(s.dofollow_ratio);
  drawIeChart(s.internal_count, s.external_count);
  drawDnChart(s.dofollow_count, s.nofollow_count);
  renderTable();
}

function drawRing(pct) {
  if (ringChart) ringChart.destroy();
  const ctx = document.getElementById('ratioRing').getContext('2d');
  ringChart = new Chart(ctx, {
    type: 'doughnut',
    data: {
      datasets: [{
        data: [pct, 100 - pct],
        backgroundColor: ['#6af7c8', 'rgba(136,136,168,0.18)'],
        borderWidth: 0
      }]
    },
    options: {
      cutout: '78%',
      plugins: { legend: { display: false }, tooltip: { enabled: false } },
      responsive: true, maintainAspectRatio: false
    }
  });
}

function drawIeChart(internal, external) {
  if (ieChart) ieChart.destroy();
  const ctx = document.getElementById('ieChart').getContext('2d');
  ieChart = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: ['Internal', 'External'],
      datasets: [{ data: [internal, external], backgroundColor: ['#7c6af7', '#f7a26a'], borderWidth: 0 }]
    },
    options: {
      cutout: '65%',
      plugins: { legend: { position: 'bottom', labels: { color: '#8888a8', font: { family: 'Inter', size: 11 }, boxWidth: 10, padding: 12 } } },
      responsive: true, maintainAspectRatio: false
    }
  });
}

function drawDnChart(dofollow, nofollow) {
  if (dnChart) dnChart.destroy();
  const ctx = document.getElementById('dnChart').getContext('2d');
  dnChart = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: ['Dofollow', 'Nofollow'],
      datasets: [{ data: [dofollow, nofollow], backgroundColor: ['#6af7c8', '#8888a8'], borderWidth: 0 }]
    },
    options: {
      cutout: '65%',
      plugins: { legend: { position: 'bottom', labels: { color: '#8888a8', font: { family: 'Inter', size: 11 }, boxWidth: 10, padding: 12 } } },
      responsive: true, maintainAspectRatio: false
    }
  });
}

function setFilter(f) {
  currentFilter = f;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.toggle('active', b.dataset.filter === f));
  renderTable();
}

function renderTable() {
  if (!currentData) return;
  const search = document.getElementById('tableSearch').value.trim().toLowerCase();
  let rows = currentData.links.filter(l => l.category !== 'other');

  if (currentFilter === 'internal') rows = rows.filter(l => l.category === 'internal');
  else if (currentFilter === 'external') rows = rows.filter(l => l.category === 'external');
  else if (currentFilter === 'dofollow') rows = rows.filter(l => l.follow === 'dofollow');
  else if (currentFilter === 'nofollow') rows = rows.filter(l => l.follow === 'nofollow');

  if (search) {
    rows = rows.filter(l => l.anchor_text.toLowerCase().includes(search) || l.url.toLowerCase().includes(search));
  }

  document.getElementById('tableCount').textContent = `(${rows.length})`;
  const tbody = document.getElementById('tableBody');

  if (rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--muted);padding:2rem;">No links match this filter.</td></tr>';
    return;
  }

  tbody.innerHTML = rows.map(l => `
    <tr>
      <td class="anchor-cell">${escapeHtml(l.anchor_text)}</td>
      <td class="url-cell"><a href="${escapeHtml(l.url)}" target="_blank" rel="noopener">${escapeHtml(l.url)}</a></td>
      <td><span class="badge ${l.category}">${l.category}</span></td>
      <td><span class="badge ${l.follow}">${l.follow}</span></td>
    </tr>
  `).join('');
}

function exportCsv() {
  if (!currentData) return;
  const rows = currentData.links.filter(l => l.category !== 'other');
  const header = ['Anchor Text', 'URL', 'Type', 'Follow', 'Rel Attribute'];
  const csvRows = [header.join(',')];
  rows.forEach(l => {
    const cells = [l.anchor_text, l.url, l.category, l.follow || '', l.rel || ''].map(v => {
      const s = String(v).replace(/"/g, '""');
      return `"${s}"`;
    });
    csvRows.push(cells.join(','));
  });
  const blob = new Blob([csvRows.join('\\n')], {type: 'text/csv;charset=utf-8;'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  const hostPart = (currentData.page_host || 'page').replace(/[^a-z0-9.-]/gi, '_');
  a.href = url;
  a.download = `link-analysis-${hostPart}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
