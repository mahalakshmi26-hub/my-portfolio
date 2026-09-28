"""
Plagiarism Checker  -  Flask app
Find out how much of your content already exists on the web.

  * Check a live page (URL) or pasted text - up to 2,000 words
  * Web check: searches the web for your sentences (needs a free Serper.dev key
    in the SERPER_API_KEY environment variable)
  * Compare check: compare your text with up to 5 URLs you choose (no key needed)
  * Separates exact copies (red) from close rewrites (orange)
  * Ignores common boilerplate (T&Cs, disclaimers) so it doesn't hurt the score
  * Uniqueness score, highlighted text, matching sources table, CSV download

Your text is never stored.

Built by Mahalakshmi Marimuthu - Digital Marketing Strategist & AI-Powered SEO Expert
"""
import ipaddress
import os
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

SERPER_KEY = os.environ.get("SERPER_API_KEY", "").strip()
REQUEST_TIMEOUT = 10
MAX_HTML_BYTES = 4 * 1024 * 1024
MAX_WORDS = 2000
MIN_WORDS = 30
MAX_QUERIES = 10          # web searches per check
MAX_WEB_PAGES = 8         # web results we open and read in full
MAX_COMPARE = 5           # user-chosen comparison URLs
SHINGLE = 5               # words per fingerprint
EXACT_AT = 0.8            # >= 80% of 5-word fingerprints found -> copied
NEAR_AT = 0.4             # >= 40% of 3-word fingerprints found -> close rewrite
NEAR_MIN_HITS = 3
MIN_SENT_WORDS = 6

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

COMMON_PHRASES = [
    "terms and conditions apply", "t c apply", "t cs apply", "terms conditions apply",
    "subject to change", "subject to credit approval", "subject to approval",
    "at the sole discretion", "all rights reserved", "privacy policy", "cookie policy",
    "we use cookies", "click here", "read more", "learn more", "for more information",
    "for illustrative purposes", "for informational purposes only", "this is not financial advice",
    "past performance is not", "disclaimer", "copyright", "sign up for our newsletter",
    "contact us", "follow us on",
]


# --------------------------------------------------------------------------- #
# Text helpers
# --------------------------------------------------------------------------- #
def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def words_of(text):
    return re.findall(r"\w+", (text or "").lower())


def fingerprints(words, n=SHINGLE):
    if not words:
        return set()
    if len(words) < n:
        return {tuple(words)}
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


SENT_SPLIT = re.compile(r"(?<=[.!?])[\"'”’)\]]*\s+(?=[\"'“‘(\[]?[A-Z0-9])")


def split_paragraphs(text):
    """-> list of paragraphs, each a list of sentences."""
    out = []
    for para in re.split(r"\n+", text or ""):
        para = clean(para)
        if not para:
            continue
        sents = [s.strip() for s in SENT_SPLIT.split(para) if s.strip()]
        if sents:
            out.append(sents)
    return out


def is_common(words):
    if len(words) > 25:
        return False
    joined = " " + " ".join(words) + " "
    return any(" " + p + " " in joined for p in COMMON_PHRASES)


def host_of(url):
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def normalize_url(raw):
    raw = (raw or "").strip()
    if not raw:
        return ""
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    return raw


def safe_url(url):
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    if os.environ.get("ALLOW_PRIVATE") == "1":      # local testing only
        return True
    try:
        infos = socket.getaddrinfo(p.hostname, None)
    except socket.gaierror:
        return True    # let the request fail with a friendly message
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    return True


def fetch_page(url):
    """-> dict(ok, final_url, title, text) or dict(ok=False, error)."""
    if not safe_url(url):
        return {"ok": False, "error": "This URL can't be checked."}
    try:
        r = requests.get(url, headers={"User-Agent": BROWSER_UA,
                                       "Accept-Language": "en-US,en;q=0.9"},
                         timeout=REQUEST_TIMEOUT, allow_redirects=True, stream=True)
        raw = r.raw.read(MAX_HTML_BYTES, decode_content=True)
    except requests.exceptions.Timeout:
        return {"ok": False, "error": "The page took too long to respond."}
    except requests.exceptions.RequestException:
        return {"ok": False, "error": "Could not connect to this URL."}
    if r.status_code >= 400:
        return {"ok": False, "error": f"The page returned HTTP {r.status_code} "
                                      "(it may be blocking automated requests)."}
    ctype = (r.headers.get("Content-Type") or "").lower()
    if ctype and "html" not in ctype and "text" not in ctype:
        return {"ok": False, "error": "This URL isn't a web page."}
    enc = r.encoding or r.apparent_encoding or "utf-8"
    if enc.lower() == "iso-8859-1" and r.apparent_encoding:
        enc = r.apparent_encoding
    html = raw.decode(enc, errors="replace")
    title, text = main_text(html)
    return {"ok": True, "final_url": r.url, "title": title, "text": text}


BLOCKS = ["p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "td", "th",
          "blockquote", "dd", "dt", "figcaption"]


def main_text(html):
    soup = BeautifulSoup(html, "html.parser")
    title = clean(soup.title.get_text()) if soup.title else ""
    for t in soup(["script", "style", "noscript", "svg", "nav", "header", "footer",
                   "aside", "form", "iframe", "button", "select", "template"]):
        t.decompose()
    body = soup.body or soup
    root = body
    for cand in (soup.find("article"), soup.find("main")):
        if cand and len(cand.get_text(" ").split()) >= 150:
            root = cand
            break
    lines, seen = [], set()
    for el in root.find_all(BLOCKS):
        if el.find(BLOCKS):
            continue      # the inner element will be picked up on its own
        txt = clean(el.get_text(" "))
        if len(txt.split()) >= 4 and txt not in seen:
            seen.add(txt)
            lines.append(txt)
    if not lines:
        for line in root.get_text("\n").split("\n"):
            line = clean(line)
            if len(line.split()) >= 4 and line not in seen:
                seen.add(line)
                lines.append(line)
    return title, "\n".join(lines)


# --------------------------------------------------------------------------- #
# Web search (Serper.dev - free 2,500 searches, no card)
# --------------------------------------------------------------------------- #
def serper_search(query):
    try:
        r = requests.post("https://google.serper.dev/search",
                          headers={"X-API-KEY": SERPER_KEY, "Content-Type": "application/json"},
                          json={"q": query, "num": 10}, timeout=12)
    except requests.exceptions.RequestException:
        return None, "net"
    if r.status_code in (401, 403):
        return None, "key"
    if r.status_code == 429 or r.status_code == 402:
        return None, "quota"
    if r.status_code >= 400:
        return None, "net"
    try:
        return r.json().get("organic", []) or [], None
    except ValueError:
        return None, "net"


def pick_query_sentences(paragraphs, ignore_common):
    cands = []
    for para in paragraphs:
        for s in para:
            w = words_of(s)
            if 8 <= len(w) and not (ignore_common and is_common(w)):
                cands.append(s)
    if len(cands) <= MAX_QUERIES:
        return cands
    step = len(cands) / MAX_QUERIES
    return [cands[int(i * step)] for i in range(MAX_QUERIES)]


def web_sources(paragraphs, ignore_common, exclude_hosts, notes):
    queries = []
    for s in pick_query_sentences(paragraphs, ignore_common):
        queries.append(" ".join(s.split()[:32]))
    if not queries:
        return []
    with ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(serper_search, queries))

    errs = {e for _, e in results if e}
    if "key" in errs:
        notes.append("Web search key is invalid, so only your comparison URLs were checked.")
    elif "quota" in errs:
        notes.append("Web search credits have run out, so only your comparison URLs were checked.")
    elif errs and all(r is None for r, _ in results):
        notes.append("Web search didn't respond, so only your comparison URLs were checked.")

    found = {}
    for organic, _ in results:
        for item in organic or []:
            link = item.get("link") or ""
            if not link.startswith("http") or host_of(link) in exclude_hosts:
                continue
            f = found.setdefault(link, {"url": link, "title": item.get("title", ""),
                                        "hits": 0, "snippets": []})
            f["hits"] += 1
            if item.get("snippet"):
                f["snippets"].append(item["snippet"])
    ranked = sorted(found.values(), key=lambda x: -x["hits"])
    to_open = ranked[:MAX_WEB_PAGES]

    with ThreadPoolExecutor(max_workers=6) as ex:
        pages = list(ex.map(lambda f: fetch_page(f["url"]), to_open))

    sources = []
    for f, page in zip(to_open, pages):
        text = page["text"] if page.get("ok") else ""
        text = (text + "\n" + "\n".join(f["snippets"])).strip()
        sources.append({"url": f["url"], "title": (page.get("title") if page.get("ok") else "")
                        or f["title"], "kind": "web", "text": text})
    for f in ranked[MAX_WEB_PAGES:]:
        sources.append({"url": f["url"], "title": f["title"], "kind": "web",
                        "text": "\n".join(f["snippets"])})
    return sources


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #
def analyse(paragraphs, sources, ignore_common):
    prepared = []
    for s in sources:
        w = words_of(s["text"])
        prepared.append((s, fingerprints(w), fingerprints(w, 3), " " + " ".join(w) + " "))

    out_paras = []
    for para in paragraphs:
        row = []
        for sent in para:
            w = words_of(sent)
            item = {"text": sent, "status": "unique", "sim": 0, "src": None, "words": len(w)}
            if len(w) < MIN_SENT_WORDS:
                item["status"] = "short"
            elif ignore_common and is_common(w):
                item["status"] = "common"
            else:
                fp5, fp3 = fingerprints(w), fingerprints(w, 3)
                joined = " " + " ".join(w) + " "
                best_exact, exact_i, best_near, near_i = 0.0, None, 0.0, None
                for i, (_, s5, s3, sjoined) in enumerate(prepared):
                    r5 = 1.0 if joined in sjoined else len(fp5 & s5) / len(fp5)
                    hits3 = len(fp3 & s3)
                    r3 = hits3 / len(fp3) if hits3 >= NEAR_MIN_HITS else 0.0
                    if r5 > best_exact:
                        best_exact, exact_i = r5, i
                    if r3 > best_near:
                        best_near, near_i = r3, i
                if best_exact >= EXACT_AT:
                    item.update(status="exact", sim=round(best_exact * 100), src=exact_i)
                elif best_near >= NEAR_AT:
                    item.update(status="near", sim=round(max(best_near, best_exact) * 100), src=near_i)
            row.append(item)
        out_paras.append(row)
    return out_paras


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/api/check", methods=["POST"])
def api_check():
    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "url")
    ignore_common = bool(data.get("ignore_common", True))
    notes = []
    exclude_hosts = set()
    checked_url = ""

    if mode == "url":
        url = normalize_url(data.get("url", ""))
        if not url:
            return jsonify({"ok": False, "error": "Please enter a page URL."}), 400
        page = fetch_page(url)
        if not page["ok"]:
            return jsonify({"ok": False, "error": page["error"] +
                            " Try the “Paste text” option instead."}), 400
        text = page["text"]
        checked_url = page["final_url"]
        exclude_hosts.add(host_of(checked_url))
    else:
        text = data.get("text", "")

    paragraphs = split_paragraphs(text)
    total_words = sum(len(words_of(s)) for p in paragraphs for s in p)
    if total_words < MIN_WORDS:
        return jsonify({"ok": False, "error": f"Please give at least {MIN_WORDS} words to check."}), 400
    if total_words > MAX_WORDS:
        # keep whole sentences up to the limit
        kept, count = [], 0
        for p in paragraphs:
            row = []
            for s in p:
                n = len(words_of(s))
                if count + n > MAX_WORDS:
                    break
                row.append(s)
                count += n
            if row:
                kept.append(row)
            if count >= MAX_WORDS or len(row) < len(p):
                break
        paragraphs = kept
        notes.append(f"Only the first {MAX_WORDS:,} words were checked.")

    # comparison URLs chosen by the user
    raw_list = data.get("compare", []) or []
    if isinstance(raw_list, str):
        raw_list = raw_list.splitlines()
    compare_urls = []
    for u in raw_list:
        u = normalize_url(u)
        if u and u not in compare_urls:
            compare_urls.append(u)
    if len(compare_urls) > MAX_COMPARE:
        notes.append(f"Only the first {MAX_COMPARE} comparison URLs were used.")
        compare_urls = compare_urls[:MAX_COMPARE]

    if not SERPER_KEY and not compare_urls:
        return jsonify({"ok": False, "error": "Web search isn't switched on for this tool yet — "
                        "add at least one URL in “Compare against” to run a check."}), 400

    sources = []
    if compare_urls:
        with ThreadPoolExecutor(max_workers=5) as ex:
            pages = list(ex.map(fetch_page, compare_urls))
        for u, page in zip(compare_urls, pages):
            if page.get("ok") and page["text"]:
                sources.append({"url": page["final_url"], "title": page["title"],
                                "kind": "compare", "text": page["text"]})
            else:
                notes.append(f"Couldn't read {u} — {page.get('error', 'no text found')}")

    if SERPER_KEY:
        sources += web_sources(paragraphs, ignore_common, exclude_hosts, notes)

    result = analyse(paragraphs, sources, ignore_common)

    counted = [it for p in result for it in p if it["status"] in ("exact", "near", "unique")]
    checked = sum(it["words"] for it in counted) or 1
    exact_w = sum(it["words"] for it in counted if it["status"] == "exact")
    near_w = sum(it["words"] for it in counted if it["status"] == "near")
    exact_pct = round(exact_w * 100 / checked)
    near_pct = round(near_w * 100 / checked)
    unique_pct = max(0, 100 - exact_pct - near_pct)

    src_rows = []
    for i, s in enumerate(sources):
        hits = [it for it in counted if it["src"] == i]
        w = sum(it["words"] for it in hits)
        if not hits and s["kind"] != "compare":
            continue
        src_rows.append({"id": i, "url": s["url"], "title": s["title"] or host_of(s["url"]),
                         "kind": s["kind"], "match_pct": round(w * 100 / checked),
                         "sentences": len(hits),
                         "exact": sum(1 for it in hits if it["status"] == "exact")})
    src_rows.sort(key=lambda r: (-r["match_pct"], r["kind"] != "compare"))

    return jsonify({
        "ok": True,
        "checked_url": checked_url,
        "web_search": bool(SERPER_KEY),
        "sources_scanned": len(sources),
        "stats": {"words_checked": checked if counted else 0,
                  "unique_pct": unique_pct, "exact_pct": exact_pct, "near_pct": near_pct,
                  "sentences": len(counted)},
        "paragraphs": result,
        "sources": src_rows,
        "notes": notes,
    })


@app.route("/")
def home():
    page = PAGE.replace("__WEB_ON__", "true" if SERPER_KEY else "false")
    return Response(page, mimetype="text/html")


# --------------------------------------------------------------------------- #
# Page (plain HTML + JS; no Jinja so the JS can use braces freely)
# --------------------------------------------------------------------------- #
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Plagiarism Checker – Free Duplicate Content Check</title>
<meta name="description" content="Free Plagiarism Checker: check a page or pasted text against the web or against URLs you choose. See exact copies and close rewrites highlighted, with a uniqueness score.">
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
  .topbar .sub{font-size:.82rem;color:var(--muted);margin-top:.2rem}

  .card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.2rem 1.4rem}
  .card + .card{margin-top:1.2rem}
  .card h2{font-family:'Sora',sans-serif;font-size:.9rem;font-weight:700;margin-bottom:.9rem;display:flex;align-items:center;gap:.5rem;flex-wrap:wrap}
  .card h2 .step{display:inline-flex;width:22px;height:22px;border-radius:6px;background:rgba(124,106,247,.14);border:1px solid rgba(124,106,247,.4);color:var(--accent);font-size:.7rem;align-items:center;justify-content:center;font-family:'DM Mono',monospace}
  .card h2 .right{margin-left:auto}

  .modes{display:grid;grid-template-columns:1fr 1fr;gap:.8rem;margin-bottom:1rem}
  .mode{display:flex;align-items:center;gap:.8rem;text-align:left;background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:.9rem 1.1rem;cursor:pointer;color:var(--text);font-family:'Inter',sans-serif;transition:border-color .2s,background .2s}
  .mode:hover{border-color:rgba(124,106,247,.5)}
  .mode.active{background:rgba(124,106,247,.12);border-color:rgba(124,106,247,.6)}
  .mode .mi{width:38px;height:38px;border-radius:10px;background:var(--surface2);border:1px solid var(--border);display:flex;align-items:center;justify-content:center;font-size:1.1rem;flex-shrink:0}
  .mode.active .mi{border-color:rgba(124,106,247,.5)}
  .mode .mt{display:block;font-family:'Sora',sans-serif;font-size:.88rem;font-weight:700}
  .mode.active .mt{color:var(--accent)}
  .mode .md{display:block;font-size:.74rem;color:var(--muted);margin-top:.1rem}

  label.fl{display:flex;justify-content:space-between;gap:.5rem;font-size:.76rem;color:var(--muted);margin-bottom:.35rem}
  label.fl .opt{font-family:'DM Mono',monospace;font-size:.64rem;opacity:.8}
  input[type=text],textarea{width:100%;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:.6rem .85rem;font-family:'Inter',sans-serif;font-size:.86rem}
  textarea{resize:vertical;line-height:1.55}
  #txt{min-height:210px}
  #cmp{min-height:74px;font-family:'DM Mono',monospace;font-size:.78rem}
  input:focus,textarea:focus{outline:none;border-color:var(--accent)}
  .field{margin-bottom:1rem}
  .hint{font-size:.7rem;color:var(--muted);margin-top:.3rem;line-height:1.5}
  .hide{display:none!important}
  .check{display:flex;align-items:center;gap:.5rem;font-size:.8rem;color:var(--muted);cursor:pointer;user-select:none}
  .check input{accent-color:var(--accent);width:15px;height:15px}

  .btn{display:inline-flex;align-items:center;gap:.5rem;background:var(--accent);color:#fff;border:none;border-radius:8px;padding:.65rem 1.3rem;font-size:.86rem;font-weight:600;cursor:pointer;font-family:'Inter',sans-serif;white-space:nowrap}
  .btn:hover{background:var(--accent-h)}
  .btn.ghost{background:var(--surface2);border:1px solid var(--border);color:var(--text)}
  .btn.ghost:hover{border-color:var(--accent)}
  .btn.sm{padding:.4rem .8rem;font-size:.76rem}
  .btn:disabled{opacity:.5;cursor:not-allowed}
  .actions{display:flex;gap:.8rem;flex-wrap:wrap;align-items:center;justify-content:space-between;margin-top:.4rem}
  .msg{font-size:.8rem;margin-top:.8rem}
  .msg.err{color:var(--red)} .msg.wait{color:var(--muted)}
  .spin{display:inline-block;width:12px;height:12px;border:2px solid rgba(255,255,255,.35);border-top-color:#fff;border-radius:50%;animation:sp .8s linear infinite}
  @keyframes sp{to{transform:rotate(360deg)}}

  /* score */
  .score-row{display:flex;gap:1.4rem;align-items:center;flex-wrap:wrap}
  .ring{position:relative;width:110px;height:110px;flex-shrink:0}
  .ring svg{transform:rotate(-90deg)}
  .ring .num{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;font-family:'Sora',sans-serif;font-weight:800;font-size:1.6rem;line-height:1}
  .ring .num small{font-family:'DM Mono',monospace;font-size:.58rem;color:var(--muted);font-weight:400;margin-top:.3rem;letter-spacing:.08em;text-transform:uppercase}
  .score-text{flex:1;min-width:220px}
  .verdict{font-family:'Sora',sans-serif;font-weight:700;font-size:1rem;margin-bottom:.25rem}
  .score-text .sub{font-size:.8rem;color:var(--muted);line-height:1.55}
  .tiles{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:.7rem;margin-top:1.1rem}
  .tile{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:.7rem .9rem}
  .tile .tl{font-family:'DM Mono',monospace;font-size:.62rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
  .tile .tv{font-family:'Sora',sans-serif;font-weight:800;font-size:1.3rem;margin-top:.15rem}
  .g{color:var(--mint)} .o{color:var(--orange)} .r{color:var(--red)}
  .notes{margin-top:.9rem;font-size:.74rem;color:var(--orange);line-height:1.7}

  /* highlighted text */
  .legend{display:flex;gap:.9rem;flex-wrap:wrap;font-size:.72rem;color:var(--muted);margin-bottom:.8rem}
  .legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:.35rem;vertical-align:-1px}
  .doc{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:1rem 1.1rem;max-height:520px;overflow:auto;font-size:.88rem;line-height:1.85}
  .doc p{margin-bottom:.7rem}
  .doc p:last-child{margin-bottom:0}
  .s-exact{background:rgba(247,106,106,.18);border-bottom:2px solid var(--red);cursor:help}
  .s-near{background:rgba(247,162,106,.15);border-bottom:2px solid var(--orange);cursor:help}
  .s-common,.s-short{color:var(--muted)}

  /* sources */
  table{width:100%;border-collapse:collapse;font-size:.82rem}
  th{text-align:left;font-family:'DM Mono',monospace;font-size:.62rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);font-weight:500;padding:.5rem .6rem;border-bottom:1px solid var(--border)}
  td{padding:.6rem;border-bottom:1px solid var(--border);vertical-align:middle}
  tr:last-child td{border-bottom:none}
  td .st{font-weight:600;display:block;max-width:520px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  td .su{font-family:'DM Mono',monospace;font-size:.68rem;color:var(--muted);display:block;max-width:520px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .bar{height:6px;border-radius:6px;background:var(--bg);border:1px solid var(--border);overflow:hidden;width:90px;display:inline-block;vertical-align:middle;margin-right:.5rem}
  .bar b{display:block;height:100%}
  .badge{font-family:'DM Mono',monospace;font-size:.62rem;padding:.18rem .55rem;border-radius:100px;white-space:nowrap;display:inline-block}
  .badge.web{background:rgba(124,106,247,.12);border:1px solid rgba(124,106,247,.4);color:var(--accent)}
  .badge.compare{background:rgba(106,247,200,.1);border:1px solid rgba(106,247,200,.35);color:var(--mint)}
  .empty{font-size:.8rem;color:var(--muted);padding:.4rem 0}
  .tbl-wrap{overflow-x:auto}

  @media(max-width:960px){.sidebar{display:none}.main{margin-left:0;padding:1.2rem}}
  @media(max-width:560px){.modes,.tiles{grid-template-columns:1fr}}
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
      <a href="#" class="sidebar-link active">📑&nbsp; Plagiarism Checker</a>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-label">Navigate</div>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="sidebar-link">🧰&nbsp; All My Tools</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio" class="sidebar-link">👩‍💻&nbsp; Portfolio</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/blog/index.html" class="sidebar-link">📝&nbsp; Blog Posts</a>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-label">Good to know</div>
      <div style="font-size:.72rem;color:var(--muted);line-height:1.7;padding:0 .3rem">
        Checks up to <b style="color:var(--text)">2,000 words</b> at a time.<br>
        <span class="r">Red</span> = copied word for word.<br>
        <span class="o">Orange</span> = lightly reworded.<br>
        Your text is never stored.<br>
        <span id="webState"></span>
      </div>
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
      <h1>📑 Plagiarism <span>Checker</span></h1>
      <div class="sub">Find out how much of your content already exists on the web — before Google does.</div>
    </div>

    <div class="modes" role="tablist">
      <button type="button" class="mode active" data-mode="url" role="tab">
        <span class="mi">🔗</span>
        <span><span class="mt">Check a URL</span><span class="md">Read the main content of a live page</span></span>
      </button>
      <button type="button" class="mode" data-mode="text" role="tab">
        <span class="mi">✍️</span>
        <span><span class="mt">Paste text</span><span class="md">Check a draft before you publish</span></span>
      </button>
    </div>

    <div class="card">
      <div class="field" id="urlField">
        <label class="fl" for="url"><span>Page URL</span></label>
        <input type="text" id="url" placeholder="https://www.yourwebsite.com/your-page">
        <div class="hint">Your own website is skipped automatically, so the page won't match itself.</div>
      </div>
      <div class="field hide" id="txtField">
        <label class="fl" for="txt"><span>Your text</span><span class="opt" id="wc">0 / 2,000 words</span></label>
        <textarea id="txt" placeholder="Paste your article or page copy here (at least 30 words)"></textarea>
      </div>
      <div class="field">
        <label class="fl" for="cmp"><span>Compare against</span><span class="opt" id="cmpOpt">optional · up to 5 URLs, one per line</span></label>
        <textarea id="cmp" placeholder="https://competitor.com/similar-page"></textarea>
      </div>
      <div class="actions">
        <label class="check"><input type="checkbox" id="ignore" checked> Ignore common phrases (T&amp;Cs, disclaimers)</label>
        <button class="btn" id="go" type="button">Check plagiarism</button>
      </div>
      <div class="msg" id="msg"></div>
    </div>

    <div id="results" class="hide">
      <div class="card" style="margin-top:1.2rem">
        <div class="score-row">
          <div class="ring" id="ring"></div>
          <div class="score-text">
            <div class="verdict" id="verdict"></div>
            <div class="sub" id="verdictSub"></div>
          </div>
          <button class="btn ghost sm" id="csv" type="button">⬇ Download CSV</button>
        </div>
        <div class="tiles">
          <div class="tile"><div class="tl">Unique</div><div class="tv g" id="tU"></div></div>
          <div class="tile"><div class="tl">Exact copies</div><div class="tv r" id="tE"></div></div>
          <div class="tile"><div class="tl">Close rewrites</div><div class="tv o" id="tN"></div></div>
        </div>
        <div class="notes" id="notes"></div>
      </div>

      <div class="card">
        <h2><span class="step">1</span>Your text, highlighted</h2>
        <div class="legend">
          <span><i style="background:var(--red)"></i>Copied word for word</span>
          <span><i style="background:var(--orange)"></i>Close rewrite</span>
          <span><i style="background:var(--muted)"></i>Not scored (common phrase / very short)</span>
        </div>
        <div class="doc" id="doc"></div>
        <div class="hint">Hover over a highlighted sentence to see where it was found.</div>
      </div>

      <div class="card">
        <h2><span class="step">2</span>Matching sources</h2>
        <div class="tbl-wrap"><table>
          <thead><tr><th>#</th><th>Source</th><th>Match</th><th>Sentences</th><th>Found via</th></tr></thead>
          <tbody id="srcBody"></tbody>
        </table></div>
      </div>
    </div>
  </main>
</div>

<script>
const WEB_ON = __WEB_ON__;
let mode = 'url', last = null;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const host = u => { try { return new URL(u).hostname.replace(/^www\./,''); } catch(e) { return u; } };

$('webState').innerHTML = WEB_ON
  ? '<span class="g">● Web search is on</span>'
  : '<span class="o">● Web search is off</span> — add URLs to compare against.';
if (!WEB_ON) $('cmpOpt').textContent = 'required · up to 5 URLs, one per line';

document.querySelectorAll('.mode').forEach(b => b.addEventListener('click', () => {
  mode = b.dataset.mode;
  document.querySelectorAll('.mode').forEach(x => x.classList.toggle('active', x === b));
  $('urlField').classList.toggle('hide', mode !== 'url');
  $('txtField').classList.toggle('hide', mode !== 'text');
}));

$('txt').addEventListener('input', () => {
  const n = ($('txt').value.match(/\w+/g) || []).length;
  $('wc').textContent = n.toLocaleString() + ' / 2,000 words';
  $('wc').style.color = n > 2000 ? 'var(--orange)' : '';
});
$('url').addEventListener('keydown', e => { if (e.key === 'Enter') run(); });
$('go').addEventListener('click', run);

async function run() {
  const body = {
    mode,
    url: $('url').value.trim(),
    text: $('txt').value,
    compare: $('cmp').value.split('\n').map(s => s.trim()).filter(Boolean),
    ignore_common: $('ignore').checked
  };
  if (mode === 'url' && !body.url) return showErr('Please enter a page URL.');
  if (mode === 'text' && (body.text.match(/\w+/g) || []).length < 30) return showErr('Please paste at least 30 words.');
  if (!WEB_ON && !body.compare.length) return showErr('Add at least one URL in “Compare against”.');

  $('go').disabled = true;
  $('go').innerHTML = '<span class="spin"></span> Checking…';
  $('msg').className = 'msg wait';
  $('msg').textContent = 'Searching and comparing — this usually takes 15–40 seconds.';
  try {
    const r = await fetch('/api/check', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    const d = await r.json();
    if (!d.ok) return showErr(d.error || 'Something went wrong.');
    $('msg').textContent = '';
    last = d; render(d);
  } catch (e) {
    showErr('The check didn’t finish. Please try again (the first run can take longer while the tool wakes up).');
  } finally {
    $('go').disabled = false;
    $('go').textContent = 'Check plagiarism';
  }
}
function showErr(t) { $('msg').className = 'msg err'; $('msg').textContent = t; }

function ring(pct) {
  const c = pct >= 90 ? 'var(--mint)' : pct >= 70 ? 'var(--orange)' : 'var(--red)';
  const R = 47, L = 2 * Math.PI * R;
  return `<svg width="110" height="110" viewBox="0 0 110 110">
    <circle cx="55" cy="55" r="${R}" fill="none" stroke="#23233a" stroke-width="9"/>
    <circle cx="55" cy="55" r="${R}" fill="none" stroke="${c}" stroke-width="9" stroke-linecap="round"
      stroke-dasharray="${L}" stroke-dashoffset="${L * (1 - pct / 100)}"/></svg>
    <div class="num" style="color:${c}">${pct}%<small>unique</small></div>`;
}

function render(d) {
  const s = d.stats, srcById = {};
  d.sources.forEach((x, i) => { srcById[x.id] = {...x, n: i + 1}; });
  $('results').classList.remove('hide');
  $('ring').innerHTML = ring(s.unique_pct);
  const [v, sub] = s.unique_pct >= 90
    ? ['Looks original — safe to publish', 'Very little of this text was found elsewhere.']
    : s.unique_pct >= 70
    ? ['Mostly original — fix the highlighted lines', 'Rewrite the red and orange sentences in your own words before publishing.']
    : ['High overlap — rewrite before publishing', 'A large part of this text already exists online. Google may treat it as duplicate content.'];
  $('verdict').textContent = v;
  $('verdictSub').textContent = `${sub} ${s.words_checked.toLocaleString()} words checked against ${d.sources_scanned} source${d.sources_scanned === 1 ? '' : 's'}.`;
  $('tU').textContent = s.unique_pct + '%';
  $('tE').textContent = s.exact_pct + '%';
  $('tN').textContent = s.near_pct + '%';
  $('notes').innerHTML = d.notes.map(n => '⚠ ' + esc(n)).join('<br>');

  $('doc').innerHTML = d.paragraphs.map(p => '<p>' + p.map(it => {
    if (it.status === 'exact' || it.status === 'near') {
      const src = srcById[it.src];
      const tip = `${it.sim}% match · source #${src ? src.n : '?'} · ${src ? host(src.url) : ''}`;
      return `<span class="s-${it.status}" title="${esc(tip)}">${esc(it.text)}</span>`;
    }
    if (it.status === 'common' || it.status === 'short')
      return `<span class="s-${it.status}">${esc(it.text)}</span>`;
    return esc(it.text);
  }).join(' ') + '</p>').join('');

  $('srcBody').innerHTML = d.sources.length ? d.sources.map((x, i) => {
    const c = x.match_pct >= 20 ? 'var(--red)' : x.match_pct >= 5 ? 'var(--orange)' : 'var(--mint)';
    return `<tr><td>${i + 1}</td>
      <td><a href="${esc(x.url)}" target="_blank" rel="noopener"><span class="st">${esc(x.title)}</span></a><span class="su">${esc(x.url)}</span></td>
      <td><span class="bar"><b style="width:${Math.min(100, x.match_pct)}%;background:${c}"></b></span>${x.match_pct}%</td>
      <td>${x.sentences}${x.exact ? ` <span class="r" style="font-size:.72rem">(${x.exact} exact)</span>` : ''}</td>
      <td><span class="badge ${x.kind}">${x.kind === 'compare' ? 'Your list' : 'Web search'}</span></td></tr>`;
  }).join('') : '<tr><td colspan="5" class="empty">No matching sources found — nice!</td></tr>';
  $('results').scrollIntoView({behavior:'smooth', block:'start'});
}

$('csv').addEventListener('click', () => {
  if (!last) return;
  const srcById = {}; last.sources.forEach(x => srcById[x.id] = x);
  const q = v => '"' + String(v ?? '').replace(/"/g, '""') + '"';
  const label = {exact:'Copied', near:'Close rewrite', unique:'Unique', common:'Common phrase (not scored)', short:'Too short (not scored)'};
  const rows = [['Sentence', 'Result', 'Match %', 'Source URL']];
  last.paragraphs.flat().forEach(it => rows.push([it.text, label[it.status], it.sim || '', srcById[it.src]?.url || '']));
  rows.push([]);
  rows.push(['Unique %', last.stats.unique_pct], ['Exact copies %', last.stats.exact_pct], ['Close rewrites %', last.stats.near_pct]);
  const blob = new Blob(['﻿' + rows.map(r => r.map(q).join(',')).join('\n')], {type:'text/csv;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'plagiarism-check.csv';
  a.click();
});
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(debug=True, port=5000)
