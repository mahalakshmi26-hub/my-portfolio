"""
Broken Link Checker — Flask dashboard
Built by Mahalakshmi Marimuthu · Digital Marketing Strategist & AI-Powered SEO Expert

Scans a site (by crawling from a homepage / auto-discovering sitemap.xml) OR a
pasted list of page URLs, extracts every <a href> link on those pages, and
checks each unique link (internal + external) for broken status.
"""

import random
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config / limits (keep scans fast + safe on Render's free tier)
# ---------------------------------------------------------------------------
MAX_PAGES_TO_SCAN = 30          # how many pages we pull HTML from
MAX_LINKS_TO_CHECK = 250        # how many unique links we HTTP-check
PAGE_FETCH_TIMEOUT = 8
LINK_CHECK_TIMEOUT = 8
PAGE_WORKERS = 8
LINK_WORKERS = 15
DOMAIN_CONCURRENCY = 6          # max simultaneous requests to any single domain
                                 # (avoids tripping bot/WAF protection with a burst)
# A normal browser UA — self-identifying as a bot gets flagged/blocked by most
# WAFs (Akamai, Cloudflare, etc.) before a single request even completes.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_domain_semaphores = {}
_domain_semaphores_lock = threading.Lock()


def _get_domain_semaphore(url):
    """One semaphore per host, so we never hammer a single domain with a burst
    of concurrent requests (a common trigger for WAF/bot-protection 403s)."""
    netloc = urlparse(url).netloc.lower()
    with _domain_semaphores_lock:
        sem = _domain_semaphores.get(netloc)
        if sem is None:
            sem = threading.Semaphore(DOMAIN_CONCURRENCY)
            _domain_semaphores[netloc] = sem
        return sem


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def normalize_url(url):
    """Strip fragments, trim whitespace."""
    url = url.strip()
    url = url.split("#")[0]
    return url.rstrip("/") if url.count("/") > 2 else url


def same_domain(url, root_netloc):
    try:
        return urlparse(url).netloc.lower() == root_netloc.lower()
    except Exception:
        return False


def is_checkable_scheme(url):
    scheme = urlparse(url).scheme.lower()
    return scheme in ("http", "https")


# Domains known to routinely reject simple HTTP requests (no cookies/JS/browser
# fingerprint) even when the page is genuinely live — social platforms and URL
# shorteners are the classic offenders. A failed check against one of these is
# reported as "unverifiable" instead of "broken" so it doesn't read as a real dead link.
UNRELIABLE_DOMAINS = {
    "facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com",
    "threads.net", "tiktok.com",
    "bit.ly", "tinyurl.com", "t.co", "goo.gl", "rebrand.ly", "ow.ly", "bnkbzr.co",
}


def is_unreliable_domain(url):
    netloc = urlparse(url).netloc.lower()
    netloc = netloc[4:] if netloc.startswith("www.") else netloc
    return any(netloc == d or netloc.endswith("." + d) for d in UNRELIABLE_DOMAINS)


def fetch_sitemap_urls(base_url, limit=MAX_PAGES_TO_SCAN):
    """Try {base}/sitemap.xml (and one level of nested sitemaps). Returns [] if none."""
    parsed = urlparse(base_url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    sitemap_url = f"{root}/sitemap.xml"
    try:
        resp = requests.get(sitemap_url, headers=HEADERS, timeout=PAGE_FETCH_TIMEOUT)
        if resp.status_code != 200 or not resp.content:
            return []
        root_el = ET.fromstring(resp.content)
        tag = root_el.tag.lower()
        urls = []

        if tag.endswith("sitemapindex"):
            # nested sitemaps — pull from the first couple only, stay within limit
            child_maps = [
                el.text.strip()
                for el in root_el.iter()
                if el.tag.lower().endswith("loc") and el.text
            ][:3]
            for child in child_maps:
                try:
                    r2 = requests.get(child, headers=HEADERS, timeout=PAGE_FETCH_TIMEOUT)
                    if r2.status_code == 200:
                        child_root = ET.fromstring(r2.content)
                        urls += [
                            el.text.strip()
                            for el in child_root.iter()
                            if el.tag.lower().endswith("loc") and el.text
                        ]
                except Exception:
                    continue
                if len(urls) >= limit:
                    break
        else:
            urls = [
                el.text.strip()
                for el in root_el.iter()
                if el.tag.lower().endswith("loc") and el.text
            ]

        return urls[:limit]
    except Exception:
        return []


def crawl_site(start_url, max_pages=MAX_PAGES_TO_SCAN):
    """BFS crawl of internal pages starting at start_url. Falls back from sitemap."""
    start_url = normalize_url(start_url)
    root_netloc = urlparse(start_url).netloc

    sitemap_pages = [normalize_url(p) for p in fetch_sitemap_urls(start_url, limit=max_pages)]
    if sitemap_pages:
        pages, seen_pages = [], set()
        if same_domain(start_url, root_netloc):
            pages.append(start_url)
            seen_pages.add(start_url)
        for p in sitemap_pages:
            if same_domain(p, root_netloc) and p not in seen_pages:
                pages.append(p)
                seen_pages.add(p)
            if len(pages) >= max_pages:
                break
        return pages, "sitemap"

    # fallback: manual BFS crawl over internal <a href> links
    seen = {start_url}
    queue = [start_url]
    pages = []

    while queue and len(pages) < max_pages:
        url = queue.pop(0)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=PAGE_FETCH_TIMEOUT)
            if resp.status_code != 200 or "text/html" not in resp.headers.get("Content-Type", ""):
                continue
            pages.append(url)
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                link = normalize_url(urljoin(url, a["href"]))
                if (
                    is_checkable_scheme(link)
                    and same_domain(link, root_netloc)
                    and link not in seen
                    and len(seen) < max_pages * 4
                ):
                    seen.add(link)
                    queue.append(link)
        except Exception:
            continue

    return pages, "crawl"


def extract_links_from_page(page_url):
    """Fetch a page and return list of (link_url, anchor_text)."""
    try:
        resp = requests.get(page_url, headers=HEADERS, timeout=PAGE_FETCH_TIMEOUT)
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        found = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith(("mailto:", "tel:", "javascript:")):
                continue
            link = normalize_url(urljoin(page_url, href))
            if is_checkable_scheme(link):
                text = a.get_text(strip=True)[:80]
                found.append((link, text))
        return found
    except Exception:
        return []


def classify(status_code, redirect_count, error):
    if error:
        return "broken"
    if status_code is None:
        return "broken"
    if 200 <= status_code < 300:
        return "redirect" if redirect_count > 0 else "ok"
    if 300 <= status_code < 400:
        return "redirect"
    return "broken"  # 4xx, 5xx


def _request_once(url):
    resp = requests.head(url, headers=HEADERS, timeout=LINK_CHECK_TIMEOUT, allow_redirects=True)
    # A lot of WAFs (Akamai, Cloudflare, etc.) treat a bare HEAD request as a bot
    # signal and block it specifically, while a normal GET sails through. So any
    # non-2xx/3xx from HEAD gets a GET before we trust it — cheap insurance,
    # and a genuinely dead page will still come back the same way on GET.
    if resp.status_code >= 400:
        resp = requests.get(
            url, headers=HEADERS, timeout=LINK_CHECK_TIMEOUT, allow_redirects=True, stream=True
        )
        resp.close()
    return resp


def check_link(url):
    start = time.time()
    sem = _get_domain_semaphore(url)
    sem.acquire()
    try:
        resp = _request_once(url)
        # 403/429/503 are frequently a bot-protection layer blocking a burst of
        # requests rather than a genuinely broken link — pause briefly and
        # retry once before calling it broken.
        if resp.status_code in (403, 429, 503):
            time.sleep(1.2 + random.random())
            resp = _request_once(url)
        elapsed_ms = round((time.time() - start) * 1000)
        redirect_count = len(resp.history)
        severity = classify(resp.status_code, redirect_count, None)
        return {
            "status_code": resp.status_code,
            "final_url": resp.url,
            "redirect_count": redirect_count,
            "response_ms": elapsed_ms,
            "error": None,
            "severity": severity,
        }
    except requests.exceptions.Timeout:
        return _error_result(start, "Timeout")
    except requests.exceptions.TooManyRedirects:
        return _error_result(start, "Too many redirects")
    except requests.exceptions.SSLError:
        return _error_result(start, "SSL error")
    except requests.exceptions.ConnectionError:
        return _error_result(start, "Connection failed")
    except Exception as exc:
        return _error_result(start, str(exc)[:120])
    finally:
        sem.release()


def _error_result(start, message):
    return {
        "status_code": None,
        "final_url": None,
        "redirect_count": 0,
        "response_ms": round((time.time() - start) * 1000),
        "error": message,
        "severity": "broken",
    }


def run_scan(pages_to_scan, root_netloc):
    """Fetch links from each page, dedupe, check each unique link concurrently."""
    link_index = {}  # url -> {"found_on": [...], "anchor": text}

    with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as pool:
        futures = {pool.submit(extract_links_from_page, p): p for p in pages_to_scan}
        for fut in as_completed(futures):
            page_url = futures[fut]
            try:
                links = fut.result()
            except Exception:
                links = []
            for link, anchor in links:
                entry = link_index.setdefault(link, {"found_on": [], "anchor": anchor})
                if page_url not in entry["found_on"] and len(entry["found_on"]) < 3:
                    entry["found_on"].append(page_url)

    unique_links = list(link_index.keys())[:MAX_LINKS_TO_CHECK]

    results = []
    with ThreadPoolExecutor(max_workers=LINK_WORKERS) as pool:
        futures = {pool.submit(check_link, url): url for url in unique_links}
        for fut in as_completed(futures):
            url = futures[fut]
            info = link_index[url]
            check = fut.result()
            # Facebook/Instagram/URL-shorteners etc. routinely reject plain HTTP
            # checks even when the page is live — don't report those as broken.
            if check["severity"] == "broken" and is_unreliable_domain(url):
                check = {**check, "severity": "unverifiable"}
            results.append(
                {
                    "url": url,
                    "found_on": info["found_on"],
                    "anchor": info["anchor"],
                    "type": "internal" if same_domain(url, root_netloc) else "external",
                    **check,
                }
            )

    severity_rank = {"broken": 0, "unverifiable": 1, "redirect": 2, "ok": 3}
    results.sort(key=lambda r: severity_rank.get(r["severity"], 4))

    ok = sum(1 for r in results if r["severity"] == "ok")
    redirect = sum(1 for r in results if r["severity"] == "redirect")
    broken = sum(1 for r in results if r["severity"] == "broken")
    unverifiable = sum(1 for r in results if r["severity"] == "unverifiable")
    internal = sum(1 for r in results if r["type"] == "internal")
    external = sum(1 for r in results if r["type"] == "external")
    total = len(results)
    scoreable = total - unverifiable
    health_score = round((ok / scoreable) * 100) if scoreable else 100

    summary = {
        "pages_scanned": len(pages_to_scan),
        "total_links": total,
        "ok": ok,
        "redirect": redirect,
        "broken": broken,
        "unverifiable": unverifiable,
        "internal": internal,
        "external": external,
        "health_score": health_score,
    }
    return summary, results


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template_string(PAGE_TEMPLATE)


@app.route("/api/scan", methods=["POST"])
def api_scan():
    data = request.get_json(force=True, silent=True) or {}
    mode = data.get("mode", "crawl")
    raw_input = (data.get("input") or "").strip()

    if not raw_input:
        return jsonify({"error": "Please enter a URL or list of URLs."}), 400

    if mode == "paste":
        pages = [normalize_url(u) for u in re.split(r"[\n,]+", raw_input) if u.strip()]
        pages = [p for p in pages if is_checkable_scheme(p)][:MAX_PAGES_TO_SCAN]
        if not pages:
            return jsonify({"error": "No valid http(s) URLs found in the pasted list."}), 400
        root_netloc = urlparse(pages[0]).netloc
        source = "pasted list"
    else:
        start_url = normalize_url(raw_input)
        if not is_checkable_scheme(start_url):
            start_url = "https://" + start_url
        if not is_checkable_scheme(start_url):
            return jsonify({"error": "Please enter a valid http(s) URL."}), 400
        pages, source = crawl_site(start_url)
        root_netloc = urlparse(start_url).netloc
        if not pages:
            return jsonify({"error": "Couldn't reach that site or find any pages to scan."}), 400

    try:
        summary, results = run_scan(pages, root_netloc)
    except Exception as exc:
        return jsonify({"error": f"Scan failed: {exc}"}), 500

    summary["source"] = source
    return jsonify({"summary": summary, "results": results})


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
<title>Broken Link Checker | SEO Tools by Mahalakshmi</title>
<meta name="description" content="Free broken link checker. Crawl a site or paste page URLs to find broken internal and external links, redirects, and dead pages.">
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
  .mode-toggle { display: inline-flex; background: var(--surface2); border-radius: 8px; padding: 0.25rem; margin-bottom: 1.1rem; }
  .mode-btn { border: none; background: transparent; color: var(--muted); padding: 0.5rem 1.1rem; border-radius: 6px; font-family: 'Inter', sans-serif; font-size: 0.82rem; font-weight: 600; cursor: pointer; transition: all 0.15s; }
  .mode-btn.active { background: var(--accent); color: #fff; }
  .scan-input { width: 100%; background: var(--surface2); border: 1px solid var(--border); border-radius: 8px; color: var(--text); padding: 0.75rem 0.9rem; font-family: 'Inter', sans-serif; font-size: 0.88rem; resize: vertical; }
  textarea.scan-input { min-height: 110px; display: none; }
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
  .stat-card.broken .stat-num { color: var(--danger); }
  .stat-card.redirect .stat-num { color: var(--accent2); }
  .stat-card.ok .stat-num { color: var(--accent3); }
  .stat-card.unverifiable .stat-num { color: var(--muted); }

  .chart-row { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1.6rem; }
  .chart-card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 1.2rem; }
  .chart-card h3 { font-family: 'Sora', sans-serif; font-size: 0.85rem; font-weight: 700; margin-bottom: 0.9rem; }
  .chart-card canvas { max-height: 190px; }

  .results-card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 1.2rem; }
  .results-head { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 0.8rem; margin-bottom: 1rem; }
  .filter-row { display: flex; gap: 0.5rem; }
  .filter-btn { background: var(--surface2); border: 1px solid var(--border); color: var(--muted); padding: 0.4rem 0.85rem; border-radius: 100px; font-size: 0.76rem; font-family: 'DM Mono', monospace; cursor: pointer; transition: all 0.15s; }
  .filter-btn.active { background: var(--accent); border-color: var(--accent); color: #fff; }
  .btn-export { background: var(--surface2); border: 1px solid var(--border); color: var(--text); padding: 0.45rem 1rem; border-radius: 8px; font-size: 0.78rem; font-weight: 600; cursor: pointer; }
  .btn-export:hover { border-color: var(--accent); color: var(--accent); }

  table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
  thead th { text-align: left; padding: 0.6rem 0.7rem; color: var(--muted); font-size: 0.68rem; text-transform: uppercase; letter-spacing: 0.06em; font-family: 'DM Mono', monospace; border-bottom: 1px solid var(--border); }
  tbody td { padding: 0.65rem 0.7rem; border-bottom: 1px solid rgba(255,255,255,0.04); vertical-align: top; }
  tbody tr:hover { background: rgba(124,106,247,0.05); }
  .url-cell { max-width: 380px; overflow-wrap: anywhere; }
  .url-cell a { text-decoration: none; color: var(--text); }
  .url-cell a:hover { color: var(--accent); }
  .found-on { font-size: 0.72rem; color: var(--muted); margin-top: 0.2rem; overflow-wrap: anywhere; }
  .badge { display: inline-block; font-family: 'DM Mono', monospace; font-size: 0.68rem; padding: 0.22rem 0.6rem; border-radius: 100px; font-weight: 600; }
  .badge.ok { background: rgba(106,247,200,0.12); color: var(--accent3); border: 1px solid rgba(106,247,200,0.25); }
  .badge.redirect { background: rgba(247,162,106,0.12); color: var(--accent2); border: 1px solid rgba(247,162,106,0.25); }
  .badge.broken { background: rgba(247,106,106,0.12); color: var(--danger); border: 1px solid rgba(247,106,106,0.3); }
  .badge.unverifiable { background: rgba(136,136,168,0.15); color: var(--muted); border: 1px solid rgba(136,136,168,0.3); }
  .type-tag { font-size: 0.72rem; color: var(--muted); }
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
      <a href="#" class="sidebar-link active">🔗 Broken Link Checker</a>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-label">Navigate</div>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="sidebar-link">← All Tools</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio" class="sidebar-link">Portfolio Home</a>
    </div>
    <div class="sidebar-limits">
      Scans up to <strong>30 pages</strong> and checks up to <strong>250 links</strong> per run, so results stay fast and free.
    </div>
  </aside>

  <main class="main">
    <div class="page-head">
      <div class="page-eyebrow">// seo tool</div>
      <div class="page-title">Broken Link Checker</div>
      <div class="page-sub">Crawl a site or paste page URLs to find broken internal and external links, redirects, and dead pages — with a health score and downloadable report.</div>
    </div>

    <div class="scan-card">
      <div class="mode-toggle">
        <button class="mode-btn active" data-mode="crawl" onclick="setMode('crawl')">Crawl a site</button>
        <button class="mode-btn" data-mode="paste" onclick="setMode('paste')">Paste URLs</button>
      </div>

      <input type="text" id="crawlInput" class="scan-input" placeholder="https://example.com" style="display:block;">
      <textarea id="pasteInput" class="scan-input" placeholder="https://example.com/page-1&#10;https://example.com/page-2&#10;https://example.com/page-3"></textarea>
      <div class="scan-hint" id="scanHint">Enter a homepage URL — I'll check /sitemap.xml first, then crawl internal links if there's no sitemap.</div>

      <div class="scan-actions">
        <button class="btn-primary" id="scanBtn" onclick="startScan()">Check Links</button>
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
          <div class="health-label">Link Health</div>
        </div>
        <div class="stat-card"><div class="stat-num" id="statTotal">0</div><div class="stat-label">Links checked</div></div>
        <div class="stat-card ok"><div class="stat-num" id="statOk">0</div><div class="stat-label">Working</div></div>
        <div class="stat-card redirect"><div class="stat-num" id="statRedirect">0</div><div class="stat-label">Redirects</div></div>
        <div class="stat-card broken"><div class="stat-num" id="statBroken">0</div><div class="stat-label">Broken</div></div>
        <div class="stat-card unverifiable"><div class="stat-num" id="statUnverifiable">0</div><div class="stat-label">Unverifiable</div></div>
      </div>

      <div class="chart-row">
        <div class="chart-card">
          <h3>Status Breakdown</h3>
          <canvas id="statusChart"></canvas>
        </div>
        <div class="chart-card">
          <h3>Internal vs External</h3>
          <canvas id="typeChart"></canvas>
        </div>
      </div>

      <div class="results-card">
        <div class="results-head">
          <div class="filter-row">
            <button class="filter-btn active" data-filter="all" onclick="setFilter('all')">All</button>
            <button class="filter-btn" data-filter="broken" onclick="setFilter('broken')">Broken</button>
            <button class="filter-btn" data-filter="redirect" onclick="setFilter('redirect')">Redirects</button>
            <button class="filter-btn" data-filter="ok" onclick="setFilter('ok')">Working</button>
            <button class="filter-btn" data-filter="unverifiable" onclick="setFilter('unverifiable')">Unverifiable</button>
          </div>
          <button class="btn-export" onclick="exportCsv()">⬇ Export CSV</button>
        </div>
        <div style="overflow-x:auto;">
          <table>
            <thead>
              <tr><th>Link</th><th>Type</th><th>Status</th><th>Time</th><th>Severity</th></tr>
            </thead>
            <tbody id="resultsBody"></tbody>
          </table>
        </div>
        <div class="empty-state" id="emptyState" style="display:none;">No links match this filter.</div>
      </div>
    </div>
  </main>
</div>

<script>
let currentMode = 'crawl';
let currentFilter = 'all';
let allResults = [];
let statusChartInstance = null;
let typeChartInstance = null;

function setMode(mode) {
  currentMode = mode;
  document.querySelectorAll('.mode-btn').forEach(b => b.classList.toggle('active', b.dataset.mode === mode));
  document.getElementById('crawlInput').style.display = mode === 'crawl' ? 'block' : 'none';
  document.getElementById('pasteInput').style.display = mode === 'paste' ? 'block' : 'none';
  document.getElementById('scanHint').textContent = mode === 'crawl'
    ? "Enter a homepage URL — I'll check /sitemap.xml first, then crawl internal links if there's no sitemap."
    : 'Paste one page URL per line (or comma-separated). Each page is scanned for links.';
}

function setFilter(filter) {
  currentFilter = filter;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.toggle('active', b.dataset.filter === filter));
  renderTable();
}

async function startScan() {
  const input = currentMode === 'crawl'
    ? document.getElementById('crawlInput').value.trim()
    : document.getElementById('pasteInput').value.trim();

  const errorBox = document.getElementById('errorBox');
  errorBox.style.display = 'none';

  if (!input) {
    errorBox.textContent = 'Please enter a URL to check.';
    errorBox.style.display = 'block';
    return;
  }

  const btn = document.getElementById('scanBtn');
  const status = document.getElementById('scanStatus');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Scanning…';
  status.textContent = 'This can take up to a minute depending on site size.';
  document.getElementById('results').style.display = 'none';

  try {
    const resp = await fetch('/api/scan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: currentMode, input: input })
    });
    const data = await resp.json();

    if (!resp.ok) {
      errorBox.textContent = data.error || 'Something went wrong. Please try again.';
      errorBox.style.display = 'block';
      return;
    }

    allResults = data.results;
    renderSummary(data.summary);
    renderCharts(data.summary);
    currentFilter = 'all';
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.toggle('active', b.dataset.filter === 'all'));
    renderTable();
    document.getElementById('results').style.display = 'block';
    status.textContent = `Scanned ${data.summary.pages_scanned} page(s) via ${data.summary.source}.`;
  } catch (err) {
    errorBox.textContent = 'Network error — please try again in a moment.';
    errorBox.style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Check Links';
  }
}

function renderSummary(summary) {
  document.getElementById('statTotal').textContent = summary.total_links;
  document.getElementById('statOk').textContent = summary.ok;
  document.getElementById('statRedirect').textContent = summary.redirect;
  document.getElementById('statBroken').textContent = summary.broken;
  document.getElementById('statUnverifiable').textContent = summary.unverifiable || 0;
  document.getElementById('healthScoreText').textContent = summary.health_score + '%';

  const circumference = 264;
  const offset = circumference - (summary.health_score / 100) * circumference;
  const ring = document.getElementById('healthRing');
  ring.style.strokeDashoffset = offset;
  ring.style.stroke = summary.health_score >= 90 ? '#6af7c8' : summary.health_score >= 70 ? '#f7a26a' : '#f76a6a';
}

function renderCharts(summary) {
  const statusCtx = document.getElementById('statusChart');
  const typeCtx = document.getElementById('typeChart');

  if (statusChartInstance) statusChartInstance.destroy();
  if (typeChartInstance) typeChartInstance.destroy();

  const chartFont = { family: 'Inter', size: 11 };

  statusChartInstance = new Chart(statusCtx, {
    type: 'doughnut',
    data: {
      labels: ['Working', 'Redirects', 'Broken', 'Unverifiable'],
      datasets: [{ data: [summary.ok, summary.redirect, summary.broken, summary.unverifiable || 0], backgroundColor: ['#6af7c8', '#f7a26a', '#f76a6a', '#8888a8'], borderWidth: 0 }]
    },
    options: { plugins: { legend: { position: 'bottom', labels: { color: '#c8c8e0', font: chartFont, padding: 12 } } }, cutout: '65%' }
  });

  typeChartInstance = new Chart(typeCtx, {
    type: 'doughnut',
    data: {
      labels: ['Internal', 'External'],
      datasets: [{ data: [summary.internal, summary.external], backgroundColor: ['#7c6af7', '#8888a8'], borderWidth: 0 }]
    },
    options: { plugins: { legend: { position: 'bottom', labels: { color: '#c8c8e0', font: chartFont, padding: 12 } } }, cutout: '65%' }
  });
}

function renderTable() {
  const tbody = document.getElementById('resultsBody');
  const emptyState = document.getElementById('emptyState');
  const filtered = currentFilter === 'all' ? allResults : allResults.filter(r => r.severity === currentFilter);

  tbody.innerHTML = '';
  emptyState.style.display = filtered.length ? 'none' : 'block';

  filtered.forEach(r => {
    const tr = document.createElement('tr');
    const foundOn = (r.found_on || []).map(u => `Found on: ${u}`).join('<br>');
    const statusLabel = r.error ? r.error : (r.status_code ?? '—');

    tr.innerHTML = `
      <td class="url-cell"><a href="${r.url}" target="_blank" rel="noopener">${r.url}</a>${foundOn ? `<div class="found-on">${foundOn}</div>` : ''}</td>
      <td class="type-tag">${r.type}</td>
      <td>${statusLabel}${r.redirect_count ? ` (${r.redirect_count} hop${r.redirect_count > 1 ? 's' : ''})` : ''}</td>
      <td>${r.response_ms}ms</td>
      <td><span class="badge ${r.severity}">${r.severity}</span></td>
    `;
    tbody.appendChild(tr);
  });
}

function exportCsv() {
  if (!allResults.length) return;
  const rows = [['URL', 'Type', 'Status Code', 'Error', 'Redirects', 'Response (ms)', 'Severity', 'Found On']];
  allResults.forEach(r => {
    rows.push([
      r.url, r.type, r.status_code ?? '', r.error ?? '', r.redirect_count,
      r.response_ms, r.severity, (r.found_on || []).join(' | ')
    ]);
  });
  const csv = rows.map(row => row.map(cell => `"${String(cell).replace(/"/g, '""')}"`).join(',')).join('\\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = 'broken-link-report.csv';
  link.click();
}
</script>
</body>
</html>"""

if __name__ == "__main__":
    app.run(debug=True, port=5050)
