"""
Schema Markup (JSON-LD) Generator  -  Flask app
Simple 3-step tool: pick a schema type, fill in a short form, copy the code.

  * 15 schema types, incl. banking templates (Personal Loan, Home Loan,
    Credit Card, Bank Branch with IFSC)
  * Only the important fields are shown; optional ones sit behind
    "Show more optional fields"
  * 2026-aware label for every type: can it still earn a Google rich result,
    is it retired (FAQ - May 2026), or is it an AI-search signal only
  * Live code with a simple "Ready / Fix these first" check, Copy, Download
    and a one-click hand-off to Google's Rich Results Test
  * Optional auto-fill from a page URL, which also shows the schema the page
    already has and flags broken JSON-LD

Almost everything runs in the browser. The server only does one thing:
  /api/fetch  - fetch an existing page, read its meta tags and existing schema

Built by Mahalakshmi Marimuthu - Digital Marketing Strategist & AI-Powered SEO Expert
"""
import json
import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

REQUEST_TIMEOUT = 12
MAX_HTML_BYTES = 5 * 1024 * 1024
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def normalize_url(raw):
    raw = (raw or "").strip()
    if not raw:
        return ""
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    return raw


def is_public_http(url):
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def _types_of(node):
    t = node.get("@type") if isinstance(node, dict) else None
    if isinstance(t, list):
        return [str(x) for x in t]
    return [str(t)] if t else []


def flatten_jsonld(data):
    """Return a flat list of top-level schema nodes (expands arrays and @graph)."""
    out = []
    if isinstance(data, list):
        for d in data:
            out.extend(flatten_jsonld(d))
    elif isinstance(data, dict):
        if "@graph" in data and isinstance(data["@graph"], list):
            for d in data["@graph"]:
                out.extend(flatten_jsonld(d))
        else:
            out.append(data)
    return out


def meta_content(soup, **attrs):
    tag = soup.find("meta", attrs=attrs)
    return (tag.get("content") or "").strip() if tag else ""


def read_page(url):
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

    enc = r.encoding or r.apparent_encoding or "utf-8"
    html = raw.decode(enc, errors="replace")
    final_url = r.url
    soup = BeautifulSoup(html, "html.parser")

    # ---- existing JSON-LD ------------------------------------------------- #
    existing, invalid = [], 0
    for s in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        txt = (s.string or s.get_text() or "").strip()
        if not txt:
            continue
        try:
            data = json.loads(txt, strict=False)
        except ValueError:
            invalid += 1
            continue
        for node in flatten_jsonld(data):
            existing.append({"types": _types_of(node) or ["(no @type)"], "json": node})

    microdata = len(soup.find_all(attrs={"itemtype": True}))
    rdfa = len(soup.find_all(attrs={"typeof": True}))

    # ---- page facts used for auto-fill ------------------------------------ #
    def og(prop):
        return meta_content(soup, property=re.compile(rf"^{re.escape(prop)}$", re.I))

    canonical_tag = soup.find("link", rel="canonical")
    canonical = ""
    if canonical_tag and canonical_tag.get("href"):
        canonical = urljoin(final_url, canonical_tag["href"].strip())

    logo = ""
    for rel in ("apple-touch-icon", "apple-touch-icon-precomposed", "icon", "shortcut icon"):
        tag = soup.find("link", rel=lambda v, rel=rel: v and rel in " ".join(v).lower()
                        if isinstance(v, list) else v and rel in v.lower())
        if tag and tag.get("href"):
            logo = urljoin(final_url, tag["href"].strip())
            break

    time_tag = soup.find("time", attrs={"datetime": True})
    published = (og("article:published_time")
                 or meta_content(soup, itemprop="datePublished")
                 or (time_tag.get("datetime") if time_tag else ""))
    modified = og("article:modified_time") or og("og:updated_time") \
        or meta_content(soup, itemprop="dateModified")

    image = og("og:image") or meta_content(soup, name=re.compile(r"^twitter:image$", re.I))
    html_tag = soup.find("html")

    h1 = soup.find("h1")
    return {
        "ok": True,
        "final_url": final_url,
        "canonical": canonical,
        "title": (og("og:title")
                  or (soup.title.string.strip() if soup.title and soup.title.string else "")),
        "title_tag": soup.title.string.strip() if soup.title and soup.title.string else "",
        "h1": h1.get_text(" ", strip=True)[:200] if h1 else "",
        "description": og("og:description")
        or meta_content(soup, name=re.compile(r"^description$", re.I)),
        "image": urljoin(final_url, image) if image else "",
        "site_name": og("og:site_name"),
        "og_type": og("og:type"),
        "author": meta_content(soup, name=re.compile(r"^author$", re.I)) or og("article:author"),
        "published": published,
        "modified": modified,
        "price": og("product:price:amount") or og("og:price:amount"),
        "currency": og("product:price:currency") or og("og:price:currency"),
        "logo": logo,
        "lang": (html_tag.get("lang") or "").strip() if html_tag else "",
        "existing": existing[:40],
        "invalid_jsonld": invalid,
        "microdata": microdata,
        "rdfa": rdfa,
    }


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/api/fetch")
def api_fetch():
    url = normalize_url(request.args.get("url", ""))
    if not url or not is_public_http(url):
        return jsonify({"ok": False, "error": "Please enter a valid URL."}), 400
    return jsonify(read_page(url))


@app.route("/")
def home():
    return Response(PAGE, mimetype="text/html")


# --------------------------------------------------------------------------- #
# Page (plain HTML + JS; no Jinja so the JS can use braces freely)
# --------------------------------------------------------------------------- #
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Schema Markup (JSON-LD) Generator</title>
<meta name="description" content="Free Schema Markup Generator: pick a schema type, fill a short form and copy Google-ready JSON-LD. Includes 2026 rich result status and loan / bank templates.">
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
  .topbar .sub{font-size:.82rem;color:var(--muted);margin-top:.2rem}

  .card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.2rem 1.4rem}
  .card h2{font-family:'Sora',sans-serif;font-size:.9rem;font-weight:700;margin-bottom:.9rem}
  .card h2 .step{display:inline-flex;width:22px;height:22px;border-radius:6px;background:rgba(124,106,247,.14);border:1px solid rgba(124,106,247,.4);color:var(--accent);font-size:.7rem;align-items:center;justify-content:center;margin-right:.5rem;font-family:'DM Mono',monospace;vertical-align:1px}

  .picker{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.2fr);gap:1.2rem;align-items:start}
  .picker select{font-size:.95rem;font-weight:600;padding:.75rem 1rem}
  .type-info{display:flex;flex-direction:column;gap:.45rem}
  .type-info .desc{font-size:.8rem;color:var(--muted);line-height:1.55}
  .type-info .desc b{color:var(--text);font-weight:600}
  .modes{display:grid;grid-template-columns:1fr 1fr;gap:.8rem;margin-bottom:1rem}
  .mode{display:flex;align-items:center;gap:.8rem;text-align:left;background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:.9rem 1.1rem;cursor:pointer;color:var(--text);font-family:'Inter',sans-serif;transition:border-color .2s,background .2s}
  .mode:hover{border-color:rgba(124,106,247,.5)}
  .mode.active{background:rgba(124,106,247,.12);border-color:rgba(124,106,247,.6)}
  .mode .mi{width:38px;height:38px;border-radius:10px;background:var(--surface2);border:1px solid var(--border);display:flex;align-items:center;justify-content:center;font-size:1.1rem;flex-shrink:0}
  .mode.active .mi{border-color:rgba(124,106,247,.5)}
  .mode .mt{display:block;font-family:'Sora',sans-serif;font-size:.88rem;font-weight:700}
  .mode.active .mt{color:var(--accent)}
  .mode .md{display:block;font-size:.74rem;color:var(--muted);margin-top:.1rem}
  .urlbox{display:none;margin-bottom:1.2rem;padding-bottom:1.2rem;border-bottom:1px dashed var(--border)}
  .urlbox.show{display:block}
  .row-inline{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center}
  .row-inline input{flex:1;min-width:220px}

  label.fl{display:block;font-size:.76rem;color:var(--muted);margin-bottom:.35rem}
  label.fl .star{color:var(--red);font-weight:700;margin-left:.15rem}
  input[type=text],input[type=url],input[type=number],input[type=date],input[type=time],input[type=datetime-local],select,textarea{width:100%;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:.6rem .85rem;font-family:'Inter',sans-serif;font-size:.84rem}
  textarea{min-height:70px;resize:vertical}
  input:focus,textarea:focus,select:focus{outline:none;border-color:var(--accent)}
  .bad{border-color:rgba(247,106,106,.7) !important}
  .field{margin-bottom:.8rem}
  .fgrid{display:grid;grid-template-columns:1fr 1fr;gap:0 .8rem}
  .fgrid .full{grid-column:1 / -1}
  .ferr{font-size:.7rem;color:var(--red);margin-top:.3rem}
  .hint{font-size:.7rem;color:var(--muted);margin-top:.3rem;line-height:1.5}
  .more-btn{background:none;border:1px dashed var(--border);color:var(--muted);border-radius:10px;padding:.55rem .9rem;font-size:.78rem;cursor:pointer;width:100%;font-family:'Inter',sans-serif;margin:.2rem 0 .9rem}
  .more-btn:hover{border-color:var(--accent);color:var(--text)}
  #moreFields{display:none}
  #moreFields.show{display:grid}

  .btn{display:inline-flex;align-items:center;gap:.5rem;background:var(--accent);color:#fff;border:none;border-radius:8px;padding:.62rem 1.2rem;font-size:.84rem;font-weight:600;cursor:pointer;font-family:'Inter',sans-serif;white-space:nowrap}
  .btn:hover{background:var(--accent-h)}
  .btn.ghost{background:var(--surface2);border:1px solid var(--border);color:var(--text)}
  .btn.ghost:hover{border-color:var(--accent)}
  .btn.mint{background:rgba(106,247,200,.12);border:1px solid rgba(106,247,200,.4);color:var(--mint)}
  .btn.mint:hover{background:rgba(106,247,200,.2)}
  .btn.sm{padding:.4rem .8rem;font-size:.76rem}
  .btn:disabled{opacity:.45;cursor:not-allowed}
  .xbtn{background:none;border:1px solid var(--border);color:var(--muted);border-radius:7px;width:30px;height:30px;cursor:pointer;font-size:.8rem;flex-shrink:0}
  .xbtn:hover{border-color:var(--red);color:var(--red)}
  .msg{font-size:.78rem;margin-top:.6rem}
  .msg.err{color:var(--red)} .msg.ok{color:var(--mint)}

  .layout{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:1.2rem;align-items:start;margin-top:1.2rem}
  .sticky{position:sticky;top:1rem}

  .rows{display:flex;flex-direction:column;gap:.45rem}
  .rrow{display:flex;gap:.45rem;align-items:flex-start}
  .rrow .rn{font-family:'DM Mono',monospace;font-size:.66rem;color:var(--muted);width:18px;padding-top:.65rem;flex-shrink:0;text-align:right}
  .rrow .rcells{flex:1;display:grid;gap:.45rem;min-width:0}
  .rrow textarea{min-height:52px}

  .badge{font-family:'DM Mono',monospace;font-size:.64rem;padding:.2rem .6rem;border-radius:100px;white-space:nowrap;display:inline-block;width:fit-content}
  .badge.eligible{background:rgba(106,247,200,.1);border:1px solid rgba(106,247,200,.35);color:var(--mint)}
  .badge.retired{background:rgba(247,106,106,.1);border:1px solid rgba(247,106,106,.35);color:var(--red)}
  .badge.none{background:rgba(247,162,106,.1);border:1px solid rgba(247,162,106,.35);color:var(--orange)}
  .badge.schemaorg{background:rgba(124,106,247,.12);border:1px solid rgba(124,106,247,.4);color:var(--accent)}

  .status{border-radius:10px;padding:.65rem .9rem;font-size:.8rem;margin-bottom:.8rem;line-height:1.5}
  .status.ok{background:rgba(106,247,200,.08);border:1px solid rgba(106,247,200,.35);color:var(--mint)}
  .status.err{background:rgba(247,106,106,.08);border:1px solid rgba(247,106,106,.35);color:var(--red)}
  .status.idle{background:var(--surface2);border:1px solid var(--border);color:var(--muted)}
  .status .tip{display:block;color:var(--muted);font-size:.74rem;margin-top:.2rem}
  pre.code{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:1rem 1.1rem;font-family:'DM Mono',monospace;font-size:.72rem;line-height:1.6;color:#cfcfe6;overflow:auto;white-space:pre;max-height:520px}
  pre.code .k{color:#a99cff} pre.code .s{color:var(--mint)} pre.code .n{color:var(--orange)} pre.code .t{color:var(--muted)}
  .code-actions{display:flex;gap:.5rem;margin-top:.8rem;flex-wrap:wrap;align-items:center}
  .foot-hint{font-size:.72rem;color:var(--muted);margin-top:.7rem;line-height:1.55}

  .found{margin-top:.8rem;font-size:.76rem;color:var(--muted);line-height:1.9}
  .found .badge{margin-right:.3rem}

  @media(max-width:1100px){.layout,.picker{grid-template-columns:1fr}.sticky{position:static}}
  @media(max-width:960px){.sidebar{display:none}.main{margin-left:0;padding:1.2rem}}
  @media(max-width:560px){.fgrid,.modes{grid-template-columns:1fr}}
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
      <a href="#" class="sidebar-link active">🧩&nbsp; Schema Generator</a>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-label">Navigate</div>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="sidebar-link">🧰&nbsp; All My Tools</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio" class="sidebar-link">👩‍💻&nbsp; Portfolio</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/blog/index.html" class="sidebar-link">📝&nbsp; Blog Posts</a>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-label">How it works</div>
      <div style="font-size:.72rem;color:var(--muted);line-height:1.7;padding:0 .3rem">
        1. Type it in or paste a URL<br>2. Pick a schema type<br>3. Copy the code into your page
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
      <h1>🧩 Schema Markup <span>(JSON-LD) Generator</span></h1>
      <div class="sub">Choose how to start, fill in the form, copy the code. That's it.</div>
    </div>

    <div class="modes" id="modes" role="tablist">
      <button type="button" class="mode active" data-mode="manual" role="tab">
        <span class="mi">✍️</span>
        <span><span class="mt">Enter details manually</span><span class="md">Pick a type and type the details into a short form</span></span>
      </button>
      <button type="button" class="mode" data-mode="url" role="tab">
        <span class="mi">🔗</span>
        <span><span class="mt">Auto-fill from a URL</span><span class="md">Paste a page link — the form fills itself from that page</span></span>
      </button>
    </div>

    <div class="card">
      <div id="urlBox" class="urlbox">
        <h2><span class="step">1</span>Paste the page URL</h2>
        <div class="row-inline">
          <input type="text" id="fetchUrl" placeholder="https://www.example.com/personal-loan">
          <button class="btn" id="fetchBtn" type="button">⚡ Auto-fill</button>
        </div>
        <div class="msg" id="fetchMsg"></div>
        <div class="found" id="found"></div>
      </div>
      <h2><span class="step" id="typeStep">1</span>Which schema do you need?</h2>
      <div class="picker">
        <select id="typeSel" aria-label="Schema type"></select>
        <div class="type-info">
          <span class="badge" id="typeBadge"></span>
          <div class="desc" id="typeDesc"></div>
        </div>
      </div>
    </div>

    <div class="layout">
      <div class="card">
        <h2><span class="step">2</span>Fill in the details</h2>
        <div class="fgrid" id="mainFields"></div>
        <button type="button" class="more-btn" id="moreBtn"></button>
        <div class="fgrid" id="moreFields"></div>
        <button type="button" class="btn ghost sm" id="resetBtn">↺ Clear form</button>
      </div>

      <div class="sticky">
        <div class="card">
          <h2><span class="step">3</span>Copy your code</h2>
          <div class="status" id="status"></div>
          <pre class="code" id="code"></pre>
          <div class="code-actions">
            <button class="btn" type="button" id="copyCode">📋 Copy code</button>
            <button class="btn mint" type="button" id="testGoogle">🔎 Test in Google</button>
            <button class="btn ghost" type="button" id="dlCode">⬇ Download</button>
            <span class="msg ok" id="copyMsg" style="margin:0"></span>
          </div>
          <div class="foot-hint">Paste the code inside your page's &lt;head&gt;. <b>Test in Google</b> copies the code and opens Google's Rich Results Test — pick the “Code” tab there and paste.</div>
        </div>
      </div>
    </div>
  </main>
</div>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const trim = s => String(s ?? '').trim();
const lines = s => String(s ?? '').split(/\r?\n/).map(x => x.trim()).filter(Boolean);
const isAbs = u => /^https?:\/\/[^\s/]+\.[^\s]+/i.test(trim(u));
const num = v => { const n = parseFloat(v); return isNaN(n) ? undefined : n; };
const originOf = u => { try { return new URL(u).origin; } catch (e) { return ''; } };

// ------------------------------------------------------------ helpers
const TZ = (() => { const o = -new Date().getTimezoneOffset(), s = o >= 0 ? '+' : '-', a = Math.abs(o);
  return s + String(Math.floor(a / 60)).padStart(2, '0') + ':' + String(a % 60).padStart(2, '0'); })();
function dt(v) {
  v = trim(v); if (!v) return undefined;
  if (/^\d{4}-\d{2}-\d{2}$/.test(v)) return v;
  if (v.length === 16) v += ':00';
  return v + TZ;
}
function address(v) {
  const a = {'@type':'PostalAddress', streetAddress:v.street, addressLocality:v.locality, addressRegion:v.region, postalCode:v.postal, addressCountry:v.country};
  return (trim(v.street) || trim(v.locality) || trim(v.postal)) ? a : undefined;
}
function range(min, max, extra) {
  const a = num(min), b = num(max);
  if (a === undefined && b === undefined) return undefined;
  const o = Object.assign({}, extra);
  if (a !== undefined && b !== undefined && a === b) o.value = a;
  else { if (a !== undefined) o.minValue = a; if (b !== undefined) o.maxValue = b; }
  return o;
}
const imgs = v => { const l = lines(v); return l.length > 1 ? l : (l[0] || undefined); };
const DAY_OPTS = [['Mo-Fr','Monday – Friday'],['Mo-Sa','Monday – Saturday'],['Mo-Su','Every day'],['Sa-Su','Weekend'],
  ['Mo','Monday'],['Tu','Tuesday'],['We','Wednesday'],['Th','Thursday'],['Fr','Friday'],['Sa','Saturday'],['Su','Sunday']];
const DAY_NAMES = {Mo:'Monday',Tu:'Tuesday',We:'Wednesday',Th:'Thursday',Fr:'Friday',Sa:'Saturday',Su:'Sunday'};
const DAY_ORDER = ['Mo','Tu','We','Th','Fr','Sa','Su'];
const daysOf = c => { if (!c) return undefined; if (!c.includes('-')) return [DAY_NAMES[c]];
  const [a, b] = c.split('-'); return DAY_ORDER.slice(DAY_ORDER.indexOf(a), DAY_ORDER.indexOf(b) + 1).map(d => DAY_NAMES[d]); };
const COUNTRY_OPTS = [['IN','India'],['US','United States'],['GB','United Kingdom'],['AE','UAE'],['SG','Singapore'],['CA','Canada'],['AU','Australia'],['','—']];

// Field flags: req = required (red *), m = show in the main form, rc = recommended (used for tips)
// Everything without "m" or "req" goes under "Show more optional fields".
const TYPES = {
  article: {
    fields:[
      {k:'sub', l:'Type', t:'select', m:1, def:'BlogPosting', opts:[['BlogPosting','Blog post'],['Article','Article'],['NewsArticle','News article']]},
      {k:'headline', l:'Headline', req:1, full:1, ph:'Personal Loan Interest Rates 2026: Compare 40+ Banks'},
      {k:'url', l:'Page URL', t:'url', m:1, rc:1, full:1, ph:'https://www.example.com/blog/personal-loan-rates'},
      {k:'image', l:'Image URL', t:'url', m:1, rc:1, full:1, ph:'https://www.example.com/images/cover.jpg', hint:'At least 1200px wide.'},
      {k:'description', l:'Short summary', t:'textarea', m:1, full:1},
      {k:'authorName', l:'Author name', m:1, rc:1, ph:'Mahalakshmi Marimuthu'},
      {k:'publisherName', l:'Publisher (website / company)', m:1, rc:1, ph:'BankBazaar'},
      {k:'datePublished', l:'Published on', t:'datetime', m:1, rc:1},
      {k:'dateModified', l:'Last updated on', t:'datetime', m:1, rc:1},
      {k:'authorUrl', l:'Author profile URL', t:'url'},
      {k:'publisherLogo', l:'Publisher logo URL', t:'url'},
      {k:'articleSection', l:'Category', ph:'Personal Finance'},
      {k:'keywords', l:'Keywords', ph:'personal loan, interest rate, EMI'}
    ],
    build: v => ({'@type':v.sub || 'BlogPosting', headline:v.headline, description:v.description, image:v.image,
      author: trim(v.authorName) ? {'@type':'Person', name:v.authorName, url:v.authorUrl} : undefined,
      publisher: trim(v.publisherName) ? {'@type':'Organization', name:v.publisherName, logo: isAbs(v.publisherLogo) ? {'@type':'ImageObject', url:v.publisherLogo} : undefined} : undefined,
      datePublished:dt(v.datePublished), dateModified:dt(v.dateModified) || dt(v.datePublished),
      mainEntityOfPage: isAbs(v.url) ? {'@type':'WebPage', '@id':trim(v.url)} : undefined,
      articleSection:v.articleSection, keywords:v.keywords}),
    check(v, add) { if (v.datePublished && v.dateModified && v.dateModified < v.datePublished) add('dateModified', '“Last updated” can’t be before “Published”'); }
  },

  breadcrumb: {
    fields:[
      {k:'items', l:'Breadcrumb trail (top level first)', t:'rows', req:1, full:1, cols:[{k:'name', l:'Name', ph:'Home'},{k:'url', l:'URL', ph:'https://www.example.com/'}],
       def:[{name:'Home', url:''},{name:'', url:''}], add:'+ Add level', hint:'The last item is the current page — its URL can stay empty.'}
    ],
    build: v => { const rows = (v.items || []).filter(r => trim(r.name));
      return {'@type':'BreadcrumbList', itemListElement: rows.map((r, i) => ({'@type':'ListItem', position:i + 1, name:r.name, item:r.url}))}; },
    check(v, add) {
      const rows = (v.items || []).filter(r => trim(r.name));
      if (rows.length && rows.length < 2) add('items', 'Add at least 2 levels (e.g. Home › This page)');
      rows.forEach((r, i) => { if (i < rows.length - 1 && !trim(r.url)) add('items', `Level ${i + 1} (“${trim(r.name)}”) needs a URL`);
        else if (trim(r.url) && !isAbs(r.url)) add('items', `Level ${i + 1} URL should start with https://`); });
    }
  },

  faq: {
    fields:[
      {k:'qa', l:'Questions & answers', t:'rows', req:1, full:1, cols:[{k:'q', l:'Question', ph:'What is the minimum salary for a personal loan?'},{k:'a', l:'Answer', t:'textarea', ph:'Most banks ask for a net monthly income of at least ₹25,000.'}],
       def:[{q:'', a:''},{q:'', a:''}], add:'+ Add question', hint:'Use the same questions and answers that appear on the page.'}
    ],
    build: v => ({'@type':'FAQPage', mainEntity:(v.qa || []).filter(r => trim(r.q) && trim(r.a)).map(r => ({'@type':'Question', name:r.q, acceptedAnswer:{'@type':'Answer', text:r.a}}))}),
    check(v, add) { (v.qa || []).forEach((r, i) => { if (trim(r.q) && !trim(r.a)) add('qa', `Question ${i + 1} has no answer`); if (!trim(r.q) && trim(r.a)) add('qa', `Answer ${i + 1} has no question`); }); }
  },

  organization: {
    fields:[
      {k:'name', l:'Company name', req:1, ph:'BankBazaar'},
      {k:'url', l:'Website URL', t:'url', req:1, ph:'https://www.example.com'},
      {k:'logo', l:'Logo URL', t:'url', m:1, rc:1, full:1, ph:'https://www.example.com/logo.png'},
      {k:'sameAs', l:'Social profiles (one per line)', t:'lines', m:1, rc:1, full:1, ph:'https://www.linkedin.com/company/…\nhttps://www.instagram.com/…\nhttps://x.com/…'},
      {k:'sub', l:'Organization type', t:'select', def:'Organization', opts:[['Organization','Organization'],['Corporation','Company'],['FinancialService','Financial service'],['BankOrCreditUnion','Bank'],['NewsMediaOrganization','News / media'],['EducationalOrganization','Education'],['NGO','NGO']]},
      {k:'legalName', l:'Legal name'},
      {k:'description', l:'Description', t:'textarea', full:1},
      {k:'telephone', l:'Customer care phone', ph:'+91-44-66511111'},
      {k:'email', l:'Email'},
      {k:'street', l:'Street address', full:1}, {k:'locality', l:'City'}, {k:'region', l:'State'}, {k:'postal', l:'PIN code'},
      {k:'country', l:'Country', t:'select', def:'IN', opts:COUNTRY_OPTS},
      {k:'foundingDate', l:'Founded', t:'date'}
    ],
    build: v => ({'@type':v.sub || 'Organization', name:v.name, legalName:v.legalName, url:v.url,
      logo: isAbs(v.logo) ? {'@type':'ImageObject', url:v.logo} : undefined, description:v.description, sameAs:lines(v.sameAs), email:v.email, telephone:v.telephone,
      contactPoint: trim(v.telephone) ? {'@type':'ContactPoint', telephone:v.telephone, contactType:'customer service'} : undefined,
      address:address(v), foundingDate:v.foundingDate})
  },

  localbusiness: {
    fields:[
      {k:'sub', l:'Business type', t:'select', m:1, def:'LocalBusiness', opts:[['LocalBusiness','Local business'],['BankOrCreditUnion','Bank branch'],['FinancialService','Financial service / NBFC'],['InsuranceAgency','Insurance agency'],['AccountingService','CA / accounting firm'],['ProfessionalService','Professional service'],['RealEstateAgent','Real estate agent'],['Store','Store'],['Restaurant','Restaurant']]},
      {k:'name', l:'Business / branch name', req:1, ph:'XYZ Bank – MG Road Branch'},
      {k:'street', l:'Street address', req:1, full:1, ph:'12 MG Road'},
      {k:'locality', l:'City', req:1, ph:'Bengaluru'},
      {k:'postal', l:'PIN code', m:1, rc:1, ph:'560001'},
      {k:'region', l:'State', m:1, ph:'Karnataka'},
      {k:'country', l:'Country', t:'select', m:1, def:'IN', opts:COUNTRY_OPTS},
      {k:'telephone', l:'Phone', m:1, rc:1, ph:'+91-80-12345678'},
      {k:'url', l:'Page URL', t:'url', m:1, rc:1},
      {k:'branchCode', l:'IFSC / branch code', ph:'XYZB0001234'},
      {k:'image', l:'Photo URL', t:'url'},
      {k:'hours', l:'Opening hours', t:'rows', full:1, cols:[{k:'days', l:'Days', t:'select', opts:DAY_OPTS},{k:'opens', l:'Opens', t:'time'},{k:'closes', l:'Closes', t:'time'}], def:[{days:'Mo-Fr', opens:'09:30', closes:'16:30'}], add:'+ Add hours'},
      {k:'parentName', l:'Parent brand', ph:'XYZ Bank'},
      {k:'lat', l:'Latitude', t:'number'}, {k:'lng', l:'Longitude', t:'number'},
      {k:'priceRange', l:'Price range', ph:'₹₹'}
    ],
    build: v => ({'@type':v.sub || 'LocalBusiness', name:v.name, url:v.url, image:v.image, telephone:v.telephone, priceRange:v.priceRange, branchCode:v.branchCode,
      parentOrganization: trim(v.parentName) ? {'@type':'Organization', name:v.parentName} : undefined, address:address(v),
      geo:(num(v.lat) !== undefined && num(v.lng) !== undefined) ? {'@type':'GeoCoordinates', latitude:num(v.lat), longitude:num(v.lng)} : undefined,
      openingHoursSpecification:(v.hours || []).filter(h => h.days && h.opens && h.closes).map(h => ({'@type':'OpeningHoursSpecification', dayOfWeek:daysOf(h.days), opens:h.opens, closes:h.closes}))})
  },

  product: {
    fields:[
      {k:'name', l:'Product name', req:1, full:1},
      {k:'image', l:'Image URL', t:'url', m:1, rc:1, full:1},
      {k:'description', l:'Description', t:'textarea', m:1, full:1},
      {k:'brand', l:'Brand', m:1, rc:1},
      {k:'price', l:'Price', t:'number', m:1, rc:1, ph:'999'},
      {k:'currency', l:'Currency', m:1, def:'INR'},
      {k:'availability', l:'Availability', t:'select', m:1, def:'InStock', opts:[['InStock','In stock'],['OutOfStock','Out of stock'],['PreOrder','Pre-order'],['Discontinued','Discontinued']]},
      {k:'ratingValue', l:'Average rating (out of 5)', t:'number', m:1, ph:'4.4'},
      {k:'ratingCount', l:'Number of ratings', t:'number', m:1, ph:'128'},
      {k:'sku', l:'SKU'}, {k:'gtin', l:'GTIN / barcode'},
      {k:'url', l:'Product page URL', t:'url'},
      {k:'priceValidUntil', l:'Price valid until', t:'date'}
    ],
    build: v => ({'@type':'Product', name:v.name, description:v.description, image:v.image, brand: trim(v.brand) ? {'@type':'Brand', name:v.brand} : undefined, sku:v.sku, gtin:v.gtin,
      offers: trim(v.price) !== '' ? {'@type':'Offer', price:trim(v.price), priceCurrency:trim(v.currency).toUpperCase(), availability:'https://schema.org/' + (v.availability || 'InStock'), url:v.url, priceValidUntil:v.priceValidUntil} : undefined,
      aggregateRating: trim(v.ratingValue) ? {'@type':'AggregateRating', ratingValue:num(v.ratingValue), ratingCount:num(v.ratingCount), bestRating:5} : undefined}),
    check(v, add) {
      if (trim(v.name) && trim(v.price) === '' && !trim(v.ratingValue)) add('price', 'Add a price or a rating — Google needs one of them to show a product result');
      if (trim(v.price) !== '' && !/^[A-Za-z]{3}$/.test(trim(v.currency))) add('currency', 'Currency must be a 3-letter code like INR');
      if (trim(v.ratingValue) && !trim(v.ratingCount)) add('ratingCount', 'Add the number of ratings along with the average rating');
      if (num(v.ratingValue) > 5) add('ratingValue', 'Rating can’t be more than 5');
    }
  },

  person: {
    fields:[
      {k:'name', l:'Full name', req:1},
      {k:'jobTitle', l:'Job title', m:1, ph:'SEO Strategist'},
      {k:'url', l:'Profile / about page URL', t:'url', m:1, rc:1, full:1},
      {k:'worksFor', l:'Works for', m:1, ph:'Company name'},
      {k:'image', l:'Photo URL', t:'url', m:1},
      {k:'sameAs', l:'Social profiles (one per line)', t:'lines', m:1, rc:1, full:1, ph:'https://linkedin.com/in/…'},
      {k:'description', l:'Short bio', t:'textarea', full:1}
    ],
    build: v => ({'@type':'Person', name:v.name, jobTitle:v.jobTitle, url:v.url, image:v.image, description:v.description,
      worksFor: trim(v.worksFor) ? {'@type':'Organization', name:v.worksFor} : undefined, sameAs:lines(v.sameAs)})
  },

  event: {
    fields:[
      {k:'name', l:'Event name', req:1, full:1},
      {k:'startDate', l:'Starts', t:'datetime', req:1},
      {k:'endDate', l:'Ends', t:'datetime', m:1, rc:1},
      {k:'mode', l:'Where', t:'select', m:1, def:'Offline', opts:[['Offline','In person'],['Online','Online'],['Mixed','Both']]},
      {k:'venue', l:'Venue name', m:1, ph:'Bangalore International Centre'},
      {k:'locality', l:'City', m:1},
      {k:'onlineUrl', l:'Online event link', t:'url', m:1},
      {k:'image', l:'Image URL', t:'url', m:1, rc:1},
      {k:'description', l:'Description', t:'textarea', m:1, rc:1, full:1},
      {k:'price', l:'Ticket price (0 = free)', t:'number', m:1},
      {k:'currency', l:'Currency', def:'INR'},
      {k:'ticketUrl', l:'Ticket / registration link', t:'url'},
      {k:'organizer', l:'Organizer'},
      {k:'street', l:'Street address'}, {k:'postal', l:'PIN code'},
      {k:'country', l:'Country', t:'select', def:'IN', opts:COUNTRY_OPTS},
      {k:'status', l:'Status', t:'select', def:'EventScheduled', opts:[['EventScheduled','Scheduled'],['EventRescheduled','Rescheduled'],['EventPostponed','Postponed'],['EventMovedOnline','Moved online'],['EventCancelled','Cancelled']]}
    ],
    build: v => {
      const mode = v.mode || 'Offline';
      const place = (trim(v.venue) || address(v)) ? {'@type':'Place', name:v.venue, address:address(v)} : undefined;
      const virt = isAbs(v.onlineUrl) ? {'@type':'VirtualLocation', url:v.onlineUrl} : undefined;
      return {'@type':'Event', name:v.name, description:v.description, image:v.image, startDate:dt(v.startDate), endDate:dt(v.endDate),
        eventAttendanceMode:'https://schema.org/' + mode + 'EventAttendanceMode', eventStatus:'https://schema.org/' + (v.status || 'EventScheduled'),
        location: mode === 'Online' ? virt : mode === 'Mixed' ? [place, virt] : place,
        organizer: trim(v.organizer) ? {'@type':'Organization', name:v.organizer} : undefined,
        offers: trim(v.price) !== '' ? {'@type':'Offer', price:trim(v.price), priceCurrency:trim(v.currency).toUpperCase(), url:v.ticketUrl, availability:'https://schema.org/InStock'} : undefined};
    },
    check(v, add) {
      const mode = v.mode || 'Offline';
      if (trim(v.name) && mode !== 'Online' && !trim(v.venue) && !trim(v.locality)) add('venue', 'Add the venue name or city');
      if (trim(v.name) && mode !== 'Offline' && !isAbs(v.onlineUrl)) add('onlineUrl', 'Add the online event link');
      if (v.startDate && v.endDate && v.endDate < v.startDate) add('endDate', 'The event ends before it starts');
    }
  },

  video: {
    fields:[
      {k:'name', l:'Video title', req:1, full:1},
      {k:'thumbnailUrl', l:'Thumbnail image URL', t:'url', req:1, full:1},
      {k:'uploadDate', l:'Upload date', t:'datetime', req:1},
      {k:'embedUrl', l:'Video / embed URL', t:'url', m:1, rc:1, ph:'https://www.youtube.com/embed/…'},
      {k:'description', l:'Description', t:'textarea', m:1, rc:1, full:1},
      {k:'durMin', l:'Length — minutes', t:'number', m:1}, {k:'durSec', l:'Length — seconds', t:'number', m:1},
      {k:'contentUrl', l:'Video file URL (.mp4)', t:'url'}
    ],
    build: v => { const m = num(v.durMin), s = num(v.durSec);
      return {'@type':'VideoObject', name:v.name, description:v.description, thumbnailUrl:v.thumbnailUrl, uploadDate:dt(v.uploadDate),
        duration:(m || s) ? 'PT' + (m ? Math.floor(m) + 'M' : '') + (s ? Math.floor(s) + 'S' : '') : undefined, embedUrl:v.embedUrl, contentUrl:v.contentUrl}; }
  },

  website: {
    fields:[
      {k:'name', l:'Site name', req:1, ph:'BankBazaar'},
      {k:'url', l:'Home page URL', t:'url', req:1, ph:'https://www.example.com/'},
      {k:'alternateName', l:'Other name', ph:'BankBazaar.com'}
    ],
    build: v => ({'@type':'WebSite', name:v.name, alternateName:v.alternateName, url:v.url})
  },

  loan: {
    fields:[
      {k:'name', l:'Product name', req:1, full:1, ph:'XYZ Bank Personal Loan'},
      {k:'providerName', l:'Bank / lender', m:1, rc:1, ph:'XYZ Bank'},
      {k:'url', l:'Page URL', t:'url', m:1, rc:1},
      {k:'rateMin', l:'Interest rate from (% p.a.)', t:'number', m:1, rc:1, ph:'10.50'},
      {k:'rateMax', l:'Interest rate up to (% p.a.)', t:'number', m:1, ph:'24'},
      {k:'amtMin', l:'Amount from (₹)', t:'number', m:1, rc:1, ph:'50000'},
      {k:'amtMax', l:'Amount up to (₹)', t:'number', m:1, ph:'4000000'},
      {k:'termMin', l:'Tenure from (months)', t:'number', m:1, rc:1, ph:'12'},
      {k:'termMax', l:'Tenure up to (months)', t:'number', m:1, ph:'60'},
      {k:'description', l:'Short description', t:'textarea', m:1, full:1},
      {k:'fees', l:'Processing fee', full:1, ph:'Up to 2% of the loan amount + GST'},
      {k:'aprMin', l:'APR from (%)', t:'number'}, {k:'aprMax', l:'APR up to (%)', t:'number'},
      {k:'collateral', l:'Collateral / security', ph:'None (unsecured)'},
      {k:'currency', l:'Currency', def:'INR'},
      {k:'loanType', l:'Loan type', ph:'Personal Loan'},
      {k:'areaServed', l:'Available in', def:'India'}
    ],
    build: v => { const cur = trim(v.currency).toUpperCase() || undefined;
      return {'@type':v.sub || 'LoanOrCredit', name:v.name, loanType:v.loanType, description:v.description, url:v.url,
        provider: trim(v.providerName) ? {'@type':'BankOrCreditUnion', name:v.providerName} : undefined,
        interestRate:range(v.rateMin, v.rateMax, {'@type':'QuantitativeValue', unitText:'PERCENT'}),
        annualPercentageRate:range(v.aprMin, v.aprMax, {'@type':'QuantitativeValue', unitText:'PERCENT'}),
        feesAndCommissionsSpecification:v.fees,
        amount:range(v.amtMin, v.amtMax, {'@type':'MonetaryAmount', currency:cur}),
        loanTerm:range(v.termMin, v.termMax, {'@type':'QuantitativeValue', unitCode:'MON'}),
        requiredCollateral:v.collateral, currency:cur, areaServed: trim(v.areaServed) ? {'@type':'Country', name:v.areaServed} : undefined}; },
    check(v, add) {
      [['rateMin','rateMax','Interest rate'],['amtMin','amtMax','Amount'],['termMin','termMax','Tenure'],['aprMin','aprMax','APR']].forEach(([a, b, n]) => {
        if (num(v[a]) !== undefined && num(v[b]) !== undefined && num(v[a]) > num(v[b])) add(a, `${n}: “from” is bigger than “up to”`); });
      if (num(v.rateMin) > 60) add('rateMin', 'Interest rate looks too high — enter it like 10.5 for 10.5%');
    }
  }
};

// ------------------------------------------------------------ menu
const ELIG = {
  eligible:  ['eligible','✓ Can show as a Google rich result'],
  retired:   ['retired','✕ Google rich result retired (May 2026)'],
  none:      ['none','No rich result on its own'],
  schemaorg: ['schemaorg','AI-search signal · no Google rich result']
};
const MENU = [
  {g:'Most used'},
  {id:'article', base:'article', label:'📰 Article / Blog post', elig:'eligible', desc:'For blog posts and news. Google can show the <b>headline, image and date</b> (Top stories, Discover).'},
  {id:'breadcrumb', base:'breadcrumb', label:'🧭 Breadcrumbs', elig:'eligible', desc:'Shows the page path — <b>Home › Loans › Personal Loan</b> — in Google instead of the raw URL.'},
  {id:'faq', base:'faq', label:'❓ FAQ', elig:'retired', desc:'Question & answer list. <b>Google stopped showing FAQ rich results on 7 May 2026.</b> Still valid, and it helps AI assistants pick up your answers.'},
  {id:'organization', base:'organization', label:'🏢 Organization', elig:'eligible', desc:'Your company’s <b>name, logo and social profiles</b>. Helps Google’s knowledge panel and AI search know who you are.'},
  {id:'localbusiness', base:'localbusiness', label:'📍 Local Business', elig:'eligible', desc:'A shop or office with an <b>address, phone and opening hours</b>.'},
  {id:'product', base:'product', label:'🛒 Product', elig:'eligible', desc:'Shows <b>price, stock and star rating</b> in Google results.'},
  {id:'person', base:'person', label:'👤 Person / Author', elig:'none', desc:'An author or expert profile. No rich result on its own, but it builds <b>trust (E-E-A-T)</b>.'},
  {id:'event', base:'event', label:'🎟️ Event', elig:'eligible', desc:'Shows the <b>date, venue and tickets</b> in Google’s event listings.'},
  {id:'video', base:'video', label:'🎬 Video', elig:'eligible', desc:'Helps your video show up in <b>video results</b> with its thumbnail.'},
  {id:'website', base:'website', label:'🌐 WebSite (site name)', elig:'eligible', desc:'Tells Google which <b>site name</b> to show above your results.'},
  {g:'Banking & finance'},
  {id:'personal-loan', base:'loan', label:'💰 Personal Loan', elig:'schemaorg', seed:{sub:'LoanOrCredit', loanType:'Personal Loan', collateral:'None (unsecured)'},
   desc:'Rate, amount and tenure of a loan in a machine-readable way. Google has no loan rich result, but this is a <b>strong signal for AI Overviews, ChatGPT and Perplexity</b>.'},
  {id:'home-loan', base:'loan', label:'🏠 Home Loan', elig:'schemaorg', seed:{sub:'MortgageLoan', loanType:'Home Loan', collateral:'The property being purchased'},
   desc:'Home loan rate, amount and tenure (MortgageLoan). <b>Strong signal for AI search</b> — no Google rich result for loans.'},
  {id:'credit-card', base:'loan', label:'💳 Credit Card', elig:'schemaorg', seed:{sub:'CreditCard', loanType:'Credit Card'},
   desc:'Credit card details (interest, fees). <b>AI-search signal</b> — no Google rich result for cards.'},
  {id:'bank-branch', base:'localbusiness', label:'🏦 Bank Branch', elig:'eligible', seed:{sub:'BankOrCreditUnion'}, promote:['branchCode','hours'],
   desc:'A bank branch with <b>address, IFSC and opening hours</b> — powers local details in Google.'}
];
const MENU_BY_ID = {}; MENU.forEach(m => { if (m.id) MENU_BY_ID[m.id] = m; });

// ------------------------------------------------------------ state
let cur = 'article';
const store = {};      // values per menu id, so switching types keeps what you typed
let showMore = false;
let touched = false;   // don't shout "required" before the user types anything

function fieldsOf(id) {
  const m = MENU_BY_ID[id];
  return TYPES[m.base].fields.map(f => Object.assign({}, f, (m.promote || []).includes(f.k) ? {m:1} : {}));
}
function freshValues(id) {
  const m = MENU_BY_ID[id], v = {};
  TYPES[m.base].fields.forEach(f => {
    if (f.t === 'rows') v[f.k] = JSON.parse(JSON.stringify(f.def || [{}]));
    else if (f.def !== undefined) v[f.k] = f.def;
  });
  Object.assign(v, JSON.parse(JSON.stringify(m.seed || {})));
  return v;
}
const vals = () => (store[cur] = store[cur] || freshValues(cur));

// ------------------------------------------------------------ form rendering
function fieldHTML(f, v) {
  const val = v[f.k] ?? '';
  const id = 'f_' + f.k;
  const star = f.req ? '<span class="star">*</span>' : '';
  let input;
  if (f.t === 'select') input = `<select id="${id}" data-k="${f.k}">${f.opts.map(o => `<option value="${esc(o[0])}"${o[0] === val ? ' selected' : ''}>${esc(o[1])}</option>`).join('')}</select>`;
  else if (f.t === 'textarea' || f.t === 'lines') input = `<textarea id="${id}" data-k="${f.k}" placeholder="${esc(f.ph || '')}">${esc(val)}</textarea>`;
  else if (f.t === 'rows') input = rowsHTML(f, v);
  else {
    const t = {url:'url', number:'number', date:'date', datetime:'datetime-local'}[f.t] || 'text';
    input = `<input type="${t}" id="${id}" data-k="${f.k}" value="${esc(val)}" placeholder="${esc(f.ph || '')}"${t === 'number' ? ' step="any"' : ''}>`;
  }
  return `<div class="field${f.full || f.t === 'rows' ? ' full' : ''}"><label class="fl" for="${id}">${esc(f.l)}${star}</label>${input}${f.hint ? `<div class="hint">${esc(f.hint)}</div>` : ''}</div>`;
}
function rowsHTML(f, v) {
  const rows = v[f.k] || [];
  const tmpl = f.cols.length === 3 ? '1.3fr 1fr 1fr' : f.cols.some(c => c.t === 'textarea') ? '1fr' : '1fr 1.4fr';
  return `<div class="rows" id="f_${f.k}">` + rows.map((r, i) => `<div class="rrow"><div class="rn">${i + 1}</div><div class="rcells" style="grid-template-columns:${tmpl}">` +
    f.cols.map(c => {
      const d = `data-k="${f.k}" data-i="${i}" data-c="${c.k}" aria-label="${esc(c.l)}"`; const cv = r[c.k] ?? '';
      if (c.t === 'select') return `<select ${d}>${c.opts.map(o => `<option value="${esc(o[0])}"${o[0] === cv ? ' selected' : ''}>${esc(o[1])}</option>`).join('')}</select>`;
      if (c.t === 'textarea') return `<textarea ${d} placeholder="${esc(c.ph || c.l)}">${esc(cv)}</textarea>`;
      return `<input type="${c.t === 'time' ? 'time' : 'text'}" ${d} value="${esc(cv)}" placeholder="${esc(c.ph || c.l)}">`;
    }).join('') + `</div><button type="button" class="xbtn" data-act="rowdel" data-k="${f.k}" data-i="${i}" title="Remove">✕</button></div>`).join('') +
    `</div><button type="button" class="btn ghost sm" style="margin-top:.5rem" data-act="rowadd" data-k="${f.k}">${esc(f.add || '+ Add row')}</button>`;
}
function renderForm() {
  const v = vals(), fs = fieldsOf(cur);
  const main = fs.filter(f => f.req || f.m), more = fs.filter(f => !(f.req || f.m));
  $('mainFields').innerHTML = main.map(f => fieldHTML(f, v)).join('');
  $('moreFields').innerHTML = more.map(f => fieldHTML(f, v)).join('');
  $('moreBtn').style.display = more.length ? '' : 'none';
  $('moreBtn').textContent = showMore ? '− Hide optional fields' : `+ Show ${more.length} more optional field${more.length > 1 ? 's' : ''}`;
  $('moreFields').classList.toggle('show', showMore);
  const m = MENU_BY_ID[cur], e = ELIG[m.elig];
  $('typeBadge').className = 'badge ' + e[0]; $('typeBadge').textContent = e[1];
  $('typeDesc').innerHTML = m.desc;
}

// ------------------------------------------------------------ validation + output
function problems() {
  const v = vals(), out = [], tips = [];
  const add = (k, m) => out.push({k, m});
  fieldsOf(cur).forEach(f => {
    const raw = v[f.k];
    const filled = f.t === 'rows' ? (raw || []).some(r => Object.values(r).some(x => trim(x))) : !!trim(raw);
    if (f.req && !filled) add(f.k, `${f.l.replace(/ \(.*\)$/, '')} is required`);
    if (filled && f.t === 'url' && !isAbs(raw)) add(f.k, `${f.l} should start with https://`);
    if (filled && f.t === 'lines' && lines(raw).some(u => !isAbs(u))) add(f.k, `${f.l.replace(/ \(.*\)$/, '')}: each line should start with https://`);
    if (!filled && f.rc) tips.push(f.l.replace(/ \(.*\)$/, ''));
  });
  const t = TYPES[MENU_BY_ID[cur].base];
  if (t.check) t.check(v, add);
  return {errors: out, tips};
}
function clean(o) {
  if (Array.isArray(o)) { const a = o.map(clean).filter(x => x !== undefined); return a.length ? (a.length === 1 && o.length === 1 ? a : a) : undefined; }
  if (o && typeof o === 'object') {
    const r = {}; Object.keys(o).forEach(k => { const v = clean(o[k]); if (v !== undefined) r[k] = v; });
    return Object.keys(r).some(k => k !== '@type') ? r : undefined;
  }
  if (typeof o === 'string') { const s = o.trim(); return s || undefined; }
  if (o === undefined || o === null || (typeof o === 'number' && isNaN(o))) return undefined;
  return o;
}
function buildData() {
  const node = clean(TYPES[MENU_BY_ID[cur].base].build(vals()));
  return node ? Object.assign({'@context':'https://schema.org'}, node) : null;
}
function scriptText(data) {
  return '<script type="application/ld+json">\n' + JSON.stringify(data, null, 2).replace(/</g, '\\u003c') + '\n<\/script>';
}
function highlight(text) {
  return esc(text).replace(/&quot;/g, '"')
    .replace(/("(?:\\u[a-fA-F0-9]{4}|\\[^u]|[^\\"])*")(\s*:)?|\b(true|false|null)\b|(-?\b\d+(?:\.\d+)?\b)/g, (m, s, colon, kw, n) =>
      s ? (colon ? `<span class="k">${s}</span>${colon}` : `<span class="s">${s}</span>`) : `<span class="n">${kw || n}</span>`)
    .replace(/^(&lt;script.*?&gt;)$/m, '<span class="t">$1</span>').replace(/(&lt;\/script&gt;)$/, '<span class="t">$1</span>');
}
function refresh() {
  const {errors, tips} = problems();
  const data = buildData();
  document.querySelectorAll('#mainFields .bad, #moreFields .bad').forEach(el => el.classList.remove('bad'));
  if (touched) errors.forEach(e => { const el = $('f_' + e.k); if (el && el.tagName !== 'DIV') el.classList.add('bad'); });

  const st = $('status');
  if (touched && errors.length) { st.className = 'status err'; st.innerHTML = '⚠ Fix these first: ' + errors.slice(0, 4).map(e => esc(e.m)).join(' · ') + (errors.length > 4 ? ` · +${errors.length - 4} more` : ''); }
  else if (!data || errors.length) { st.className = 'status idle'; st.innerHTML = 'Fill in the fields marked <b style="color:var(--red)">*</b> — your code updates here as you type.'; }
  else { st.className = 'status ok'; st.innerHTML = '✓ Ready to use — copy the code below.' + (tips.length ? `<span class="tip">Tip: also add ${esc(tips.slice(0, 3).join(', '))} for a better result.</span>` : ''); }
  $('code').innerHTML = data ? highlight(scriptText(data)) : '<span class="t">// Your JSON-LD code will appear here.</span>';
  ['copyCode','testGoogle','dlCode'].forEach(id => $(id).disabled = !data);
}

// ------------------------------------------------------------ events
function onEdit(e) {
  const t = e.target; if (!t.dataset || !t.dataset.k) return;
  const v = vals();
  if (t.dataset.i !== undefined) v[t.dataset.k][+t.dataset.i][t.dataset.c] = t.value; else v[t.dataset.k] = t.value;
  touched = true; refresh();
}
['mainFields','moreFields'].forEach(id => {
  $(id).addEventListener('input', onEdit); $(id).addEventListener('change', onEdit);
  $(id).addEventListener('click', e => {
    const b = e.target.closest('[data-act]'); if (!b) return;
    const v = vals(), k = b.dataset.k;
    if (b.dataset.act === 'rowadd') { const f = fieldsOf(cur).find(x => x.k === k); const r = {}; f.cols.forEach(c => r[c.k] = c.t === 'select' ? c.opts[0][0] : ''); v[k].push(r); }
    if (b.dataset.act === 'rowdel') v[k].splice(+b.dataset.i, 1);
    renderForm(); refresh();
  });
});
$('moreBtn').onclick = () => { showMore = !showMore; renderForm(); refresh(); };
$('resetBtn').onclick = () => { store[cur] = freshValues(cur); touched = false; renderForm(); refresh(); };
$('typeSel').innerHTML = (() => { let h = '', open = false;
  MENU.forEach(m => { if (m.g) { h += (open ? '</optgroup>' : '') + `<optgroup label="${esc(m.g)}">`; open = true; } else h += `<option value="${m.id}">${esc(m.label)}</option>`; });
  return h + (open ? '</optgroup>' : ''); })();
$('typeSel').onchange = () => { cur = $('typeSel').value; showMore = false; touched = false; renderForm(); refresh(); if (lastFetch) fillFrom(lastFetch); };

async function copyText(txt) {
  try { await navigator.clipboard.writeText(txt); }
  catch (e) { const t = document.createElement('textarea'); t.value = txt; document.body.appendChild(t); t.select(); try { document.execCommand('copy'); } catch (x) {} t.remove(); }
}
function flash(m) { $('copyMsg').textContent = m; clearTimeout(flash.t); flash.t = setTimeout(() => $('copyMsg').textContent = '', 2800); }
$('copyCode').onclick = async () => { const d = buildData(); if (!d) return; await copyText(scriptText(d)); flash('✓ Copied'); };
$('testGoogle').onclick = async () => { const d = buildData(); if (!d) return; await copyText(scriptText(d)); flash('✓ Copied — paste it in the “Code” tab'); window.open('https://search.google.com/test/rich-results', '_blank', 'noopener'); };
$('dlCode').onclick = () => { const d = buildData(); if (!d) return;
  const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([JSON.stringify(d, null, 2)], {type:'application/ld+json'}));
  a.download = cur + '-schema.json'; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000); };

// ------------------------------------------------------------ auto-fill
let lastFetch = null;
const RETIRED = {FAQPage:1, HowTo:1, ClaimReview:1, SpecialAnnouncement:1, Course:1, Car:1, Vehicle:1, Occupation:1, LearningResource:1};
const isoToLocal = iso => { const m = trim(iso).match(/^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}))?/); return m ? `${m[1]}T${m[2] || '00:00'}` : ''; };
function crumbsFrom(url, title) {
  try {
    const u = new URL(url), segs = u.pathname.split('/').filter(Boolean), rows = [{name:'Home', url:u.origin + '/'}];
    let path = '';
    segs.forEach((s, i) => { path += '/' + s;
      rows.push({name: i === segs.length - 1 && title ? title : decodeURIComponent(s).replace(/\.(html?|php|aspx?)$/i, '').replace(/[-_]+/g, ' ').replace(/\b\w/g, c => c.toUpperCase()),
                 url: i === segs.length - 1 ? '' : u.origin + path + '/'}); });
    return rows.length >= 2 ? rows : null;
  } catch (e) { return null; }
}
function fillFrom(d) {
  const v = vals(), base = MENU_BY_ID[cur].base;
  const page = d.canonical || d.final_url, origin = originOf(d.final_url);
  const title = d.h1 || d.title;
  const set = (k, val) => { if (val && !trim(v[k])) v[k] = val; };
  const org = (d.existing || []).map(x => x.json).find(j => [].concat(j['@type'] || []).some(t => /Organization|Corporation|Bank|FinancialService/.test(t)));
  switch (base) {
    case 'article': set('headline', title); set('url', page); set('image', d.image); set('description', d.description); set('authorName', d.author);
      set('publisherName', d.site_name); set('datePublished', isoToLocal(d.published)); set('dateModified', isoToLocal(d.modified)); break;
    case 'breadcrumb': if (!(v.items || []).some(r => trim(r.url))) { const r = crumbsFrom(page, title); if (r) v.items = r; } break;
    case 'organization': set('name', d.site_name || (org && org.name)); set('url', origin);
      set('logo', org && (typeof org.logo === 'string' ? org.logo : org.logo && org.logo.url) || d.logo);
      if (org && org.sameAs) set('sameAs', [].concat(org.sameAs).join('\n')); break;
    case 'website': set('name', d.site_name); set('url', origin + '/'); break;
    case 'localbusiness': set('name', title); set('url', page); set('image', d.image); break;
    case 'product': set('name', title); set('image', d.image); set('description', d.description); set('price', d.price); set('url', page);
      if (d.currency) v.currency = d.currency.toUpperCase(); break;
    case 'person': set('name', d.author); break;
    case 'event': set('name', title); set('image', d.image); set('description', d.description); break;
    case 'video': set('name', title); set('thumbnailUrl', d.image); set('description', d.description); break;
    case 'loan': set('name', title); set('url', page); set('description', d.description); break;
  }
  renderForm(); refresh();
}
function showFound(d) {
  const ex = d.existing || [];
  let h = '';
  if (ex.length) {
    const types = [...new Set(ex.flatMap(x => x.types))];
    h += 'Schema already on this page: ' + types.map(t => `<span class="badge ${RETIRED[t] ? 'retired' : 'schemaorg'}">${esc(t)}${RETIRED[t] ? ' · retired' : ''}</span>`).join('');
  } else h += 'No schema found on this page yet — good opportunity to add some.';
  if (d.invalid_jsonld) h += `<br><span style="color:var(--red)">⚠ ${d.invalid_jsonld} schema block${d.invalid_jsonld > 1 ? 's are' : ' is'} broken on this page (invalid code) — Google ignores ${d.invalid_jsonld > 1 ? 'them' : 'it'}.</span>`;
  $('found').innerHTML = h;
}
async function autofill() {
  const url = trim($('fetchUrl').value);
  if (!url) { $('fetchMsg').className = 'msg err'; $('fetchMsg').textContent = 'Paste a page URL first.'; return; }
  $('fetchBtn').disabled = true; $('fetchMsg').className = 'msg'; $('fetchMsg').textContent = 'Reading the page… (first use can take ~30–50 seconds while the free server wakes up)';
  try {
    const r = await fetch('/api/fetch?url=' + encodeURIComponent(url));
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || 'Could not read that page.');
    lastFetch = d; touched = true; fillFrom(d); showFound(d);
    $('fetchMsg').className = 'msg ok'; $('fetchMsg').textContent = '✓ Form filled from the page — check the details and add anything missing.';
  } catch (err) { $('fetchMsg').className = 'msg err'; $('fetchMsg').textContent = '✕ ' + err.message; }
  finally { $('fetchBtn').disabled = false; }
}
$('fetchBtn').onclick = autofill;
$('fetchUrl').addEventListener('keydown', e => { if (e.key === 'Enter') autofill(); });

// ------------------------------------------------------------ input mode (manual / from URL)
let mode = 'manual';
function setMode(m) {
  mode = m;
  document.querySelectorAll('#modes .mode').forEach(b => b.classList.toggle('active', b.dataset.mode === m));
  $('urlBox').classList.toggle('show', m === 'url');
  $('typeStep').textContent = m === 'url' ? '2' : '1';
  document.querySelectorAll('.layout .step').forEach((el, i) => el.textContent = String((m === 'url' ? 3 : 2) + i));
  if (m === 'url') setTimeout(() => $('fetchUrl').focus(), 50);
}
document.querySelectorAll('#modes .mode').forEach(b => b.onclick = () => setMode(b.dataset.mode));

// ------------------------------------------------------------ init
(function init() {
  const q = new URLSearchParams(location.search);
  if (q.get('type') && MENU_BY_ID[q.get('type')]) cur = q.get('type');
  $('typeSel').value = cur;
  renderForm(); refresh();
  if (q.get('url')) { setMode('url'); $('fetchUrl').value = q.get('url'); autofill(); }
})();

</script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(debug=True, port=8501)
