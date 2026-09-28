"""
Meta Tags Length Checker  -  Flask app
Check the length of every meta tag on a page - in characters AND pixels -
the way Google actually measures it.

  * Two ways in: Fetch from a URL (default) or Manual input
  * Meta title + description: characters, pixel width vs Google's 580px / 920px limits,
    a score out of 100, a colour bar and a plain-English fix for each
  * Live Google preview (desktop + mobile) that shows exactly where the "..." cut happens
  * Optional focus keyword: bolded in the preview + keyword placement checks
  * Every other meta tag on the page: robots / googlebot / X-Robots-Tag (incl. max-snippet),
    canonical, H1, Open Graph, Twitter Card, viewport, charset, lang - with length checks
  * Copy title, copy description, or copy the finished HTML tags

Pixel widths are measured in the browser (Arial 20px for titles, 13px for descriptions,
like Google). The server only reads pages.

Built by Mahalakshmi Marimuthu - Digital Marketing Strategist & AI-Powered SEO Expert
"""
import ipaddress
import os
import re
import socket
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

REQUEST_TIMEOUT = 9
MAX_HTML_BYTES = 4 * 1024 * 1024
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": BROWSER_UA,
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
           "Accept-Language": "en-US,en;q=0.9"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_url(raw):
    raw = (raw or "").strip()
    if not raw:
        return ""
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    return raw


def safe_url(url):
    """Only public http(s) addresses."""
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
        return True    # the request itself will fail with a friendly message
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    return True


def rel_has(value, word):
    if not value:
        return False
    v = " ".join(value) if isinstance(value, list) else value
    return word in v.lower().split()


def meta_by(soup, attr, name):
    """All content values of <meta attr="name">, case-insensitive."""
    rx = re.compile(r"^\s*" + re.escape(name) + r"\s*$", re.I)
    return [clean(m.get("content")) for m in soup.find_all("meta", attrs={attr: rx})
            if m.get("content") is not None]


def first(lst):
    return lst[0] if lst else ""


# --------------------------------------------------------------------------- #
# Reading a page
# --------------------------------------------------------------------------- #
def get_html(url):
    """-> (response, html) or raises. One retry on typical bot-protection codes."""
    for attempt in (1, 2):
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT,
                         allow_redirects=True, stream=True)
        if r.status_code in (403, 429, 503) and attempt == 1:
            r.close()
            time.sleep(1.2)
            continue
        raw = r.raw.read(MAX_HTML_BYTES, decode_content=True)
        enc = r.encoding or r.apparent_encoding or "utf-8"
        if enc.lower() == "iso-8859-1" and r.apparent_encoding:
            enc = r.apparent_encoding
        try:
            html = raw.decode(enc, errors="replace")
        except LookupError:
            html = raw.decode("utf-8", errors="replace")
        return r, html
    return r, ""


def read_page(url):
    url = normalize_url(url)
    if not url or not safe_url(url):
        return {"ok": False, "url": url, "error": "This isn't a valid public URL."}
    try:
        r, html = get_html(url)
    except requests.exceptions.Timeout:
        return {"ok": False, "url": url, "error": "The page took too long to respond."}
    except requests.exceptions.RequestException:
        return {"ok": False, "url": url, "error": "Could not connect to this URL."}

    if r.status_code >= 400:
        blocked = r.status_code in (401, 403, 429, 503)
        return {"ok": False, "url": url, "status": r.status_code,
                "error": f"The page returned HTTP {r.status_code}" +
                         (" — the site may be blocking automated checks. Use Manual input instead."
                          if blocked else ".")}
    ctype = (r.headers.get("Content-Type") or "").lower()
    if ctype and "html" not in ctype and "xml" not in ctype and "text" not in ctype:
        return {"ok": False, "url": url, "status": r.status_code,
                "error": "This URL isn't a web page."}

    final_url = r.url
    soup = BeautifulSoup(html, "html.parser")
    head = soup.head or soup

    titles = [clean(t.get_text()) for t in soup.find_all("title")
              if not t.find_parent("svg")]
    descs = meta_by(soup, "name", "description")

    canonical = ""
    ctag = head.find("link", rel=lambda v: rel_has(v, "canonical"))
    if ctag and ctag.get("href"):
        canonical = urljoin(final_url, ctag["href"].strip())

    favicon = ""
    for tag in head.find_all("link"):
        rels = tag.get("rel") or []
        if any(x.lower() in ("icon", "shortcut", "apple-touch-icon") for x in rels) and tag.get("href"):
            favicon = urljoin(final_url, tag["href"].strip())
            break
    if not favicon:
        p = urlparse(final_url)
        favicon = f"{p.scheme}://{p.netloc}/favicon.ico"

    charset = ""
    mc = soup.find("meta", attrs={"charset": True})
    if mc:
        charset = clean(mc.get("charset"))
    else:
        he = soup.find("meta", attrs={"http-equiv": re.compile(r"^content-type$", re.I)})
        if he and "charset=" in (he.get("content") or "").lower():
            charset = he["content"].lower().split("charset=")[-1].strip()

    html_tag = soup.find("html")
    h1s = [clean(h.get_text(" "))[:300] for h in soup.find_all("h1")]
    h1s = [h for h in h1s if h]

    og = {}
    for key in ("title", "description", "image", "url", "type", "site_name"):
        vals = meta_by(soup, "property", "og:" + key) or meta_by(soup, "name", "og:" + key)
        og[key] = first(vals)
    tw = {}
    for key in ("card", "title", "description", "image", "site"):
        vals = meta_by(soup, "name", "twitter:" + key) or meta_by(soup, "property", "twitter:" + key)
        tw[key] = first(vals)

    redirected = final_url.rstrip("/") != url.rstrip("/")

    return {
        "ok": True,
        "url": url,
        "final_url": final_url,
        "redirected": redirected,
        "status": r.status_code,
        "title": first(titles),
        "title_count": len(titles),
        "description": first(descs),
        "description_count": len(descs),
        "keywords": first(meta_by(soup, "name", "keywords")),
        "robots": first(meta_by(soup, "name", "robots")),
        "googlebot": first(meta_by(soup, "name", "googlebot")),
        "x_robots": clean(r.headers.get("X-Robots-Tag", "")),
        "canonical": canonical,
        "h1": first(h1s),
        "h1_count": len(h1s),
        "viewport": first(meta_by(soup, "name", "viewport")),
        "charset": charset,
        "lang": clean(html_tag.get("lang")) if html_tag and html_tag.get("lang") else "",
        "og": og,
        "twitter": tw,
        "favicon": favicon,
    }


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/api/fetch")
def api_fetch():
    url = normalize_url(request.args.get("url", ""))
    if not url:
        return jsonify({"ok": False, "error": "Please enter a page URL."}), 400
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
<title>Meta Tags Length Checker – Title &amp; Description Pixel Checker</title>
<meta name="description" content="Free Meta Tags Length Checker: check your meta title and description in characters and pixels, see a Google preview, and audit robots, canonical, Open Graph and Twitter tags.">
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
  .card + .card,.gap{margin-top:1.2rem}
  .card h2{font-family:'Sora',sans-serif;font-size:.9rem;font-weight:700;margin-bottom:.9rem;display:flex;align-items:center;gap:.5rem;flex-wrap:wrap}
  .card h2 .right{margin-left:auto;display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}

  .modebar{display:flex;align-items:center;gap:.6rem;flex-wrap:wrap;margin-bottom:1rem}
  .mtab{background:var(--surface);border:1px solid var(--border);color:var(--muted);font-family:'Inter',sans-serif;font-size:.84rem;font-weight:600;padding:.55rem 1.1rem;border-radius:9px;cursor:pointer}
  .mtab:hover{color:var(--text);border-color:rgba(124,106,247,.5)}
  .mtab.active{background:rgba(124,106,247,.14);border-color:rgba(124,106,247,.55);color:var(--accent)}
  .clear{margin-left:auto;background:none;border:none;color:var(--muted);font-size:.78rem;cursor:pointer;font-family:'Inter',sans-serif}
  .clear:hover{color:var(--text)}

  label.fl{display:flex;justify-content:space-between;align-items:baseline;gap:.5rem;flex-wrap:wrap;font-size:.8rem;font-weight:600;color:var(--text);margin-bottom:.35rem}
  label.fl .opt{font-family:'DM Mono',monospace;font-size:.66rem;font-weight:500;color:var(--muted)}
  input[type=text],textarea{width:100%;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:.6rem .85rem;font-family:'Inter',sans-serif;font-size:.88rem}
  textarea{resize:vertical;line-height:1.5}
  input::placeholder,textarea::placeholder{color:#5c5c78}
  input:focus,textarea:focus{outline:none;border-color:var(--accent)}
  .field{margin-bottom:1rem}
  .row{display:flex;gap:.7rem;flex-wrap:wrap}
  .row input{flex:1;min-width:220px}
  .grid2{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:1rem}
  .hint{font-size:.72rem;color:var(--muted);margin-top:.3rem;line-height:1.5}
  .hide{display:none!important}

  .bar{height:6px;border-radius:6px;background:var(--surface2);border:1px solid var(--border);overflow:hidden;margin-top:.55rem}
  .bar .fill{height:100%;border-radius:6px;transition:width .15s,background .15s}
  .say{display:flex;gap:.5rem;align-items:flex-start;font-size:.8rem;margin-top:.5rem;line-height:1.5}
  .say .dot{width:17px;height:17px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:.62rem;font-weight:800;color:#0a0a0f;margin-top:.1rem}
  .say .more{display:block;font-size:.72rem;color:var(--muted);margin-top:.1rem}
  .g{color:var(--mint)} .o{color:var(--orange)} .r{color:var(--red)} .m{color:var(--muted)}
  .bg-g{background:var(--mint)} .bg-o{background:var(--orange)} .bg-r{background:var(--red)} .bg-m{background:var(--muted)}

  .btn{display:inline-flex;align-items:center;gap:.45rem;background:var(--accent);color:#fff;border:none;border-radius:8px;padding:.62rem 1.25rem;font-size:.85rem;font-weight:600;cursor:pointer;font-family:'Inter',sans-serif;white-space:nowrap}
  .btn:hover{background:var(--accent-h)}
  .btn.ghost{background:var(--surface2);border:1px solid var(--border);color:var(--text)}
  .btn.ghost:hover{border-color:var(--accent)}
  .btn.sm{padding:.4rem .8rem;font-size:.76rem}
  .btn:disabled{opacity:.5;cursor:not-allowed}
  .btns{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center}
  .msg{font-size:.8rem;margin-top:.7rem}
  .msg.err{color:var(--red)} .msg.wait{color:var(--muted)} .msg.ok{color:var(--mint)}
  .spin{display:inline-block;width:12px;height:12px;border:2px solid rgba(255,255,255,.35);border-top-color:#fff;border-radius:50%;animation:sp .8s linear infinite}
  @keyframes sp{to{transform:rotate(360deg)}}

  /* Google preview */
  .tabs{display:inline-flex;background:var(--surface2);border:1px solid var(--border);border-radius:9px;padding:3px;gap:3px}
  .tabs button{background:none;border:1px solid transparent;color:var(--muted);font-family:'Inter',sans-serif;font-size:.74rem;font-weight:600;padding:.3rem .8rem;border-radius:6px;cursor:pointer}
  .tabs button.on{background:rgba(124,106,247,.16);border-color:rgba(124,106,247,.45);color:var(--accent)}
  .serp-wrap{background:#fff;border-radius:10px;padding:1.1rem 1.2rem;overflow:hidden}
  .serp-wrap.mobile{display:flex;justify-content:center;background:#f1f3f4}
  .serp{font-family:Arial,sans-serif;text-align:left}
  .serp.desktop{width:600px;transform-origin:top left}
  .serp.mobile{width:360px;background:#fff;border-radius:14px;padding:14px 16px;box-shadow:0 1px 3px rgba(0,0,0,.12)}
  .s-site{display:flex;align-items:center;gap:10px;margin-bottom:6px}
  .fav{width:26px;height:26px;border-radius:50%;background:#f1f3f4;border:1px solid #dadce0;display:flex;align-items:center;justify-content:center;overflow:hidden;flex-shrink:0;font-size:12px;font-weight:700;color:#5f6368}
  .fav img{width:18px;height:18px}
  .s-name{font-size:14px;color:#202124;line-height:20px}
  .s-url{font-size:12px;color:#4d5156;line-height:18px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:520px}
  .serp.mobile .s-url{max-width:280px}
  .s-title{font-size:20px;line-height:26px;color:#1a0dab;margin-bottom:3px;word-wrap:break-word}
  .serp.mobile .s-title{font-size:18px;line-height:24px}
  .s-desc{font-size:14px;line-height:22px;color:#4d5156;word-wrap:break-word}
  .s-desc b{color:#202124;font-weight:700}
  .s-empty{color:#9aa0a6;font-style:italic}

  /* score + checks */
  .score-row{display:flex;gap:1.3rem;align-items:center;flex-wrap:wrap}
  .ring{position:relative;width:96px;height:96px;flex-shrink:0}
  .ring svg{transform:rotate(-90deg)}
  .ring .num{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;font-family:'Sora',sans-serif;font-weight:800;font-size:1.5rem;line-height:1}
  .ring .num small{font-family:'DM Mono',monospace;font-size:.58rem;color:var(--muted);font-weight:400;margin-top:.25rem}
  .score-text{flex:1;min-width:200px}
  .verdict{font-family:'Sora',sans-serif;font-weight:700;font-size:.95rem;margin-bottom:.2rem}
  .score-text .sub{font-size:.78rem;color:var(--muted);line-height:1.55}
  .checks{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:.55rem;margin-top:1rem}
  .chk{display:flex;gap:.6rem;align-items:flex-start;background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:.6rem .75rem}
  .chk .ic{width:20px;height:20px;border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:.66rem;font-weight:800;flex-shrink:0;margin-top:.05rem}
  .ic.pass{background:rgba(106,247,200,.14);color:var(--mint)} .ic.warn{background:rgba(247,162,106,.14);color:var(--orange)}
  .ic.fail{background:rgba(247,106,106,.14);color:var(--red)} .ic.info{background:rgba(124,106,247,.14);color:var(--accent)}
  .chk .ct{font-size:.8rem;font-weight:600}
  .chk .cd{font-size:.72rem;color:var(--muted);line-height:1.5}

  /* tables */
  .tbl-wrap{overflow-x:auto}
  table{width:100%;border-collapse:collapse;font-size:.8rem}
  th{text-align:left;font-family:'DM Mono',monospace;font-size:.62rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);font-weight:500;padding:.5rem .6rem;border-bottom:1px solid var(--border);white-space:nowrap}
  td{padding:.6rem;border-bottom:1px solid var(--border);vertical-align:top}
  tr:last-child td{border-bottom:none}
  tr.grp td{background:var(--surface2);font-family:'DM Mono',monospace;font-size:.64rem;letter-spacing:.12em;text-transform:uppercase;color:var(--accent);padding:.4rem .6rem}
  .tag{font-family:'DM Mono',monospace;font-size:.72rem;white-space:nowrap}
  .val{max-width:430px;word-break:break-word}
  .val.none{color:var(--muted);font-style:italic}
  .len{font-family:'DM Mono',monospace;font-size:.7rem;white-space:nowrap;color:var(--muted)}
  .tip{font-size:.72rem;color:var(--muted);margin-top:.2rem;line-height:1.45}
  .badge{font-family:'DM Mono',monospace;font-size:.62rem;padding:.18rem .55rem;border-radius:100px;white-space:nowrap;display:inline-block}
  .badge.pass{background:rgba(106,247,200,.1);border:1px solid rgba(106,247,200,.35);color:var(--mint)}
  .badge.warn{background:rgba(247,162,106,.1);border:1px solid rgba(247,162,106,.35);color:var(--orange)}
  .badge.fail{background:rgba(247,106,106,.1);border:1px solid rgba(247,106,106,.35);color:var(--red)}
  .badge.info{background:rgba(124,106,247,.12);border:1px solid rgba(124,106,247,.4);color:var(--accent)}
  .filters{display:flex;gap:.4rem;flex-wrap:wrap}
  .filters button{background:var(--surface2);border:1px solid var(--border);color:var(--muted);font-size:.72rem;padding:.28rem .7rem;border-radius:100px;cursor:pointer;font-family:'Inter',sans-serif}
  .filters button.on{border-color:rgba(124,106,247,.55);color:var(--accent);background:rgba(124,106,247,.12)}
  .tiles{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:.7rem;margin-bottom:1rem}
  .tile{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:.65rem .85rem}
  .tile .tl{font-family:'DM Mono',monospace;font-size:.6rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
  .tile .tv{font-family:'Sora',sans-serif;font-weight:800;font-size:1.25rem;margin-top:.1rem}
  .burl{font-family:'DM Mono',monospace;font-size:.7rem;color:var(--muted);max-width:230px;word-break:break-all;display:block}
  .btxt{max-width:300px;word-break:break-word}
  .linkbtn{background:none;border:none;color:var(--accent);font-size:.72rem;cursor:pointer;font-family:'Inter',sans-serif;padding:0;margin-top:.25rem}
  .linkbtn:hover{text-decoration:underline}
  .empty{font-size:.8rem;color:var(--muted);padding:.6rem 0;text-align:center}
  .placeholder{font-size:.82rem;color:var(--muted);text-align:center;padding:1.4rem .5rem}

  @media(max-width:960px){.sidebar{display:none}.main{margin-left:0;padding:1.2rem}}
  @media(max-width:760px){.grid2,.checks{grid-template-columns:1fr}.tiles{grid-template-columns:repeat(2,minmax(0,1fr))}}
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
      <a href="#" class="sidebar-link active">📏&nbsp; Meta Tags Length Checker</a>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-label">Navigate</div>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="sidebar-link">🧰&nbsp; All My Tools</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio" class="sidebar-link">👩‍💻&nbsp; Portfolio</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/blog/index.html" class="sidebar-link">📝&nbsp; Blog Posts</a>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-label">Google limits</div>
      <div style="font-size:.72rem;color:var(--muted);line-height:1.75;padding:0 .3rem">
        Title: <b style="color:var(--text)">250 – 580px</b><br>
        <span style="opacity:.8">≈ 30 – 60 characters</span><br>
        Description: <b style="color:var(--text)">400 – 920px</b><br>
        <span style="opacity:.8">≈ 70 – 155 characters</span><br>
        Google cuts by pixel width, not by character count — a “W” is wider than an “i”.
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
      <h1>📏 Meta Tags <span>Length Checker</span></h1>
      <div class="sub">Check your meta title, description and every other meta tag in characters and pixels, the way Google measures them.</div>
    </div>

    <div class="modebar" role="tablist">
      <button type="button" class="mtab active" data-mode="url">🔗 Fetch from URL</button>
      <button type="button" class="mtab" data-mode="manual">✍️ Manual input</button>
      <button type="button" class="clear" id="clearAll">✕ Clear all</button>
    </div>

    <!-- Fetch from URL -->
    <div class="card" id="urlCard">
      <label class="fl" for="url"><span>Page URL</span></label>
      <div class="row">
        <input type="text" id="url" placeholder="https://www.yourwebsite.com/your-page">
        <button class="btn" id="fetchBtn" type="button">Check meta tags</button>
      </div>
      <div class="hint">I’ll read the page’s title, description and all its other meta tags. You can then edit the title and description below to test a better version.</div>
      <div class="msg" id="urlMsg"></div>
    </div>

    <!-- Editor (URL + manual) -->
    <div id="editorArea">
      <div class="card gap">
        <div class="field">
          <label class="fl" for="title"><span>Meta title</span><span class="opt" id="tStat">0 chars · 0px / 580px limit · Score: 0/100</span></label>
          <textarea id="title" rows="2" placeholder="Write your page title here"></textarea>
          <div class="bar"><div class="fill" id="tBar"></div></div>
          <div class="say" id="tSay"></div>
        </div>
        <div class="field">
          <label class="fl" for="desc"><span>Meta description</span><span class="opt" id="dStat">0 chars · 0px / 920px limit · Score: 0/100</span></label>
          <textarea id="desc" rows="3" placeholder="Write your meta description here"></textarea>
          <div class="bar"><div class="fill" id="dBar"></div></div>
          <div class="say" id="dSay"></div>
        </div>
        <div class="grid2">
          <div class="field">
            <label class="fl" for="durl"><span>Display URL</span><span class="opt">for the preview</span></label>
            <input type="text" id="durl" placeholder="www.yourwebsite.com/your-page">
          </div>
          <div class="field">
            <label class="fl" for="kw"><span>Focus keyword</span><span class="opt">optional</span></label>
            <input type="text" id="kw" placeholder="Your main keyword">
          </div>
        </div>
        <div class="btns">
          <button class="btn ghost sm" type="button" id="cpT">⧉ Copy title</button>
          <button class="btn ghost sm" type="button" id="cpD">⧉ Copy description</button>
          <button class="btn ghost sm" type="button" id="cpH">&lt;/&gt; Copy HTML tags</button>
          <span class="msg ok" id="cpMsg" style="margin:0"></span>
        </div>
      </div>

      <div class="card">
        <h2>Google preview
          <span class="right"><span class="tabs"><button type="button" class="on" data-dev="desktop">Desktop</button><button type="button" data-dev="mobile">Mobile</button></span></span>
        </h2>
        <div id="preview"></div>
        <div class="hint" id="prevHint"></div>
      </div>

      <div class="card">
        <div class="score-row">
          <div class="ring" id="ring"></div>
          <div class="score-text">
            <div class="verdict" id="verdict"></div>
            <div class="sub" id="verdictSub"></div>
          </div>
        </div>
        <div class="checks" id="checks"></div>
      </div>

      <div class="card hide" id="allCard">
        <h2>All meta tags on this page
          <span class="right">
            <span class="filters" id="allFilters"><button type="button" class="on" data-f="all">All</button><button type="button" data-f="issues">Issues only</button></span>
            <button class="btn ghost sm" type="button" id="allCsv">⬇ CSV</button>
          </span>
        </h2>
        <div class="hint" id="allMeta" style="margin:-.4rem 0 .8rem"></div>
        <div class="tbl-wrap"><table>
          <thead><tr><th>Tag</th><th>Value found</th><th>Length</th><th>Status</th></tr></thead>
          <tbody id="allBody"></tbody>
        </table></div>
      </div>
    </div>

  </main>
</div>

<script>
// ---------------------------------------------------------------- limits
const T = { min: 250, max: 580, font: '20px Arial', minCh: 30, maxCh: 60 };
const D = { min: 400, max: 920, font: '13px Arial', minCh: 70, maxCh: 155 };
const MOB = { title: 656, desc: 680, tFont: '18px Arial', dFont: '13px Arial' };

const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const trim = s => String(s || '').replace(/\s+/g, ' ').trim();
const cvs = document.createElement('canvas').getContext('2d');
function px(text, font) { cvs.font = font; return Math.round(cvs.measureText(text || '').width); }
function cut(text, font, budget) {
  text = trim(text);
  const w = px(text, font);
  if (w <= budget) return { text, cut: false, shown: text };
  let acc = '';
  for (const word of text.split(' ')) {
    const next = acc ? acc + ' ' + word : word;
    if (px(next + ' ...', font) > budget) break;
    acc = next;
  }
  if (!acc) {
    let i = text.length;
    while (i > 0 && px(text.slice(0, i) + '...', font) > budget) i--;
    return { text: text.slice(0, i) + '...', cut: true, shown: text.slice(0, i) };
  }
  return { text: acc.replace(/[\s,;:|\-–—]+$/, '') + ' ...', cut: true, shown: acc };
}

// Score out of 100 for a length
function lenScore(w, L) {
  if (!w) return 0;
  if (w < L.min) return Math.round(w / L.min * 60);
  const ideal = L.max * 0.78;
  if (w < ideal) return Math.round(60 + (w - L.min) / (ideal - L.min) * 40);
  if (w <= L.max) return 100;
  return Math.max(10, Math.round(100 - (w - L.max) / L.max * 180));
}
function lenState(w, L) { return !w ? 'empty' : w < L.min ? 'short' : w > L.max ? 'long' : 'good'; }
const stateCls = { empty: 'm', short: 'o', long: 'r', good: 'g' };

// ---------------------------------------------------------------- state
const S = { mode: 'url', device: 'desktop', live: null, allFilter: 'all' };

// ---------------------------------------------------------------- keyword helpers
const STOP = new Set('a an the and or of for to in on at by with from is are be your you our best top vs how what why when'.split(' '));
function kwTerms() {
  const kw = trim($('kw').value).toLowerCase();
  if (!kw) return [];
  const words = kw.split(' ').filter(w => w.length > 1 && !STOP.has(w));
  return [...new Set([kw, ...words])].sort((a, b) => b.length - a.length);
}
const reEsc = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
function boldKw(text) {
  const terms = kwTerms();
  if (!terms.length) return esc(text);
  const re = new RegExp('(^|[^\\p{L}\\p{N}])(' + terms.map(reEsc).join('|') + ')(s|es)?(?=$|[^\\p{L}\\p{N}])', 'giu');
  let out = '', last = 0, m;
  while ((m = re.exec(text))) {
    const start = m.index + m[1].length;
    out += esc(text.slice(last, start)) + '<b>' + esc(m[2] + (m[3] || '')) + '</b>';
    last = start + m[2].length + (m[3] || '').length;
    re.lastIndex = last;
  }
  return out + esc(text.slice(last));
}
function findTerm(text, term) {
  const m = new RegExp('(^|[^\\p{L}\\p{N}])' + reEsc(term) + '(s|es)?($|[^\\p{L}\\p{N}])', 'iu').exec(text);
  return m ? m.index + m[1].length : -1;
}

// ---------------------------------------------------------------- length messages
function sayTitle(t) {
  const w = px(t, T.font), st = lenState(w, T);
  if (st === 'empty') return ['m', '–', 'No meta title yet.', 'Without a title Google makes one up from your page — usually not the one you want.'];
  if (st === 'short') return ['o', '!', `Page title is <b>${w}</b> pixel(s) long — too short.`, `Try to expand it to at least ${T.min} pixels (approx. ${T.minCh} characters). Add your main keyword or a clear benefit.`];
  if (st === 'long') {
    const c = cut(t, T.font, T.max), extra = t.length - c.shown.length;
    return ['r', '✕', `Page title is <b>${w}</b> pixels long — too long.`, `Google cuts titles at about ${T.max}px, so roughly the last ${extra} character${extra === 1 ? '' : 's'} will be hidden behind “...”. Shorten it or move the important words to the front.`];
  }
  const room = T.max - w;
  return ['g', '✓', `Page title is <b>${w}</b> pixels long — great length.`, room > 60 ? `It fits within Google’s ${T.max}px limit, with about ${room}px to spare if you want to add more.` : `It fits within Google’s ${T.max}px limit.`];
}
function sayDesc(d) {
  const w = px(d, D.font), st = lenState(w, D);
  if (st === 'empty') return ['m', '–', 'No meta description yet.', 'Google will pick a sentence from your page instead. Write one to control what searchers see.'];
  if (st === 'short') return ['o', '!', `Meta description is <b>${w}</b> pixel(s) long — too short.`, `Try to expand it to at least ${D.min} pixels (approx. ${D.minCh} characters) to use your space in search results.`];
  if (st === 'long') {
    const c = cut(d, D.font, D.max), extra = d.length - c.shown.length;
    return ['r', '✕', `Meta description is <b>${w}</b> pixels long — too long.`, `Google shows about ${D.max}px on desktop, so roughly the last ${extra} character${extra === 1 ? '' : 's'} will be cut. Keep the key message and call-to-action early.`];
  }
  const mob = px(d, MOB.dFont) > MOB.desc;
  return ['g', '✓', `Meta description is <b>${w}</b> pixels long — great length.`, mob ? `Fits on desktop. Mobile shows a bit less (about ${MOB.desc}px), so keep the key point in the first half.` : 'Fits on both desktop and mobile.'];
}
function sayHTML(a) {
  return `<span class="dot bg-${a[0]}">${a[1]}</span><span class="${a[0]}">${a[2]}<span class="more">${a[3]}</span></span>`;
}

// ---------------------------------------------------------------- quick checks
const CTA = /\b(apply|check|compare|get|find|learn|calculate|discover|explore|try|start|save|download|book|shop|buy|read|see|view|sign up|join|call|claim|order|register|subscribe|contact)\b/i;
function quickChecks(t, d) {
  const out = [], add = (s, n, x) => out.push({ s, n, x });
  const terms = kwTerms(), kw = terms[0];
  if (kw) {
    const ti = findTerm(t, kw);
    if (ti < 0) add('fail', 'Keyword in title', `“${kw}” isn’t in the title. Add it so searchers see it matches their search.`);
    else if (ti > t.length / 2) add('warn', 'Keyword placement', 'The keyword is in the second half of the title. Moving it towards the front usually helps.');
    else add('pass', 'Keyword in title', 'Your keyword appears early in the title.');
    if (!d) add('info', 'Keyword in description', 'Add a description to check this.');
    else if (findTerm(d, kw) < 0) add('warn', 'Keyword in description', 'Not found. Google bolds matching words in the description, which catches the eye.');
    else add('pass', 'Keyword in description', 'Found — Google will bold it in the result.');
  } else {
    add('info', 'Focus keyword', 'Add a focus keyword above to check where it appears.');
  }
  if (t) {
    const letters = t.replace(/[^A-Za-z]/g, '');
    if (letters.length > 8 && letters === letters.toUpperCase()) add('warn', 'Title case', 'The title is ALL CAPS — this looks spammy and Google may rewrite it.');
    else add('pass', 'Title case', 'Not written in ALL CAPS.');
    const words = (t.toLowerCase().match(/[\p{L}\p{N}]+/gu) || []).filter(w => w.length > 2 && !STOP.has(w));
    const cnt = {}; words.forEach(w => cnt[w] = (cnt[w] || 0) + 1);
    const rep = Object.keys(cnt).filter(w => cnt[w] >= 3);
    if (rep.length) add('warn', 'Repeated words', `“${rep[0]}” is used ${cnt[rep[0]]} times. Repeating words looks like keyword stuffing.`);
    else add('pass', 'Repeated words', 'No word is repeated too often.');
    const seps = (t.match(/[|•·»]/g) || []).length + (t.match(/\s[-–—]\s/g) || []).length;
    if (seps >= 3) add('warn', 'Separators', `${seps} separators (| - •). Too many pieces can make Google rewrite your title.`);
  }
  if (t && d && trim(t).toLowerCase() === trim(d).toLowerCase()) add('fail', 'Title vs description', 'The title and description are the same. Use the description to add new information.');
  if (d) {
    if (d.includes('"')) add('warn', 'Double quotes', 'Double quotes (") can break the HTML tag and cut your description short. Use single quotes instead.');
    if (CTA.test(d)) add('pass', 'Call to action', 'The description invites a click.');
    else add('info', 'Call to action', 'Consider adding an action word like “Learn”, “Get” or “Discover”.');
  }
  return out;
}

// ---------------------------------------------------------------- Google preview
function parseUrl(u) {
  u = trim(u);
  if (!u) return null;
  if (!/^https?:\/\//i.test(u)) u = 'https://' + u;
  try { return new URL(u); } catch (e) { return null; }
}
function crumbs(u) {
  const p = parseUrl(u);
  if (!p) return { host: 'yourwebsite.com', line: 'https://www.yourwebsite.com' };
  const segs = p.pathname.split('/').filter(Boolean).map(s => { try { return decodeURIComponent(s); } catch (e) { return s; } }).map(s => s.replace(/\.(html?|php|aspx?)$/i, ''));
  let line = p.origin;
  if (segs.length) line += ' › ' + segs.slice(0, 3).join(' › ') + (segs.length > 3 ? ' › …' : '');
  return { host: p.hostname.replace(/^www\./, ''), line };
}
function siteName(u) {
  if (S.live && S.live.og && S.live.og.site_name) return S.live.og.site_name;
  const base = crumbs(u).host.split('.')[0] || 'Your website';
  return base.charAt(0).toUpperCase() + base.slice(1);
}
function renderPreview() {
  const t = trim($('title').value), d = trim($('desc').value), u = $('durl').value;
  const dev = S.device;
  const tc = cut(t, dev === 'desktop' ? T.font : MOB.tFont, dev === 'desktop' ? T.max : MOB.title);
  const dc = cut(d, D.font, dev === 'desktop' ? D.max : MOB.desc);
  const c = crumbs(u), name = siteName(u);
  const favOk = S.live && S.live.favicon && parseUrl(u) && S.live.final_url && parseUrl(u).hostname === new URL(S.live.final_url).hostname;
  const fav = favOk ? `<img src="${esc(S.live.favicon)}" alt="" onerror="this.parentNode.textContent='${esc(name.charAt(0).toUpperCase())}'">` : esc(name.charAt(0).toUpperCase());
  $('preview').innerHTML = `<div class="serp-wrap ${dev}"><div class="serp ${dev}">
      <div class="s-site"><span class="fav">${fav}</span><div style="min-width:0"><div class="s-name">${esc(name)}</div><div class="s-url">${esc(c.line)}</div></div></div>
      <div class="s-title">${tc.text ? esc(tc.text) : '<span class="s-empty">Your title will appear here</span>'}</div>
      <div class="s-desc">${dc.text ? boldKw(dc.text) : '<span class="s-empty">Your meta description will appear here</span>'}</div>
    </div></div>`;
  const bits = [];
  if (tc.cut) bits.push('the title is cut off');
  if (dc.cut) bits.push('the description is cut off');
  $('prevHint').textContent = (t || d)
    ? (bits.length ? `On ${dev}, ${bits.join(' and ')} (shown with “...”).` : `Everything fits on ${dev}.`) + ' Google may still choose to show different text.'
    : '';
  fitDesktop();
}
function fitDesktop() {
  const w = document.querySelector('.serp-wrap.desktop');
  if (!w) return;
  const s = w.querySelector('.serp');
  s.style.transform = ''; w.style.height = '';
  const avail = w.clientWidth - 2 * 19;
  if (avail > 0 && avail < 600) {
    const k = avail / 600;
    s.style.transform = `scale(${k})`;
    w.style.height = (s.offsetHeight * k + 2 * 18) + 'px';
  }
}
window.addEventListener('resize', fitDesktop);

// ---------------------------------------------------------------- editor render
function ringHTML(score, empty) {
  const r = 40, c = 2 * Math.PI * r, off = c * (1 - score / 100);
  const col = empty ? 'var(--border)' : score >= 80 ? 'var(--mint)' : score >= 50 ? 'var(--orange)' : 'var(--red)';
  return `<svg width="96" height="96" viewBox="0 0 96 96"><circle cx="48" cy="48" r="${r}" fill="none" stroke="var(--surface2)" stroke-width="8"/>
    <circle cx="48" cy="48" r="${r}" fill="none" stroke="${col}" stroke-width="8" stroke-linecap="round" stroke-dasharray="${c}" stroke-dashoffset="${empty ? c : off}" style="transition:stroke-dashoffset .3s"/></svg>
    <div class="num" style="color:${empty ? 'var(--muted)' : col}">${empty ? '–' : score}<small>/ 100</small></div>`;
}
function fillBar(el, w, L) {
  const st = lenState(w, L);
  el.style.width = Math.min(100, w / L.max * 100) + '%';
  el.style.background = { empty: 'var(--border)', short: 'var(--orange)', long: 'var(--red)', good: 'var(--mint)' }[st];
}
function renderEditor() {
  const t = trim($('title').value), d = trim($('desc').value);
  const tw = px(t, T.font), dw = px(d, D.font);
  const ts = lenScore(tw, T), ds = lenScore(dw, D);
  const tst = lenState(tw, T), dst = lenState(dw, D);
  $('tStat').innerHTML = `<span class="${stateCls[tst]}">${t.length} chars · ${tw}px / ${T.max}px limit · Score: ${ts}/100</span>`;
  $('dStat').innerHTML = `<span class="${stateCls[dst]}">${d.length} chars · ${dw}px / ${D.max}px limit · Score: ${ds}/100</span>`;
  fillBar($('tBar'), tw, T); fillBar($('dBar'), dw, D);
  $('tSay').innerHTML = sayHTML(sayTitle(t));
  $('dSay').innerHTML = sayHTML(sayDesc(d));

  const qc = (t || d) ? quickChecks(t, d) : [];
  const scored = qc.filter(c => c.s !== 'info');
  const qScore = scored.length ? Math.round(scored.reduce((a, c) => a + (c.s === 'pass' ? 1 : c.s === 'warn' ? .5 : 0), 0) / scored.length * 100) : 100;
  const empty = !t && !d;
  const score = Math.round(ts * .4 + ds * .4 + qScore * .2);
  $('ring').innerHTML = ringHTML(score, empty);
  if (empty) {
    $('verdict').textContent = 'Your score will appear here';
    $('verdictSub').textContent = S.mode === 'url' ? 'Enter a URL above, or switch to Manual input and type a title and description.' : 'Type a title and description to see how they score.';
  } else {
    $('verdict').textContent = score >= 85 ? 'Great — your snippet is ready for Google' : score >= 60 ? 'Good start — a few fixes will help' : 'Needs work — fix the red items first';
    $('verdictSub').textContent = `Title ${ts}/100 · Description ${ds}/100 · Checks ${qScore}/100.`;
  }
  $('checks').innerHTML = qc.map(c => `<div class="chk"><span class="ic ${c.s}">${{pass:'✓',warn:'!',fail:'✕',info:'i'}[c.s]}</span><div><div class="ct">${esc(c.n)}</div><div class="cd">${esc(c.x)}</div></div></div>`).join('');
  renderPreview();
}

// ---------------------------------------------------------------- all meta tags (URL mode)
const isNoindex = s => /(^|[\s,])(noindex|none)(?=$|[\s,])/.test(String(s || '').toLowerCase());
function robotsDirectives(d) {
  return [d.robots, d.googlebot, d.x_robots].filter(Boolean).join(', ').toLowerCase();
}
function buildAllRows(d) {
  const rows = [], add = (group, tag, value, len, status, tip) => rows.push({ group, tag, value, len, status, tip });
  const t = d.title || '', ds = d.description || '';
  const tw = px(t, T.font), dw = px(ds, D.font);
  const tS = lenState(tw, T), dS = lenState(dw, D);
  const lenMap = { empty: 'fail', short: 'warn', long: 'fail', good: 'pass' };

  // Basics
  add('Search result', '<title>', t, t ? `${t.length} ch · ${tw}px` : '', tS === 'long' ? 'warn' : lenMap[tS],
      !t ? 'Missing — add a unique title for this page.' : tS === 'short' ? `Under ${T.min}px — make it more descriptive.` : tS === 'long' ? `Over ${T.max}px — will be cut off in Google.` : 'Good length.');
  if (d.title_count > 1) add('Search result', '<title> count', `${d.title_count} title tags found`, '', 'warn', 'Keep only one <title> tag in the <head>.');
  add('Search result', 'meta description', ds, ds ? `${ds.length} ch · ${dw}px` : '', !ds ? 'warn' : dS === 'good' ? 'pass' : 'warn',
      !ds ? 'Missing — Google will pick text from your page.' : dS === 'short' ? `Under ${D.min}px — use more of your space.` : dS === 'long' ? `Over ${D.max}px — will be cut off in Google.` : 'Good length.');
  if (d.description_count > 1) add('Search result', 'description count', `${d.description_count} description tags found`, '', 'warn', 'Keep only one meta description.');
  const h1 = d.h1 || '';
  add('Search result', '<h1>', h1, h1 ? `${h1.length} ch` : '', !h1 ? 'warn' : h1.length > 70 ? 'info' : 'pass',
      !h1 ? 'No H1 heading found.' : (d.h1_count > 1 ? `${d.h1_count} H1s on the page — one is usually clearer. ` : '') + (h1.length > 70 ? 'Quite long for a heading.' : (t && trim(h1).toLowerCase() === trim(t).toLowerCase() ? 'Same as the title — fine.' : 'Found.')));
  if (d.keywords) add('Search result', 'meta keywords', d.keywords, `${d.keywords.length} ch`, 'info', 'Google ignores meta keywords — it does no harm, but it doesn’t help either.');

  // Indexing
  const dir = robotsDirectives(d);
    const nosnip = /\bnosnippet\b/.test(dir);
  const ms = dir.match(/max-snippet\s*:\s*(-?\d+)/);
  add('Indexing', 'meta robots', d.robots, '', isNoindex(d.robots) ? 'fail' : 'pass',
      !d.robots ? 'Not set — Google will index and follow by default.' : isNoindex(d.robots) ? 'noindex — this page will NOT appear in Google.' : 'Page can be indexed.');
  if (d.googlebot) add('Indexing', 'meta googlebot', d.googlebot, '', isNoindex(d.googlebot) ? 'fail' : 'pass', 'Googlebot-only rules.');
  if (d.x_robots) add('Indexing', 'X-Robots-Tag (header)', d.x_robots, '', isNoindex(d.x_robots) ? 'fail' : 'pass', isNoindex(d.x_robots) ? 'noindex in the HTTP header — this page will NOT appear in Google.' : 'Sent in the HTTP header — applies together with meta robots.');
  if (nosnip) add('Indexing', 'nosnippet', 'nosnippet', '', 'warn', 'Google won’t show any description text for this page.');
  if (ms) {
    const n = parseInt(ms[1], 10);
    const over = n >= 0 && ds.length > n;
    add('Indexing', 'max-snippet', `max-snippet:${n}`, '', n === 0 ? 'warn' : over ? 'warn' : 'pass',
        n === -1 ? 'No limit on snippet length.' : n === 0 ? 'Same as nosnippet — no description text shown.' : over ? `Google can show at most ${n} characters, but your description is ${ds.length}.` : `Google may show up to ${n} characters.`);
  }
  let canTip = 'Missing — add a canonical to tell Google the main version of this page.', canSt = 'warn';
  if (d.canonical) {
    const same = d.canonical.replace(/\/$/, '').toLowerCase() === (d.final_url || '').replace(/\/$/, '').toLowerCase();
    canSt = same ? 'pass' : 'info';
    canTip = same ? 'Points to this page (self-canonical).' : 'Points to a different URL — Google may show that page instead.';
  }
  add('Indexing', 'canonical', d.canonical, '', canSt, canTip);

  // Social
  const og = d.og || {}, tw2 = d.twitter || {};
  const ogt = og.title || '', ogd = og.description || '';
  add('Social sharing', 'og:title', ogt, ogt ? `${ogt.length} ch` : '', !ogt ? 'warn' : ogt.length > 90 ? 'warn' : 'pass',
      !ogt ? 'Missing — Facebook/LinkedIn will guess a title.' : ogt.length > 90 ? 'Over 90 characters — may be cut on Facebook/LinkedIn.' : ogt.length > 60 ? 'OK — up to about 60 characters looks best.' : 'Good length.');
  add('Social sharing', 'og:description', ogd, ogd ? `${ogd.length} ch` : '', !ogd ? 'warn' : ogd.length > 200 ? 'warn' : 'pass',
      !ogd ? 'Missing — add one for better-looking shares.' : ogd.length > 200 ? 'Over 200 characters — will be cut on most platforms.' : 'Good length (keep the key message in the first ~110 characters).');
  add('Social sharing', 'og:image', og.image, '', og.image ? 'pass' : 'warn', og.image ? 'Found — 1200×630px works best.' : 'Missing — shares will have no image or a random one.');
  add('Social sharing', 'og:url', og.url, '', og.url ? 'pass' : 'info', og.url ? 'Found.' : 'Optional — helps platforms group shares of the same page.');
  add('Social sharing', 'og:type', og.type, '', og.type ? 'pass' : 'info', og.type ? 'Found.' : 'Optional — usually “website” or “article”.');
  add('Social sharing', 'twitter:card', tw2.card, '', tw2.card ? 'pass' : 'info', tw2.card ? 'Found.' : 'Not set — X will fall back to your Open Graph tags.');
  const twt = tw2.title || '', twd = tw2.description || '';
  if (twt || tw2.card) add('Social sharing', 'twitter:title', twt, twt ? `${twt.length} ch` : '', !twt ? 'info' : twt.length > 70 ? 'warn' : 'pass', !twt ? 'Not set — og:title will be used.' : twt.length > 70 ? 'Over 70 characters — may be cut on X.' : 'Good length.');
  if (twd || tw2.card) add('Social sharing', 'twitter:description', twd, twd ? `${twd.length} ch` : '', !twd ? 'info' : twd.length > 200 ? 'warn' : 'pass', !twd ? 'Not set — og:description will be used.' : twd.length > 200 ? 'Over 200 characters — will be cut.' : 'Good length.');

  // Technical
  add('Technical', 'viewport', d.viewport, '', d.viewport ? (/width\s*=\s*device-width/i.test(d.viewport) ? 'pass' : 'warn') : 'fail',
      !d.viewport ? 'Missing — the page may not display properly on phones.' : /width\s*=\s*device-width/i.test(d.viewport) ? 'Mobile-friendly setting.' : 'Should include width=device-width.');
  add('Technical', 'charset', d.charset, '', d.charset ? 'pass' : 'info', d.charset ? 'Found.' : 'Not declared — add <meta charset="UTF-8">.');
  add('Technical', 'html lang', d.lang, '', d.lang ? 'pass' : 'warn', d.lang ? 'Language declared.' : 'Missing — add lang="en" (or your language) to the <html> tag.');
  return rows;
}
function renderAll() {
  const d = S.live;
  if (!d) { $('allCard').classList.add('hide'); return; }
  $('allCard').classList.remove('hide');
  const rows = buildAllRows(d);
  S.allRows = rows;
  const issues = rows.filter(r => r.status === 'warn' || r.status === 'fail').length;
  $('allMeta').innerHTML = `${esc(d.final_url)} · HTTP ${d.status}${d.redirected ? ' · redirected from ' + esc(d.url) : ''} · <span class="${issues ? 'o' : 'g'}">${issues} issue${issues === 1 ? '' : 's'} found</span>`;
  const show = S.allFilter === 'all' ? rows : rows.filter(r => r.status === 'warn' || r.status === 'fail');
  let html = '', grp = '';
  const lbl = { pass: 'Good', warn: 'Check', fail: 'Fix', info: 'Info' };
  show.forEach(r => {
    if (r.group !== grp) { grp = r.group; html += `<tr class="grp"><td colspan="4">${esc(grp)}</td></tr>`; }
    html += `<tr><td class="tag">${esc(r.tag)}</td>
      <td><div class="val${r.value ? '' : ' none'}">${r.value ? esc(r.value) : 'not found'}</div><div class="tip">${esc(r.tip)}</div></td>
      <td class="len">${esc(r.len || '—')}</td>
      <td><span class="badge ${r.status}">${lbl[r.status]}</span></td></tr>`;
  });
  $('allBody').innerHTML = html || '<tr><td colspan="4" class="empty">No issues — every tag looks good.</td></tr>';
}

// ---------------------------------------------------------------- fetch single
async function fetchOne() {
  const url = trim($('url').value);
  if (!url) return msg('urlMsg', 'err', 'Please enter a page URL.');
  $('fetchBtn').disabled = true;
  $('fetchBtn').innerHTML = '<span class="spin"></span> Checking…';
  msg('urlMsg', 'wait', 'Reading the page…');
  try {
    const r = await fetch('/api/fetch?url=' + encodeURIComponent(url));
    const d = await r.json();
    if (!d.ok) { S.live = null; renderAll(); return msg('urlMsg', 'err', d.error || 'Could not read this page.'); }
    S.live = d;
    $('title').value = d.title || '';
    $('desc').value = d.description || '';
    $('durl').value = d.final_url || url;
    msg('urlMsg', 'ok', '✓ Page read. Edit the title or description below to try a better version — the numbers update as you type.');
    renderEditor(); renderAll();
  } catch (e) {
    msg('urlMsg', 'err', 'The check didn’t finish. Please try again (the first run can take a few seconds while the tool wakes up).');
  } finally {
    $('fetchBtn').disabled = false;
    $('fetchBtn').textContent = 'Check meta tags';
  }
}
function msg(id, cls, text) { $(id).className = 'msg ' + cls; $(id).textContent = text; }

// ---------------------------------------------------------------- CSV
function downloadCsv(name, rows) {
  const q = v => '"' + String(v ?? '').replace(/"/g, '""') + '"';
  const blob = new Blob(['﻿' + rows.map(r => r.map(q).join(',')).join('\n')], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = name; a.click();
}
$('allCsv').addEventListener('click', () => {
  if (!S.allRows) return;
  const lbl = { pass: 'Good', warn: 'Check', fail: 'Fix', info: 'Info' };
  downloadCsv('meta-tags-report.csv', [['Page', S.live.final_url], [], ['Group', 'Tag', 'Value', 'Length', 'Status', 'Tip'],
    ...S.allRows.map(r => [r.group, r.tag, r.value || '', r.len || '', lbl[r.status], r.tip])]);
});
// ---------------------------------------------------------------- copy
function flash(t) { $('cpMsg').textContent = t; setTimeout(() => $('cpMsg').textContent = '', 2000); }
async function copyText(t) {
  try { await navigator.clipboard.writeText(t); }
  catch (e) { const a = document.createElement('textarea'); a.value = t; document.body.appendChild(a); a.select(); document.execCommand('copy'); a.remove(); }
}
const attr = s => trim(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
$('cpT').addEventListener('click', () => { copyText(trim($('title').value)); flash('Title copied'); });
$('cpD').addEventListener('click', () => { copyText(trim($('desc').value)); flash('Description copied'); });
$('cpH').addEventListener('click', () => {
  copyText(`<title>${attr($('title').value)}</title>\n<meta name="description" content="${attr($('desc').value)}">`);
  flash('HTML tags copied');
});

// ---------------------------------------------------------------- modes + events
function setMode(m) {
  S.mode = m;
  document.querySelectorAll('.mtab').forEach(b => b.classList.toggle('active', b.dataset.mode === m));
  $('urlCard').classList.toggle('hide', m !== 'url');
  if (m === 'manual') { $('allCard').classList.add('hide'); }
  else if (m === 'url') renderAll();
  renderEditor();
}
document.querySelectorAll('.mtab').forEach(b => b.addEventListener('click', () => setMode(b.dataset.mode)));
document.querySelectorAll('[data-dev]').forEach(b => b.addEventListener('click', () => {
  S.device = b.dataset.dev;
  document.querySelectorAll('[data-dev]').forEach(x => x.classList.toggle('on', x === b));
  renderPreview();
}));
document.querySelectorAll('#allFilters button').forEach(b => b.addEventListener('click', () => {
  S.allFilter = b.dataset.f;
  document.querySelectorAll('#allFilters button').forEach(x => x.classList.toggle('on', x === b));
  renderAll();
}));
['title', 'desc', 'kw'].forEach(id => $(id).addEventListener('input', renderEditor));
$('durl').addEventListener('input', renderPreview);
$('fetchBtn').addEventListener('click', fetchOne);
$('url').addEventListener('keydown', e => { if (e.key === 'Enter') fetchOne(); });
$('clearAll').addEventListener('click', () => {
  ['url', 'title', 'desc', 'durl', 'kw'].forEach(id => $(id).value = '');
  S.live = null; S.allRows = null;
  msg('urlMsg', '', '');
  renderAll(); renderEditor();
});

// wait for fonts so pixel widths are right, then draw the empty state
(document.fonts && document.fonts.ready ? document.fonts.ready : Promise.resolve()).then(renderEditor);
renderEditor();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(debug=True, port=5000)
