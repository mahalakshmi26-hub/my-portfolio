"""
XML Sitemap Generator — Flask dashboard
Built by Mahalakshmi Marimuthu · Digital Marketing Strategist & AI-Powered SEO Expert

Crawl a website, discover its indexable pages, and build a valid sitemap.xml.

Backend (this file): a polite, capped crawler that runs as a background job and is
polled by the page. It follows internal links only, respects robots.txt (optional),
skips noindex / canonicalised / broken / redirecting pages, can seed itself from an
existing sitemap.xml, and refuses to touch private or internal network addresses.

Frontend: dashboard with settings, live progress, an editable pages table, priority /
changefreq / lastmod controls, a live XML preview and copy / download (XML, TXT, CSV).
Priority, changefreq and lastmod are applied in the browser, so changing them never
needs a re-crawl.
"""

import ipaddress
import os
import re
import socket
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

# ---------------------------------------------------------------- limits
HARD_MAX_URLS = 500          # fetched pages per crawl (free-tier friendly)
HARD_MAX_DEPTH = 5
MAX_BYTES = 1_500_000        # read at most ~1.5 MB of any page
REQ_TIMEOUT = 10
WORKERS = 5
TIME_BUDGET = 240            # seconds per crawl
MAX_ACTIVE_JOBS = 4
JOB_TTL = 30 * 60
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

SKIP_EXT = {
    "jpg", "jpeg", "png", "gif", "webp", "svg", "ico", "bmp", "avif", "css", "js", "json", "xml",
    "pdf", "zip", "rar", "gz", "7z", "mp4", "mp3", "avi", "mov", "wav", "woff", "woff2", "ttf",
    "eot", "otf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv", "txt", "rss", "atom", "apk", "exe", "dmg",
}
TRACKING = re.compile(r"^(utm_|gclid$|fbclid$|msclkid$|mc_cid$|mc_eid$|_ga$|ref$|source$)", re.I)

# Local testing only: set SITEMAP_ALLOW_PRIVATE=1 to crawl localhost. Never set on Render.
ALLOW_PRIVATE = os.environ.get("SITEMAP_ALLOW_PRIVATE") == "1"

JOBS = {}
JOBS_LOCK = threading.Lock()


# ---------------------------------------------------------------- helpers
def is_public_host(host):
    """Refuse loopback / private / link-local / reserved addresses (SSRF guard)."""
    if not host:
        return False
    if ALLOW_PRIVATE:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return False
    return True


def normalize(url, keep_query=False):
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.hostname:
        return None
    host = p.hostname.lower()
    port = p.port
    if port and not ((p.scheme == "http" and port == 80) or (p.scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = p.path or "/"
    path = re.sub(r"/{2,}", "/", path)
    query = ""
    if keep_query and p.query:
        pairs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING.match(k)]
        query = urlencode(pairs)
    return urlunsplit((p.scheme, host, path, query, ""))


def host_key(url):
    h = urlsplit(url).hostname or ""
    return h[4:] if h.startswith("www.") else h


def has_skip_ext(url):
    path = urlsplit(url).path
    if "." not in path.rsplit("/", 1)[-1]:
        return False
    return path.rsplit(".", 1)[-1].lower() in SKIP_EXT


def safe_get(url, want_body=True):
    """GET with manual redirect handling, private-IP checks and a body size cap."""
    hops = 0
    cur = url
    while True:
        p = urlsplit(cur)
        if not is_public_host(p.hostname):
            raise ValueError("blocked: non-public or unresolvable host")
        r = requests.get(cur, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                                       "Accept-Language": "en"},
                         timeout=REQ_TIMEOUT, allow_redirects=False, stream=True)
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
            r.close()
            hops += 1
            if hops > 5:
                raise ValueError("too many redirects")
            cur = urljoin(cur, r.headers["Location"])
            continue
        body = b""
        ctype = (r.headers.get("Content-Type") or "").lower()
        if want_body and r.status_code == 200 and ("html" in ctype or "xml" in ctype or "text" in ctype):
            for chunk in r.iter_content(65536):
                body += chunk
                if len(body) >= MAX_BYTES:
                    break
        r.close()
        return r, cur, body, hops


def to_text(resp, body):
    enc = resp.encoding or "utf-8"
    try:
        return body.decode(enc, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def iso_date(http_date):
    if not http_date:
        return ""
    try:
        return parsedate_to_datetime(http_date).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def compile_excludes(lines):
    out = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        out.append(re.compile(".*".join(re.escape(x) for x in ln.split("*")), re.I))
    return out


def load_sitemap_urls(base, keep_query, host, limit=2000):
    urls, todo, fetched = [], [urljoin(base, "/sitemap.xml")], 0
    while todo and fetched < 6 and len(urls) < limit:
        sm = todo.pop(0)
        fetched += 1
        try:
            r, _, body, _ = safe_get(sm)
            if r.status_code != 200:
                continue
            txt = to_text(r, body)
        except Exception:
            continue
        locs = re.findall(r"<loc>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</loc>", txt, re.S | re.I)
        if "<sitemapindex" in txt.lower():
            todo.extend(l for l in locs[:5] if host_key(l) == host)
        else:
            for l in locs:
                n = normalize(l.replace("&amp;", "&"), keep_query)
                if n and host_key(n) == host:
                    urls.append(n)
    return urls[:limit]


# ---------------------------------------------------------------- crawl job
def analyse_page(url, depth, opts):
    """Fetch one URL and return a result dict plus discovered links."""
    res = {"url": url, "depth": depth, "status": 0, "lastmod": "", "title": "", "reason": None, "note": ""}
    links = []
    try:
        r, final, body, hops = safe_get(url)
    except Exception as e:  # noqa: BLE001
        res["reason"] = "Could not fetch: " + str(e)[:90]
        return res, links, None
    res["status"] = r.status_code
    final_n = normalize(final, opts["keep_query"])
    if final_n and final_n != url:
        res["note"] = f"redirects to {final_n}"
        res["final"] = final_n
    if r.status_code >= 400:
        res["reason"] = f"HTTP {r.status_code}"
        return res, links, None
    if r.status_code != 200:
        res["reason"] = f"HTTP {r.status_code}"
        return res, links, None
    ctype = (r.headers.get("Content-Type") or "").lower()
    if "html" not in ctype:
        res["reason"] = "Not an HTML page"
        return res, links, None
    if final_n and final_n != url:
        if host_key(final_n) != opts["host"]:
            res["reason"] = "Redirects to another domain"
            return res, links, None
    xrobots = (r.headers.get("X-Robots-Tag") or "").lower()
    res["lastmod"] = iso_date(r.headers.get("Last-Modified"))
    soup = BeautifulSoup(to_text(r, body), "html.parser")
    t = soup.find("title")
    res["title"] = (t.get_text(" ", strip=True)[:120] if t else "")
    noindex = "noindex" in xrobots
    for m in soup.find_all("meta", attrs={"name": re.compile(r"^(robots|googlebot)$", re.I)}):
        if "noindex" in (m.get("content") or "").lower():
            noindex = True
    canonical = None
    for l in soup.find_all("link", href=True):
        if "canonical" in [x.lower() for x in (l.get("rel") or [])]:
            canonical = normalize(urljoin(final, l["href"]), opts["keep_query"])
            break
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "sms:", "data:")):
            continue
        if "nofollow" in [x.lower() for x in (a.get("rel") or [])]:
            continue
        n = normalize(urljoin(final, href), opts["keep_query"])
        if n and host_key(n) == opts["host"] and not has_skip_ext(n):
            links.append(n)
    if noindex:
        res["reason"] = "noindex"
        return res, links, canonical
    here = final_n or url
    if canonical and canonical != here and host_key(canonical) == opts["host"]:
        res["reason"] = f"Canonical points elsewhere: {canonical}"
        return res, links, canonical
    return res, links, None


def path_depth(url):
    """Depth for a pasted URL = number of path segments (home page = 0)."""
    segs = [x for x in urlsplit(url).path.split("/") if x]
    return min(len(segs), HARD_MAX_DEPTH)


def run_list_job(job):
    """Check a pasted list of URLs (no link following) with the same rules as a crawl."""
    opts = job["opts"]
    started = time.time()
    try:
        opts["keep_query"] = True
        raw = opts["urls"]
        pages, todo, seen = [], [], set()
        for line in raw:
            line = line.strip()
            if not line:
                continue
            cand = line if re.match(r"^[a-z][a-z0-9+.-]*://", line, re.I) else "https://" + line
            n = normalize(cand, True)
            if n and (re.search(r"\s", line) or "." not in (urlsplit(n).hostname or "")):
                n = None
            if not n:
                pages.append({"url": line[:200], "depth": 0, "status": 0, "lastmod": "", "title": "",
                              "reason": "Not a valid http(s) URL", "note": ""})
                continue
            if n in seen:
                continue
            seen.add(n)
            todo.append(n)
        if not todo:
            raise ValueError("No valid URLs found in the list.")
        if len(todo) > HARD_MAX_URLS:
            todo = todo[:HARD_MAX_URLS]
            job["truncated"] = True
        start = todo[0]
        opts["host"] = host_key(start)
        base = f"{urlsplit(start).scheme}://{urlsplit(start).netloc}"

        rp = None
        job["robots_sitemaps"] = []
        try:
            rr, _, rbody, _ = safe_get(urljoin(base, "/robots.txt"))
            if rr.status_code == 200:
                rtxt = to_text(rr, rbody)
                job["robots_sitemaps"] = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", rtxt)
                if opts["respect_robots"]:
                    rp = RobotFileParser()
                    rp.parse(rtxt.splitlines())
        except Exception:
            pass
        excludes = compile_excludes(opts["exclude"])
        job["queued"] = len(todo)

        def work(u):
            d = path_depth(u)
            if host_key(u) != opts["host"]:
                return {"url": u, "depth": d, "status": 0, "lastmod": "", "title": "",
                        "reason": "Different domain from the first URL", "note": ""}
            q = urlsplit(u)
            if excludes and any(rx.search(q.path + ("?" + q.query if q.query else "")) for rx in excludes):
                return {"url": u, "depth": d, "status": 0, "lastmod": "", "title": "",
                        "reason": "Matches an exclude pattern", "note": ""}
            if rp and not rp.can_fetch("*", u):
                return {"url": u, "depth": d, "status": 0, "lastmod": "", "title": "",
                        "reason": "Blocked by robots.txt", "note": ""}
            if job["cancel"] or time.time() - started > TIME_BUDGET:
                job["truncated"] = True
                return None
            res, _links, _canon = analyse_page(u, d, opts)
            fin = res.get("final")
            if fin and fin != u and res["reason"] is None:
                res["reason"] = f"Redirects to {fin}"
            job["crawled"] += 1
            job["current"] = u
            return res

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for res in pool.map(work, todo):
                if res:
                    pages.append(res)
        job["pages"] = pages
        job["start_url"] = start
        job["state"] = "done"
    except Exception as e:  # noqa: BLE001
        job["state"] = "error"
        job["error"] = str(e)[:200]
    finally:
        job["elapsed"] = round(time.time() - started, 1)
        job["finished"] = time.time()


def run_job(job):
    if job["opts"].get("mode") == "list":
        return run_list_job(job)
    opts = job["opts"]
    started = time.time()
    try:
        start = normalize(opts["url"], opts["keep_query"])
        if not start:
            raise ValueError("That doesn't look like a valid http(s) URL.")
        if not is_public_host(urlsplit(start).hostname):
            raise ValueError("Couldn't reach that host (it may not exist or is not public).")
        # find the real start URL / host after redirects
        r0, final0, _, _ = safe_get(start, want_body=False)
        start = normalize(final0, opts["keep_query"]) or start
        opts["host"] = host_key(start)
        base = f"{urlsplit(start).scheme}://{urlsplit(start).netloc}"

        rp = None
        job["robots_sitemaps"] = []
        try:
            rr, _, rbody, _ = safe_get(urljoin(base, "/robots.txt"))
            if rr.status_code == 200:
                rtxt = to_text(rr, rbody)
                job["robots_sitemaps"] = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", rtxt)
                if opts["respect_robots"]:
                    rp = RobotFileParser()
                    rp.parse(rtxt.splitlines())
        except Exception:
            pass

        excludes = compile_excludes(opts["exclude"])
        seen = {start}
        level = [(start, 0)]
        if opts["use_sitemap"]:
            extra = load_sitemap_urls(base, opts["keep_query"], opts["host"])
            job["sitemap_found"] = len(extra)
            for u in extra:
                if u not in seen:
                    seen.add(u)
                    level.append((u, 1))
        pages, done_urls = [], set()
        max_urls, max_depth = opts["max_urls"], opts["depth"]

        def skip_reason(u):
            if excludes and any(rx.search(urlsplit(u).path + ("?" + urlsplit(u).query if urlsplit(u).query else ""))
                                for rx in excludes):
                return "Matches an exclude pattern"
            if rp and not rp.can_fetch("*", u):
                return "Blocked by robots.txt"
            return None

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            while level and len(pages) < max_urls:
                if job["cancel"] or time.time() - started > TIME_BUDGET:
                    job["truncated"] = True
                    break
                batch, nxt = [], []
                for u, d in level:
                    if len(pages) + len(batch) >= max_urls:
                        job["truncated"] = True
                        break
                    why = skip_reason(u)
                    if why:
                        pages.append({"url": u, "depth": d, "status": 0, "lastmod": "", "title": "",
                                      "reason": why, "note": ""})
                        continue
                    batch.append((u, d))
                for (u, d), (res, links, canon) in zip(batch, pool.map(lambda x: analyse_page(x[0], x[1], opts), batch)):
                    pages.append(res)
                    done_urls.add(u)
                    job["crawled"] = len(pages)
                    job["current"] = u
                    fin = res.get("final")
                    if fin and fin != u:
                        if fin in seen and fin in done_urls:
                            res["reason"] = res["reason"] or "Duplicate of a redirect target"
                        elif fin not in seen:
                            seen.add(fin)
                            nxt.append((fin, d))
                        if res["reason"] is None:
                            res["reason"] = f"Redirects to {fin}"
                    if canon and canon not in seen and host_key(canon) == opts["host"]:
                        seen.add(canon)
                        nxt.append((canon, d))
                    if d < max_depth:
                        for l in links:
                            if l not in seen:
                                seen.add(l)
                                nxt.append((l, d + 1))
                level = nxt
                job["queued"] = len(nxt)
            if level and len(pages) >= max_urls:
                job["truncated"] = True
        job["pages"] = pages
        job["start_url"] = start
        job["state"] = "done"
    except Exception as e:  # noqa: BLE001
        job["state"] = "error"
        job["error"] = str(e)[:200]
    finally:
        job["elapsed"] = round(time.time() - started, 1)
        job["finished"] = time.time()


def clean_jobs():
    now = time.time()
    with JOBS_LOCK:
        for jid in [j for j, v in JOBS.items() if now - v["created"] > JOB_TTL]:
            JOBS.pop(jid, None)


# ---------------------------------------------------------------- routes
@app.route("/")
def index():
    return Response(PAGE_TEMPLATE, mimetype="text/html")


@app.route("/healthz")
def healthz():
    return {"status": "ok"}


@app.route("/api/crawl", methods=["POST"])
def api_crawl():
    clean_jobs()
    data = request.get_json(silent=True) or {}
    mode = "list" if data.get("mode") == "list" else "crawl"
    url = (data.get("url") or "").strip()
    url_list = [str(x) for x in (data.get("urls") or [])][:5000]
    if mode == "list":
        if not any(x.strip() for x in url_list):
            return jsonify(error="Paste at least one URL first."), 400
    else:
        if url and not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
            url = "https://" + url
        if not normalize(url):
            return jsonify(error="Please enter a valid website URL, e.g. https://www.example.com"), 400
    with JOBS_LOCK:
        active = sum(1 for j in JOBS.values() if j["state"] == "running")
        if active >= MAX_ACTIVE_JOBS:
            return jsonify(error="The tool is busy right now. Please try again in a minute."), 429
        try:
            max_urls = max(1, min(int(data.get("max_urls", 100)), HARD_MAX_URLS))
            depth = max(0, min(int(data.get("depth", 3)), HARD_MAX_DEPTH))
        except (TypeError, ValueError):
            return jsonify(error="Invalid limits."), 400
        job = {
            "id": uuid.uuid4().hex[:12], "state": "running", "created": time.time(), "cancel": False,
            "crawled": 0, "queued": 1, "current": "", "pages": [], "truncated": False,
            "opts": {
                "mode": mode, "urls": url_list,
                "url": url, "max_urls": max_urls, "depth": depth,
                "respect_robots": bool(data.get("respect_robots", True)),
                "use_sitemap": bool(data.get("use_sitemap", False)),
                "keep_query": bool(data.get("keep_query", False)),
                "exclude": [str(x) for x in (data.get("exclude") or [])][:30],
            },
        }
        JOBS[job["id"]] = job
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return jsonify(job_id=job["id"])


@app.route("/api/status/<jid>")
def api_status(jid):
    job = JOBS.get(jid)
    if not job:
        return jsonify(error="Job not found or expired. Please run the crawl again."), 404
    out = {"state": job["state"], "crawled": job["crawled"], "queued": job["queued"],
           "current": job["current"], "elapsed": round(time.time() - job["created"], 1)}
    if job["state"] == "done":
        out.update(pages=job["pages"], start_url=job["start_url"], truncated=job["truncated"],
                   robots_sitemaps=job.get("robots_sitemaps", []), sitemap_found=job.get("sitemap_found", 0),
                   elapsed=job.get("elapsed"))
    elif job["state"] == "error":
        out["error"] = job.get("error", "Crawl failed.")
    return jsonify(out)


@app.route("/api/cancel/<jid>", methods=["POST"])
def api_cancel(jid):
    job = JOBS.get(jid)
    if job:
        job["cancel"] = True
    return jsonify(ok=True)


PAGE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>XML Sitemap Generator | SEO Tools by Mahalakshmi</title>
<meta name="description" content="Free XML sitemap generator. Crawl your site, skip noindex and broken pages, set priority, change frequency and last modified, preview live and download sitemap.xml.">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%237c6af7'/%3E%3Ctext x='50%25' y='50%25' dominant-baseline='central' text-anchor='middle' font-family='Georgia%2C serif' font-size='18' font-weight='900' fill='white'%3EM%3C/text%3E%3C/svg%3E">
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;600;700;800;900&family=Inter:wght@300;400;500;600&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
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

  .sidebar { width: 240px; flex-shrink: 0; background: var(--surface); border-right: 1px solid var(--border); padding: 1.6rem 1.2rem; position: sticky; top: 0; height: 100vh; display: flex; flex-direction: column; gap: 2rem; }
  .brand { display: flex; align-items: center; gap: 0.55rem; text-decoration: none; }
  .brand-mark { width: 30px; height: 30px; border-radius: 8px; background: var(--accent); display: flex; align-items: center; justify-content: center; font-family: 'Sora', sans-serif; font-weight: 900; font-size: 1rem; color: #fff; flex-shrink: 0; }
  .brand-text { font-family: 'Sora', sans-serif; font-weight: 700; font-size: 0.95rem; }
  .sidebar-section { display: flex; flex-direction: column; gap: 0.3rem; }
  .sidebar-label { font-family: 'DM Mono', monospace; font-size: 0.65rem; letter-spacing: 0.14em; text-transform: uppercase; color: var(--muted); margin-bottom: 0.5rem; }
  .sidebar-link { display: flex; align-items: center; gap: 0.6rem; padding: 0.55rem 0.7rem; border-radius: 8px; border: 1px solid transparent; font-size: 0.85rem; font-weight: 500; color: var(--muted); text-decoration: none; transition: all 0.15s; }
  .sidebar-link:hover { background: rgba(124,106,247,0.12); color: var(--text); }
  .sidebar-link.active { background: rgba(124,106,247,0.14); border: 1px solid rgba(124,106,247,0.4); color: var(--accent); font-weight: 700; }
  .sidebar-limits { margin-top: auto; background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 0.9rem; font-size: 0.72rem; color: var(--muted); line-height: 1.6; }
  .sidebar-limits strong { color: var(--text); }
  .sidebar-footer { padding-top: 1rem; border-top: 1px solid var(--border); }
  .sidebar-footer p { font-size: 0.7rem; color: var(--muted); line-height: 1.5; margin-bottom: 0.7rem; }
  .credit-name { color: var(--accent3); font-weight: 700; text-decoration: none; }
  .credit-name:hover { text-decoration: underline; }
  .sidebar-social { display: flex; gap: 0.5rem; }
  .sidebar-social a { width: 28px; height: 28px; border-radius: 6px; background: rgba(106,247,200,0.08); border: 1px solid rgba(106,247,200,0.35); display: flex; align-items: center; justify-content: center; color: var(--accent3); text-decoration: none; font-size: 0.68rem; font-family: 'DM Mono', monospace; transition: all 0.2s; }
  .sidebar-social a:hover { background: rgba(106,247,200,0.2); border-color: var(--accent3); }

  .main { flex: 1; padding: 2.2rem 3rem 4rem; max-width: 1240px; min-width: 0; }
  .page-head { margin-bottom: 1.6rem; }
  .page-eyebrow { font-family: 'DM Mono', monospace; font-size: 0.68rem; letter-spacing: 0.16em; text-transform: uppercase; color: var(--accent3); margin-bottom: 0.5rem; }
  .page-title { font-family: 'Sora', sans-serif; font-size: 1.7rem; font-weight: 800; margin-bottom: 0.4rem; }
  .page-sub { color: var(--muted); font-size: 0.92rem; max-width: 700px; }

  .card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 1.4rem; margin-bottom: 1.2rem; }
  .card h3 { font-family: 'Sora', sans-serif; font-size: 0.9rem; font-weight: 700; margin-bottom: 0.25rem; }
  .card .hint { font-size: 0.76rem; color: var(--muted); margin-bottom: 0.9rem; }
  code { font-family: 'DM Mono', monospace; color: var(--accent3); font-size: 0.76rem; }

  label.lbl { display: block; font-size: 0.72rem; font-family: 'DM Mono', monospace; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); margin-bottom: 0.4rem; }
  select, textarea, input[type=text], input[type=number], input[type=date] { width: 100%; background: var(--surface2); border: 1px solid var(--border); border-radius: 8px; color: var(--text); padding: 0.6rem 0.8rem; font-family: 'Inter', sans-serif; font-size: 0.85rem; }
  textarea { font-family: 'DM Mono', monospace; font-size: 0.78rem; min-height: 78px; resize: vertical; }
  select:focus, textarea:focus, input:focus { outline: none; border-color: var(--accent); }
  .url-row { display: grid; grid-template-columns: 1fr auto; gap: 0.7rem; align-items: end; margin-bottom: 1rem; }
  .grid4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1rem; margin-bottom: 1rem; }
  .grid3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
  .checks { display: flex; gap: 1.4rem; flex-wrap: wrap; margin-bottom: 1rem; }
  .chk { display: flex; align-items: center; gap: 0.5rem; font-size: 0.82rem; color: var(--text); cursor: pointer; }
  .chk input { accent-color: var(--accent); width: 15px; height: 15px; }
  details.adv { margin-top: 0.3rem; }
  details.adv summary { cursor: pointer; font-size: 0.8rem; color: var(--accent); font-weight: 600; margin-bottom: 0.8rem; }

  .btn-primary { background: var(--accent); color: #fff; border: none; padding: 0.65rem 1.5rem; border-radius: 8px; font-family: 'Inter', sans-serif; font-weight: 600; font-size: 0.86rem; cursor: pointer; transition: background 0.2s, transform 0.2s; }
  .btn-primary:hover { background: #6a58e8; transform: translateY(-1px); }
  .btn-primary:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }
  .btn-secondary { background: var(--surface2); border: 1px solid var(--border); color: var(--text); padding: 0.6rem 1.1rem; border-radius: 8px; font-size: 0.82rem; font-weight: 600; cursor: pointer; }
  .btn-secondary:hover { border-color: var(--accent); color: var(--accent); }
  .btn-row { display: flex; gap: 0.5rem; flex-wrap: wrap; }

  .progress { display: none; }
  .bar { height: 8px; background: var(--surface2); border-radius: 100px; overflow: hidden; margin: 0.7rem 0 0.5rem; }
  .bar > div { height: 100%; width: 30%; background: linear-gradient(90deg, var(--accent), var(--accent3)); border-radius: 100px; animation: slide 1.4s ease-in-out infinite; }
  @keyframes slide { 0% { margin-left: -30%; } 100% { margin-left: 100%; } }
  .prog-line { font-family: 'DM Mono', monospace; font-size: 0.74rem; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .err-box { display: none; background: rgba(247,106,106,0.1); border: 1px solid rgba(247,106,106,0.3); color: #ff9d9d; border-radius: 10px; padding: 0.8rem 1rem; font-size: 0.85rem; margin-bottom: 1.2rem; }

  #results { display: none; }
  .stat-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 0.8rem; margin-bottom: 1.2rem; }
  .stat { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 0.9rem 1rem; }
  .stat .n { font-family: 'Sora', sans-serif; font-size: 1.5rem; font-weight: 800; }
  .stat .t { font-family: 'DM Mono', monospace; font-size: 0.64rem; letter-spacing: 0.1em; text-transform: uppercase; color: var(--muted); }
  .stat.good .n { color: var(--accent3); } .stat.bad .n { color: var(--danger); } .stat.warn .n { color: var(--accent2); }
  .notice { font-size: 0.8rem; padding: 0.6rem 0.9rem; border-radius: 8px; border: 1px solid rgba(247,162,106,0.3); background: rgba(247,162,106,0.1); color: var(--accent2); margin-bottom: 1rem; display: none; }

  .tabs { display: flex; gap: 0.4rem; margin-bottom: 1rem; flex-wrap: wrap; }
  .tab { background: var(--surface2); border: 1px solid var(--border); color: var(--muted); padding: 0.45rem 1rem; border-radius: 100px; font-size: 0.78rem; font-family: 'DM Mono', monospace; cursor: pointer; }
  .tab.active { background: var(--accent); border-color: var(--accent); color: #fff; }
  .pane { display: none; } .pane.active { display: block; }
  .mode-tab { background: var(--surface2); border: 1px solid var(--border); color: var(--muted); padding: 0.5rem 1.1rem; border-radius: 100px; font-size: 0.8rem; font-family: 'DM Mono', monospace; cursor: pointer; }
  .mode-tab.active { background: var(--accent); border-color: var(--accent); color: #fff; }
  #crawlBtn { justify-self: start; }

  .tbl-wrap { max-height: 480px; overflow: auto; border: 1px solid var(--border); border-radius: 10px; }
  table { width: 100%; border-collapse: collapse; font-size: 0.78rem; }
  th { position: sticky; top: 0; background: var(--surface2); text-align: left; font-family: 'DM Mono', monospace; font-size: 0.65rem; letter-spacing: 0.1em; text-transform: uppercase; color: var(--muted); padding: 0.6rem 0.7rem; z-index: 1; }
  td { padding: 0.5rem 0.7rem; border-top: 1px solid var(--border); vertical-align: top; }
  td.url { font-family: 'DM Mono', monospace; font-size: 0.74rem; word-break: break-all; max-width: 460px; }
  td.url small { display: block; color: var(--muted); font-family: 'Inter', sans-serif; font-size: 0.7rem; }
  tr.off td { opacity: 0.45; }
  .badge { display: inline-block; font-family: 'DM Mono', monospace; font-size: 0.66rem; padding: 0.15rem 0.55rem; border-radius: 100px; }
  .badge.ok { background: rgba(106,247,200,0.12); color: var(--accent3); border: 1px solid rgba(106,247,200,0.3); }
  .badge.bad { background: rgba(247,106,106,0.12); color: var(--danger); border: 1px solid rgba(247,106,106,0.3); }
  .badge.warn { background: rgba(247,162,106,0.12); color: var(--accent2); border: 1px solid rgba(247,162,106,0.3); }
  .tbl-tools { display: flex; gap: 0.6rem; margin-bottom: 0.8rem; align-items: center; flex-wrap: wrap; }
  .tbl-tools input[type=text] { max-width: 300px; }
  .tbl-tools .count { font-size: 0.75rem; color: var(--muted); margin-left: auto; }

  .code-box { background: #08080d; border: 1px solid var(--border); border-radius: 10px; padding: 1rem; font-family: 'DM Mono', monospace; font-size: 0.75rem; line-height: 1.6; max-height: 480px; overflow: auto; white-space: pre; color: #cfd0ee; }
  .code-box .x-tag { color: var(--accent3); } .code-box .x-loc { color: var(--text); }
  .next-steps { list-style: none; counter-reset: s; display: flex; flex-direction: column; gap: 0.6rem; }
  .next-steps li { counter-increment: s; display: flex; gap: 0.8rem; font-size: 0.83rem; color: #c8c8e0; }
  .next-steps li::before { content: counter(s); width: 24px; height: 24px; flex-shrink: 0; border-radius: 50%; background: rgba(124,106,247,0.15); border: 1px solid rgba(124,106,247,0.3); color: var(--accent); font-family: 'DM Mono', monospace; font-size: 0.7rem; display: flex; align-items: center; justify-content: center; }
  .learn-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; }
  .learn-grid .card { margin-bottom: 0; }
  .learn-grid p, .learn-grid li { font-size: 0.8rem; color: var(--muted); }
  .learn-grid ul { padding-left: 1.1rem; }
  .toast { position: fixed; bottom: 1.5rem; right: 1.5rem; background: var(--accent3); color: #0a0a0f; padding: 0.6rem 1.1rem; border-radius: 8px; font-weight: 600; font-size: 0.82rem; opacity: 0; transform: translateY(10px); transition: all 0.25s; pointer-events: none; }
  .toast.show { opacity: 1; transform: none; }

  @media (max-width: 1000px) { .grid4 { grid-template-columns: 1fr 1fr; } .stat-grid { grid-template-columns: 1fr 1fr; } .learn-grid { grid-template-columns: 1fr; } }
  @media (max-width: 900px) { .sidebar { display: none; } .main { padding: 1.6rem 1.2rem 3rem; } .url-row, .grid3, .grid2 { grid-template-columns: 1fr; } }
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
      <a href="#" class="sidebar-link active">🗺️ XML Sitemap Generator</a>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-label">Navigate</div>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="sidebar-link">🧰 All My Tools</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio" class="sidebar-link">👩‍💻 Portfolio</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/blog/index.html" class="sidebar-link">📝 Blog Posts</a>
    </div>
    <div class="sidebar-limits">
      <strong>Free-tier limits</strong><br>
      Up to <strong>500 pages</strong> per crawl, depth 5. Nothing you crawl is stored — results are kept in memory for about 30 minutes.
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
    <div class="page-head">
      <div class="page-eyebrow">// seo tool</div>
      <div class="page-title">XML Sitemap Generator</div>
      <div class="page-sub">Crawl your website, keep only the pages that belong in search, tune priority, change frequency and last modified, then download a ready-to-upload <code>sitemap.xml</code>.</div>
    </div>

    <div class="card">
      <h3>1. Choose how to add your pages</h3>
      <div class="hint">Crawl a whole site from its homepage, or paste the exact URLs you want checked. Either way, noindex, canonicalised, broken and redirecting pages are skipped.</div>
      <div class="tabs" id="modeTabs">
        <button class="mode-tab active" data-mode="crawl">Crawl a website</button>
        <button class="mode-tab" data-mode="list">Paste a list of URLs</button>
      </div>
      <div id="listWrap" style="display:none;margin-bottom:1rem">
        <label class="lbl" for="urlList">Your URLs — one per line (up to 500)</label>
        <textarea id="urlList" style="min-height:170px" placeholder="https://www.example.com/&#10;https://www.example.com/about&#10;https://www.example.com/blog/my-post"></textarea>
        <div class="hint" id="listCount" style="margin:0.4rem 0 0">0 URLs. Every URL is checked (status, noindex, canonical, redirects) and only the good ones go into the sitemap. Links on the pages are not followed.</div>
      </div>
      <div class="url-row">
        <div id="urlWrap">
          <label class="lbl" for="siteUrl">Website URL</label>
          <input type="text" id="siteUrl" placeholder="https://www.example.com" autocomplete="off">
        </div>
        <button class="btn-primary" id="crawlBtn">Generate sitemap</button>
      </div>
      <div class="grid4">
        <div id="wrapMax">
          <label class="lbl" for="maxUrls">Max pages</label>
          <select id="maxUrls"><option>50</option><option selected>100</option><option>250</option><option>500</option></select>
        </div>
        <div id="wrapDepth">
          <label class="lbl" for="depth">Crawl depth</label>
          <select id="depth"><option value="1">1 level</option><option value="2">2 levels</option><option value="3" selected>3 levels</option><option value="4">4 levels</option><option value="5">5 levels</option></select>
        </div>
        <div>
          <label class="lbl" for="chMode">Change frequency</label>
          <select id="chMode">
            <option value="auto">Auto (by depth)</option>
            <option value="none">Leave out</option>
            <option value="always">always</option><option value="hourly">hourly</option><option value="daily">daily</option>
            <option value="weekly">weekly</option><option value="monthly">monthly</option><option value="yearly">yearly</option><option value="never">never</option>
          </select>
        </div>
        <div>
          <label class="lbl" for="prMode">Priority</label>
          <select id="prMode">
            <option value="auto">Auto (by depth)</option>
            <option value="none">Leave out</option>
            <option value="1.0">1.0</option><option value="0.9">0.9</option><option value="0.8">0.8</option><option value="0.7">0.7</option>
            <option value="0.6">0.6</option><option value="0.5">0.5</option><option value="0.4">0.4</option><option value="0.3">0.3</option>
          </select>
        </div>
      </div>
      <div class="grid3" style="margin-bottom:1rem">
        <div>
          <label class="lbl" for="lmMode">Last modified</label>
          <select id="lmMode">
            <option value="server">From server header (if sent)</option>
            <option value="today">Today's date</option>
            <option value="custom">A date I choose</option>
            <option value="none">Leave out</option>
          </select>
        </div>
        <div id="lmCustomWrap" style="display:none">
          <label class="lbl" for="lmCustom">Custom date</label>
          <input type="date" id="lmCustom">
        </div>
      </div>
      <div class="checks">
        <label class="chk"><input type="checkbox" id="optRobots" checked> Respect robots.txt</label>
        <label class="chk" id="lblSitemap"><input type="checkbox" id="optSitemap"> Also discover URLs from my existing sitemap.xml</label>
        <label class="chk" id="lblQuery"><input type="checkbox" id="optQuery"> Include URLs with query strings (?a=b)</label>
      </div>
      <details class="adv">
        <summary>Advanced: exclude patterns and extra URLs</summary>
        <div class="grid2">
          <div>
            <label class="lbl" for="excl">Exclude URLs containing (one per line, * allowed)</label>
            <textarea id="excl" placeholder="/admin/&#10;/cart&#10;/tag/*"></textarea>
          </div>
          <div>
            <label class="lbl" for="manual">Add extra URLs manually (one per line)</label>
            <textarea id="manual" placeholder="https://www.example.com/landing-page/"></textarea>
          </div>
        </div>
      </details>
    </div>

    <div class="err-box" id="errBox"></div>

    <div class="card progress" id="progress">
      <h3>Checking pages…</h3>
      <div class="bar"><div></div></div>
      <div class="prog-line" id="progLine">Starting…</div>
      <div class="btn-row" style="margin-top:0.8rem"><button class="btn-secondary" id="cancelBtn">Stop and use what was found</button></div>
    </div>

    <div id="results">
      <div class="stat-grid" id="stats"></div>
      <div class="notice" id="notice"></div>

      <div class="card">
        <h3>2. Review and download</h3>
        <div class="hint">Untick any page you don't want. Priority, change frequency and last modified update instantly — no re-crawl needed.</div>
        <div class="tabs">
          <button class="tab active" data-pane="pPages">Pages</button>
          <button class="tab" data-pane="pXml">sitemap.xml preview</button>
          <button class="tab" data-pane="pExcl">Skipped &amp; issues</button>
        </div>

        <div class="pane active" id="pPages">
          <div class="tbl-tools">
            <input type="text" id="filter" placeholder="Filter URLs…">
            <button class="btn-secondary" id="selAll">Select all</button>
            <button class="btn-secondary" id="selNone">Select none</button>
            <label class="chk"><input type="checkbox" id="showSkipped"> Show skipped pages too</label>
            <span class="count" id="rowCount"></span>
          </div>
          <div class="tbl-wrap"><table>
            <thead><tr><th style="width:36px"></th><th>URL</th><th>Depth</th><th>Priority</th><th>Changefreq</th><th>Lastmod</th></tr></thead>
            <tbody id="pagesBody"></tbody>
          </table></div>
        </div>
        <div class="pane" id="pXml"><div class="code-box" id="xmlBox"></div></div>
        <div class="pane" id="pExcl">
          <div class="tbl-wrap"><table>
            <thead><tr><th>URL</th><th>Status</th><th>Why it was skipped</th></tr></thead>
            <tbody id="exclBody"></tbody>
          </table></div>
        </div>

        <div class="btn-row" style="margin-top:1rem">
          <button class="btn-primary" id="dlXml">⬇ Download sitemap.xml</button>
          <button class="btn-secondary" id="copyXml">Copy XML</button>
          <button class="btn-secondary" id="dlTxt">URL list (.txt)</button>
          <button class="btn-secondary" id="dlCsv">Full report (.csv)</button>
        </div>
      </div>

      <div class="card">
        <h3>3. Next steps</h3>
        <div class="hint">How to get the sitemap in front of Google and Bing.</div>
        <ol class="next-steps">
          <li><span>Upload <code>sitemap.xml</code> to the root of your site so it opens at <code id="smUrl">https://yourdomain.com/sitemap.xml</code>.</span></li>
          <li><span>Add this line to your robots.txt: <code id="robotsLine">Sitemap: https://yourdomain.com/sitemap.xml</code> <button class="btn-secondary" id="copyRobots" style="padding:0.2rem 0.6rem;font-size:0.7rem;margin-left:0.4rem">Copy</button></span></li>
          <li><span>Submit the URL in Google Search Console (Indexing → Sitemaps) and Bing Webmaster Tools.</span></li>
        </ol>
      </div>
    </div>

    <div class="learn-grid">
      <div class="card">
        <h3>What is an XML sitemap?</h3>
        <p>A file listing the URLs you want search engines to crawl and index, with optional hints about how important each page is and when it last changed. It helps crawlers find pages that internal links alone might miss.</p>
      </div>
      <div class="card">
        <h3>Which pages should be in it?</h3>
        <ul>
          <li>Only indexable pages that return 200</li>
          <li>Canonical URLs, not duplicates</li>
          <li>No noindex, redirected or blocked pages</li>
        </ul>
      </div>
      <div class="card">
        <h3>Good to know</h3>
        <ul>
          <li>Google ignores <code>priority</code> and <code>changefreq</code>; an accurate <code>lastmod</code> is what it uses.</li>
          <li>One sitemap holds up to 50,000 URLs or 50 MB.</li>
          <li>A crawler can only find pages that are linked — orphan pages need the manual box.</li>
        </ul>
      </div>
    </div>
  </main>
</div>
<div class="toast" id="toast"></div>

<script>
// ==CORE== (pure logic — no DOM access)
function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&apos;'); }

function autoPriority(d) { return d <= 0 ? '1.0' : d === 1 ? '0.8' : d === 2 ? '0.6' : d === 3 ? '0.5' : '0.4'; }
function autoChange(d) { return d <= 0 ? 'daily' : d <= 2 ? 'weekly' : 'monthly'; }

function todayStr() { const d = new Date(); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); }

function metaFor(p, s) {
  const pr = s.prMode === 'auto' ? autoPriority(p.depth) : s.prMode === 'none' ? '' : s.prMode;
  const ch = s.chMode === 'auto' ? autoChange(p.depth) : s.chMode === 'none' ? '' : s.chMode;
  let lm = '';
  if (s.lmMode === 'server') lm = p.lastmod || '';
  else if (s.lmMode === 'today') lm = todayStr();
  else if (s.lmMode === 'custom') lm = s.lmCustom || '';
  return { pr, ch, lm };
}

function buildXml(rows, s) {
  const out = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'];
  rows.forEach(p => {
    const m = metaFor(p, s);
    out.push('  <url>');
    out.push('    <loc>' + esc(p.url) + '</loc>');
    if (m.lm) out.push('    <lastmod>' + m.lm + '</lastmod>');
    if (m.ch) out.push('    <changefreq>' + m.ch + '</changefreq>');
    if (m.pr) out.push('    <priority>' + m.pr + '</priority>');
    out.push('  </url>');
  });
  out.push('</urlset>');
  return out.join('\n') + '\n';
}

function csvCell(v) { v = String(v == null ? '' : v); return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; }
// ==/CORE==

const $ = id => document.getElementById(id);
let PAGES = [];      // {url, depth, status, lastmod, title, reason, note, include}
let MANUAL = [];
let START = '';
let pollTimer = null, jobId = null;

function toast(msg) { const t = $('toast'); t.textContent = msg; t.classList.add('show'); setTimeout(() => t.classList.remove('show'), 1800); }
function showErr(msg) { const b = $('errBox'); b.textContent = msg; b.style.display = msg ? 'block' : 'none'; }

let MODE = 'crawl';
function setMode(m) {
  MODE = m;
  document.querySelectorAll('.mode-tab').forEach(t => t.classList.toggle('active', t.dataset.mode === m));
  const list = m === 'list';
  $('listWrap').style.display = list ? 'block' : 'none';
  ['urlWrap', 'wrapMax', 'wrapDepth', 'lblSitemap', 'lblQuery'].forEach(id => { $(id).style.display = list ? 'none' : ''; });
  $('crawlBtn').textContent = list ? 'Check URLs & generate sitemap' : 'Generate sitemap';
}
function listUrls() { return $('urlList').value.split(/\r?\n/).map(x => x.trim()).filter(Boolean); }

function settings() {
  return { prMode: $('prMode').value, chMode: $('chMode').value, lmMode: $('lmMode').value, lmCustom: $('lmCustom').value };
}

function manualRows() {
  const seen = new Set(PAGES.filter(p => p.include).map(p => p.url));
  const rows = [];
  $('manual').value.split(/\r?\n/).map(x => x.trim()).filter(Boolean).forEach(u => {
    if (/^https?:\/\/\S+$/i.test(u) && !seen.has(u)) { seen.add(u); rows.push({ url: u, depth: 1, lastmod: '' }); }
  });
  return rows;
}

function includedRows() { return PAGES.filter(p => p.include).concat(manualRows()); }

async function startCrawl() {
  showErr('');
  const url = $('siteUrl').value.trim();
  if (MODE === 'crawl' && !url) { showErr('Enter your website URL first.'); return; }
  if (MODE === 'list' && !listUrls().length) { showErr('Paste at least one URL first.'); return; }
  $('results').style.display = 'none';
  $('crawlBtn').disabled = true;
  $('progress').style.display = 'block';
  $('progLine').textContent = 'Starting…';
  const body = {
    mode: MODE, urls: listUrls(),
    url, max_urls: +$('maxUrls').value, depth: +$('depth').value,
    respect_robots: $('optRobots').checked, use_sitemap: $('optSitemap').checked, keep_query: $('optQuery').checked,
    exclude: $('excl').value.split(/\r?\n/).map(x => x.trim()).filter(Boolean)
  };
  try {
    const r = await fetch('/api/crawl', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || 'Could not start the crawl.');
    jobId = j.job_id;
    poll();
  } catch (e) { endProgress(); showErr(e.message); }
}

function endProgress() { $('progress').style.display = 'none'; $('crawlBtn').disabled = false; clearTimeout(pollTimer); }

async function poll() {
  try {
    const r = await fetch('/api/status/' + jobId);
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || 'Lost the crawl job.');
    if (j.state === 'running') {
      $('progLine').textContent = `${j.crawled} pages checked · ${j.queued} waiting · ${Math.round(j.elapsed)}s — ${j.current || ''}`;
      pollTimer = setTimeout(poll, 1000);
    } else if (j.state === 'error') {
      endProgress(); showErr(j.error);
    } else {
      endProgress(); showResults(j);
    }
  } catch (e) { endProgress(); showErr(e.message); }
}

function showResults(j) {
  START = j.start_url;
  PAGES = j.pages.map(p => Object.assign({ include: !p.reason }, p));
  const inc = PAGES.filter(p => p.include).length;
  const broken = PAGES.filter(p => p.status >= 400 || (p.status === 0 && p.reason && p.reason.startsWith('Could not'))).length;
  const noidx = PAGES.filter(p => p.reason === 'noindex').length;
  const skipped = PAGES.length - inc;
  $('stats').innerHTML =
    `<div class="stat good"><div class="n" id="statIn">${inc}</div><div class="t">In sitemap</div></div>` +
    `<div class="stat"><div class="n">${PAGES.length}</div><div class="t">Pages checked</div></div>` +
    `<div class="stat warn"><div class="n">${skipped}</div><div class="t">Skipped</div></div>` +
    `<div class="stat bad"><div class="n">${broken}</div><div class="t">Broken (4xx/5xx)</div></div>` +
    `<div class="stat"><div class="n">${noidx}</div><div class="t">Noindex</div></div>`;
  const notes = [];
  if (j.truncated) notes.push('The crawl stopped at your page or time limit — there may be more pages. Raise "Max pages" or depth to find them.');
  if (j.sitemap_found) notes.push(`Found ${j.sitemap_found} URLs in your existing sitemap.xml and added them to the crawl.`);
  if (j.robots_sitemaps && j.robots_sitemaps.length) notes.push('Your robots.txt already lists a sitemap: ' + j.robots_sitemaps[0]);
  const cats = {};
  PAGES.filter(p => p.reason).forEach(p => {
    const r = p.reason;
    const k = r.startsWith('Canonical') ? 'canonical points to a different URL' : r.startsWith('Redirects') ? 'redirect to another URL'
      : r === 'noindex' ? 'noindex' : r.startsWith('HTTP') ? 'HTTP errors' : r.startsWith('Blocked') ? 'blocked by robots.txt'
      : r.startsWith('Matches') ? 'matched an exclude pattern' : r.startsWith('Different') ? 'on a different domain' : r.startsWith('Not a valid') ? 'not a valid URL' : 'other (not HTML / could not fetch)';
    cats[k] = (cats[k] || 0) + 1;
  });
  const catTxt = Object.entries(cats).sort((a, b) => b[1] - a[1]).map(([k, v]) => `${v} ${k}`).join(', ');
  if (catTxt) notes.push('Skipped pages: ' + catTxt + '. Open "Skipped & issues" for the full list, or tick "Show skipped pages too" to add any of them back.');
  if (cats['canonical points to a different URL'] > 3) notes.push('Many pages canonicalise to another URL. If your site is built with JavaScript or a template that copies one canonical tag to every page, fix the canonicals first — a sitemap should list each page\'s own canonical URL.');
  if (j.pages.length && PAGES.filter(p => p.title && /^loading/i.test(p.title)).length) notes.push('Some pages show "Loading…" as their title, which usually means the content is rendered by JavaScript. This crawler reads the HTML the server sends, so it cannot see JS-only links — use "discover from existing sitemap.xml" or add URLs manually.');
  const nb = $('notice'); nb.innerHTML = notes.map(esc).join('<br>'); nb.style.display = notes.length ? 'block' : 'none';
  const origin = new URL(START).origin;
  $('smUrl').textContent = origin + '/sitemap.xml';
  $('robotsLine').textContent = 'Sitemap: ' + origin + '/sitemap.xml';
  renderAll();
  $('results').style.display = 'block';
  $('results').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function renderAll() { renderPages(); renderExcluded(); renderXml(); }

function renderPages() {
  const f = $('filter').value.trim().toLowerCase();
  const s = settings();
  const showSk = $('showSkipped').checked;
  const eligible = PAGES.map((p, i) => ({ p, i })).filter(x => !x.p.reason || showSk);
  const shown = eligible.filter(x => !f || x.p.url.toLowerCase().includes(f));
  $('pagesBody').innerHTML = shown.map(({ p, i }) => {
    const m = metaFor(p, s);
    const dead = p.status >= 400 || p.status === 0;
    const sub = p.reason ? `<small><span class="badge ${dead ? 'bad' : 'warn'}">skipped</span> ${esc(p.reason)}</small>` : (p.title ? `<small>${esc(p.title)}</small>` : '');
    return `<tr class="${p.include ? '' : 'off'}"><td><input type="checkbox" data-i="${i}" ${p.include ? 'checked' : ''} ${dead ? 'disabled' : ''}></td>` +
      `<td class="url">${esc(p.url)}${sub}</td><td>${p.depth}</td>` +
      `<td>${m.pr || '—'}</td><td>${m.ch || '—'}</td><td>${m.lm || '—'}</td></tr>`;
  }).join('') || '<tr><td colspan="6" style="color:var(--muted)">No pages to show. Tick "Show skipped pages too" to see everything the crawler found.</td></tr>';
  const sel = PAGES.filter(p => p.include).length;
  $('rowCount').textContent = `${sel} in sitemap` + (manualRows().length ? ` + ${manualRows().length} manual` : '');
}

function renderExcluded() {
  const ex = PAGES.filter(p => p.reason);
  $('exclBody').innerHTML = ex.map(p => {
    const cls = p.status >= 400 || p.status === 0 ? 'bad' : 'warn';
    return `<tr><td class="url">${esc(p.url)}</td><td><span class="badge ${cls}">${p.status || '—'}</span></td><td>${esc(p.reason)}</td></tr>`;
  }).join('') || '<tr><td colspan="3" style="color:var(--muted)">Nothing was skipped.</td></tr>';
}

function highlightXml(x) {
  return x.split('\n').map(l => {
    const m = l.match(/^(\s*)<(\w+)>(.*)<\/(\w+)>$/);
    if (m) return `${m[1]}<span class="x-tag">&lt;${m[2]}&gt;</span><span class="x-loc">${m[3]}</span><span class="x-tag">&lt;/${m[4]}&gt;</span>`;
    return `<span class="x-tag">${l.replace(/</g, '&lt;').replace(/>/g, '&gt;')}</span>`;
  }).join('\n');
}

function renderXml() {
  const rows = includedRows();
  const xml = buildXml(rows, settings());
  $('xmlBox').innerHTML = highlightXml(xml);
  $('xmlBox').dataset.raw = xml;
  if ($('statIn')) $('statIn').textContent = PAGES.filter(p => p.include).length;
  const over = rows.length > 50000;
  if (over) toast('Over 50,000 URLs — split into several sitemaps');
}

function currentXml() { return buildXml(includedRows(), settings()); }

function download(name, text, type) {
  const blob = new Blob([text], { type: type + ';charset=utf-8' });
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = name;
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(a.href), 500);
}

function copy(text, msg) {
  const done = () => toast(msg);
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, () => fb());
  else fb();
  function fb() { const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); try { document.execCommand('copy'); done(); } catch (e) { toast('Copy failed'); } document.body.removeChild(ta); }
}

function csvReport() {
  const s = settings();
  const lines = [['url', 'in_sitemap', 'depth', 'http_status', 'priority', 'changefreq', 'lastmod', 'skipped_reason', 'title'].join(',')];
  PAGES.forEach(p => {
    const m = metaFor(p, s);
    lines.push([p.url, p.include ? 'yes' : 'no', p.depth, p.status || '', p.include ? m.pr : '', p.include ? m.ch : '', p.include ? m.lm : '', p.reason || '', p.title].map(csvCell).join(','));
  });
  return lines.join('\n') + '\n';
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.mode-tab').forEach(t => t.onclick = () => setMode(t.dataset.mode));
  $('urlList').addEventListener('input', () => { const n = listUrls().length; $('listCount').textContent = n + (n === 1 ? ' URL' : ' URLs') + (n > 500 ? ' — only the first 500 will be checked.' : '. Every URL is checked (status, noindex, canonical, redirects) and only the good ones go into the sitemap. Links on the pages are not followed.'); });
  $('crawlBtn').onclick = startCrawl;
  $('siteUrl').addEventListener('keydown', e => { if (e.key === 'Enter') startCrawl(); });
  $('cancelBtn').onclick = () => { if (jobId) fetch('/api/cancel/' + jobId, { method: 'POST' }); $('progLine').textContent = 'Stopping…'; };
  $('lmMode').onchange = () => { $('lmCustomWrap').style.display = $('lmMode').value === 'custom' ? 'block' : 'none'; if (PAGES.length) renderAll(); };
  ['prMode', 'chMode', 'lmCustom'].forEach(id => $(id).addEventListener('input', () => { if (PAGES.length) renderAll(); }));
  $('manual').addEventListener('input', () => { if (PAGES.length) renderAll(); });
  $('filter').addEventListener('input', renderPages);
  $('showSkipped').addEventListener('change', renderPages);
  $('pagesBody').addEventListener('change', e => {
    if (e.target.matches('input[data-i]')) { PAGES[+e.target.dataset.i].include = e.target.checked; renderPages(); renderXml(); }
  });
  $('selAll').onclick = () => { PAGES.forEach(p => { if (!p.reason) p.include = true; }); renderPages(); renderXml(); };
  $('selNone').onclick = () => { PAGES.forEach(p => { if (!p.reason) p.include = false; }); renderPages(); renderXml(); };
  document.querySelectorAll('.tab[data-pane]').forEach(t => t.onclick = () => {
    document.querySelectorAll('.tab[data-pane]').forEach(x => x.classList.toggle('active', x === t));
    document.querySelectorAll('.pane').forEach(x => x.classList.toggle('active', x.id === t.dataset.pane));
  });
  $('dlXml').onclick = () => download('sitemap.xml', currentXml(), 'application/xml');
  $('copyXml').onclick = () => copy(currentXml(), 'Copied sitemap XML');
  $('dlTxt').onclick = () => download('sitemap-urls.txt', includedRows().map(p => p.url).join('\n') + '\n', 'text/plain');
  $('dlCsv').onclick = () => download('sitemap-report.csv', csvReport(), 'text/csv');
  $('copyRobots').onclick = () => copy($('robotsLine').textContent, 'Copied robots.txt line');
});
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(debug=True, port=5055)
