"""
Google Index Checker  -  Flask app
Can Google index these URLs? For every URL the tool checks what decides
indexing - status code, redirects, robots.txt, noindex (meta + header),
canonical and sitemap - and explains the result in plain English.
A one-click "Is it on Google?" button runs the real site: search in your
own browser, so the answer comes straight from Google.

Built by Mahalakshmi Marimuthu - Digital Marketing Strategist & AI-Powered SEO Expert
"""
import gzip
import re
import threading
import json
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

MAX_URLS = 20
TIMEOUT = 12
PAGE_LIMIT = 2 * 1024 * 1024
SITEMAP_LIMIT = 10 * 1024 * 1024
MAX_SITEMAP_FILES = 12
PER_HOST = 4
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": BROWSER_UA,
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
           "Accept-Language": "en-US,en;q=0.9"}

AI_BOTS = [("GPTBot", "gptbot"), ("OAI-SearchBot", "oai-searchbot"), ("ClaudeBot", "claudebot"),
           ("PerplexityBot", "perplexitybot"), ("Google-Extended", "google-extended")]

_host_locks, _host_sems = {}, {}
_glock = threading.Lock()


def host_sem(host):
    with _glock:
        return _host_sems.setdefault(host, threading.Semaphore(PER_HOST))


def get(url, **kw):
    """GET with a real browser UA, a per-host cap and one retry on 403/429/503."""
    host = urlparse(url).netloc.lower()
    with host_sem(host):
        for attempt in range(2):
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True, stream=True, **kw)
            if r.status_code in (403, 429, 503) and attempt == 0:
                r.close()
                time.sleep(1.2)
                continue
            return r


def read(r, limit):
    try:
        return r.raw.read(limit, decode_content=True) or b""
    finally:
        r.close()


def norm_url(u):
    """Compare URLs loosely: lowercase host, no fragment, no trailing slash."""
    p = urlparse(u.strip())
    path = p.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", p.query, ""))


def clean_input(raw):
    raw = raw.strip()
    if not raw:
        return ""
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    return raw


# --------------------------------------------------------------------------- #
# robots.txt (Google rules: longest match wins, Allow wins a tie, * and $)
# --------------------------------------------------------------------------- #
def parse_robots(text):
    groups, sitemaps, cur, last_ua = [], [], None, False
    for i, raw in enumerate(re.split(r"\r\n|\r|\n", text.lstrip("﻿")), start=1):
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip().lower(), v.strip()
        if k in ("user-agent", "useragent"):
            tok = "*" if v.startswith("*") else (re.match(r"[A-Za-z_\-]*", v).group(0).lower())
            if not tok:
                continue
            if cur is None or not last_ua:
                cur = {"agents": [], "rules": []}
                groups.append(cur)
            cur["agents"].append(tok)
            last_ua = True
        elif k in ("allow", "disallow"):
            last_ua = False
            if cur is not None:
                cur["rules"].append({"type": k, "path": v, "line": i, "raw": raw.strip()})
        elif k == "sitemap" and re.match(r"^https?://", v, re.I):
            sitemaps.append(v)
    return {"groups": groups, "sitemaps": sitemaps}


SAFE = "".join(chr(c) for c in range(33, 127) if chr(c) != "%")


def _enc(s):
    s = re.sub(r"%[0-9a-fA-F]{2}", lambda m: m.group(0).upper(), s)
    return quote(s, safe=SAFE + "%")


def robots_allows(parsed, token, url):
    p = urlparse(url)
    path = _enc((p.path or "/") + ("?" + p.query if p.query else ""))
    if path.startswith("/robots.txt"):
        return True, None
    groups = [g for g in parsed["groups"] if token in g["agents"]] or \
             [g for g in parsed["groups"] if "*" in g["agents"]]
    best = None
    for r in (r for g in groups for r in g["rules"]):
        if not r["path"]:
            continue
        pat = _enc(r["path"])
        anchored = pat.endswith("$")
        rx = "^" + "".join(".*" if c == "*" else re.escape(c) for c in (pat[:-1] if anchored else pat)) + ("$" if anchored else "")
        if re.match(rx, path, re.S):
            n = len(r["path"])
            if best is None or n > best[1] or (n == best[1] and r["type"] == "allow"):
                best = (r, n)
    if best is None:
        return True, None
    return best[0]["type"] == "allow", best[0]


# --------------------------------------------------------------------------- #
# Per-site data (robots.txt + sitemap URLs), cached per request
# --------------------------------------------------------------------------- #
def load_robots(origin):
    try:
        r = get(origin + "/robots.txt")
        body = read(r, 600 * 1024)
        if 200 <= r.status_code < 300 and not body.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
            return {"state": "ok", **parse_robots(body.decode(r.encoding or "utf-8", errors="replace"))}
        if r.status_code == 429 or r.status_code >= 500:
            return {"state": "error", "groups": [], "sitemaps": [], "status": r.status_code}
        return {"state": "missing", "groups": [], "sitemaps": [], "status": r.status_code}
    except requests.RequestException:
        return {"state": "unreachable", "groups": [], "sitemaps": []}


def fetch_sitemap(url):
    try:
        r = get(url)
        if r.status_code >= 400:
            r.close()
            return None
        body = read(r, SITEMAP_LIMIT)
        if body[:2] == b"\x1f\x8b":
            body = gzip.decompress(body)
        text = body.decode("utf-8", errors="replace")
        loc_rx = r"<loc>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</loc>"
        if re.search(r"<sitemapindex", text, re.I):
            return {"index": True, "locs": [l.strip() for l in re.findall(loc_rx, text, re.S | re.I)]}
        entries = {}
        for block in re.findall(r"<url>(.*?)</url>", text, re.S | re.I):
            m = re.search(loc_rx, block, re.S | re.I)
            if m:
                lm = re.search(r"<lastmod>\s*(.*?)\s*</lastmod>", block, re.S | re.I)
                entries[norm_url(m.group(1).strip())] = lm.group(1) if lm else ""
        return {"index": False, "entries": entries}
    except Exception:
        return None


def load_sitemaps(origin, robots):
    queue = list(dict.fromkeys(robots.get("sitemaps") or [origin + "/sitemap.xml"]))
    seen, urls, files = set(), {}, 0
    while queue and files < MAX_SITEMAP_FILES:
        batch = [u for u in queue[:MAX_SITEMAP_FILES - files] if u not in seen]
        queue = queue[len(batch):] if batch else queue[1:]
        if not batch:
            continue
        seen.update(batch)
        with ThreadPoolExecutor(max_workers=6) as ex:
            results = list(ex.map(fetch_sitemap, batch))
        for res in results:
            files += 1
            if not res:
                continue
            if res["index"]:
                queue.extend(res["locs"])
            else:
                urls.update(res["entries"])
    return {"urls": urls, "files": files, "more": bool(queue)}


class Site:
    def __init__(self, origin):
        self.origin = origin
        self.lock = threading.Lock()
        self.robots = self.sitemaps = None

    def get_robots(self):
        with self.lock:
            if self.robots is None:
                self.robots = load_robots(self.origin)
            return self.robots

    def get_sitemaps(self):
        robots = self.get_robots()
        with self.lock:
            if self.sitemaps is None:
                self.sitemaps = load_sitemaps(self.origin, robots)
            return self.sitemaps


# --------------------------------------------------------------------------- #
# Checking one URL
# --------------------------------------------------------------------------- #
CHALLENGE = re.compile(r"just a moment|attention required|access denied|are you a robot|captcha|request blocked", re.I)


def has_noindex(value):
    parts = {p.strip().lower() for p in re.split(r"[,\s]+", value or "") if p.strip()}
    return "noindex" in parts or "none" in parts


# --------------------------------------------------------------------------- #
# Freshness dates
# --------------------------------------------------------------------------- #
def parse_date(value):
    if not value:
        return None
    v = str(value).strip()
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        try:
            d = parsedate_to_datetime(v)
        except (TypeError, ValueError):
            m = re.match(r"(\d{4})-(\d{2})-(\d{2})", v)
            if not m:
                return None
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def page_dates(soup):
    """dateModified / datePublished from JSON-LD, then meta tags."""
    mod = pub = None
    for s in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        txt = s.string or s.get_text() or ""
        m = re.search(r'"dateModified"\s*:\s*"([^"]+)"', txt)
        p = re.search(r'"datePublished"\s*:\s*"([^"]+)"', txt)
        mod = mod or (m and m.group(1))
        pub = pub or (p and p.group(1))
    for prop in ("article:modified_time", "og:updated_time"):
        t = soup.find("meta", attrs={"property": prop})
        if t and not mod:
            mod = t.get("content")
    t = soup.find("meta", attrs={"property": "article:published_time"})
    if t and not pub:
        pub = t.get("content")
    return parse_date(mod), parse_date(pub)


def wayback_last(url):
    try:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        r = requests.get("https://archive.org/wayback/available", params={"url": url, "timestamp": ts},
                         headers={"User-Agent": BROWSER_UA}, timeout=8)
        snap = (r.json().get("archived_snapshots") or {}).get("closest") or {}
        if snap.get("available") and snap.get("timestamp"):
            d = datetime.strptime(snap["timestamp"][:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            return d, snap.get("url", "").replace("http://", "https://", 1)
    except Exception:
        pass
    return None, None


def fmt(d):
    return d.strftime("%d %b %Y") if d else None


DIRECTIVES_WITH_COLON = {"max-snippet", "max-image-preview", "max-video-preview", "unavailable_after"}


def xrt_noindex(header):
    """X-Robots-Tag: 'noindex' or 'googlebot: noindex' count; 'otherbot: noindex' doesn't."""
    bot = None
    for part in (header or "").split(","):
        m = re.match(r"^\s*([A-Za-z_\-]+)\s*:\s*(.*)$", part)
        if m and m.group(1).lower() not in DIRECTIVES_WITH_COLON:
            bot, part = m.group(1).lower(), m.group(2)
        if bot in (None, "googlebot") and has_noindex(part):
            return True
    return False


def check_url(url, sites):
    res = {"url": url, "reasons": [], "final_url": url, "status": None}
    add = lambda sev, msg: res["reasons"].append({"sev": sev, "msg": msg})
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        res.update(verdict="unknown", label="Invalid URL")
        add("fail", "This doesn't look like a web address.")
        return res

    try:
        r = get(url)
        body = read(r, PAGE_LIMIT)
    except requests.exceptions.Timeout:
        res.update(verdict="unknown", label="Couldn't check")
        add("info", "The page took too long to respond. Try again, or check it on Google with the button.")
        return res
    except requests.RequestException:
        res.update(verdict="unknown", label="Couldn't check")
        add("info", "Couldn't connect to this site. Check the address is correct.")
        return res

    final = r.url
    res.update(status=r.status_code, final_url=final, redirected=bool(r.history))
    fp = urlparse(final)
    site = sites.setdefault(f"{fp.scheme}://{fp.netloc}".lower(), Site(f"{fp.scheme}://{fp.netloc}"))
    ctype = (r.headers.get("Content-Type") or "").lower()
    is_html = "html" in ctype or (not ctype and b"<html" in body[:2000].lower())
    html = body.decode(r.encoding or "utf-8", errors="replace") if is_html else ""
    soup = BeautifulSoup(html, "html.parser") if html else None

    # blocked by bot protection?
    title = soup.title.get_text(" ", strip=True) if soup and soup.title else ""
    if r.status_code in (401, 403, 429, 503) or (r.status_code == 200 and CHALLENGE.search(title) and len(html) < 40000):
        res.update(verdict="unknown", label="Couldn't check")
        add("info", f"The site blocked this automated check (HTTP {r.status_code}). This doesn't mean Google is blocked — "
                    "use “Check on Google” to see the real answer.")
        return res

    fails, risks = [], []

    # 1. status code / redirects
    if r.history:
        chain = len(r.history)
        msg = (f"Redirects to {final}" + (f" ({chain} hops)" if chain > 1 else "") +
               ". Google indexes the final URL, not this one — link to the final URL directly.")
        risks.append(msg)
    if r.status_code >= 400:
        fails.append(f"The page returns HTTP {r.status_code}. Google drops pages that don't load.")
    elif 200 <= r.status_code < 300:
        pass

    # 2. robots.txt
    robots = site.get_robots()
    g_ok, rule = True, None
    if robots["state"] == "ok":
        g_ok, rule = robots_allows(robots, "googlebot", final)
        if not g_ok:
            fails.append(f"robots.txt blocks Googlebot (line {rule['line']}: “{rule['raw']}”). "
                         "Google can't read the page, so it can't rank it.")
    elif robots["state"] in ("error", "unreachable"):
        fails.append("robots.txt can't be read (server error or timeout). Google pauses crawling the whole site until it can.")

    # 3. noindex (meta + header)
    xrt = ", ".join(v for k, v in r.headers.items() if k.lower() == "x-robots-tag")
    x_noindex = xrt_noindex(xrt)
    meta_noindex = False
    if soup:
        for m in soup.find_all("meta", attrs={"name": True}):
            if m["name"].strip().lower() in ("robots", "googlebot") and has_noindex(m.get("content")):
                meta_noindex = True
    if meta_noindex or x_noindex:
        where = "a meta robots tag" if meta_noindex else "the X-Robots-Tag header"
        if not g_ok:
            fails.append(f"It also has noindex in {where} — but because robots.txt blocks the page, Google can't see it. "
                         "To remove a page from Google, allow crawling and keep the noindex.")
        else:
            fails.append(f"noindex found in {where}. Google will keep this page out of search — remove it if the page should rank.")

    # 4. canonical
    canon = None
    if soup:
        for l in soup.find_all("link", href=True):
            rel = l.get("rel") or []
            rel = rel if isinstance(rel, list) else [rel]
            if "canonical" in [x.lower() for x in rel]:
                canon = urljoin(final, l["href"].strip())
                break
    if not canon:
        m = re.search(r'<([^>]+)>\s*;\s*rel="?canonical"?', r.headers.get("Link", ""), re.I)
        if m:
            canon = urljoin(final, m.group(1))
    res["canonical"] = canon
    if canon and norm_url(canon) != norm_url(final):
        risks.append(f"Canonical points to a different URL ({canon}). Google will most likely index that one instead.")

    # 5. sitemap
    in_sm = sm_lastmod = None
    if 200 <= r.status_code < 300:
        sm = site.get_sitemaps()
        if sm["files"]:
            in_sm = norm_url(final) in sm["urls"]
            sm_lastmod = parse_date(sm["urls"].get(norm_url(final))) if in_sm else None
            res["sitemap_note"] = (f"checked {sm['files']} sitemap file{'s' if sm['files'] != 1 else ''}"
                                   + (" (first files only)" if sm["more"] else ""))
    res["in_sitemap"] = in_sm
    if in_sm and (meta_noindex or x_noindex or not g_ok):
        risks.append("Mixed signals: the URL is in your sitemap but is also blocked or noindexed. Pick one.")

    # 6. freshness dates
    mod, pub = page_dates(soup) if soup else (None, None)
    server = parse_date(r.headers.get("Last-Modified"))
    wb, wb_url = wayback_last(final)
    res["dates"] = {"sitemap": fmt(sm_lastmod), "modified": fmt(mod), "published": fmt(pub),
                    "server": fmt(server), "wayback": fmt(wb), "wayback_url": wb_url}
    fresh_note = None
    known = [d for d in (sm_lastmod, mod, pub, server) if d]
    now = datetime.now(timezone.utc)
    if known and 200 <= r.status_code < 300:
        newest = max(known)
        if newest > now.replace(microsecond=0) and (newest - now).days > 1:
            fresh_note = (f"A date on this page is in the future ({fmt(newest)}). Check your sitemap lastmod / dateModified.")
        elif (now - newest).days > 365:
            fresh_note = (f"Last updated {fmt(newest)} — over a year ago. Refreshing key pages helps Google recrawl them.")

    # verdict
    for f in fails:
        add("fail", f)
    for w in risks:
        add("warn", w)
    if fails:
        res.update(verdict="no", label="Not indexable")
    elif risks:
        res.update(verdict="risk", label="At risk")
    else:
        res.update(verdict="yes", label="Indexable")
        add("pass", "Loads fine, open to Googlebot, no noindex" + (", self-canonical" if canon else "") + ".")
        if canon is None and is_html:
            add("info", "No canonical tag — adding a self-referencing canonical is a good habit.")
    if in_sm is False and res["verdict"] != "no":
        add("info", "Not found in your sitemap" + (f" ({res['sitemap_note']})" if res.get("sitemap_note") else "")
            + ". Adding it helps Google discover the page.")
    if fresh_note:
        add("info", fresh_note)
    if not is_html and 200 <= r.status_code < 300:
        add("info", f"This is not an HTML page ({ctype.split(';')[0] or 'unknown type'}).")
    return res


def ai_access(sites):
    out = []
    for origin, site in sites.items():
        robots = site.robots
        if not robots:
            continue
        bots = []
        for name, tok in AI_BOTS:
            if robots["state"] == "ok":
                ok, _ = robots_allows(robots, tok, origin + "/")
            else:
                ok = robots["state"] == "missing"
            bots.append({"name": name, "allowed": ok})
        out.append({"site": origin, "bots": bots})
    return out


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/api/check", methods=["POST"])
def api_check():
    data = request.get_json(silent=True) or {}
    raw = [clean_input(u) for u in re.split(r"[\r\n]+", data.get("urls", "")) if u.strip()]
    urls = list(dict.fromkeys(u for u in raw if u))
    if not urls:
        return jsonify({"ok": False, "error": "Please enter at least one URL."}), 400
    truncated = len(urls) > MAX_URLS
    urls = urls[:MAX_URLS]
    sites = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        rows = list(ex.map(lambda u: check_url(u, sites), urls))
    return jsonify({"ok": True, "rows": rows, "ai": ai_access(sites), "truncated": truncated, "max": MAX_URLS})


@app.route("/")
def home():
    return Response(PAGE, mimetype="text/html")


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Google Index Checker – Can Google Index Your Pages?</title>
<meta name="description" content="Free Google Index Checker: paste up to 20 URLs and see if Google can index each one — with the exact reason (noindex, robots.txt, redirect, canonical, 404) and a one-click check on Google.">
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
  input[type=text],textarea{width:100%;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:.75rem 1rem;font-family:'DM Mono',monospace;font-size:.82rem;resize:vertical}
  input:focus,textarea:focus{outline:none;border-color:var(--accent)}
  .btn{display:inline-flex;align-items:center;gap:.5rem;background:var(--accent);color:#fff;border:none;border-radius:8px;padding:.7rem 1.5rem;font-size:.85rem;font-weight:600;cursor:pointer;font-family:'Inter',sans-serif;white-space:nowrap}
  .btn:hover{background:var(--accent-h)}
  .btn:disabled{opacity:.5;cursor:wait}
  .btn.ghost{background:var(--surface);border:1px solid var(--border);color:var(--text);font-weight:500;padding:.55rem 1.1rem;font-size:.8rem}
  .btn.ghost:hover{border-color:var(--accent)}
  .btn.sm{padding:.35rem .75rem;font-size:.72rem;background:rgba(124,106,247,.14);border:1px solid rgba(124,106,247,.4);color:var(--accent)}
  .btn.sm:hover{background:rgba(124,106,247,.25)}
  .form-foot{display:flex;gap:.8rem;align-items:center;flex-wrap:wrap;margin-top:.8rem}
  .hint{font-size:.72rem;color:var(--muted);font-family:'DM Mono',monospace}
  .err{font-size:.8rem;color:var(--red);margin-top:.6rem}
  .spinner{display:none;margin-top:.8rem;font-size:.78rem;color:var(--muted);font-family:'DM Mono',monospace}
  .spinner.show{display:block}
  .check{display:flex;gap:.5rem;align-items:center;font-size:.8rem;color:var(--muted)}
  .check input{accent-color:var(--accent)}

  .overview{display:grid;grid-template-columns:220px 1fr 1fr;gap:1rem;margin:1.4rem 0}
  .chart-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.1rem 1.2rem;min-width:0}
  .chart-card h3{font-family:'DM Mono',monospace;font-size:.66rem;letter-spacing:.12em;font-weight:500;margin-bottom:.8rem;color:var(--muted);text-transform:uppercase}
  .ring-wrap{position:relative;width:160px;height:160px;margin:0 auto}
  .ring-center{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
  .ring-center .score{font-family:'Sora',sans-serif;font-size:1.9rem;font-weight:800}
  .ring-center .sub{font-size:.6rem;font-family:'DM Mono',monospace;color:var(--muted);text-transform:uppercase;letter-spacing:.1em}
  .bar-box{position:relative;height:190px}
  .metrics{display:grid;grid-template-columns:1fr 1fr;gap:.7rem}
  .metric{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:.7rem .9rem;min-width:0}
  .metric .k{font-family:'DM Mono',monospace;font-size:.6rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
  .metric .v{font-family:'Sora',sans-serif;font-size:1.05rem;font-weight:700;margin-top:.15rem}
  .v.mint{color:var(--mint)} .v.orange{color:var(--orange)} .v.red{color:var(--red)} .v.muted{color:var(--muted)}

  .badge{display:inline-flex;align-items:center;gap:.3rem;font-size:.66rem;font-family:'DM Mono',monospace;padding:.22rem .6rem;border-radius:100px;white-space:nowrap;border:1px solid}
  .badge.yes{background:rgba(106,247,200,.1);border-color:rgba(106,247,200,.3);color:var(--mint)}
  .badge.risk{background:rgba(247,162,106,.1);border-color:rgba(247,162,106,.35);color:var(--orange)}
  .badge.no{background:rgba(247,106,106,.1);border-color:rgba(247,106,106,.35);color:var(--red)}
  .badge.unknown{background:rgba(136,136,168,.1);border-color:rgba(136,136,168,.3);color:var(--muted)}

  .sec-title{font-family:'Sora',sans-serif;font-size:1rem;font-weight:700;margin:2rem 0 .9rem;display:flex;align-items:center;gap:.6rem;flex-wrap:wrap}
  .sec-title .count{font-family:'DM Mono',monospace;font-size:.7rem;color:var(--muted);font-weight:400}
  .tbl-tools{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center;margin-bottom:.7rem}
  .tbl-tools input{max-width:280px;padding:.5rem .8rem}
  .tbl-wrap{overflow-x:auto;background:var(--surface);border:1px solid var(--border);border-radius:12px}
  table{width:100%;border-collapse:collapse;font-size:.8rem}
  th{font-family:'DM Mono',monospace;font-size:.6rem;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);text-align:left;padding:.65rem .8rem;border-bottom:1px solid var(--border);white-space:nowrap}
  td{padding:.7rem .8rem;border-bottom:1px solid var(--border);vertical-align:top}
  tr:last-child td{border-bottom:none}
  tbody tr:hover td{background:rgba(124,106,247,.05)}
  td.mono{font-family:'DM Mono',monospace;font-size:.72rem;word-break:break-all;max-width:340px}
  .small{font-size:.7rem;color:var(--muted);margin-top:.2rem}
  .dates{display:flex;flex-direction:column;gap:.1rem;margin-top:.45rem;padding-top:.4rem;border-top:1px dashed var(--border);font-family:'DM Mono',monospace;font-size:.66rem;color:var(--muted);word-break:normal}
  .dates b{color:var(--text);font-weight:500}
  .dates a{color:var(--accent)}
  ul.why{list-style:none;display:flex;flex-direction:column;gap:.3rem}
  ul.why li{font-size:.76rem;padding-left:1.1rem;position:relative;overflow-wrap:anywhere}
  ul.why li::before{position:absolute;left:0;font-weight:700}
  ul.why li.fail::before{content:'✕';color:var(--red)}
  ul.why li.warn::before{content:'!';color:var(--orange)}
  ul.why li.pass::before{content:'✓';color:var(--mint)}
  ul.why li.info{color:var(--muted)}
  ul.why li.info::before{content:'i';color:var(--muted)}

  .ai-row{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;padding:.7rem 1rem;border-bottom:1px solid var(--border)}
  .ai-row:last-child{border-bottom:none}
  .ai-site{font-family:'DM Mono',monospace;font-size:.72rem;color:var(--muted);min-width:200px}
  .note{font-size:.74rem;color:var(--muted);margin-top:.8rem;line-height:1.6}

  @media(max-width:1100px){.overview{grid-template-columns:220px 1fr}.overview .bars{grid-column:1/-1}}
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
      <a href="#" class="sidebar-link active">🔎&nbsp; Google Index Checker</a>
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
      <div class="crumb">// seo tool · free</div>
      <h1>🔎 Google Index <span>Checker</span></h1>
    </div>

    <form class="card" id="form">
      <label class="lbl" for="urls">Page URLs — one per line (up to 20)</label>
      <textarea id="urls" rows="4" placeholder="https://www.yourwebsite.com/&#10;https://www.yourwebsite.com/blog/my-post"></textarea>
      <div class="form-foot">
        <button class="btn" type="submit" id="runBtn">🔎 Check Indexing</button>
        <span class="hint">Checks status, redirects, robots.txt, noindex, canonical &amp; sitemap</span>
      </div>
      <div class="spinner" id="sp">Checking your pages… this can take up to a minute for big sites.</div>
      <div class="err" id="err"></div>
    </form>

    <div id="results" style="display:none">
      <div class="overview">
        <div class="chart-card">
          <h3>Indexability score</h3>
          <div class="ring-wrap" id="ring"></div>
        </div>
        <div class="chart-card">
          <h3>At a glance</h3>
          <div class="metrics" id="metrics"></div>
        </div>
        <div class="chart-card bars">
          <h3>Breakdown</h3>
          <div class="bar-box"><canvas id="chart"></canvas></div>
        </div>
      </div>

      <div class="sec-title">📋 Results <span class="count" id="resCount"></span></div>
      <div class="tbl-tools">
        <input type="text" id="filter" placeholder="Filter by URL…">
        <label class="check"><input type="checkbox" id="probOnly"> Show problems only</label>
        <button type="button" class="btn ghost" id="csvBtn" style="margin-left:auto">⬇ Download CSV</button>
      </div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>URL</th><th>Can Google index it?</th><th>Why</th><th>Is it on Google?</th></tr></thead>
          <tbody id="resBody"></tbody>
        </table>
      </div>
      <p class="note">Google no longer shows cached pages or crawl dates publicly, so the dates under each URL come from your sitemap, the page itself, the server and the Wayback Machine.<br>“Indexable” means nothing on the page stops Google from indexing it. To see if Google has <b>actually</b> indexed it, click
        <b>Check on Google</b> — it runs a <code>site:</code> search in your browser. For your own site, Search Console's URL Inspection gives the official answer.</p>

      <div class="sec-title">🧠 AI crawler access <span class="count">from robots.txt</span></div>
      <div class="tbl-wrap" id="aiBox"></div>
    </div>
  </main>
</div>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ORDER = { no: 0, risk: 1, unknown: 2, yes: 3 };
let DATA = null, chart = null;

function googleLink(u) {
  return 'https://www.google.com/search?q=' + encodeURIComponent('site:' + u.replace(/^https?:\/\//i, '').replace(/#.*$/, ''));
}

$('form').addEventListener('submit', async e => {
  e.preventDefault();
  const urls = $('urls').value.trim();
  $('err').textContent = '';
  if (!urls) { $('err').textContent = 'Please enter at least one URL.'; return; }
  $('sp').classList.add('show'); $('runBtn').disabled = true;
  try {
    const r = await fetch('/api/check', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ urls }) });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'Something went wrong.');
    DATA = j; render(); $('results').style.display = '';
    if (j.truncated) $('err').textContent = `Only the first ${j.max} URLs were checked.`;
  } catch (err) {
    $('err').textContent = err.message || 'Could not reach the server — please try again.';
  } finally {
    $('sp').classList.remove('show'); $('runBtn').disabled = false;
  }
});

function render() {
  const rows = DATA.rows, n = k => rows.filter(r => r.verdict === k).length;
  const yes = n('yes'), risk = n('risk'), no = n('no'), unk = n('unknown');
  const checked = rows.length - unk;
  const sc = checked ? Math.round((yes + risk * 0.5) / checked * 100) : 0;
  const col = sc >= 80 ? '#6af7c8' : sc >= 50 ? '#f7a26a' : '#f76a6a';
  $('ring').innerHTML = `<svg viewBox="0 0 160 160" width="160" height="160">
      <circle cx="80" cy="80" r="68" fill="none" stroke="#23233a" stroke-width="12"/>
      <circle cx="80" cy="80" r="68" fill="none" stroke="${checked ? col : '#23233a'}" stroke-width="12" stroke-linecap="${sc > 0 ? 'round' : 'butt'}"
        stroke-dasharray="${(427.26 * sc / 100).toFixed(2)} 427.26" transform="rotate(-90 80 80)"/></svg>
    <div class="ring-center"><div class="score" style="color:${checked ? col : '#8888a8'}">${checked ? sc : '–'}</div><div class="sub">out of 100</div></div>`;
  $('metrics').innerHTML = [
    ['Indexable', yes, 'mint'], ['At risk', risk, 'orange'],
    ['Not indexable', no, 'red'], ["Couldn't check", unk, 'muted'],
  ].map(([k, v, c]) => `<div class="metric"><div class="k">${k}</div><div class="v ${c}">${v}</div></div>`).join('');
  if (typeof Chart !== 'undefined') {
    const data = { labels: ['Indexable', 'At risk', 'Not indexable', "Couldn't check"],
      datasets: [{ data: [yes, risk, no, unk], backgroundColor: ['#6af7c8', '#f7a26a', '#f76a6a', '#55557a'], borderColor: '#12121a', borderWidth: 3 }] };
    if (chart) { chart.data = data; chart.update(); }
    else chart = new Chart($('chart'), { type: 'doughnut', data, options: { cutout: '65%', maintainAspectRatio: false,
      plugins: { legend: { position: 'bottom', labels: { color: '#8888a8', font: { family: 'DM Mono', size: 11 }, boxWidth: 10 } } } } });
  }
  renderTable();
  $('aiBox').innerHTML = DATA.ai.length ? DATA.ai.map(s => `<div class="ai-row"><span class="ai-site">${esc(s.site.replace(/^https?:\/\//, ''))}</span>
      ${s.bots.map(b => `<span class="badge ${b.allowed ? 'yes' : 'no'}">${b.allowed ? '✓' : '✕'} ${esc(b.name)}</span>`).join('')}</div>`).join('')
    : '<div class="ai-row"><span class="hint">No robots.txt could be read.</span></div>';
}

function datesHtml(d) {
  if (!d) return '';
  const parts = [];
  if (d.sitemap) parts.push(['Sitemap lastmod', d.sitemap]);
  if (d.modified) parts.push(['Page updated', d.modified]);
  else if (d.published) parts.push(['Published', d.published]);
  if (d.server) parts.push(['Server', d.server]);
  const wb = d.wayback ? `<a href="${esc(d.wayback_url)}" target="_blank" rel="noopener">${esc(d.wayback)} ↗</a>` : 'no snapshot';
  return `<div class="dates">${parts.map(([k, v]) => `<span>${k}: <b>${esc(v)}</b></span>`).join('')}
    <span>Wayback: <b>${wb}</b></span></div>`;
}

function renderTable() {
  const q = $('filter').value.trim().toLowerCase(), only = $('probOnly').checked;
  const rows = DATA.rows.slice().sort((a, b) => ORDER[a.verdict] - ORDER[b.verdict])
    .filter(r => (!only || r.verdict !== 'yes') && (!q || r.url.toLowerCase().includes(q)));
  $('resCount').textContent = `${DATA.rows.length} URL${DATA.rows.length === 1 ? '' : 's'} · problems first`;
  $('resBody').innerHTML = rows.length ? rows.map(r => `<tr>
      <td class="mono">${esc(r.url)}${r.status ? `<div class="small">HTTP ${r.status}${r.in_sitemap === true ? ' · in sitemap' : ''}</div>` : ''}${datesHtml(r.dates)}</td>
      <td><span class="badge ${r.verdict}">${esc(r.label)}</span></td>
      <td><ul class="why">${r.reasons.map(x => `<li class="${x.sev}">${esc(x.msg)}</li>`).join('')}</ul></td>
      <td><a class="btn sm" href="${googleLink(r.url)}" target="_blank" rel="noopener">Check on Google ↗</a></td></tr>`).join('')
    : '<tr><td colspan="4" style="color:var(--muted)">No results match this filter.</td></tr>';
}
$('filter').addEventListener('input', renderTable);
$('probOnly').addEventListener('change', renderTable);

$('csvBtn').addEventListener('click', () => {
  if (!DATA) return;
  const q = v => '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"';
  const out = [['URL', 'Result', 'HTTP status', 'Final URL', 'Canonical', 'In sitemap', 'Sitemap lastmod', 'Page updated', 'Published', 'Server Last-Modified', 'Last Wayback snapshot', 'Reasons', 'Google check'].map(q).join(',')];
  DATA.rows.forEach(r => out.push([r.url, r.label, r.status, r.final_url, r.canonical,
    r.in_sitemap === true ? 'Yes' : r.in_sitemap === false ? 'No' : '',
    ...(d => [d.sitemap, d.modified, d.published, d.server, d.wayback])(r.dates || {}), r.reasons.map(x => x.msg).join(' | '), googleLink(r.url)].map(q).join(',')));
  const blob = new Blob(['﻿' + out.join('\r\n')], { type: 'text/csv;charset=utf-8' });
  const link = document.createElement('a'); link.href = URL.createObjectURL(blob);
  link.download = 'google-index-check.csv'; link.click(); setTimeout(() => URL.revokeObjectURL(link.href), 1000);
});
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(debug=True, port=5000)
