"""
SERP Snippet Preview  -  Flask app
See how a page will look in Google search results before you publish.

  * Live desktop + mobile Google-style preview that updates as you type
  * Pixel-width meters (title / description) with the exact cut-off point
  * Focus keyword bolding in the description, like Google does
  * Snippet health score out of 100 + a plain-English fix list
  * "Rewrite risk" flag - how likely Google is to rewrite your title
  * A/B compare: two versions side by side, each with its own score
  * Fetch the live title + description from any URL, then edit a copy
  * Copy the finished <title> + meta description tags, or download a CSV report

Almost everything runs in the browser. The server only does one thing:
  /api/fetch  - read an existing page's title, meta description and a few extras

Built by Mahalakshmi Marimuthu - Digital Marketing Strategist & AI-Powered SEO Expert
"""
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


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def meta_content(soup, **attrs):
    tag = soup.find("meta", attrs=attrs)
    return clean(tag.get("content")) if tag else ""


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
                                      "(it may be blocking automated requests). "
                                      "You can still type the title and description in yourself."}

    enc = r.encoding or r.apparent_encoding or "utf-8"
    if enc.lower() == "iso-8859-1" and r.apparent_encoding:
        enc = r.apparent_encoding
    html = raw.decode(enc, errors="replace")
    final_url = r.url
    soup = BeautifulSoup(html, "html.parser")
    head = soup.head or soup

    titles = [clean(t.get_text()) for t in soup.find_all("title")
              if not t.find_parent("svg")]
    descs = [clean(m.get("content")) for m in soup.find_all(
        "meta", attrs={"name": re.compile(r"^description$", re.I)})]

    canonical = ""
    ctag = head.find("link", rel=lambda v: v and "canonical" in (
        " ".join(v) if isinstance(v, list) else v).lower())
    if ctag and ctag.get("href"):
        canonical = urljoin(final_url, ctag["href"].strip())

    favicon = ""
    for want in ("icon", "shortcut icon", "apple-touch-icon"):
        tag = head.find("link", rel=lambda v, w=want: v and w in (
            " ".join(v) if isinstance(v, list) else v).lower())
        if tag and tag.get("href"):
            favicon = urljoin(final_url, tag["href"].strip())
            break
    if not favicon:
        p = urlparse(final_url)
        favicon = f"{p.scheme}://{p.netloc}/favicon.ico"

    robots = meta_content(soup, name=re.compile(r"^(robots|googlebot)$", re.I)).lower()
    x_robots = (r.headers.get("X-Robots-Tag") or "").lower()
    h1 = soup.find("h1")
    html_tag = soup.find("html")

    return {
        "ok": True,
        "final_url": final_url,
        "canonical": canonical,
        "title": titles[0] if titles else "",
        "title_count": len(titles),
        "description": descs[0] if descs else "",
        "description_count": len(descs),
        "og_title": meta_content(soup, property=re.compile(r"^og:title$", re.I)),
        "og_description": meta_content(soup, property=re.compile(r"^og:description$", re.I)),
        "site_name": meta_content(soup, property=re.compile(r"^og:site_name$", re.I)),
        "h1": clean(h1.get_text(" ")) [:200] if h1 else "",
        "favicon": favicon,
        "noindex": "noindex" in robots or "noindex" in x_robots,
        "nosnippet": "nosnippet" in robots or "nosnippet" in x_robots,
        "lang": (html_tag.get("lang") or "").strip() if html_tag else "",
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
<title>SERP Snippet Preview – See Your Google Result Before You Publish</title>
<meta name="description" content="Free SERP Snippet Preview: see your title and meta description the way Google shows them on desktop and mobile, with pixel meters, keyword bolding, a snippet score and A/B compare.">
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
  .urlbox{display:none;margin-bottom:1.2rem}
  .urlbox.show{display:block}
  .row-inline{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center}
  .row-inline input{flex:1;min-width:220px}

  label.fl{display:flex;justify-content:space-between;gap:.5rem;font-size:.76rem;color:var(--muted);margin-bottom:.35rem}
  label.fl .opt{font-family:'DM Mono',monospace;font-size:.64rem;opacity:.8}
  input[type=text],textarea{width:100%;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:.6rem .85rem;font-family:'Inter',sans-serif;font-size:.86rem}
  textarea{min-height:84px;resize:vertical;line-height:1.5}
  input:focus,textarea:focus{outline:none;border-color:var(--accent)}
  .field{margin-bottom:.95rem}
  .fgrid{display:grid;grid-template-columns:1fr 1fr;gap:0 .8rem}
  .hint{font-size:.7rem;color:var(--muted);margin-top:.3rem;line-height:1.5}

  .meter{margin-top:.45rem}
  .meter .bar{height:6px;border-radius:6px;background:var(--surface2);border:1px solid var(--border);overflow:hidden;position:relative}
  .meter .fill{height:100%;border-radius:6px;transition:width .15s,background .15s}
  .meter .info{display:flex;justify-content:space-between;font-family:'DM Mono',monospace;font-size:.66rem;color:var(--muted);margin-top:.3rem;gap:.5rem;flex-wrap:wrap}
  .meter .info b{font-weight:500}
  .g{color:var(--mint)} .o{color:var(--orange)} .r{color:var(--red)}

  .tabs{display:inline-flex;background:var(--surface2);border:1px solid var(--border);border-radius:9px;padding:3px;gap:3px}
  .tabs button{background:none;border:1px solid transparent;color:var(--muted);font-family:'Inter',sans-serif;font-size:.76rem;font-weight:600;padding:.32rem .8rem;border-radius:6px;cursor:pointer}
  .tabs button.on{background:rgba(124,106,247,.14);border-color:rgba(124,106,247,.4);color:var(--accent)}
  .tabs button:disabled{opacity:.4;cursor:not-allowed}

  .btn{display:inline-flex;align-items:center;gap:.5rem;background:var(--accent);color:#fff;border:none;border-radius:8px;padding:.62rem 1.2rem;font-size:.84rem;font-weight:600;cursor:pointer;font-family:'Inter',sans-serif;white-space:nowrap}
  .btn:hover{background:var(--accent-h)}
  .btn.ghost{background:var(--surface2);border:1px solid var(--border);color:var(--text)}
  .btn.ghost:hover{border-color:var(--accent)}
  .btn.mint{background:rgba(106,247,200,.12);border:1px solid rgba(106,247,200,.4);color:var(--mint)}
  .btn.mint:hover{background:rgba(106,247,200,.2)}
  .btn.sm{padding:.4rem .8rem;font-size:.76rem}
  .btn:disabled{opacity:.45;cursor:not-allowed}
  .msg{font-size:.78rem;margin-top:.6rem}
  .msg.err{color:var(--red)} .msg.ok{color:var(--mint)}
  .actions{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;margin-top:.4rem}

  .layout{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.15fr);gap:1.2rem;align-items:start}
  .sticky{position:sticky;top:1rem}

  .live{margin-top:.9rem;font-size:.76rem;color:var(--muted);line-height:1.8;display:none}
  .live.show{display:block}
  .live .warn{color:var(--orange)} .live .bad{color:var(--red)}

  /* Google-style result cards */
  .serp-wrap{background:#fff;border-radius:10px;padding:1.1rem 1.2rem;overflow:hidden}
  .serp-wrap.dark{background:#1f1f1f}
  .serp-wrap.mobile{display:flex;justify-content:center;background:#f1f3f4}
  .serp-wrap.mobile.dark{background:#101010}
  .serp{font-family:Arial,sans-serif;text-align:left}
  .serp.desktop{width:600px;max-width:none;transform-origin:top left}
  .serp.desktop .s-title{overflow:hidden}
  .serp.mobile{width:360px;background:#fff;border-radius:14px;padding:14px 16px;box-shadow:0 1px 3px rgba(0,0,0,.12)}
  .dark .serp.mobile{background:#1f1f1f;box-shadow:none;border:1px solid #3c4043}
  .s-site{display:flex;align-items:center;gap:10px;margin-bottom:6px}
  .fav{width:26px;height:26px;border-radius:50%;background:#f1f3f4;border:1px solid #dadce0;display:flex;align-items:center;justify-content:center;overflow:hidden;flex-shrink:0;font-size:12px;font-weight:700;color:#5f6368}
  .dark .fav{background:#303134;border-color:#5f6368;color:#bdc1c6}
  .fav img{width:18px;height:18px}
  .s-name{font-size:14px;color:#202124;line-height:20px}
  .s-url{font-size:12px;color:#4d5156;line-height:18px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:520px}
  .serp.mobile .s-url{max-width:280px}
  .dark .s-name{color:#dadce0} .dark .s-url{color:#bdc1c6}
  .s-title{font-size:20px;line-height:26px;color:#1a0dab;margin-bottom:3px;word-wrap:break-word}
  .serp.desktop .s-title{white-space:nowrap}
  .serp.mobile .s-title{font-size:18px;line-height:24px}
  .dark .s-title{color:#99c3ff}
  .s-title:hover{text-decoration:underline;cursor:pointer}
  .s-desc{font-size:14px;line-height:22px;color:#4d5156;word-wrap:break-word}
  .s-desc b{color:#202124;font-weight:700}
  .dark .s-desc{color:#bdc1c6} .dark .s-desc b{color:#e8eaed}
  .s-empty{color:#9aa0a6;font-style:italic}
  .prev-head{display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;margin:1rem 0 .5rem;font-family:'DM Mono',monospace;font-size:.68rem;color:var(--muted);letter-spacing:.08em;text-transform:uppercase}
  .prev-head:first-child{margin-top:0}
  .prev-head .mini{margin-left:auto;text-transform:none;letter-spacing:0}
  .winner{font-family:'DM Mono',monospace;font-size:.62rem;padding:.12rem .5rem;border-radius:100px;background:rgba(106,247,200,.1);border:1px solid rgba(106,247,200,.4);color:var(--mint)}

  /* score */
  .score-row{display:flex;gap:1.2rem;align-items:center;margin-bottom:1rem;flex-wrap:wrap}
  .ring{position:relative;width:96px;height:96px;flex-shrink:0}
  .ring svg{transform:rotate(-90deg)}
  .ring .num{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;font-family:'Sora',sans-serif;font-weight:800;font-size:1.5rem;line-height:1}
  .ring .num small{font-family:'DM Mono',monospace;font-size:.58rem;color:var(--muted);font-weight:400;margin-top:.25rem}
  .score-text{flex:1;min-width:200px}
  .score-text .verdict{font-family:'Sora',sans-serif;font-weight:700;font-size:.95rem;margin-bottom:.3rem}
  .score-text .sub{font-size:.78rem;color:var(--muted);line-height:1.55}
  .pills{display:flex;gap:.4rem;flex-wrap:wrap;margin-top:.55rem}
  .badge{font-family:'DM Mono',monospace;font-size:.64rem;padding:.2rem .6rem;border-radius:100px;white-space:nowrap;display:inline-block}
  .badge.pass{background:rgba(106,247,200,.1);border:1px solid rgba(106,247,200,.35);color:var(--mint)}
  .badge.warn{background:rgba(247,162,106,.1);border:1px solid rgba(247,162,106,.35);color:var(--orange)}
  .badge.fail{background:rgba(247,106,106,.1);border:1px solid rgba(247,106,106,.35);color:var(--red)}
  .badge.info{background:rgba(124,106,247,.12);border:1px solid rgba(124,106,247,.4);color:var(--accent)}

  .filters{display:flex;gap:.4rem;flex-wrap:wrap;margin-bottom:.7rem}
  .filters button{background:var(--surface2);border:1px solid var(--border);color:var(--muted);font-size:.72rem;padding:.28rem .7rem;border-radius:100px;cursor:pointer;font-family:'Inter',sans-serif}
  .filters button.on{border-color:rgba(124,106,247,.5);color:var(--accent);background:rgba(124,106,247,.1)}
  .checks{display:flex;flex-direction:column;gap:.45rem}
  .chk{display:flex;gap:.7rem;align-items:flex-start;background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:.65rem .8rem}
  .chk .ic{width:22px;height:22px;border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:.72rem;font-weight:700;flex-shrink:0;margin-top:.05rem}
  .chk.pass .ic{background:rgba(106,247,200,.12);color:var(--mint)}
  .chk.warn .ic{background:rgba(247,162,106,.12);color:var(--orange)}
  .chk.fail .ic{background:rgba(247,106,106,.12);color:var(--red)}
  .chk .ct{font-size:.82rem;font-weight:600}
  .chk .cd{font-size:.74rem;color:var(--muted);line-height:1.5;margin-top:.1rem}
  .chk .badge{margin-left:auto;flex-shrink:0}
  .foot-hint{font-size:.72rem;color:var(--muted);margin-top:.8rem;line-height:1.55}

  @media(max-width:1180px){.layout{grid-template-columns:minmax(0,1fr)}.sticky{position:static}}
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
      <a href="#" class="sidebar-link active">🔎&nbsp; SERP Snippet Preview</a>
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
        Google measures length in <b style="color:var(--text)">pixels</b>, not characters.<br>
        Desktop title ≈ 600px · description ≈ 920px.<br>
        Google may still rewrite your snippet for some searches — this preview is a close estimate.
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
      <h1>🔎 SERP Snippet <span>Preview</span></h1>
      <div class="sub">See your page the way Google shows it — then fix it before you publish.</div>
    </div>

    <div class="modes" id="modes" role="tablist">
      <button type="button" class="mode active" data-mode="url" role="tab">
        <span class="mi">🔗</span>
        <span><span class="mt">Fetch from a URL</span><span class="md">Pull a live page's snippet, then edit a copy</span></span>
      </button>
      <button type="button" class="mode" data-mode="manual" role="tab">
        <span class="mi">✍️</span>
        <span><span class="mt">Type it in</span><span class="md">Write or paste a title and description</span></span>
      </button>
    </div>

    <div class="card urlbox show" id="urlBox">
      <h2><span class="step">⚡</span>Paste the page URL</h2>
      <div class="row-inline">
        <input type="text" id="fetchUrl" placeholder="https://www.yourwebsite.com/your-page">
        <button class="btn" id="fetchBtn" type="button">Fetch snippet</button>
      </div>
      <div class="msg" id="fetchMsg"></div>
      <div class="live" id="liveInfo"></div>
    </div>

    <div class="layout" style="margin-top:1.2rem">
      <div>
        <div class="card">
          <h2><span class="step">1</span>Your snippet
            <span class="right tabs" id="verTabs">
              <button type="button" data-v="A" class="on">Version A</button>
              <button type="button" data-v="B">Version B</button>
            </span>
          </h2>
          <div class="field">
            <label class="fl" for="fTitle"><span>Title tag</span></label>
            <input type="text" id="fTitle" placeholder="Write your page title here">
            <div class="meter" id="mTitle"></div>
          </div>
          <div class="field">
            <label class="fl" for="fDesc"><span>Meta description</span></label>
            <textarea id="fDesc" placeholder="Write your meta description here"></textarea>
            <div class="meter" id="mDesc"></div>
          </div>
          <div class="field">
            <label class="fl" for="fUrl"><span>Page URL</span></label>
            <input type="text" id="fUrl" placeholder="https://www.yourwebsite.com/your-page">
          </div>
          <div class="fgrid">
            <div class="field">
              <label class="fl" for="fKw"><span>Focus keyword</span><span class="opt">optional</span></label>
              <input type="text" id="fKw" placeholder="Your main keyword">
              <div class="hint">Shown in bold, like Google does.</div>
            </div>
            <div class="field">
              <label class="fl" for="fBrand"><span>Brand / site name</span><span class="opt">optional</span></label>
              <input type="text" id="fBrand" placeholder="Your brand name">
              <div class="hint">Shown above the title.</div>
            </div>
          </div>
          <div class="actions">
            <button class="btn ghost sm" type="button" id="compareBtn">⇆ Compare A vs B</button>
            <button class="btn ghost sm" type="button" id="copyToB">Copy A → B</button>
            <button class="btn ghost sm" type="button" id="clearBtn">↺ Clear</button>
          </div>
          <div class="hint" id="abHint" style="margin-top:.6rem"></div>
        </div>

        <div class="card">
          <h2><span class="step">3</span>Use it</h2>
          <div class="actions" style="margin-top:0">
            <button class="btn" type="button" id="copyTags">📋 Copy HTML tags</button>
            <button class="btn mint" type="button" id="dlCsv">⬇ Download report (CSV)</button>
            <span class="msg ok" id="copyMsg" style="margin:0"></span>
          </div>
          <div class="foot-hint">Copies the &lt;title&gt; and meta description tags for the version you're editing, ready to paste into your page's &lt;head&gt;.</div>
        </div>
      </div>

      <div class="sticky">
        <div class="card">
          <h2><span class="step">2</span>Google preview
            <span class="right" style="display:flex;gap:.4rem;flex-wrap:wrap">
              <span class="tabs" id="devTabs">
                <button type="button" data-d="desktop" class="on">🖥 Desktop</button>
                <button type="button" data-d="mobile">📱 Mobile</button>
              </span>
              <span class="tabs" id="themeTabs">
                <button type="button" data-t="light" class="on">☀</button>
                <button type="button" data-t="dark">☾</button>
              </span>
            </span>
          </h2>
          <div id="previews"></div>
        </div>

        <div class="card">
          <h2>Snippet health <span class="badge info" id="scoreFor" style="margin-left:.2rem">Version A</span></h2>
          <div class="score-row">
            <div class="ring" id="ring"></div>
            <div class="score-text">
              <div class="verdict" id="verdict"></div>
              <div class="sub" id="verdictSub"></div>
              <div class="pills" id="pills"></div>
            </div>
          </div>
          <div class="filters" id="filters">
            <button type="button" data-f="all" class="on">All</button>
            <button type="button" data-f="fail">Issues</button>
            <button type="button" data-f="warn">Warnings</button>
            <button type="button" data-f="pass">Passed</button>
          </div>
          <div class="checks" id="checks"></div>
        </div>
      </div>
    </div>
  </main>
</div>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const trim = s => String(s || '').replace(/\s+/g, ' ').trim();

// ------------------------------------------------------------ pixel limits (estimates of Google's cut-off)
const LIM = {
  desktop: { title: 600, desc: 920, tFont: '20px Arial', dFont: '14px Arial' },
  mobile:  { title: 656, desc: 680, tFont: '18px Arial', dFont: '14px Arial' },
};
const TITLE_MIN = 285;   // ~ 30 characters
const DESC_MIN  = 430;   // ~ 70 characters

const cvs = document.createElement('canvas').getContext('2d');
function px(text, font) { cvs.font = font; return Math.round(cvs.measureText(text || '').width); }
function cut(text, font, budget) {
  text = trim(text);
  const w = px(text, font);
  if (w <= budget) return { text, cut: false, w };
  const words = text.split(' ');
  let acc = '';
  for (const word of words) {
    const next = acc ? acc + ' ' + word : word;
    if (px(next + ' ...', font) > budget) break;
    acc = next;
  }
  if (!acc) { // one very long word
    let i = text.length;
    while (i > 0 && px(text.slice(0, i) + '...', font) > budget) i--;
    return { text: text.slice(0, i) + '...', cut: true, w, shown: text.slice(0, i) };
  }
  return { text: acc.replace(/[\s,;:|\-–—]+$/, '') + ' ...', cut: true, w, shown: acc };
}

// ------------------------------------------------------------ state
const blank = () => ({ title: '', desc: '', url: '' });
const S = {
  v: {
    A: blank(),
    B: blank(),
  },
  kw: '', brand: '',
  edit: 'A', compare: false, device: 'desktop', theme: 'light', filter: 'all',
  live: null, favicon: '',
};

// ------------------------------------------------------------ keyword helpers
const STOP = new Set('a an the and or of for to in on at by with from is are be your you our best top vs how what why when'.split(' '));
function kwTerms() {
  const kw = trim(S.kw).toLowerCase();
  if (!kw) return [];
  const words = kw.split(' ').filter(w => w.length > 1 && !STOP.has(w));
  return [...new Set([kw, ...words])].sort((a, b) => b.length - a.length);
}
const reEsc = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
function bold(text) {
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
function hasTerm(text, term) {
  return new RegExp('(^|[^\\p{L}\\p{N}])' + reEsc(term) + '(s|es)?($|[^\\p{L}\\p{N}])', 'iu').test(text);
}

// ------------------------------------------------------------ URL display
function parseUrl(u) {
  u = trim(u);
  if (!u) return null;
  if (!/^https?:\/\//i.test(u)) u = 'https://' + u;
  try { return new URL(u); } catch (e) { return null; }
}
function crumbs(u) {
  const p = parseUrl(u);
  if (!p) return { host: 'yourwebsite.com', line: 'https://yourwebsite.com' };
  const segs = p.pathname.split('/').filter(Boolean).map(s => decodeURIComponent(s).replace(/\.(html?|php|aspx?)$/i, ''));
  let line = p.origin;
  if (segs.length) line += ' › ' + segs.slice(0, 3).join(' › ') + (segs.length > 3 ? ' › …' : '');
  return { host: p.hostname.replace(/^www\./, ''), line };
}
function siteName(u) {
  if (trim(S.brand)) return trim(S.brand);
  const c = crumbs(u);
  const base = c.host.split('.')[0] || c.host;
  return base.charAt(0).toUpperCase() + base.slice(1);
}

// ------------------------------------------------------------ snippet renderer
function snippetHTML(v, device) {
  const L = LIM[device];
  const t = cut(v.title, L.tFont, L.title);
  const d = cut(v.desc, L.dFont, L.desc);
  const c = crumbs(v.url);
  const name = siteName(v.url);
  const favOk = S.favicon && S.live && parseUrl(v.url) && parseUrl(v.url).hostname === S.live.host;
  const fav = favOk ? `<img src="${esc(S.favicon)}" alt="" onerror="this.parentNode.textContent='${esc(name.charAt(0).toUpperCase())}'">` : esc(name.charAt(0).toUpperCase());
  return `<div class="serp ${device}">
    <div class="s-site"><span class="fav">${fav}</span>
      <div style="min-width:0"><div class="s-name">${esc(name)}</div><div class="s-url">${esc(c.line)}</div></div></div>
    <div class="s-title">${t.text ? esc(t.text) : '<span class="s-empty">Your title will appear here</span>'}</div>
    <div class="s-desc">${d.text ? bold(d.text) : '<span class="s-empty">No meta description — Google will pick text from your page instead.</span>'}</div>
  </div>`;
}

// ------------------------------------------------------------ analysis
const CTA = /\b(apply|check|compare|get|find|learn|calculate|discover|explore|know|try|start|save|download|book|shop|buy|read|see|view|sign up|join|call|claim|grab|avail)\b/i;
const GENERIC = /^(home|homepage|untitled|index|page|welcome|new page|document)$/i;
function analyze(v) {
  const title = trim(v.title), desc = trim(v.desc), url = trim(v.url);
  const out = [];
  const add = (id, name, status, detail, weight) => out.push({ id, name, status, detail, weight });
  const tw = px(title, LIM.desktop.tFont), dw = px(desc, LIM.desktop.dFont);
  const tm = px(title, LIM.mobile.tFont), dm = px(desc, LIM.mobile.dFont);
  let risk = 0; const riskWhy = [];

  // 1 title length
  if (!title) add('t_len', 'Title tag', 'fail', 'There is no title. Google will make one up from your page.', 20);
  else if (tw > LIM.desktop.title) { add('t_len', 'Title length', 'fail', `${tw}px — longer than ~600px, so Google cuts it off after “${esc(cut(title, LIM.desktop.tFont, LIM.desktop.title).shown || '')}”. Trim ${tw - LIM.desktop.title}px (about ${Math.ceil((tw - LIM.desktop.title) / 9.5)} characters).`, 20); risk += 2; riskWhy.push('title is too long'); }
  else if (tw < TITLE_MIN) { add('t_len', 'Title length', 'warn', `${tw}px — quite short. You have room to add a benefit, number or your brand.`, 20); risk += 1; riskWhy.push('title is very short'); }
  else add('t_len', 'Title length', 'pass', `${tw}px of ~600px — fits on desktop${tm > LIM.mobile.title ? ', but gets cut on mobile' : ' and mobile'}.`, 20);

  // 2 description length
  if (!desc) { add('d_len', 'Meta description', 'fail', 'Missing. Google will pull a random sentence from your page instead.', 15); risk += 1; }
  else if (dw > LIM.desktop.desc) add('d_len', 'Description length', 'warn', `${dw}px — longer than ~920px, so the end is cut off with “...”. Put the important part first.`, 15);
  else if (dw < DESC_MIN) add('d_len', 'Description length', 'warn', `${dw}px — short. Google often replaces short descriptions with its own text.`, 15);
  else add('d_len', 'Description length', 'pass', `${dw}px of ~920px on desktop${dm > LIM.mobile.desc ? ' (mobile shows a little less — keep key info early)' : ' — fits on mobile too'}.`, 15);

  // 3-4 keyword
  const kw = trim(S.kw);
  if (kw && title) {
    const words = kwTerms().filter(t => t !== kw.toLowerCase());
    const full = hasTerm(title, kw);
    const partial = words.length ? words.filter(w => hasTerm(title, w)).length / words.length : 0;
    if (full) {
      const pos = title.toLowerCase().indexOf(kw.toLowerCase());
      if (pos >= 0 && pos <= title.length * 0.4) add('kw_t', 'Keyword in title', 'pass', 'Your focus keyword is in the title, near the start — great for relevance and clicks.', 15);
      else add('kw_t', 'Keyword in title', 'warn', 'Keyword is in the title, but late. Move it closer to the start.', 15);
    } else if (partial >= 0.5) add('kw_t', 'Keyword in title', 'warn', `Only part of “${esc(kw)}” is in the title. Use the full phrase if it reads naturally.`, 15);
    else add('kw_t', 'Keyword in title', 'fail', `“${esc(kw)}” isn't in the title. Searchers scan for their words first.`, 15);
  }
  if (kw && desc) {
    if (hasTerm(desc, kw)) add('kw_d', 'Keyword in description', 'pass', 'Google will bold it in the snippet — it catches the eye.', 10);
    else if (kwTerms().some(t => hasTerm(desc, t))) add('kw_d', 'Keyword in description', 'warn', 'Only some keyword words appear (bold parts in the preview). Add the full phrase once.', 10);
    else add('kw_d', 'Keyword in description', 'fail', 'Not in the description, so nothing gets bolded for this search.', 10);
  }

  // 5 brand
  const brand = trim(S.brand);
  if (title && brand) {
    if (title.toLowerCase().includes(brand.toLowerCase())) add('brand', 'Brand in title', 'pass', 'Brand name is in the title — builds trust, especially for finance searches.', 5);
    else add('brand', 'Brand in title', 'warn', `Add “| ${esc(brand)}” at the end if there's room — people click brands they know.`, 5);
  }

  // 6 keyword stuffing / repetition
  if (title) {
    const counts = {};
    title.toLowerCase().split(/[^\p{L}\p{N}]+/u).filter(w => w.length > 2 && !STOP.has(w)).forEach(w => counts[w] = (counts[w] || 0) + 1);
    const rep = Object.entries(counts).filter(([, n]) => n >= 3).map(([w]) => w);
    const rep2 = Object.entries(counts).filter(([, n]) => n === 2).map(([w]) => w);
    const seps = (title.match(/[|•·»>]/g) || []).length + (title.match(/\s[-–—]\s/g) || []).length;
    if (rep.length || seps > 2) { add('stuff', 'No keyword stuffing', 'fail', rep.length ? `“${esc(rep.join(', '))}” repeats 3+ times — Google often rewrites stuffed titles.` : `${seps} separators — titles packed with | or – sections are often rewritten.`, 15); risk += 2; riskWhy.push('title looks stuffed'); }
    else if (rep2.length) add('stuff', 'No keyword stuffing', 'warn', `“${esc(rep2.join(', '))}” appears twice. Fine if natural, but it wastes space.`, 10);
    else add('stuff', 'No keyword stuffing', 'pass', 'No repeated words — reads naturally.', 10);
  }

  // 7 caps (title and description checked separately)
  const capRatio = t => { const l = t.replace(/[^A-Za-z]/g, ''); return l.length > 12 ? l.replace(/[^A-Z]/g, '').length / l.length : 0; };
  if ((title + desc).replace(/[^A-Za-z]/g, '').length > 12) {
    const tc = capRatio(title), dc = capRatio(desc);
    if (tc > 0.5 || dc > 0.5) { add('caps', 'No ALL CAPS', 'fail', `Too many capital letters in the ${tc > 0.5 ? 'title' : 'description'} — looks spammy, and Google may rewrite it.`, 10); risk += 1; riskWhy.push('too many capitals'); }
    else add('caps', 'No ALL CAPS', 'pass', 'Normal casing — easy to read.', 10);
  }

  // 8 title vs description
  if (title && desc) {
    const tl = title.toLowerCase(), dl = desc.toLowerCase();
    if (tl === dl || dl.startsWith(tl)) add('dup', 'Description adds something new', 'fail', 'The description repeats the title. Use it to add detail and a reason to click.', 5);
    else add('dup', 'Description adds something new', 'pass', 'The title and description say different things.', 5);
  }

  // 9 call to action
  if (desc) {
    if (CTA.test(desc)) add('cta', 'Call to action', 'pass', 'The description tells people what to do next.', 5);
    else add('cta', 'Call to action', 'warn', 'Add an action word — “Check”, “Compare”, “Apply”, “Calculate” — to lift clicks.', 5);
  }

  // 10 numbers
  if (title || desc) {
    if (/\d/.test(title + ' ' + desc)) add('num', 'Numbers or year', 'pass', 'Contains a number (rate, year, count) — numbers stand out in results.', 5);
    else add('num', 'Numbers or year', 'warn', 'Try a number: a rate, a year (2026), or “30+ banks”. They draw the eye.', 5);
  }

  // 11 generic title
  if (title && (GENERIC.test(title) || title.split(' ').length < 3)) { risk += 2; riskWhy.push('title is too generic'); }

  // 12 URL
  if (url) {
    const p = parseUrl(url);
    if (!p) add('url', 'Readable URL', 'fail', 'This doesn\'t look like a valid URL.', 5);
    else {
      const probs = [];
      if (p.search) probs.push('has ? parameters');
      if (/[A-Z]/.test(p.pathname)) probs.push('has capital letters');
      if (/_/.test(p.pathname)) probs.push('uses _ instead of -');
      if (url.length > 90) probs.push('is very long');
      if (probs.length) add('url', 'Readable URL', 'warn', 'The URL ' + probs.join(', ') + '. Short, lowercase, hyphenated URLs look cleaner in the breadcrumb.', 5);
      else add('url', 'Readable URL', 'pass', 'Clean, readable URL for the breadcrumb.', 5);
    }
  }

  // 13 live page extras (only for the fetched live version)
  if (v.isLive && S.live) {
    const L = S.live;
    if (L.noindex) add('noindex', 'Page can be indexed', 'fail', 'This page has a noindex tag — it will not show in Google at all.', 0);
    if (L.title_count > 1) add('multi_t', 'Only one title tag', 'warn', `Found ${L.title_count} title tags. Google uses one — remove the extras.`, 0);
    if (L.description_count > 1) add('multi_d', 'Only one meta description', 'warn', `Found ${L.description_count} meta descriptions. Keep just one.`, 0);
    if (L.h1 && title) {
      const tw2 = new Set(title.toLowerCase().split(/[^\p{L}\p{N}]+/u).filter(w => w.length > 2 && !STOP.has(w)));
      const hw = L.h1.toLowerCase().split(/[^\p{L}\p{N}]+/u).filter(w => w.length > 2 && !STOP.has(w));
      const overlap = hw.length ? hw.filter(w => tw2.has(w)).length / hw.length : 1;
      if (overlap < 0.3) { risk += 1; riskWhy.push('title and H1 don\'t match'); add('h1', 'Title matches the H1', 'warn', `The page H1 (“${esc(L.h1.slice(0, 80))}”) is very different from the title. Google sometimes uses the H1 instead.`, 0); }
      else add('h1', 'Title matches the H1', 'pass', 'Title and H1 are about the same thing — less chance of a rewrite.', 0);
    }
  }

  // score
  let got = 0, max = 0;
  out.forEach(c => { if (!c.weight) return; max += c.weight; got += c.status === 'pass' ? c.weight : c.status === 'warn' ? c.weight / 2 : 0; });
  let score = max ? Math.round(got / max * 100) : 0;
  if (!title && !desc) score = 0;
  if (v.isLive && S.live && S.live.noindex) score = Math.min(score, 40);
  const riskLevel = risk >= 3 ? 'High' : risk >= 1 ? 'Medium' : 'Low';
  return { checks: out, score, risk: riskLevel, riskWhy, tw, dw, tm, dm };
}

// ------------------------------------------------------------ rendering
function meterHTML(w, budget, min, chars, mobileW, mobileBudget) {
  const pct = Math.min(100, w / budget * 100);
  const cls = !w ? '' : w > budget ? 'r' : w < min ? 'o' : 'g';
  const color = cls === 'r' ? 'var(--red)' : cls === 'o' ? 'var(--orange)' : cls === 'g' ? 'var(--mint)' : 'var(--border)';
  const label = !w ? 'empty' : w > budget ? 'cut off on desktop' : w < min ? 'a bit short' : 'good length';
  return `<div class="bar"><div class="fill" style="width:${pct}%;background:${color}"></div></div>
    <div class="info"><span><b class="${cls}">${w} / ${budget}px</b> · ${chars} chars · <span class="${cls}">${label}</span></span>
    <span>mobile: <b class="${mobileW > mobileBudget ? 'r' : ''}">${mobileW} / ${mobileBudget}px</b></span></div>`;
}
function ringHTML(score) {
  const r = 40, c = 2 * Math.PI * r, off = c * (1 - score / 100);
  const col = score >= 80 ? 'var(--mint)' : score >= 50 ? 'var(--orange)' : 'var(--red)';
  return `<svg width="96" height="96" viewBox="0 0 96 96"><circle cx="48" cy="48" r="${r}" fill="none" stroke="var(--surface2)" stroke-width="8"/>
    <circle cx="48" cy="48" r="${r}" fill="none" stroke="${col}" stroke-width="8" stroke-linecap="round" stroke-dasharray="${c}" stroke-dashoffset="${off}" style="transition:stroke-dashoffset .3s"/></svg>
    <div class="num" style="color:${col}">${score}<small>/ 100</small></div>`;
}
const vLabel = k => (S.live && k === 'A' && S.v.A.isLive) ? 'Live page' : 'Version ' + k;
const hasB = () => !!(trim(S.v.B.title) || trim(S.v.B.desc));

function renderMeters() {
  const v = S.v[S.edit];
  const t = trim(v.title), d = trim(v.desc);
  $('mTitle').innerHTML = meterHTML(px(t, LIM.desktop.tFont), LIM.desktop.title, TITLE_MIN, t.length, px(t, LIM.mobile.tFont), LIM.mobile.title);
  $('mDesc').innerHTML = meterHTML(px(d, LIM.desktop.dFont), LIM.desktop.desc, DESC_MIN, d.length, px(d, LIM.mobile.dFont), LIM.mobile.desc);
}

function renderPreviews() {
  const wrapCls = `serp-wrap ${S.device}${S.theme === 'dark' ? ' dark' : ''}`;
  if (!S.compare) {
    $('previews').innerHTML = `<div class="${wrapCls}">${snippetHTML(S.v[S.edit], S.device)}</div>
      <div class="foot-hint">Showing ${vLabel(S.edit)} · ${S.device}. The title is cut at about ${LIM[S.device].title}px and the description at about ${LIM[S.device].desc}px.</div>`;
    return;
  }
  const a = analyze(S.v.A), b = analyze(S.v.B);
  const win = a.score === b.score ? '' : (a.score > b.score ? 'A' : 'B');
  const block = (k, res) => `<div class="prev-head">${esc(vLabel(k))}${win === k ? ' <span class="winner">★ stronger</span>' : ''}
      <span class="mini badge ${res.score >= 80 ? 'pass' : res.score >= 50 ? 'warn' : 'fail'}">${res.score}/100 · rewrite risk ${res.risk}</span></div>
      <div class="${wrapCls}">${snippetHTML(S.v[k], S.device)}</div>`;
  $('previews').innerHTML = block('A', a) + block('B', b) +
    `<div class="foot-hint">${win ? `<b style="color:var(--mint)">${esc(vLabel(win))}</b> scores higher (${Math.max(a.score, b.score)} vs ${Math.min(a.score, b.score)}).` : 'Both versions score the same.'} Switch between Version A and B above to edit each one.</div>`;
}

function renderScore() {
  const cur = S.v[S.edit];
  $('scoreFor').textContent = vLabel(S.edit);
  const empty = !trim(cur.title) && !trim(cur.desc);
  $('filters').style.display = empty ? 'none' : '';
  if (empty) {
    $('ring').innerHTML = `<svg width="96" height="96" viewBox="0 0 96 96"><circle cx="48" cy="48" r="40" fill="none" stroke="var(--surface2)" stroke-width="8"/></svg><div class="num" style="color:var(--muted)">–<small>/ 100</small></div>`;
    $('verdict').textContent = 'Your score will appear here';
    $('verdictSub').textContent = 'Fetch a page URL above, or type a title and description.';
    $('pills').innerHTML = ''; $('checks').innerHTML = '';
    return;
  }
  const res = analyze(cur);
  $('ring').innerHTML = ringHTML(res.score);
  const s = res.score;
  $('verdict').textContent = s >= 80 ? 'Strong snippet — ready to publish' : s >= 50 ? 'Good start — a few quick fixes' : 'Needs work before you publish';
  const fails = res.checks.filter(c => c.status === 'fail').length, warns = res.checks.filter(c => c.status === 'warn').length;
  $('verdictSub').textContent = `${fails} issue${fails === 1 ? '' : 's'} · ${warns} warning${warns === 1 ? '' : 's'} · ${res.checks.length - fails - warns} passed`;
  const rc = res.risk === 'Low' ? 'pass' : res.risk === 'Medium' ? 'warn' : 'fail';
  $('pills').innerHTML = `<span class="badge ${rc}" title="${esc(res.riskWhy.join(', ') || 'Nothing that usually triggers a rewrite')}">Rewrite risk: ${res.risk}</span>` +
    (res.riskWhy.length ? `<span class="badge info">${esc(res.riskWhy.join(' · '))}</span>` : '');
  const order = { fail: 0, warn: 1, pass: 2 };
  const list = res.checks.slice().sort((x, y) => order[x.status] - order[y.status]).filter(c => S.filter === 'all' || c.status === S.filter);
  const icon = { pass: '✓', warn: '!', fail: '✕' }, word = { pass: 'Pass', warn: 'Warning', fail: 'Issue' };
  $('checks').innerHTML = list.length ? list.map(c => `<div class="chk ${c.status}"><span class="ic">${icon[c.status]}</span>
      <div style="flex:1;min-width:0"><div class="ct">${esc(c.name)}</div><div class="cd">${c.detail}</div></div>
      <span class="badge ${c.status}">${word[c.status]}</span></div>`).join('')
    : '<div class="foot-hint" style="margin:0">Nothing in this group.</div>';
}

function renderTabs() {
  document.querySelectorAll('#verTabs button').forEach(b => {
    b.classList.toggle('on', b.dataset.v === S.edit);
    b.textContent = vLabel(b.dataset.v);
  });
  document.querySelectorAll('#devTabs button').forEach(b => b.classList.toggle('on', b.dataset.d === S.device));
  document.querySelectorAll('#themeTabs button').forEach(b => b.classList.toggle('on', b.dataset.t === S.theme));
  $('compareBtn').textContent = S.compare ? '✕ Stop comparing' : '⇆ Compare A vs B';
  $('copyToB').textContent = `Copy ${vLabel('A')} → B`;
  $('abHint').textContent = S.compare ? 'Comparing both versions in the preview. Edit each one with the tabs above.'
    : (hasB() ? 'Version B has text — click Compare to see both side by side.' : 'Tip: write a second version in “Version B” and compare which one scores better.');
}

// shrink the 600px desktop card to fit narrow screens (keeps Google's real line breaks)
function fitDesktop() {
  document.querySelectorAll('.serp-wrap').forEach(w => {
    const s = w.querySelector('.serp.desktop');
    if (!s) return;
    s.style.transform = ''; w.style.height = '';
    const avail = w.clientWidth - 2 * 19;
    if (avail > 0 && avail < 600) {
      const k = avail / 600;
      s.style.transform = `scale(${k})`;
      w.style.height = (s.offsetHeight * k + 2 * 18) + 'px';
    }
  });
}
window.addEventListener('resize', fitDesktop);
function render() { renderMeters(); renderPreviews(); renderScore(); renderTabs(); fitDesktop(); }

function loadForm() {
  const v = S.v[S.edit];
  $('fTitle').value = v.title; $('fDesc').value = v.desc; $('fUrl').value = v.url;
  $('fKw').value = S.kw; $('fBrand').value = S.brand;
}
function onInput() {
  const v = S.v[S.edit];
  v.title = $('fTitle').value; v.desc = $('fDesc').value; v.url = $('fUrl').value;
  S.kw = $('fKw').value; S.brand = $('fBrand').value;
  render();
}
['fTitle', 'fDesc', 'fUrl', 'fKw', 'fBrand'].forEach(id => $(id).addEventListener('input', onInput));
$('fDesc').addEventListener('keydown', e => { if (e.key === 'Enter') e.preventDefault(); });

document.querySelectorAll('#verTabs button').forEach(b => b.onclick = () => {
  S.edit = b.dataset.v;
  if (S.edit === 'B' && !trim(S.v.B.url)) S.v.B.url = S.v.A.url;
  loadForm(); render();
});
document.querySelectorAll('#devTabs button').forEach(b => b.onclick = () => { S.device = b.dataset.d; render(); });
document.querySelectorAll('#themeTabs button').forEach(b => b.onclick = () => { S.theme = b.dataset.t; render(); });
document.querySelectorAll('#filters button').forEach(b => b.onclick = () => {
  S.filter = b.dataset.f;
  document.querySelectorAll('#filters button').forEach(x => x.classList.toggle('on', x === b));
  renderScore();
});
$('compareBtn').onclick = () => {
  S.compare = !S.compare;
  if (S.compare && !hasB()) { S.v.B = { title: S.v.A.title, desc: S.v.A.desc, url: S.v.A.url }; S.edit = 'B'; loadForm(); }
  render();
};
$('copyToB').onclick = () => { S.v.B = { title: S.v.A.title, desc: S.v.A.desc, url: S.v.A.url }; S.edit = 'B'; loadForm(); render(); };
$('clearBtn').onclick = () => {
  S.v = { A: blank(), B: blank() }; S.kw = ''; S.brand = ''; S.edit = 'A'; S.compare = false; S.live = null; S.favicon = '';
  $('liveInfo').classList.remove('show'); $('fetchMsg').textContent = '';
  loadForm(); render(); $('fTitle').focus();
};

// ------------------------------------------------------------ copy + CSV
function flash(t) { $('copyMsg').textContent = t; setTimeout(() => $('copyMsg').textContent = '', 2200); }
async function copyText(t) {
  try { await navigator.clipboard.writeText(t); return true; }
  catch (e) {
    const ta = document.createElement('textarea'); ta.value = t; document.body.appendChild(ta); ta.select();
    const ok = document.execCommand('copy'); ta.remove(); return ok;
  }
}
$('copyTags').onclick = async () => {
  const v = S.v[S.edit];
  const tags = `<title>${esc(trim(v.title))}</title>\n<meta name="description" content="${esc(trim(v.desc))}">`;
  flash((await copyText(tags)) ? '✓ Tags copied' : 'Could not copy');
};
$('dlCsv').onclick = () => {
  const q = s => '"' + String(s == null ? '' : s).replace(/<[^>]+>/g, '').replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/"/g, '""') + '"';
  const rows = [['Version', 'Field', 'Value / Status', 'Detail']];
  const keys = hasB() ? ['A', 'B'] : [S.edit];
  keys.forEach(k => {
    const v = S.v[k], res = analyze(v), L = vLabel(k);
    rows.push([L, 'Title', trim(v.title), `${res.tw}px desktop / ${res.tm}px mobile · ${trim(v.title).length} chars`]);
    rows.push([L, 'Meta description', trim(v.desc), `${res.dw}px desktop / ${res.dm}px mobile · ${trim(v.desc).length} chars`]);
    rows.push([L, 'URL', trim(v.url), '']);
    rows.push([L, 'Snippet score', res.score + '/100', 'Rewrite risk: ' + res.risk + (res.riskWhy.length ? ' (' + res.riskWhy.join(', ') + ')' : '')]);
    res.checks.forEach(c => rows.push([L, c.name, { pass: 'Pass', warn: 'Warning', fail: 'Issue' }[c.status], c.detail]));
  });
  rows.push(['', 'Focus keyword', trim(S.kw), '']);
  const csv = '﻿' + rows.map(r => r.map(q).join(',')).join('\r\n');
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
  a.download = 'serp-snippet-report.csv'; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
};

// ------------------------------------------------------------ fetch from URL
function showLive(d) {
  const bits = [];
  bits.push(`Found on the live page: <b style="color:var(--text)">${d.title_count}</b> title tag${d.title_count === 1 ? '' : 's'}, <b style="color:var(--text)">${d.description_count}</b> meta description${d.description_count === 1 ? '' : 's'}.`);
  if (!d.title) bits.push('<span class="bad">✕ No title tag on this page.</span>');
  if (!d.description) bits.push('<span class="warn">! No meta description — Google writes its own snippet.</span>');
  if (d.noindex) bits.push('<span class="bad">✕ This page is set to noindex — it won\'t appear in Google.</span>');
  if (d.nosnippet) bits.push('<span class="warn">! nosnippet is set — Google shows no description text.</span>');
  if (d.canonical && d.canonical.replace(/\/$/, '') !== d.final_url.replace(/\/$/, '')) bits.push(`<span class="warn">! Canonical points to a different URL: ${esc(d.canonical)}</span>`);
  bits.push('I\'ve put the live snippet in the first tab and an editable copy in Version B — change B and compare.');
  $('liveInfo').innerHTML = bits.join('<br>');
  $('liveInfo').classList.add('show');
}
async function fetchSnippet() {
  const url = trim($('fetchUrl').value);
  if (!url) { $('fetchMsg').className = 'msg err'; $('fetchMsg').textContent = 'Paste a page URL first.'; return; }
  $('fetchBtn').disabled = true; $('fetchMsg').className = 'msg';
  $('fetchMsg').textContent = 'Reading the page… (first use can take ~30–50 seconds while the free server wakes up)';
  try {
    const r = await fetch('/api/fetch?url=' + encodeURIComponent(url));
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || 'Could not read that page.');
    const page = d.canonical || d.final_url;
    const p = parseUrl(page);
    S.live = Object.assign({}, d, { host: p ? p.hostname : '' });
    S.favicon = d.favicon || '';
    S.v.A = { title: d.title, desc: d.description, url: page, isLive: true };
    S.v.B = { title: d.title, desc: d.description, url: page };
    if (d.site_name) S.brand = d.site_name;
    S.edit = 'B'; S.compare = true;
    loadForm(); render(); showLive(d);
    $('fetchMsg').className = 'msg ok'; $('fetchMsg').textContent = '✓ Live snippet loaded.';
  } catch (err) {
    $('fetchMsg').className = 'msg err'; $('fetchMsg').textContent = '✕ ' + err.message;
  } finally { $('fetchBtn').disabled = false; }
}
$('fetchBtn').onclick = fetchSnippet;
$('fetchUrl').addEventListener('keydown', e => { if (e.key === 'Enter') fetchSnippet(); });

// ------------------------------------------------------------ mode
function setMode(m) {
  document.querySelectorAll('#modes .mode').forEach(b => b.classList.toggle('active', b.dataset.mode === m));
  $('urlBox').classList.toggle('show', m === 'url');
  if (m === 'url') setTimeout(() => $('fetchUrl').focus(), 50);
  else setTimeout(() => $('fTitle').focus(), 50);
}
document.querySelectorAll('#modes .mode').forEach(b => b.onclick = () => setMode(b.dataset.mode));

// ------------------------------------------------------------ init
(function init() {
  loadForm();
  const go = () => render();
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(go);
  go();
  const q = new URLSearchParams(location.search);
  if (q.get('url')) { setMode('url'); $('fetchUrl').value = q.get('url'); fetchSnippet(); }
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(debug=True, port=8501)
