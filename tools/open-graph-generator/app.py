"""
Open Graph Generator  -  Flask app
Fill in a short form (or auto-fill it from an existing URL) and get ready-to-paste
Open Graph + Twitter Card tags, live previews on 8 platforms, a 1200x630 share
image maker, fields that change with the page type (website / article / product),
code for plain HTML / Next.js / React Helmet / WordPress, matching JSON-LD schema,
and a one-click hand-off to the Open Graph Checker to test the live page.

Almost everything runs in the browser. The server only does two things:
  /api/fetch  - fetch an existing page and read its current tags (auto-fill)
  /api/image  - read an image URL's real width / height / file size

Built by Mahalakshmi Marimuthu - Digital Marketing Strategist & AI-Powered SEO Expert
"""
import io
import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, request

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

app = Flask(__name__)

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
# Where the "Test in OG Checker" button sends people. Update this once the
# Open Graph Checker is live on Render if you pick a different subdomain.
OG_CHECKER_URL = "https://opengraph-checker.onrender.com"

REQUEST_TIMEOUT = 10
IMAGE_MAX_BYTES = 6 * 1024 * 1024
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


def check_image(image_url):
    info = {"url": image_url, "ok": False, "width": None, "height": None,
            "size_bytes": None, "content_type": None, "error": None}
    if not image_url or not is_public_http(image_url):
        info["error"] = "Not a valid http(s) image URL."
        return info
    try:
        r = requests.get(image_url, headers={"User-Agent": BROWSER_UA},
                         timeout=REQUEST_TIMEOUT, stream=True)
        if r.status_code >= 400:
            info["error"] = f"Image request returned HTTP {r.status_code}."
            return info
        info["content_type"] = r.headers.get("Content-Type", "").split(";")[0].strip()
        chunks, total = [], 0
        for chunk in r.iter_content(8192):
            total += len(chunk)
            chunks.append(chunk)
            if total > IMAGE_MAX_BYTES:
                break
        info["size_bytes"] = total
        if HAS_PIL:
            try:
                img = Image.open(io.BytesIO(b"".join(chunks)))
                info["width"], info["height"] = img.size
                info["ok"] = True
            except Exception:
                info["error"] = "Could not read the image (unsupported or corrupted format)."
        else:
            info["ok"] = True
    except requests.exceptions.Timeout:
        info["error"] = "Image request timed out."
    except requests.exceptions.RequestException:
        info["error"] = "Could not download the image."
    return info


def read_existing_tags(url):
    """Fetch a page and return whatever OG / Twitter / basic tags it already has."""
    try:
        r = requests.get(url, headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"},
                         timeout=REQUEST_TIMEOUT, allow_redirects=True)
    except requests.exceptions.Timeout:
        return {"ok": False, "error": "The page took too long to respond."}
    except requests.exceptions.RequestException:
        return {"ok": False, "error": "Could not connect to this URL."}
    if r.status_code >= 400:
        return {"ok": False, "error": f"The page returned HTTP {r.status_code}."}

    final_url = r.url
    soup = BeautifulSoup(r.text or "", "html.parser")

    def collect(attr, prefix):
        out = {}
        for t in soup.find_all("meta", attrs={attr: re.compile(rf"^{prefix}", re.I)}):
            k = (t.get(attr) or "").lower()
            v = (t.get("content") or "").strip()
            if k and v and k not in out:
                out[k] = v
        return out

    og = collect("property", "og:")
    og.update({k: v for k, v in collect("property", "article:").items()})
    og.update({k: v for k, v in collect("property", "product:").items()})
    tw = collect("name", "twitter:")
    # Some sites put twitter:* in property= instead of name=
    for k, v in collect("property", "twitter:").items():
        tw.setdefault(k, v)

    for key in ("og:image", "og:url"):
        if og.get(key):
            og[key] = urljoin(final_url, og[key])
    if tw.get("twitter:image"):
        tw["twitter:image"] = urljoin(final_url, tw["twitter:image"])

    desc_tag = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    canonical_tag = soup.find("link", rel="canonical")
    author_tag = soup.find("meta", attrs={"name": re.compile(r"^author$", re.I)})

    return {
        "ok": True,
        "final_url": final_url,
        "og": og,
        "twitter": tw,
        "title_tag": soup.title.string.strip() if soup.title and soup.title.string else "",
        "meta_description": (desc_tag.get("content") or "").strip() if desc_tag else "",
        "canonical": urljoin(final_url, canonical_tag.get("href").strip())
        if canonical_tag and canonical_tag.get("href") else "",
        "author": (author_tag.get("content") or "").strip() if author_tag else "",
    }


# --------------------------------------------------------------------------- #
# API routes
# --------------------------------------------------------------------------- #
@app.route("/api/fetch")
def api_fetch():
    url = normalize_url(request.args.get("url", ""))
    if not url or not is_public_http(url):
        return jsonify({"ok": False, "error": "Please enter a valid URL."}), 400
    data = read_existing_tags(url)
    if data.get("ok"):
        img_url = data["og"].get("og:image") or data["twitter"].get("twitter:image")
        data["image_info"] = check_image(img_url) if img_url else None
    return jsonify(data)


@app.route("/api/image")
def api_image():
    url = normalize_url(request.args.get("url", ""))
    return jsonify(check_image(url))


@app.route("/")
def home():
    return Response(PAGE.replace("__CHECKER_URL__", OG_CHECKER_URL), mimetype="text/html")


# --------------------------------------------------------------------------- #
# Page (plain HTML + JS; no Jinja so the JS can use braces freely)
# --------------------------------------------------------------------------- #
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Open Graph Generator</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%237c6af7'/%3E%3Ctext x='50%25' y='50%25' dominant-baseline='central' text-anchor='middle' font-family='Georgia%2C serif' font-size='18' font-weight='900' fill='white'%3EM%3C/text%3E%3C/svg%3E">
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@600;700;800&family=Inter:wght@400;500;600;700&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root{
    --bg:#0a0a0f;--surface:#12121a;--surface2:#171722;--border:#23233a;
    --accent:#7c6af7;--accent-h:#6a58e8;--mint:#6af7c8;--orange:#f7a26a;
    --red:#f76a7c;--text:#e8e8f0;--muted:#8888a8;
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

  .topbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:1.2rem;flex-wrap:wrap;gap:.8rem}
  .topbar h1{font-family:'Sora',sans-serif;font-size:1.25rem;font-weight:700}
  .topbar h1 span{color:var(--accent)}
  .crumb{font-family:'DM Mono',monospace;font-size:.68rem;color:var(--muted);letter-spacing:.12em;text-transform:uppercase}

  .card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.2rem 1.4rem}
  .card + .card{margin-top:1rem}
  .card h2{font-family:'Sora',sans-serif;font-size:.9rem;font-weight:700;margin-bottom:.9rem}
  .card h2 .step{display:inline-flex;width:22px;height:22px;border-radius:6px;background:rgba(124,106,247,.14);border:1px solid rgba(124,106,247,.4);color:var(--accent);font-size:.7rem;align-items:center;justify-content:center;margin-right:.5rem;font-family:'DM Mono',monospace}
  label.fl{display:flex;justify-content:space-between;align-items:baseline;font-size:.76rem;color:var(--muted);margin-bottom:.35rem;gap:.5rem}
  .cnt{font-family:'DM Mono',monospace;font-size:.66rem;white-space:nowrap}
  .cnt.ok{color:var(--mint)} .cnt.warn{color:var(--orange)} .cnt.error{color:var(--red)}
  input[type=text],input[type=url],input[type=number],input[type=datetime-local],select,textarea{width:100%;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:.65rem .9rem;font-family:'Inter',sans-serif;font-size:.84rem}
  input[type=color]{width:100%;height:38px;background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:3px;cursor:pointer}
  textarea{min-height:76px;resize:vertical}
  input:focus,textarea:focus,select:focus{outline:none;border-color:var(--accent)}
  .field{margin-bottom:.85rem}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:.8rem}
  .grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:.8rem}
  .btn{display:inline-flex;align-items:center;gap:.5rem;background:var(--accent);color:#fff;border:none;border-radius:8px;padding:.62rem 1.3rem;font-size:.84rem;font-weight:600;cursor:pointer;font-family:'Inter',sans-serif;white-space:nowrap}
  .btn:hover{background:var(--accent-h)}
  .btn.ghost{background:var(--surface2);border:1px solid var(--border);color:var(--text)}
  .btn.ghost:hover{border-color:var(--accent)}
  .btn.mint{background:rgba(106,247,200,.12);border:1px solid rgba(106,247,200,.4);color:var(--mint)}
  .btn.mint:hover{background:rgba(106,247,200,.2)}
  .btn:disabled{opacity:.45;cursor:not-allowed}
  .hint{font-size:.72rem;color:var(--muted);margin-top:.4rem;font-family:'DM Mono',monospace}
  .msg{font-size:.78rem;margin-top:.6rem}
  .msg.err{color:var(--red)} .msg.ok{color:var(--mint)}
  .row-inline{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center}
  .row-inline input{flex:1;min-width:240px}
  .seg{display:flex;gap:.4rem;flex-wrap:wrap}
  .seg button{background:var(--surface2);border:1px solid var(--border);color:var(--muted);padding:.45rem .9rem;border-radius:8px;font-size:.78rem;font-weight:600;cursor:pointer;font-family:'Inter',sans-serif}
  .seg button.active{background:rgba(124,106,247,.14);border-color:rgba(124,106,247,.4);color:var(--accent)}
  .type-fields{display:none;margin-top:.9rem;padding-top:.9rem;border-top:1px dashed var(--border)}
  .type-fields.show{display:block}
  .imginfo{font-size:.72rem;font-family:'DM Mono',monospace;margin-top:.4rem}

  .layout{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:1.2rem;align-items:start}
  .sticky{position:sticky;top:1rem}

  .charts{display:grid;grid-template-columns:190px 1fr;gap:1rem}
  .ring-wrap{position:relative;width:140px;height:140px;margin:0 auto}
  .ring-center{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
  .ring-center .score{font-family:'Sora',sans-serif;font-size:1.7rem;font-weight:800}
  .ring-center .lbl{font-size:.58rem;font-family:'DM Mono',monospace;color:var(--muted);text-transform:uppercase;letter-spacing:.1em}
  .card h3{font-family:'Sora',sans-serif;font-size:.74rem;font-weight:700;margin-bottom:.7rem;color:var(--muted)}

  .issues{display:flex;flex-direction:column;gap:.45rem;max-height:220px;overflow-y:auto}
  .issue{border-left:3px solid var(--border);background:var(--surface2);border-radius:0 10px 10px 0;padding:.5rem .8rem;font-size:.76rem}
  .issue.error{border-color:var(--red)} .issue.warn{border-color:var(--orange)}
  .issue .m{font-weight:600}
  .issue .f{color:var(--muted);font-size:.7rem;margin-top:.15rem}
  .all-good{color:var(--mint);font-size:.82rem}

  .sec-title{font-family:'Sora',sans-serif;font-size:1rem;font-weight:700;margin:1.8rem 0 .9rem}
  .preview-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:1rem}
  .pcard{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden}
  .pcard .phead{display:flex;align-items:center;gap:.5rem;padding:.55rem .85rem;border-bottom:1px solid var(--border);font-family:'DM Mono',monospace;font-size:.66rem;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
  .pcard .picon{width:20px;height:20px;border-radius:5px;background:var(--surface2);display:flex;align-items:center;justify-content:center;font-size:.6rem;font-weight:700;color:var(--accent)}
  .pcard .pimg{width:100%;background:var(--surface2);display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:.7rem;overflow:hidden}
  .pcard .pimg img{width:100%;height:100%;object-fit:cover;display:block}
  .pcard .pbody{padding:.65rem .85rem}
  .pcard .pdomain{font-size:.64rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:.15rem}
  .pcard .ptitle{font-size:.84rem;font-weight:700;color:var(--text);margin-bottom:.15rem;line-height:1.35;word-break:break-word}
  .pcard .pdesc{font-size:.74rem;color:var(--muted);line-height:1.4;word-break:break-word}
  .pcard.google .ptitle{color:#8ab4f8}
  .pcard.google .pdomain{color:var(--mint)}
  .pcard.small .prow{display:flex;gap:.7rem;padding:.65rem .85rem;align-items:flex-start}
  .pcard.small .pthumb{width:84px;height:84px;flex-shrink:0;border-radius:8px;background:var(--surface2);overflow:hidden;display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:.6rem}
  .pcard.small .pthumb img{width:100%;height:100%;object-fit:cover}
  .pcard.small .pbody{padding:0}

  .maker{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(0,1fr);gap:1.2rem;align-items:start}
  .canvas-wrap{background:var(--surface2);border:1px solid var(--border);border-radius:12px;padding:.8rem}
  .canvas-wrap canvas{width:100%;height:auto;display:block;border-radius:6px}
  .file-in{font-size:.76rem;color:var(--muted)}
  .file-in input{margin-top:.3rem;font-size:.74rem;color:var(--muted);width:100%}

  .code-tabs{display:flex;gap:.4rem;flex-wrap:wrap;margin-bottom:.8rem}
  .code-box{position:relative}
  pre.code{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:1rem 1.1rem;font-family:'DM Mono',monospace;font-size:.74rem;line-height:1.6;color:#cfcfe6;overflow-x:auto;white-space:pre;max-height:460px;overflow-y:auto}
  .code-actions{display:flex;gap:.6rem;margin-top:.8rem;flex-wrap:wrap;align-items:center}
  .toggle{display:flex;align-items:center;gap:.5rem;font-size:.78rem;color:var(--muted);cursor:pointer}
  .toggle input{accent-color:var(--accent);width:15px;height:15px}

  @media(max-width:1100px){.layout,.maker{grid-template-columns:1fr}.sticky{position:static}}
  @media(max-width:960px){.sidebar{display:none}.main{margin-left:0;padding:1.2rem}.charts{grid-template-columns:1fr}.grid3{grid-template-columns:1fr 1fr}}
  @media(max-width:560px){.grid2,.grid3{grid-template-columns:1fr}}
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
      <a href="#" class="sidebar-link active">🏷️&nbsp; Open Graph Generator</a>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-label">Navigate</div>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="sidebar-link">🧰&nbsp; All My Tools</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio" class="sidebar-link">👩‍💻&nbsp; Portfolio</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/blog/index.html" class="sidebar-link">📝&nbsp; Blog Posts</a>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-label">This tool makes</div>
      <div style="font-size:.72rem;color:var(--muted);line-height:1.6;padding:0 .3rem">
        Open Graph &amp; Twitter Card tags · live previews on 8 platforms · 1200×630 share image · HTML / Next.js / React / WordPress code · JSON-LD schema
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
      <div>
        <div class="crumb">// seo tool · free</div>
        <h1>🏷️ Open Graph <span>Generator</span></h1>
      </div>
    </div>

    <!-- Auto-fill -->
    <div class="card">
      <h2><span class="step">0</span>Start from an existing page <span style="color:var(--muted);font-weight:500;font-size:.75rem">(optional)</span></h2>
      <div class="row-inline">
        <input type="text" id="fetchUrl" placeholder="https://example.com/page — paste a URL to load its current tags">
        <button class="btn ghost" id="fetchBtn" type="button">⚡ Auto-fill</button>
      </div>
      <div class="msg" id="fetchMsg"></div>
    </div>

    <div class="layout" style="margin-top:1.2rem">
      <!-- LEFT: form -->
      <div>
        <div class="card">
          <h2><span class="step">1</span>Page details</h2>
          <div class="field">
            <label class="fl">Page type</label>
            <div class="seg" id="typeSeg">
              <button type="button" data-type="website" class="active">🌐 Website</button>
              <button type="button" data-type="article">📰 Article / Blog</button>
              <button type="button" data-type="product">🛒 Product</button>
            </div>
          </div>
          <div class="field">
            <label class="fl" for="f_title">Title (og:title) <span class="cnt" id="c_title"></span></label>
            <input type="text" id="f_title" placeholder="e.g. Personal Loan Interest Rates 2026 — Compare 40+ Banks">
          </div>
          <div class="field">
            <label class="fl" for="f_desc">Description (og:description) <span class="cnt" id="c_desc"></span></label>
            <textarea id="f_desc" placeholder="One or two sentences on why someone should click."></textarea>
          </div>
          <div class="field">
            <label class="fl" for="f_url">Page URL (og:url — use the canonical URL)</label>
            <input type="url" id="f_url" placeholder="https://example.com/page">
          </div>
          <div class="grid2">
            <div class="field">
              <label class="fl" for="f_site">Site name (og:site_name)</label>
              <input type="text" id="f_site" placeholder="e.g. BankBazaar">
            </div>
            <div class="field">
              <label class="fl" for="f_locale">Locale (og:locale)</label>
              <select id="f_locale">
                <option value="en_IN">en_IN — English (India)</option>
                <option value="en_US">en_US — English (US)</option>
                <option value="en_GB">en_GB — English (UK)</option>
                <option value="hi_IN">hi_IN — Hindi</option>
                <option value="ta_IN">ta_IN — Tamil</option>
                <option value="">(leave out)</option>
              </select>
            </div>
          </div>

          <div class="type-fields" id="tf_article">
            <div class="grid2">
              <div class="field"><label class="fl" for="f_author">Author name</label><input type="text" id="f_author" placeholder="e.g. Mahalakshmi Marimuthu"></div>
              <div class="field"><label class="fl" for="f_section">Section / category</label><input type="text" id="f_section" placeholder="e.g. Personal Finance"></div>
              <div class="field"><label class="fl" for="f_pub">Published</label><input type="datetime-local" id="f_pub"></div>
              <div class="field"><label class="fl" for="f_mod">Last updated</label><input type="datetime-local" id="f_mod"></div>
            </div>
            <div class="field"><label class="fl" for="f_tags">Tags (comma-separated)</label><input type="text" id="f_tags" placeholder="personal loan, interest rates, EMI"></div>
          </div>

          <div class="type-fields" id="tf_product">
            <div class="grid3">
              <div class="field"><label class="fl" for="f_price">Price</label><input type="number" step="0.01" min="0" id="f_price" placeholder="999"></div>
              <div class="field"><label class="fl" for="f_cur">Currency</label>
                <select id="f_cur"><option>INR</option><option>USD</option><option>EUR</option><option>GBP</option><option>AED</option></select></div>
              <div class="field"><label class="fl" for="f_avail">Availability</label>
                <select id="f_avail"><option value="in stock">In stock</option><option value="out of stock">Out of stock</option><option value="preorder">Pre-order</option></select></div>
            </div>
            <div class="field"><label class="fl" for="f_brand">Brand</label><input type="text" id="f_brand" placeholder="e.g. Acme"></div>
          </div>
        </div>

        <div class="card">
          <h2><span class="step">2</span>Share image</h2>
          <div class="field">
            <label class="fl" for="f_img">Image URL (og:image — must be a public, absolute URL)</label>
            <div class="row-inline">
              <input type="url" id="f_img" placeholder="https://example.com/og-image.jpg">
              <button class="btn ghost" type="button" id="imgCheckBtn">📏 Check size</button>
            </div>
            <div class="imginfo" id="imgInfo"></div>
            <div class="hint">No image yet? Make one in the <a href="#maker">Share Image Maker</a> below.</div>
          </div>
          <div class="grid3">
            <div class="field"><label class="fl" for="f_iw">Width (px)</label><input type="number" id="f_iw" placeholder="1200"></div>
            <div class="field"><label class="fl" for="f_ih">Height (px)</label><input type="number" id="f_ih" placeholder="630"></div>
            <div class="field"><label class="fl" for="f_itype">Type</label>
              <select id="f_itype"><option value="">(auto)</option><option>image/jpeg</option><option>image/png</option><option>image/webp</option></select></div>
          </div>
          <div class="field">
            <label class="fl" for="f_ialt">Image alt text (og:image:alt) <span class="cnt" id="c_alt"></span></label>
            <input type="text" id="f_ialt" placeholder="Describe the image for screen readers">
          </div>
        </div>

        <div class="card">
          <h2><span class="step">3</span>Twitter / X card</h2>
          <div class="grid3">
            <div class="field"><label class="fl" for="f_card">Card type</label>
              <select id="f_card"><option value="summary_large_image">Large image</option><option value="summary">Small thumbnail</option></select></div>
            <div class="field"><label class="fl" for="f_twsite">Site @handle</label><input type="text" id="f_twsite" placeholder="@brand"></div>
            <div class="field"><label class="fl" for="f_twcreator">Author @handle</label><input type="text" id="f_twcreator" placeholder="@author"></div>
          </div>
          <div class="hint">X reads og:title / og:description / og:image automatically, so the generator only adds separate twitter:* tags where they're needed.</div>
        </div>
      </div>

      <!-- RIGHT: score + issues (sticky) -->
      <div class="sticky">
        <div class="card">
          <div class="charts">
            <div>
              <h3>READY-TO-SHARE SCORE</h3>
              <div class="ring-wrap">
                <canvas id="ring"></canvas>
                <div class="ring-center"><div class="score" id="scoreNum">0</div><div class="lbl">out of 100</div></div>
              </div>
            </div>
            <div>
              <h3>WHAT TO FIX</h3>
              <div class="issues" id="issues"></div>
            </div>
          </div>
          <div class="code-actions" style="margin-top:1rem">
            <button class="btn" type="button" onclick="document.getElementById('codeSec').scrollIntoView({behavior:'smooth'})">📋 Get the code</button>
            <button class="btn mint" type="button" id="testBtn">🔍 Test in OG Checker</button>
          </div>
          <div class="hint" id="testHint">Publish the tags first, then test the live URL.</div>
        </div>

        <div class="sec-title" style="margin-top:1.2rem">👀 Quick preview</div>
        <div id="quickPreview"></div>
      </div>
    </div>

    <div class="sec-title">👀 How it looks when shared — 8 platforms</div>
    <div class="preview-grid" id="previews"></div>
    <p class="hint">Each image box uses that platform's real crop ratio, so you can see what gets cut off before you publish.</p>

    <!-- Image maker -->
    <div class="sec-title" id="maker">🎨 Share Image Maker — 1200 × 630</div>
    <div class="maker">
      <div class="canvas-wrap"><canvas id="ogCanvas" width="1200" height="630"></canvas></div>
      <div class="card" style="margin-top:0">
        <div class="field">
          <label class="fl">Layout</label>
          <div class="seg" id="layoutSeg">
            <button type="button" data-layout="left" class="active">Left-aligned</button>
            <button type="button" data-layout="center">Centered</button>
            <button type="button" data-layout="bar">Accent bar</button>
          </div>
        </div>
        <div class="field">
          <label class="fl" for="m_title">Headline <span class="hint" style="margin:0">(defaults to your title)</span></label>
          <input type="text" id="m_title" placeholder="Uses og:title if left blank">
        </div>
        <div class="field">
          <label class="fl" for="m_sub">Sub-line</label>
          <input type="text" id="m_sub" placeholder="e.g. Compare rates from 40+ banks">
        </div>
        <div class="field">
          <label class="fl" for="m_foot">Footer label</label>
          <input type="text" id="m_foot" placeholder="Uses your domain if left blank">
        </div>
        <div class="grid3">
          <div class="field"><label class="fl" for="m_bg1">Background</label><input type="color" id="m_bg1" value="#1b1640"></div>
          <div class="field"><label class="fl" for="m_bg2">Gradient to</label><input type="color" id="m_bg2" value="#7c6af7"></div>
          <div class="field"><label class="fl" for="m_fg">Text</label><input type="color" id="m_fg" value="#ffffff"></div>
        </div>
        <div class="grid2">
          <div class="field"><label class="fl" for="m_acc">Accent</label><input type="color" id="m_acc" value="#6af7c8"></div>
          <div class="field file-in">Logo (PNG/SVG/JPG)<input type="file" id="m_logo" accept="image/*"></div>
        </div>
        <div class="code-actions">
          <button class="btn" type="button" id="dlPng">⬇️ Download PNG</button>
          <button class="btn ghost" type="button" id="usePreview">👀 Show in previews</button>
        </div>
        <div class="hint">Upload the PNG to your site, then paste its public URL into the Image URL field above — social platforms can't read an image that only lives on your computer.</div>
      </div>
    </div>

    <!-- Code -->
    <div class="sec-title" id="codeSec">📋 Your code</div>
    <div class="card">
      <div class="code-tabs seg" id="codeSeg">
        <button type="button" data-fmt="html" class="active">HTML</button>
        <button type="button" data-fmt="next">Next.js</button>
        <button type="button" data-fmt="helmet">React Helmet</button>
        <button type="button" data-fmt="wp">WordPress (Yoast / Rank Math)</button>
      </div>
      <pre class="code" id="codeOut"></pre>
      <div class="code-actions">
        <button class="btn" type="button" id="copyCode">📋 Copy</button>
        <label class="toggle"><input type="checkbox" id="incJsonLd" checked> Include matching JSON-LD schema</label>
        <span class="msg ok" id="copyMsg" style="margin:0"></span>
      </div>
    </div>
  </main>
</div>

<script>
const CHECKER_URL = "__CHECKER_URL__";
const $ = id => document.getElementById(id);
const state = { type: 'website', fmt: 'html', layout: 'left', logo: null, makerImage: null, imgInfo: null };

const PLATFORMS = [
  {key:'facebook', label:'Facebook',      icon:'f',  ratio:1.91, t:100, d:135},
  {key:'linkedin', label:'LinkedIn',      icon:'in', ratio:1.91, t:70,  d:100},
  {key:'twitter',  label:'Twitter / X',   icon:'X',  ratio:2.0,  t:70,  d:125},
  {key:'whatsapp', label:'WhatsApp',      icon:'W',  ratio:1.91, t:65,  d:65},
  {key:'slack',    label:'Slack',         icon:'S',  ratio:1.91, t:70,  d:160},
  {key:'discord',  label:'Discord',       icon:'D',  ratio:1.91, t:256, d:300},
  {key:'telegram', label:'Telegram',      icon:'T',  ratio:1.91, t:90,  d:180},
  {key:'google',   label:'Google Search', icon:'G',  ratio:null, t:60,  d:160},
];

// ---------------------------------------------------------------- helpers
const esc = s => String(s ?? '').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const trunc = (s, n) => { s = s || ''; return s.length <= n ? s : s.slice(0, n - 1).trimEnd() + '…'; };
const domainOf = u => { try { return new URL(u).hostname.replace(/^www\./,''); } catch(e) { return ''; } };
const handle = h => { h = (h || '').trim(); if (!h) return ''; return h.startsWith('@') ? h : '@' + h; };
const isoOf = v => { if (!v) return ''; const d = new Date(v); return isNaN(d) ? '' : d.toISOString(); };
const toLocalInput = iso => { const d = new Date(iso); if (isNaN(d)) return ''; const p = n => String(n).padStart(2,'0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`; };
const isAbsUrl = u => /^https?:\/\/[^\s/]+\.[^\s]+/i.test(u || '');

function data() {
  return {
    type: state.type,
    title: $('f_title').value.trim(), desc: $('f_desc').value.trim(), url: $('f_url').value.trim(),
    site: $('f_site').value.trim(), locale: $('f_locale').value,
    img: $('f_img').value.trim(), iw: $('f_iw').value.trim(), ih: $('f_ih').value.trim(),
    itype: $('f_itype').value, ialt: $('f_ialt').value.trim(),
    card: $('f_card').value, twsite: handle($('f_twsite').value), twcreator: handle($('f_twcreator').value),
    author: $('f_author').value.trim(), section: $('f_section').value.trim(),
    pub: isoOf($('f_pub').value), mod: isoOf($('f_mod').value),
    tags: $('f_tags').value.split(',').map(s => s.trim()).filter(Boolean),
    price: $('f_price').value.trim(), cur: $('f_cur').value, avail: $('f_avail').value, brand: $('f_brand').value.trim(),
  };
}

// ---------------------------------------------------------------- counters
function counter(el, len, okMin, okMax, hardMax) {
  let cls = 'ok', txt = `${len} chars`;
  if (!len) { cls = 'error'; txt = 'required'; }
  else if (len < okMin) { cls = 'warn'; txt = `${len} — a bit short`; }
  else if (len > hardMax) { cls = 'error'; txt = `${len} — gets cut off`; }
  else if (len > okMax) { cls = 'warn'; txt = `${len} — may be cut off on some apps`; }
  el.className = 'cnt ' + cls; el.textContent = txt;
}

// ---------------------------------------------------------------- scoring
function evaluate(d) {
  const issues = []; let s = 0;
  const add = (sev, m, f) => issues.push({sev, m, f});
  // title 15
  if (!d.title) add('error', 'Title is missing.', 'Every platform uses og:title as the headline of the card.');
  else if (d.title.length < 15) { s += 8; add('warn', `Title is only ${d.title.length} characters.`, 'Aim for about 40–60 characters.'); }
  else if (d.title.length > 90) { s += 8; add('warn', `Title is ${d.title.length} characters.`, 'Keep it under ~60–70 so LinkedIn, X and WhatsApp don\'t cut it off.'); }
  else s += 15;
  // desc 15
  if (!d.desc) add('error', 'Description is missing.', 'Add one or two sentences — most apps show it under the title.');
  else if (d.desc.length < 50) { s += 8; add('warn', `Description is only ${d.desc.length} characters.`, 'Aim for about 100–160 characters.'); }
  else if (d.desc.length > 200) { s += 10; add('warn', `Description is ${d.desc.length} characters.`, 'Most platforms show ~100–160 characters; front-load the important part.'); }
  else s += 15;
  // image 25
  if (!d.img) add('error', 'Share image is missing.', 'Without og:image most platforms show a blank or tiny card. Use the Share Image Maker below.');
  else if (!isAbsUrl(d.img)) { s += 5; add('error', 'Image URL isn\'t a full public URL.', 'Use an absolute URL starting with https:// — relative paths often fail.'); }
  else {
    s += 10;
    const w = parseInt(d.iw), h = parseInt(d.ih);
    if (w && h) {
      if (w < 200 || h < 200) add('error', `Image is only ${w}×${h}px.`, 'Use at least 600×315px; 1200×630px is the safe size everywhere.');
      else if (w < 1200) { s += 8; add('warn', `Image is ${w}×${h}px.`, '1200×630px looks sharp on every platform, including retina screens.'); }
      else s += 10;
      const r = w / h;
      if (r < 1.5 || r > 2.1) add('warn', `Image ratio is ${r.toFixed(2)}:1.`, 'Most platforms crop to ~1.91:1 — check the previews below for anything cut off.');
      else s += 5;
    } else { s += 7; add('warn', 'Image width/height not set.', 'Click "Check size" — og:image:width/height help Facebook show the image on the very first share.'); }
  }
  // url 10
  if (!d.url) add('error', 'Page URL is missing.', 'og:url tells platforms which link to count shares against — use the canonical URL.');
  else if (!isAbsUrl(d.url)) { s += 4; add('warn', 'Page URL isn\'t a full URL.', 'Use the full canonical URL starting with https://.'); }
  else s += 10;
  // site name 5, locale 5, alt 5
  if (d.site) s += 5; else add('warn', 'Site name is missing.', 'Shown next to the title on Facebook and LinkedIn.');
  if (d.locale) s += 5;
  if (d.img) { if (d.ialt) s += 5; else add('warn', 'Image alt text is missing.', 'Helps screen-reader users and is shown on X when the image fails to load.'); }
  else s += 0;
  // twitter 10
  s += 5; // card type is always set by the generator
  if (d.twsite) s += 5; else add('warn', 'X @handle is missing.', 'twitter:site links the card to your brand\'s X account.');
  // type-specific 10
  if (d.type === 'article') {
    let t = 0;
    if (d.author) t += 4; else add('warn', 'Article author is missing.', 'Used by LinkedIn/Facebook and the Article schema.');
    if (d.pub) t += 4; else add('warn', 'Published date is missing.', 'article:published_time and datePublished help freshness signals.');
    if (d.section || d.tags.length) t += 2;
    s += t;
  } else if (d.type === 'product') {
    let t = 0;
    if (d.price) t += 6; else add('warn', 'Product price is missing.', 'Pinterest, Facebook and Product schema all use the price.');
    if (d.brand) t += 4; else add('warn', 'Product brand is missing.', 'Used by the Product schema for rich results.');
    s += t;
  } else s += 10;
  s = Math.max(0, Math.min(100, Math.round(s)));
  issues.sort((a, b) => (a.sev === 'error' ? 0 : 1) - (b.sev === 'error' ? 0 : 1));
  return {score: s, issues};
}

// ---------------------------------------------------------------- code generation
function ogPairs(d) {
  const p = [];
  const og = (k, v) => { if (v) p.push(['property', k, v]); };
  const tw = (k, v) => { if (v) p.push(['name', k, v]); };
  og('og:type', d.type === 'product' ? 'product' : d.type);
  og('og:title', d.title); og('og:description', d.desc); og('og:url', d.url);
  og('og:site_name', d.site); og('og:locale', d.locale);
  if (d.img) {
    og('og:image', d.img);
    if (d.img.startsWith('https://')) og('og:image:secure_url', d.img);
    og('og:image:type', d.itype); og('og:image:width', d.iw); og('og:image:height', d.ih); og('og:image:alt', d.ialt);
  }
  if (d.type === 'article') {
    og('article:published_time', d.pub); og('article:modified_time', d.mod);
    og('article:author', d.author); og('article:section', d.section);
    d.tags.forEach(t => p.push(['property', 'article:tag', t]));
  }
  if (d.type === 'product') {
    og('product:price:amount', d.price); if (d.price) og('product:price:currency', d.cur);
    og('product:availability', d.avail); og('product:brand', d.brand);
  }
  tw('twitter:card', d.card); tw('twitter:site', d.twsite); tw('twitter:creator', d.twcreator);
  // X falls back to og:* for title/description/image, but alt text has no fallback
  if (d.img && d.ialt) tw('twitter:image:alt', d.ialt);
  return p;
}

function jsonLd(d) {
  const img = d.img || undefined;
  let o;
  if (d.type === 'article') {
    o = {'@context':'https://schema.org','@type':'Article', headline: d.title, description: d.desc, image: img,
         datePublished: d.pub || undefined, dateModified: d.mod || d.pub || undefined,
         author: d.author ? {'@type':'Person', name: d.author} : undefined,
         publisher: d.site ? {'@type':'Organization', name: d.site} : undefined,
         mainEntityOfPage: d.url || undefined, articleSection: d.section || undefined,
         keywords: d.tags.length ? d.tags.join(', ') : undefined};
  } else if (d.type === 'product') {
    const availMap = {'in stock':'InStock','out of stock':'OutOfStock','preorder':'PreOrder'};
    o = {'@context':'https://schema.org','@type':'Product', name: d.title, description: d.desc, image: img,
         brand: d.brand ? {'@type':'Brand', name: d.brand} : undefined,
         offers: d.price ? {'@type':'Offer', price: d.price, priceCurrency: d.cur,
                            availability: 'https://schema.org/' + (availMap[d.avail] || 'InStock'), url: d.url || undefined} : undefined};
  } else {
    o = {'@context':'https://schema.org','@type':'WebPage', name: d.title, description: d.desc, url: d.url || undefined,
         image: img, inLanguage: d.locale ? d.locale.replace('_','-') : undefined,
         isPartOf: d.site ? {'@type':'WebSite', name: d.site, url: d.url ? (()=>{try{return new URL(d.url).origin}catch(e){return undefined}})() : undefined} : undefined};
  }
  return JSON.stringify(o, (k, v) => (v === '' ? undefined : v), 2);
}

function codeHtml(d, withLd) {
  const lines = ['<!-- Open Graph / Social -->'];
  ogPairs(d).forEach(([a, k, v]) => lines.push(`<meta ${a}="${k}" content="${esc(v)}" />`));
  if (withLd) lines.push('', '<!-- Structured data -->', '<script type="application/ld+json">', jsonLd(d).replace(/<\//g,'<\\/'), '</' + 'script>');
  return lines.join('\n');
}

function codeNext(d, withLd) {
  const J = v => JSON.stringify(v);
  const L = [];
  L.push('// app/<route>/page.tsx  (Next.js App Router)');
  L.push("import type { Metadata } from 'next';", '');
  L.push('export const metadata: Metadata = {');
  if (d.title) L.push(`  title: ${J(d.title)},`);
  if (d.desc) L.push(`  description: ${J(d.desc)},`);
  if (d.url) L.push(`  alternates: { canonical: ${J(d.url)} },`);
  L.push('  openGraph: {');
  L.push(`    type: ${J(d.type === 'article' ? 'article' : 'website')},`);
  if (d.title) L.push(`    title: ${J(d.title)},`);
  if (d.desc) L.push(`    description: ${J(d.desc)},`);
  if (d.url) L.push(`    url: ${J(d.url)},`);
  if (d.site) L.push(`    siteName: ${J(d.site)},`);
  if (d.locale) L.push(`    locale: ${J(d.locale)},`);
  if (d.img) {
    const im = [`url: ${J(d.img)}`]; if (d.iw) im.push(`width: ${parseInt(d.iw)}`); if (d.ih) im.push(`height: ${parseInt(d.ih)}`);
    if (d.ialt) im.push(`alt: ${J(d.ialt)}`); if (d.itype) im.push(`type: ${J(d.itype)}`);
    L.push(`    images: [{ ${im.join(', ')} }],`);
  }
  if (d.type === 'article') {
    if (d.pub) L.push(`    publishedTime: ${J(d.pub)},`);
    if (d.mod) L.push(`    modifiedTime: ${J(d.mod)},`);
    if (d.author) L.push(`    authors: [${J(d.author)}],`);
    if (d.section) L.push(`    section: ${J(d.section)},`);
    if (d.tags.length) L.push(`    tags: ${J(d.tags)},`);
  }
  L.push('  },');
  L.push('  twitter: {');
  L.push(`    card: ${J(d.card)},`);
  if (d.twsite) L.push(`    site: ${J(d.twsite)},`);
  if (d.twcreator) L.push(`    creator: ${J(d.twcreator)},`);
  if (d.title) L.push(`    title: ${J(d.title)},`);
  if (d.desc) L.push(`    description: ${J(d.desc)},`);
  if (d.img) L.push(`    images: [${J(d.img)}],`);
  L.push('  },');
  if (d.type === 'product') {
    const other = [];
    if (d.price) { other.push(`'product:price:amount': ${J(d.price)}`); other.push(`'product:price:currency': ${J(d.cur)}`); }
    other.push(`'product:availability': ${J(d.avail)}`);
    if (d.brand) other.push(`'product:brand': ${J(d.brand)}`);
    L.push(`  other: { ${other.join(', ')} },`);
  }
  L.push('};');
  if (withLd) {
    L.push('', '// Add inside your page component:');
    L.push('const jsonLd = ' + jsonLd(d).split('\n').join('\n') + ';', '');
    L.push('// <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(jsonLd) }} />');
    if (d.type === 'product') L.push('// Note: Next.js has no built-in "product" og:type — the `other` block above adds the product:* tags.');
  }
  return L.join('\n');
}

function codeHelmet(d, withLd) {
  const L = ["import { Helmet } from 'react-helmet-async';", '', '<Helmet>'];
  if (d.title) L.push(`  <title>${esc(d.title)}</title>`);
  if (d.desc) L.push(`  <meta name="description" content="${esc(d.desc)}" />`);
  if (d.url) L.push(`  <link rel="canonical" href="${esc(d.url)}" />`);
  ogPairs(d).forEach(([a, k, v]) => L.push(`  <meta ${a}="${k}" content="${esc(v)}" />`));
  if (withLd) L.push(`  <script type="application/ld+json">{\`${jsonLd(d).replace(/`/g,'\\`').replace(/\$\{/g,'\\${')}\`}<` + `/script>`);
  L.push('</Helmet>');
  return L.join('\n');
}

function codeWp(d, withLd) {
  const v = x => x || '(fill this in above)';
  const L = [];
  L.push('WORDPRESS — you don\'t paste meta tags by hand; your SEO plugin writes them.');
  L.push('Copy each value below into the matching plugin field, then save/update the post.', '');
  L.push('━━ YOAST SEO ━━');
  L.push('Post editor → Yoast SEO box → "Social" tab (Yoast Premium also shows it in the sidebar)');
  L.push(`  Facebook title        →  ${v(d.title)}`);
  L.push(`  Facebook description  →  ${v(d.desc)}`);
  L.push(`  Facebook image        →  ${v(d.img)}  (upload to Media Library first)`);
  L.push(`  X title / description / image  →  same as above, or leave empty to reuse the Facebook values`);
  L.push('Site-wide: Yoast SEO → Settings → Site basics (site name) and Site representation / Social profiles (X username)');
  L.push(`  Site name   →  ${v(d.site)}`);
  L.push(`  X username  →  ${v(d.twsite)}`, '');
  L.push('━━ RANK MATH ━━');
  L.push('Post editor → Rank Math panel → General → "Edit Snippet" → "Social" tab');
  L.push(`  Facebook: Title → ${v(d.title)}`);
  L.push(`  Facebook: Description → ${v(d.desc)}`);
  L.push(`  Facebook: Add image → ${v(d.img)}`);
  L.push(`  X (Twitter): tick "Use Data from Facebook Tab", card type → ${d.card === 'summary' ? 'Summary Card' : 'Summary Card with Large Image'}`);
  L.push('Site-wide: Rank Math → Titles & Meta → Social Meta (X username, default share image)', '');
  if (d.type === 'article') L.push('Article dates, author and section are filled in automatically from the post — no extra step.', '');
  if (d.type === 'product') L.push('For products, WooCommerce + your SEO plugin output price/availability automatically from the product data.', '');
  L.push('Schema: both plugins already output JSON-LD. Only paste the block below (e.g. with a "Custom HTML"');
  L.push('block or a header-scripts plugin) if your plugin\'s schema is turned off — otherwise you\'ll get duplicates.');
  if (withLd) L.push('', '<script type="application/ld+json">', jsonLd(d), '</' + 'script>');
  return L.join('\n');
}

function currentCode() {
  const d = data(), ld = $('incJsonLd').checked;
  return {html: codeHtml, next: codeNext, helmet: codeHelmet, wp: codeWp}[state.fmt](d, ld);
}

// ---------------------------------------------------------------- previews
function previewImage(d) { return state.makerImage || (isAbsUrl(d.img) ? d.img : ''); }

function renderPreviews(d) {
  const img = previewImage(d);
  const dom = domainOf(d.url) || 'example.com';
  const title = d.title || '(your title)', desc = d.desc || '';
  const out = PLATFORMS.map(p => {
    if (p.key === 'google') {
      return `<div class="pcard google"><div class="phead"><span class="picon">${p.icon}</span> ${p.label}</div>
        <div class="pbody"><div class="pdomain">${esc(dom)}</div><div class="ptitle">${esc(trunc(title,p.t))}</div><div class="pdesc">${esc(trunc(desc,p.d))}</div></div></div>`;
    }
    if (p.key === 'twitter' && d.card === 'summary') {
      return `<div class="pcard small"><div class="phead"><span class="picon">${p.icon}</span> ${p.label} · small card</div>
        <div class="prow"><div class="pthumb">${img ? `<img src="${esc(img)}" alt="">` : 'No image'}</div>
        <div class="pbody"><div class="pdomain">${esc(dom)}</div><div class="ptitle">${esc(trunc(title,p.t))}</div><div class="pdesc">${esc(trunc(desc,p.d))}</div></div></div></div>`;
    }
    return `<div class="pcard"><div class="phead"><span class="picon">${p.icon}</span> ${p.label}</div>
      <div class="pimg" style="aspect-ratio:${p.ratio}">${img ? `<img src="${esc(img)}" alt="" loading="lazy">` : 'No image'}</div>
      <div class="pbody"><div class="pdomain">${esc(dom)}</div><div class="ptitle">${esc(trunc(title,p.t))}</div><div class="pdesc">${esc(trunc(desc,p.d))}</div></div></div>`;
  });
  $('previews').innerHTML = out.join('');
  $('quickPreview').innerHTML = out[0];
}

// ---------------------------------------------------------------- score ring
let ring = null;
function renderScore(ev) {
  const col = ev.score >= 80 ? '#6af7c8' : ev.score >= 50 ? '#f7a26a' : '#f76a7c';
  $('scoreNum').textContent = ev.score; $('scoreNum').style.color = col;
  if (window.Chart) {
    if (!ring) ring = new Chart($('ring'), {type:'doughnut',
      data:{datasets:[{data:[ev.score,100-ev.score],backgroundColor:[col,'#23233a'],borderWidth:0,cutout:'78%'}]},
      options:{animation:{duration:250},plugins:{legend:{display:false},tooltip:{enabled:false}}}});
    else { ring.data.datasets[0].data = [ev.score, 100-ev.score]; ring.data.datasets[0].backgroundColor = [col,'#23233a']; ring.update(); }
  }
  $('issues').innerHTML = ev.issues.length
    ? ev.issues.map(i => `<div class="issue ${i.sev}"><div class="m">${esc(i.m)}</div><div class="f">${esc(i.f)}</div></div>`).join('')
    : '<div class="all-good">✓ Everything looks good — copy the code below.</div>';
}

// ---------------------------------------------------------------- main refresh
function refresh() {
  const d = data();
  counter($('c_title'), d.title.length, 15, 60, 95);
  counter($('c_desc'), d.desc.length, 50, 160, 300);
  const ac = $('c_alt'); ac.className = 'cnt ' + (d.ialt ? 'ok' : 'warn'); ac.textContent = d.ialt ? `${d.ialt.length} chars` : 'recommended';
  renderScore(evaluate(d));
  renderPreviews(d);
  $('codeOut').textContent = currentCode();
  const canTest = isAbsUrl(d.url);
  $('testBtn').disabled = !canTest;
  $('testHint').textContent = canTest ? 'Publish the tags on your page first, then this checks the live URL.' : 'Add the Page URL to enable the live test.';
  drawCanvas();
}

// ---------------------------------------------------------------- image maker
function wrapLines(ctx, text, maxW) {
  const words = (text || '').split(/\s+/).filter(Boolean), lines = []; let line = '';
  words.forEach(w => { const t = line ? line + ' ' + w : w; if (ctx.measureText(t).width > maxW && line) { lines.push(line); line = w; } else line = t; });
  if (line) lines.push(line);
  return lines;
}

function drawCanvas() {
  const c = $('ogCanvas'), ctx = c.getContext('2d'), W = 1200, H = 630;
  const d = data();
  const head = $('m_title').value.trim() || d.title || 'Your headline goes here';
  const sub = $('m_sub').value.trim();
  const foot = $('m_foot').value.trim() || domainOf(d.url) || d.site || 'yourdomain.com';
  const bg1 = $('m_bg1').value, bg2 = $('m_bg2').value, fg = $('m_fg').value, acc = $('m_acc').value;
  const L = state.layout;

  const g = ctx.createLinearGradient(0, 0, W, H); g.addColorStop(0, bg1); g.addColorStop(1, bg2);
  ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
  // soft decorative circle
  ctx.globalAlpha = .12; ctx.fillStyle = acc; ctx.beginPath(); ctx.arc(W - 80, 90, 260, 0, Math.PI * 2); ctx.fill(); ctx.globalAlpha = 1;

  const pad = L === 'bar' ? 120 : 80;
  if (L === 'bar') { ctx.fillStyle = acc; ctx.fillRect(0, 0, 28, H); }
  const center = L === 'center';
  ctx.textAlign = center ? 'center' : 'left';
  const x = center ? W / 2 : pad, maxW = W - pad * 2 - (center ? 0 : 40);

  // fit headline in max 3 lines
  let size = 76, lines;
  do { ctx.font = `800 ${size}px Sora, Inter, sans-serif`; lines = wrapLines(ctx, head, maxW); size -= 4; } while ((lines.length > 3) && size > 36);
  if (lines.length > 3) { lines = lines.slice(0, 3); lines[2] = lines[2].replace(/\s*\S*$/, '') + '…'; }
  size += 4;
  const lh = size * 1.18;
  ctx.font = '500 30px Inter, sans-serif';
  const subLines = sub ? wrapLines(ctx, sub, maxW).slice(0, 2) : [];
  const blockH = lines.length * lh + (subLines.length ? 24 + subLines.length * 40 : 0);
  let y = (H - blockH) / 2 + size * 0.85 - 20;

  if (L !== 'center') { ctx.fillStyle = acc; ctx.fillRect(x, y - size - 18, 90, 8); }
  ctx.fillStyle = fg; ctx.font = `800 ${size}px Sora, Inter, sans-serif`;
  lines.forEach(l => { ctx.fillText(l, x, y); y += lh; });
  if (subLines.length) {
    y += 14; ctx.globalAlpha = .85; ctx.font = '500 30px Inter, sans-serif';
    subLines.forEach(l => { ctx.fillText(l, x, y); y += 40; }); ctx.globalAlpha = 1;
  }
  // footer
  ctx.font = '500 24px "DM Mono", monospace'; ctx.fillStyle = fg; ctx.globalAlpha = .8;
  ctx.textAlign = center ? 'center' : 'left';
  ctx.fillText(foot, center ? W / 2 : pad, H - 56); ctx.globalAlpha = 1;
  // logo
  if (state.logo) {
    const lg = state.logo, maxH = 70, maxLW = 220, r = Math.min(maxH / lg.height, maxLW / lg.width, 1);
    const lw = lg.width * r, lhh = lg.height * r;
    ctx.drawImage(lg, center ? (W - lw) / 2 : W - pad - lw, center ? 50 : H - 56 - lhh + 10, lw, lhh);
  }
}

// ---------------------------------------------------------------- auto-fill
async function autofill() {
  const u = $('fetchUrl').value.trim(), msg = $('fetchMsg');
  if (!u) { msg.className = 'msg err'; msg.textContent = 'Paste a URL first.'; return; }
  msg.className = 'msg'; msg.style.color = 'var(--muted)'; msg.textContent = 'Reading the page…';
  $('fetchBtn').disabled = true;
  try {
    const r = await fetch('/api/fetch?url=' + encodeURIComponent(u)); const j = await r.json();
    msg.style.color = '';
    if (!j.ok) { msg.className = 'msg err'; msg.textContent = j.error || 'Could not read that page.'; return; }
    const og = j.og || {}, tw = j.twitter || {}; let filled = 0;
    const set = (id, v) => { if (v) { $(id).value = v; filled++; } };
    const t = og['og:type'] || '';
    setType(t.startsWith('article') ? 'article' : (t.startsWith('product') ? 'product' : 'website'));
    set('f_title', og['og:title'] || tw['twitter:title'] || j.title_tag);
    set('f_desc', og['og:description'] || tw['twitter:description'] || j.meta_description);
    set('f_url', og['og:url'] || j.canonical || j.final_url);
    set('f_site', og['og:site_name']);
    if (og['og:locale']) { const sel = $('f_locale'); if (![...sel.options].some(o => o.value === og['og:locale'])) sel.add(new Option(og['og:locale'], og['og:locale'])); sel.value = og['og:locale']; }
    set('f_img', og['og:image'] || tw['twitter:image']);
    set('f_ialt', og['og:image:alt'] || tw['twitter:image:alt']);
    if (tw['twitter:card'] === 'summary') $('f_card').value = 'summary';
    set('f_twsite', tw['twitter:site']); set('f_twcreator', tw['twitter:creator']);
    set('f_author', og['article:author'] || j.author); set('f_section', og['article:section']);
    if (og['article:published_time']) $('f_pub').value = toLocalInput(og['article:published_time']);
    if (og['article:modified_time']) $('f_mod').value = toLocalInput(og['article:modified_time']);
    set('f_price', og['product:price:amount']); set('f_brand', og['product:brand']);
    if (og['product:price:currency']) $('f_cur').value = og['product:price:currency'];
    if (j.image_info) applyImgInfo(j.image_info);
    else { $('f_iw').value = og['og:image:width'] || ''; $('f_ih').value = og['og:image:height'] || ''; }
    state.makerImage = null;
    const missing = ['og:title','og:description','og:image','og:url'].filter(k => !og[k]);
    msg.className = 'msg ok';
    msg.textContent = `✓ Loaded ${filled} fields from ${domainOf(j.final_url)}.` + (missing.length ? ` This page was missing: ${missing.join(', ')} — filled from the page's title/description where possible.` : '');
    refresh();
  } catch (e) { msg.className = 'msg err'; msg.textContent = 'Something went wrong reading that page.'; }
  finally { $('fetchBtn').disabled = false; }
}

function applyImgInfo(info) {
  const el = $('imgInfo');
  if (!info || !info.ok) { el.style.color = 'var(--red)'; el.textContent = '✕ ' + ((info && info.error) || 'Could not read the image.'); return; }
  if (info.width) $('f_iw').value = info.width;
  if (info.height) $('f_ih').value = info.height;
  if (info.content_type && [...$('f_itype').options].some(o => o.value === info.content_type)) $('f_itype').value = info.content_type;
  const kb = info.size_bytes ? Math.round(info.size_bytes / 1024) : null;
  const good = info.width >= 1200 && info.height >= 600;
  el.style.color = good ? 'var(--mint)' : 'var(--orange)';
  el.textContent = `${good ? '✓' : '!'} ${info.width || '?'} × ${info.height || '?'}px` + (kb ? ` · ${kb} KB` : '') + (info.content_type ? ` · ${info.content_type}` : '') + (good ? '' : ' — 1200 × 630px recommended');
}

async function checkImg() {
  const u = $('f_img').value.trim(); if (!u) return;
  $('imgInfo').style.color = 'var(--muted)'; $('imgInfo').textContent = 'Reading image…';
  try { const r = await fetch('/api/image?url=' + encodeURIComponent(u)); applyImgInfo(await r.json()); refresh(); }
  catch (e) { $('imgInfo').style.color = 'var(--red)'; $('imgInfo').textContent = '✕ Could not check the image.'; }
}

// ---------------------------------------------------------------- wiring
function setType(t) {
  state.type = t;
  document.querySelectorAll('#typeSeg button').forEach(b => b.classList.toggle('active', b.dataset.type === t));
  $('tf_article').classList.toggle('show', t === 'article');
  $('tf_product').classList.toggle('show', t === 'product');
}
document.querySelectorAll('#typeSeg button').forEach(b => b.onclick = () => { setType(b.dataset.type); refresh(); });
document.querySelectorAll('#codeSeg button').forEach(b => b.onclick = () => {
  state.fmt = b.dataset.fmt; document.querySelectorAll('#codeSeg button').forEach(x => x.classList.toggle('active', x === b)); refresh(); });
document.querySelectorAll('#layoutSeg button').forEach(b => b.onclick = () => {
  state.layout = b.dataset.layout; document.querySelectorAll('#layoutSeg button').forEach(x => x.classList.toggle('active', x === b)); drawCanvas();
  if (state.makerImage) { state.makerImage = $('ogCanvas').toDataURL('image/png'); renderPreviews(data()); } });

document.querySelectorAll('input,textarea,select').forEach(el => {
  if (el.id === 'fetchUrl' || el.type === 'file') return;
  el.addEventListener('input', () => {
    if (el.id === 'f_img') { state.makerImage = null; $('imgInfo').textContent = ''; }
    refresh();
    if (state.makerImage && el.id.startsWith('m_')) { state.makerImage = $('ogCanvas').toDataURL('image/png'); renderPreviews(data()); }
  });
});
$('f_img').addEventListener('change', checkImg);
$('imgCheckBtn').onclick = checkImg;
$('fetchBtn').onclick = autofill;
$('fetchUrl').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); autofill(); } });

$('m_logo').onchange = e => {
  const f = e.target.files[0]; if (!f) { state.logo = null; drawCanvas(); return; }
  const rd = new FileReader();
  rd.onload = () => { const im = new Image(); im.onload = () => { state.logo = im; drawCanvas(); if (state.makerImage) { state.makerImage = $('ogCanvas').toDataURL('image/png'); renderPreviews(data()); } }; im.src = rd.result; };
  rd.readAsDataURL(f);
};
$('dlPng').onclick = () => {
  drawCanvas();
  const a = document.createElement('a');
  const slug = (($('m_title').value || data().title || 'og-image').toLowerCase().replace(/[^a-z0-9]+/g,'-').replace(/^-|-$/g,'').slice(0,50)) || 'og-image';
  a.download = slug + '-1200x630.png'; a.href = $('ogCanvas').toDataURL('image/png'); a.click();
};
$('usePreview').onclick = () => {
  drawCanvas(); state.makerImage = $('ogCanvas').toDataURL('image/png');
  if (!$('f_iw').value) $('f_iw').value = 1200; if (!$('f_ih').value) $('f_ih').value = 630;
  if (!$('f_itype').value) $('f_itype').value = 'image/png';
  refresh(); $('previews').scrollIntoView({behavior:'smooth'});
};

$('copyCode').onclick = async () => {
  const txt = $('codeOut').textContent;
  try { await navigator.clipboard.writeText(txt); }
  catch (e) { const t = document.createElement('textarea'); t.value = txt; document.body.appendChild(t); t.select(); document.execCommand('copy'); t.remove(); }
  $('copyMsg').textContent = '✓ Copied'; setTimeout(() => $('copyMsg').textContent = '', 1800);
};
$('testBtn').onclick = () => {
  const u = data().url; if (!isAbsUrl(u)) return;
  window.open(CHECKER_URL.replace(/\/$/,'') + '/?url=' + encodeURIComponent(u), '_blank', 'noopener');
};

// Pre-fill from ?url= so other tools can link straight into auto-fill
(function init() {
  setType('website');
  const q = new URLSearchParams(location.search).get('url');
  const start = () => { refresh(); if (q) { $('fetchUrl').value = q; autofill(); } };
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(start); else start();
  // make sure the canvas redraws once web fonts actually arrive
  if (document.fonts) document.fonts.load('800 40px Sora').then(drawCanvas).catch(() => {});
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(debug=True, port=8501)
