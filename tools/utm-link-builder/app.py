"""
UTM Link Builder  -  Flask app
Build clean, GA4-ready campaign links in seconds.

  * Live link builder with all 9 GA4 UTM fields (the core 5 + id + 3 newer ones)
  * One-click presets: Google Ads, Meta Ads, Instagram, LinkedIn, WhatsApp, YouTube, Email, SMS...
  * GA4 channel predictor - shows which Default Channel Group the link will land in
  * Naming hygiene: auto lowercase + spaces -> hyphens, PII warning, special-character warning
  * QR code (PNG / SVG) for print and offline campaigns
  * Landing-page check - follows redirects and confirms your UTM tags survive them
  * Recent links saved in the browser, with CSV export

Almost everything runs in the browser. The server only does two small jobs:
  /api/qr     - make a QR code image
  /api/check  - follow the link's redirects and see whether the UTM tags survive

Built by Mahalakshmi Marimuthu - Digital Marketing Strategist & AI-Powered SEO Expert
"""
import io
import ipaddress
import os
import re
import socket
from urllib.parse import parse_qsl, urljoin, urlparse

import requests
import segno
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

REQUEST_TIMEOUT = 12
MAX_HOPS = 10
MAX_QR_CHARS = 1500
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
# Set ALLOW_PRIVATE=1 only for local testing against 127.0.0.1
ALLOW_PRIVATE = os.environ.get("ALLOW_PRIVATE") == "1"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def normalize_url(raw):
    raw = (raw or "").strip()
    if raw and not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    return raw


def is_public_http(url):
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        if ALLOW_PRIVATE:
            return True
        for info in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
        return True
    except Exception:
        return False


def utm_params(url):
    return {k.lower(): v for k, v in parse_qsl(urlparse(url).query, keep_blank_values=True)
            if k.lower().startswith("utm_")}


def follow(url):
    """Follow redirects one hop at a time so we can show the whole chain."""
    chain = []
    current = url
    session = requests.Session()
    headers = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9",
               "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
    for _ in range(MAX_HOPS + 1):
        if not is_public_http(current):
            return chain, "This link points to an address that can't be checked."
        try:
            r = session.get(current, headers=headers, timeout=REQUEST_TIMEOUT,
                            allow_redirects=False, stream=True)
            r.close()
        except requests.exceptions.Timeout:
            return chain, "The page took too long to respond."
        except requests.exceptions.RequestException:
            return chain, "Could not connect to this URL."
        chain.append({"url": current, "status": r.status_code})
        loc = r.headers.get("Location")
        if r.is_redirect and loc:
            current = urljoin(current, loc)
            continue
        return chain, None
    return chain, f"More than {MAX_HOPS} redirects - the page may be stuck in a redirect loop."


def check_link(url):
    sent = utm_params(url)
    chain, error = follow(url)
    if error and not chain:
        return {"ok": False, "error": error}

    final = chain[-1]
    got = utm_params(final["url"])
    missing = [k for k in sent if k not in got]
    changed = [k for k in sent if k in got and got[k] != sent[k]]
    redirects = len(chain) - 1
    start_host = (urlparse(url).hostname or "").lower()
    final_host = (urlparse(final["url"]).hostname or "").lower()

    if error:
        verdict, text = "warn", error
    elif final["status"] in (401, 403, 429, 503):
        verdict = "info"
        text = (f"The site answered HTTP {final['status']} - it is probably blocking automated checks. "
                "Open the link in your browser to confirm it works.")
    elif final["status"] >= 400:
        verdict = "fail"
        text = f"The landing page returns HTTP {final['status']} - visitors will hit an error page."
    elif missing:
        verdict = "fail"
        text = ("A redirect dropped your UTM tags (" + ", ".join(missing) + "). "
                "GA4 won't credit this campaign. Link to the final URL directly instead.")
    elif changed:
        verdict = "warn"
        text = "A redirect changed the value of " + ", ".join(changed) + "."
    elif redirects:
        verdict = "pass"
        text = f"Your UTM tags survive {redirects} redirect{'s' if redirects > 1 else ''}. " \
               "Linking to the final URL directly is still a little faster."
    else:
        verdict = "pass"
        text = "The page loads directly and your UTM tags arrive intact."

    return {
        "ok": True,
        "verdict": verdict,
        "text": text,
        "chain": chain,
        "final_url": final["url"],
        "final_status": final["status"],
        "redirects": redirects,
        "cross_domain": bool(start_host and final_host and
                             start_host.removeprefix("www.") != final_host.removeprefix("www.")),
    }


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/api/check")
def api_check():
    url = normalize_url(request.args.get("url", ""))
    if not url or not is_public_http(url):
        return jsonify({"ok": False, "error": "Please build a valid public link first."}), 400
    return jsonify(check_link(url))


@app.route("/api/qr")
def api_qr():
    data = (request.args.get("data") or "").strip()
    fmt = request.args.get("fmt", "svg")
    if not data:
        return jsonify({"ok": False, "error": "Nothing to encode."}), 400
    if len(data) > MAX_QR_CHARS:
        return jsonify({"ok": False, "error": "This link is too long for a reliable QR code."}), 400
    qr = segno.make(data, error="m")
    buf = io.BytesIO()
    if fmt == "png":
        qr.save(buf, kind="png", scale=12, border=3)
        mime, ext = "image/png", "png"
    else:
        qr.save(buf, kind="svg", scale=8, border=3, xmldecl=False)
        mime, ext = "image/svg+xml", "svg"
    resp = Response(buf.getvalue(), mimetype=mime)
    if request.args.get("dl"):
        resp.headers["Content-Disposition"] = f'attachment; filename="utm-qr-code.{ext}"'
    return resp


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
<title>UTM Link Builder – Clean, GA4-Ready Campaign URLs</title>
<meta name="description" content="Free UTM Link Builder: build clean campaign URLs with all GA4 UTM fields, one-click presets, a GA4 channel predictor, naming checks, QR codes and a redirect check.">
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
  .divider{height:1px;background:var(--border);margin:1.1rem 0}

  label.fl{display:flex;justify-content:space-between;gap:.5rem;font-size:.76rem;color:var(--muted);margin-bottom:.35rem}
  label.fl code{font-family:'DM Mono',monospace;font-size:.64rem;opacity:.8}
  label.fl .req{color:var(--orange)}
  input[type=text]{width:100%;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:.6rem .85rem;font-family:'Inter',sans-serif;font-size:.86rem}
  input:focus{outline:none;border-color:var(--accent)}
  .field{margin-bottom:.95rem}
  .fgrid{display:grid;grid-template-columns:1fr 1fr;gap:0 .8rem}
  .hint{font-size:.7rem;color:var(--muted);margin-top:.3rem;line-height:1.5}

  .chips{display:flex;gap:.4rem;flex-wrap:wrap}
  .chip{background:var(--surface2);border:1px solid var(--border);color:var(--muted);font-size:.74rem;padding:.32rem .75rem;border-radius:100px;cursor:pointer;font-family:'Inter',sans-serif;transition:border-color .15s,color .15s}
  .chip:hover{border-color:rgba(124,106,247,.5);color:var(--text)}
  .chip.on{border-color:rgba(124,106,247,.6);color:var(--accent);background:rgba(124,106,247,.1)}

  .more-toggle{background:none;border:none;color:var(--accent);font-size:.78rem;font-weight:600;cursor:pointer;font-family:'Inter',sans-serif;padding:0;display:inline-flex;align-items:center;gap:.35rem}
  .more-toggle .arr{transition:transform .2s;display:inline-block}
  .more-toggle.open .arr{transform:rotate(90deg)}
  .more{display:none;margin-top:.9rem}
  .more.show{display:block}

  .opts{display:flex;align-items:center;gap:1rem;flex-wrap:wrap;font-size:.78rem;color:var(--muted)}
  .switch{display:inline-flex;align-items:center;gap:.5rem;cursor:pointer;user-select:none}
  .switch input{display:none}
  .switch .track{width:32px;height:18px;border-radius:18px;background:var(--surface2);border:1px solid var(--border);position:relative;transition:background .2s}
  .switch .track::after{content:'';position:absolute;top:2px;left:2px;width:12px;height:12px;border-radius:50%;background:var(--muted);transition:transform .2s,background .2s}
  .switch input:checked + .track{background:rgba(124,106,247,.25);border-color:rgba(124,106,247,.5)}
  .switch input:checked + .track::after{transform:translateX(14px);background:var(--accent)}
  .tabs{display:inline-flex;background:var(--surface2);border:1px solid var(--border);border-radius:9px;padding:3px;gap:3px}
  .tabs button{background:none;border:1px solid transparent;color:var(--muted);font-family:'DM Mono',monospace;font-size:.74rem;padding:.2rem .7rem;border-radius:6px;cursor:pointer}
  .tabs button.on{background:rgba(124,106,247,.14);border-color:rgba(124,106,247,.4);color:var(--accent)}
  .tabs.off{opacity:.4;pointer-events:none}

  .btn{display:inline-flex;align-items:center;gap:.45rem;background:var(--accent);color:#fff;border:none;border-radius:8px;padding:.62rem 1.15rem;font-size:.84rem;font-weight:600;cursor:pointer;font-family:'Inter',sans-serif;white-space:nowrap;text-decoration:none;line-height:1.3}
  .btn:hover{background:var(--accent-h)}
  .btn.ghost{background:var(--surface2);border:1px solid var(--border);color:var(--text)}
  .btn.ghost:hover{border-color:var(--accent)}
  .btn.sm{padding:.38rem .75rem;font-size:.74rem}
  .btn:disabled,.btn.dis{opacity:.4;cursor:not-allowed;pointer-events:none}
  .actions{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center}

  .layout{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:1.2rem;align-items:start}
  .sticky{position:sticky;top:1rem}

  /* result */
  .status{font-family:'DM Mono',monospace;font-size:.64rem;padding:.2rem .6rem;border-radius:100px;white-space:nowrap}
  .status.ready{background:rgba(106,247,200,.1);border:1px solid rgba(106,247,200,.35);color:var(--mint)}
  .status.fix{background:rgba(247,162,106,.1);border:1px solid rgba(247,162,106,.35);color:var(--orange)}
  .status.wait{background:var(--surface2);border:1px solid var(--border);color:var(--muted)}
  .linkbox{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:.9rem 1rem;font-family:'DM Mono',monospace;font-size:.8rem;line-height:1.7;word-break:break-all;min-height:74px;margin-bottom:.9rem}
  .linkbox .base{color:var(--text)}
  .linkbox .sep{color:var(--muted)}
  .linkbox .k{color:var(--muted)}
  .linkbox .v{color:var(--mint)}
  .linkbox .empty{color:var(--muted);font-family:'Inter',sans-serif;font-size:.8rem;word-break:normal}

  .channel{display:flex;gap:.8rem;align-items:flex-start;background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:.8rem .9rem;margin-top:1rem}
  .channel .ci{font-size:1.1rem;line-height:1.3}
  .channel .ct{font-size:.72rem;color:var(--muted);font-family:'DM Mono',monospace;letter-spacing:.06em;text-transform:uppercase}
  .channel .cn{font-family:'Sora',sans-serif;font-weight:700;font-size:.98rem;margin:.1rem 0 .15rem}
  .channel .cn.good{color:var(--mint)} .channel .cn.bad{color:var(--orange)} .channel .cn.none{color:var(--muted)}
  .channel .cw{font-size:.75rem;color:var(--muted);line-height:1.5}

  .issues{display:flex;flex-direction:column;gap:.45rem;margin-top:.8rem}
  .iss{display:flex;gap:.65rem;align-items:flex-start;font-size:.78rem;line-height:1.5;padding:.55rem .75rem;border-radius:9px;border:1px solid var(--border);background:var(--surface2)}
  .iss .ic{width:20px;height:20px;border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:.68rem;font-weight:700;flex-shrink:0;margin-top:.05rem}
  .iss.fail .ic{background:rgba(247,106,106,.14);color:var(--red)}
  .iss.warn .ic{background:rgba(247,162,106,.14);color:var(--orange)}
  .iss.info .ic{background:rgba(124,106,247,.14);color:var(--accent)}
  .iss.pass .ic{background:rgba(106,247,200,.12);color:var(--mint)}
  .iss b{font-weight:600;color:var(--text)}
  .iss span{color:var(--muted)}
  .passed{font-size:.74rem;color:var(--mint);margin-top:.6rem;font-family:'DM Mono',monospace}

  .panel{display:none;margin-top:1rem;border-top:1px solid var(--border);padding-top:1rem}
  .panel.show{display:block}
  .panel h3{font-family:'Sora',sans-serif;font-size:.82rem;font-weight:700;margin-bottom:.7rem}
  .qrwrap{display:flex;gap:1rem;align-items:center;flex-wrap:wrap}
  .qrwrap .qr{width:150px;height:150px;background:#fff;border-radius:10px;padding:6px;display:flex;align-items:center;justify-content:center}
  .qrwrap .qr img{width:100%;height:100%}
  .chain{font-family:'DM Mono',monospace;font-size:.7rem;color:var(--muted);margin-top:.6rem;line-height:1.8;word-break:break-all}
  .chain .code{display:inline-block;min-width:34px;padding:0 .35rem;border-radius:4px;background:var(--surface2);border:1px solid var(--border);text-align:center;margin-right:.4rem;color:var(--text)}
  .spin{display:inline-block;width:12px;height:12px;border:2px solid rgba(255,255,255,.3);border-top-color:#fff;border-radius:50%;animation:sp .7s linear infinite}
  @keyframes sp{to{transform:rotate(360deg)}}

  /* history */
  .tbl-wrap{overflow-x:auto}
  table{width:100%;border-collapse:collapse;font-size:.78rem}
  th{text-align:left;font-family:'DM Mono',monospace;font-size:.62rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);font-weight:500;padding:.5rem .6rem;border-bottom:1px solid var(--border);white-space:nowrap}
  td{padding:.55rem .6rem;border-bottom:1px solid var(--border);vertical-align:middle}
  td.url{font-family:'DM Mono',monospace;font-size:.7rem;color:var(--muted);max-width:360px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  td.act{white-space:nowrap;text-align:right}
  .badge{font-family:'DM Mono',monospace;font-size:.62rem;padding:.15rem .55rem;border-radius:100px;white-space:nowrap;display:inline-block;background:rgba(124,106,247,.12);border:1px solid rgba(124,106,247,.4);color:var(--accent)}
  .badge.bad{background:rgba(247,162,106,.1);border-color:rgba(247,162,106,.35);color:var(--orange)}
  .empty-hist{font-size:.8rem;color:var(--muted);padding:.4rem 0}
  .toast{position:fixed;bottom:1.4rem;left:50%;transform:translateX(-50%) translateY(20px);background:var(--surface);border:1px solid rgba(106,247,200,.4);color:var(--mint);padding:.55rem 1.1rem;border-radius:100px;font-size:.8rem;opacity:0;transition:all .25s;pointer-events:none;z-index:50}
  .toast.show{opacity:1;transform:translateX(-50%) translateY(0)}

  @media(max-width:1100px){.layout{grid-template-columns:minmax(0,1fr)}.sticky{position:static}}
  @media(max-width:960px){.sidebar{display:none}.main{margin-left:0;padding:1.2rem}}
  @media(max-width:560px){.fgrid{grid-template-columns:1fr}}
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
      <a href="#" class="sidebar-link active">🏷️&nbsp; UTM Link Builder</a>
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
        Keep names <b style="color:var(--text)">lowercase</b> — GA4 treats “Facebook” and “facebook” as two sources.<br>
        Never tag links <b style="color:var(--text)">inside your own site</b> — it restarts the session.<br>
        Never put emails or phone numbers in UTMs.
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
      <h1>🏷️ UTM Link <span>Builder</span></h1>
      <div class="sub">Build clean campaign links — and see exactly how GA4 will report them.</div>
    </div>

    <div class="layout">
      <!-- ============ LEFT: form ============ -->
      <div>
        <div class="card">
          <h2><span class="step">1</span>Page you're linking to</h2>
          <div class="field" style="margin-bottom:0">
            <input type="text" id="f_url" placeholder="https://www.yourwebsite.com/landing-page" autocomplete="off" spellcheck="false">
            <div class="hint" id="urlHint">Paste the full address of the page. Existing UTM tags on it will be replaced.</div>
          </div>
        </div>

        <div class="card">
          <h2><span class="step">2</span>Where will you share it? <span class="hint right" style="margin:0">optional quick fill</span></h2>
          <div class="chips" id="presets"></div>
        </div>

        <div class="card">
          <h2><span class="step">3</span>Campaign details</h2>
          <div class="fgrid">
            <div class="field">
              <label class="fl" for="f_source"><span>Source <span class="req">*</span></span><code>utm_source</code></label>
              <input type="text" id="f_source" list="dl_source" placeholder="e.g. google, newsletter" autocomplete="off">
            </div>
            <div class="field">
              <label class="fl" for="f_medium"><span>Medium <span class="req">*</span></span><code>utm_medium</code></label>
              <input type="text" id="f_medium" list="dl_medium" placeholder="e.g. cpc, email, social" autocomplete="off">
            </div>
          </div>
          <div class="field">
            <label class="fl" for="f_campaign"><span>Campaign name <span class="req">*</span></span><code>utm_campaign</code></label>
            <input type="text" id="f_campaign" placeholder="e.g. diwali-sale-2026" autocomplete="off">
          </div>
          <div class="fgrid">
            <div class="field">
              <label class="fl" for="f_content"><span>Content</span><code>utm_content</code></label>
              <input type="text" id="f_content" placeholder="e.g. banner-top, cta-button" autocomplete="off">
            </div>
            <div class="field">
              <label class="fl" for="f_term"><span>Term</span><code>utm_term</code></label>
              <input type="text" id="f_term" placeholder="e.g. paid keyword" autocomplete="off">
            </div>
          </div>

          <button type="button" class="more-toggle" id="moreBtn"><span class="arr">▸</span> More GA4 fields</button>
          <div class="more" id="more">
            <div class="fgrid">
              <div class="field">
                <label class="fl" for="f_id"><span>Campaign ID</span><code>utm_id</code></label>
                <input type="text" id="f_id" placeholder="e.g. 20260924" autocomplete="off">
              </div>
              <div class="field">
                <label class="fl" for="f_source_platform"><span>Source platform</span><code>utm_source_platform</code></label>
                <input type="text" id="f_source_platform" placeholder="e.g. google-ads, meta" autocomplete="off">
              </div>
              <div class="field">
                <label class="fl" for="f_creative_format"><span>Creative format</span><code>utm_creative_format</code></label>
                <input type="text" id="f_creative_format" placeholder="e.g. video, carousel" autocomplete="off">
              </div>
              <div class="field">
                <label class="fl" for="f_marketing_tactic"><span>Marketing tactic</span><code>utm_marketing_tactic</code></label>
                <input type="text" id="f_marketing_tactic" placeholder="e.g. prospecting, remarketing" autocomplete="off">
              </div>
            </div>
          </div>

          <div class="divider"></div>
          <div class="opts">
            <label class="switch"><input type="checkbox" id="optClean" checked><span class="track"></span>Clean values (lowercase, no spaces)</label>
            <span style="display:inline-flex;align-items:center;gap:.5rem">Spaces become
              <span class="tabs" id="sepTabs"><button type="button" class="on" data-sep="-">-</button><button type="button" data-sep="_">_</button></span>
            </span>
            <button type="button" class="btn ghost sm" id="resetBtn" style="margin-left:auto">Clear form</button>
          </div>
        </div>
      </div>

      <!-- ============ RIGHT: result ============ -->
      <div class="sticky">
        <div class="card">
          <h2>🔗 Your campaign link <span class="right status wait" id="status">waiting</span></h2>
          <div class="linkbox" id="linkbox"></div>
          <div class="actions">
            <button type="button" class="btn" id="copyBtn">📋 Copy link</button>
            <a class="btn ghost" id="openBtn" target="_blank" rel="noopener">Open ↗</a>
            <button type="button" class="btn ghost" id="qrBtn">▦ QR code</button>
            <button type="button" class="btn ghost" id="checkBtn">🧭 Test link</button>
          </div>

          <div class="channel">
            <div class="ci">📊</div>
            <div>
              <div class="ct">GA4 will report this as</div>
              <div class="cn none" id="chName">—</div>
              <div class="cw" id="chWhy">Add a source and medium to see the channel.</div>
            </div>
          </div>

          <div class="issues" id="issues"></div>
          <div class="passed" id="passed"></div>

          <div class="panel" id="qrPanel">
            <h3>QR code</h3>
            <div class="qrwrap">
              <div class="qr"><img id="qrImg" alt="QR code for your campaign link"></div>
              <div>
                <div class="hint" style="margin:0 0 .7rem;max-width:240px">Scan-tested format with medium error correction. Use it on print, posters or packaging.</div>
                <div class="actions">
                  <a class="btn sm" id="qrPng">⬇ PNG</a>
                  <a class="btn ghost sm" id="qrSvg">⬇ SVG</a>
                </div>
              </div>
            </div>
          </div>

          <div class="panel" id="checkPanel">
            <h3>Landing page check</h3>
            <div id="checkOut"></div>
          </div>
        </div>
      </div>
    </div>

    <div class="card" style="margin-top:1.2rem">
      <h2>🕘 Recent links <span class="hint" style="margin:0">saved in this browser when you copy a link</span>
        <span class="right actions">
          <button type="button" class="btn ghost sm" id="csvBtn">⬇ CSV</button>
          <button type="button" class="btn ghost sm" id="clearHist">Clear</button>
        </span>
      </h2>
      <div class="tbl-wrap"><div id="hist"></div></div>
    </div>
  </main>
</div>

<datalist id="dl_source">
  <option value="google"><option value="bing"><option value="facebook"><option value="instagram">
  <option value="linkedin"><option value="x"><option value="youtube"><option value="whatsapp">
  <option value="telegram"><option value="newsletter"><option value="sms"><option value="partner">
</datalist>
<datalist id="dl_medium">
  <option value="cpc"><option value="paid-social"><option value="social"><option value="email">
  <option value="sms"><option value="display"><option value="video"><option value="affiliate">
  <option value="referral"><option value="push"><option value="organic"><option value="audio">
</datalist>

<div class="toast" id="toast"></div>

<script>
const $ = id => document.getElementById(id);
const FIELDS = ['source','medium','campaign','id','term','content','source_platform','creative_format','marketing_tactic'];
const ORDER  = ['source','medium','campaign','id','term','content','source_platform','creative_format','marketing_tactic'];
const REQUIRED = ['source','medium','campaign'];
let SEP = '-';

// ------------------------------------------------------------ presets
const PRESETS = [
  {n:'Google Ads',  s:'google',    m:'cpc',         p:'google-ads'},
  {n:'Meta Ads',    s:'facebook',  m:'paid-social', p:'meta'},
  {n:'Instagram',   s:'instagram', m:'social'},
  {n:'LinkedIn',    s:'linkedin',  m:'social'},
  {n:'X / Twitter', s:'x',         m:'social'},
  {n:'WhatsApp',    s:'whatsapp',  m:'social'},
  {n:'YouTube',     s:'youtube',   m:'video'},
  {n:'Email',       s:'newsletter',m:'email'},
  {n:'SMS',         s:'sms',       m:'sms'},
  {n:'Display Ads', s:'google',    m:'display',     p:'google-ads'},
  {n:'Affiliate',   s:'',          m:'affiliate'},
];
function drawPresets() {
  $('presets').innerHTML = PRESETS.map((p, i) => `<button type="button" class="chip" data-i="${i}">${p.n}</button>`).join('');
  document.querySelectorAll('#presets .chip').forEach(b => b.onclick = () => {
    const p = PRESETS[+b.dataset.i];
    $('f_source').value = p.s; $('f_medium').value = p.m;
    if (p.p) { $('f_source_platform').value = p.p; }
    else if ($('f_source_platform').value) { $('f_source_platform').value = ''; }
    if (!p.s) $('f_source').focus(); else if (!$('f_campaign').value) $('f_campaign').focus();
    render();
  });
}
function markPreset(s, m) {
  document.querySelectorAll('#presets .chip').forEach(b => {
    const p = PRESETS[+b.dataset.i];
    b.classList.toggle('on', !!s && p.m === m && (p.s === s || (!p.s && m === 'affiliate')));
  });
}

// ------------------------------------------------------------ GA4 channel rules
// Based on Google's GA4 Default Channel Group definitions (evaluated top to bottom).
const SEARCH = ['google','bing','yahoo','baidu','duckduckgo','yandex','ecosia','naver','ask','aol','qwant','seznam','sogou','daum','msn','startpage','brave','search.brave.com'];
const SOCIAL = ['facebook','fb','instagram','ig','linkedin','lnkd.in','twitter','t.co','x','pinterest','reddit','quora','tiktok','snapchat','whatsapp','telegram','threads','discord','tumblr','messenger','wechat','weibo','vk','line','mastodon','bsky','bluesky','medium'];
const VIDEO  = ['youtube','youtu.be','vimeo','twitch','dailymotion','wistia','ted','netflix'];
const SHOP   = ['amazon','ebay','etsy','shopify','walmart','igshopping','google shopping'];
const PAID_RX = /^(.*cp.*|ppc|retargeting|paid.*)$/;
const SHOPC_RX = /^(.*(([^a-df-z]|^)shop|shopping).*)$/;
const EMAIL_RX = /^(email|e-mail|e_mail|e mail)$/;
const SOCIAL_M = ['social','social-network','social-media','sm','social network','social media'];
const DISPLAY_M = ['display','banner','expandable','interstitial','cpm'];

function inList(src, list) {
  const s = src.replace(/^(www|m|l|lm|mobile)\./, '');
  return list.some(x => s === x || s.startsWith(x + '.'));
}
function predict(s, m, c) {
  s = (s||'').toLowerCase(); m = (m||'').toLowerCase(); c = (c||'').toLowerCase();
  if (!s && !m) return null;
  const paid = PAID_RX.test(m);
  const isS = inList(s, SEARCH), isSo = inList(s, SOCIAL), isV = inList(s, VIDEO);
  const isSh = inList(s, SHOP) || SHOPC_RX.test(c);
  const q = v => `“${v}”`;
  const paidWhy = `medium ${q(m)} counts as paid`;
  if (c.includes('cross-network')) return {ch:'Cross-network', why:'the campaign name contains “cross-network”.'};
  if (isSh && paid) return {ch:'Paid Shopping', why:`source ${q(s)} is a shopping site and ${paidWhy}.`};
  if (isS && paid)  return {ch:'Paid Search',   why:`${q(s)} is a search engine and ${paidWhy}.`};
  if (isSo && paid) return {ch:'Paid Social',   why:`${q(s)} is a social network and ${paidWhy}.`};
  if (isV && paid)  return {ch:'Paid Video',    why:`${q(s)} is a video site and ${paidWhy}.`};
  if (DISPLAY_M.includes(m)) return {ch:'Display', why:`medium ${q(m)} is a display medium.`};
  if (paid) return {ch:'Paid Other', why:`${paidWhy}, but GA4 doesn't recognise source ${q(s)} as a search, social, video or shopping site.`, soft:true};
  if (isSh) return {ch:'Organic Shopping', why:`source ${q(s)} (or the campaign name) points to shopping.`};
  if (isSo || SOCIAL_M.includes(m)) return {ch:'Organic Social', why: isSo ? `${q(s)} is a social network and the medium isn't paid.` : `medium ${q(m)} is a social medium.`};
  if (isV || /video/.test(m)) return {ch:'Organic Video', why: isV ? `${q(s)} is a video site and the medium isn't paid.` : `medium ${q(m)} contains “video”.`};
  if (isS || m === 'organic') return {ch:'Organic Search', why: isS ? `${q(s)} is a search engine and the medium isn't paid.` : 'medium is “organic”.'};
  if (['referral','app','link'].includes(m)) return {ch:'Referral', why:`medium ${q(m)} is a referral medium.`};
  if (EMAIL_RX.test(s) || EMAIL_RX.test(m)) return {ch:'Email', why:'source or medium is “email”.'};
  if (m === 'affiliate') return {ch:'Affiliates', why:'medium is “affiliate”.'};
  if (m === 'audio') return {ch:'Audio', why:'medium is “audio”.'};
  if (s === 'sms' || m === 'sms') return {ch:'SMS', why:'source or medium is “sms”.'};
  if (/push$/.test(m) || /mobile|notification/.test(m) || s === 'firebase') return {ch:'Mobile Push Notifications', why:`medium ${q(m)} matches the push-notification rule.`};
  return {ch:'Unassigned', why:`no GA4 rule matches source ${q(s)} + medium ${q(m)}. Use a standard medium such as email, social, paid-social, cpc, display, referral, affiliate or sms.`, bad:true};
}

// ------------------------------------------------------------ build
function cleanVal(v) {
  v = (v || '').trim();
  if (!$('optClean').checked) return v;
  v = v.toLowerCase().replace(/\s+/g, SEP);
  const e = SEP === '-' ? '\\-' : '_';
  return v.replace(new RegExp(e + '{2,}', 'g'), SEP);
}
function readVals() {
  const o = {};
  FIELDS.forEach(f => o[f] = cleanVal($('f_' + f).value));
  return o;
}
function parseBase(raw) {
  raw = (raw || '').trim();
  if (!raw) return null;
  if (!/^https?:\/\//i.test(raw)) raw = 'https://' + raw;
  let hash = '', q = '';
  const hi = raw.indexOf('#'); if (hi >= 0) { hash = raw.slice(hi); raw = raw.slice(0, hi); }
  const qi = raw.indexOf('?'); if (qi >= 0) { q = raw.slice(qi + 1); raw = raw.slice(0, qi); }
  let ok = true;
  try { const u = new URL(raw); ok = !!u.hostname && u.hostname.includes('.') && !/\s/.test(raw); } catch (e) { ok = false; }
  const parts = q ? q.split('&').filter(Boolean) : [];
  const kept = parts.filter(p => !/^utm_/i.test(decodeURIComponentSafe(p.split('=')[0])));
  return {base: raw, kept, hadUtm: kept.length !== parts.length, hash, ok};
}
function decodeURIComponentSafe(s) { try { return decodeURIComponent(s); } catch (e) { return s; } }
const enc = v => encodeURIComponent(v);
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function build() {
  const b = parseBase($('f_url').value);
  const v = readVals();
  const pairs = ORDER.filter(f => v[f]).map(f => ['utm_' + f, v[f]]);
  if (!b) return {b, v, url:'', html:'', pairs};
  const qs = b.kept.concat(pairs.map(([k, val]) => k + '=' + enc(val)));
  const url = b.base + (qs.length ? '?' + qs.join('&') : '') + b.hash;
  let html = `<span class="base">${esc(b.base)}</span>`;
  qs.forEach((p, i) => {
    html += `<span class="sep">${i ? '&amp;' : '?'}</span>`;
    const eq = p.indexOf('=');
    if (/^utm_/.test(p) && eq > 0) html += `<span class="k">${esc(p.slice(0, eq))}=</span><span class="v">${esc(p.slice(eq + 1))}</span>`;
    else html += `<span class="base">${esc(p)}</span>`;
  });
  if (b.hash) html += `<span class="base">${esc(b.hash)}</span>`;
  return {b, v, url, html, pairs};
}

// ------------------------------------------------------------ checks
const LABEL = {source:'Source', medium:'Medium', campaign:'Campaign', id:'Campaign ID', term:'Term', content:'Content', source_platform:'Source platform', creative_format:'Creative format', marketing_tactic:'Marketing tactic'};
function checks(r, ch) {
  const out = []; let passed = 0;
  const add = (lvl, t, d) => { if (lvl === 'pass') passed++; else out.push({lvl, t, d}); };
  const raw = {}; FIELDS.forEach(f => raw[f] = $('f_' + f).value.trim());

  if (!r.b) add('fail', 'Add the page URL', 'Paste the address of the page this link should open.');
  else if (!r.b.ok) add('fail', 'This URL doesn\'t look right', 'Check for typos — it should look like https://www.site.com/page.');
  else add('pass');

  const miss = REQUIRED.filter(f => !r.v[f]).map(f => LABEL[f]);
  if (miss.length) add('fail', 'Fill in ' + miss.join(', '), 'Source, medium and campaign are needed for GA4 to credit the visit.');
  else add('pass');

  if (ch) { if (ch.bad) add('warn', 'GA4 will show this as “Unassigned”', 'Pick a standard medium so the traffic lands in a proper channel.'); else add('pass'); }

  const emailRx = /[^\s@]+@[^\s@]+\.[a-z]{2,}/i, phoneRx = /(^|\D)(\+?\d{1,3}[\s-]?)?\d{10}(\D|$)/;
  const pii = FIELDS.filter(f => raw[f] && (emailRx.test(raw[f]) || (f !== 'id' && phoneRx.test(raw[f]))));
  if (pii.length) add('fail', 'Personal data found in ' + pii.map(f => LABEL[f]).join(', '), 'Emails or phone numbers in URLs break Google Analytics policy. Remove them.');
  else add('pass');

  const vals = FIELDS.filter(f => r.v[f]);
  const upper = vals.filter(f => /[A-Z]/.test(r.v[f]));
  if (upper.length) add('warn', 'Capital letters in ' + upper.map(f => LABEL[f]).join(', '), 'GA4 is case-sensitive — “Email” and “email” become two rows. Turn on “Clean values”.');
  else add('pass');
  const spaces = vals.filter(f => /\s/.test(r.v[f]));
  if (spaces.length) add('warn', 'Spaces in ' + spaces.map(f => LABEL[f]).join(', '), 'Spaces turn into %20 in the link. Use hyphens or underscores instead.');
  else add('pass');
  const special = vals.filter(f => /[&?#=%+\/]/.test(r.v[f]));
  if (special.length) add('warn', 'Special characters in ' + special.map(f => LABEL[f]).join(', '), 'Characters like & ? # = / get encoded and look messy in reports. Stick to letters, numbers, - and _.');
  else add('pass');

  if (r.b && r.b.hadUtm) add('info', 'Old UTM tags replaced', 'The URL you pasted already had UTM tags — only your new ones are kept.');
  if (r.url.length > 2000) add('warn', 'Very long link', 'Some apps cut links over 2,000 characters. Shorten the values.');
  if (ch && ch.ch === 'Paid Search' && /^google/.test(r.v.source)) add('info', 'Using Google Ads auto-tagging?', 'If auto-tagging (gclid) is on, GA4 already knows the campaign — UTMs here are optional.');
  if (ch && ch.soft) add('info', 'Grouped as “Paid Other”', 'For Google or Meta ads, use source “google” or “facebook” so GA4 files it under Paid Search / Paid Social.');
  return {out, passed};
}

// ------------------------------------------------------------ render
let current = {url:'', ch:null, v:{}};
function render() {
  const r = build();
  const ch = predict(r.v.source, r.v.medium, r.v.campaign);
  const {out, passed} = checks(r, ch);
  const blocking = out.some(i => i.lvl === 'fail');
  const ready = r.url && !blocking;
  current = {url: ready ? r.url : '', ch, v: r.v};

  $('linkbox').innerHTML = r.url ? r.html : '<span class="empty">Your link will appear here as you type — add the page URL, source, medium and campaign.</span>';
  const st = $('status');
  if (!r.url && !REQUIRED.some(f => r.v[f])) { st.className = 'right status wait'; st.textContent = 'waiting'; }
  else if (ready && !out.some(i => i.lvl === 'warn')) { st.className = 'right status ready'; st.textContent = '✓ ready to use'; }
  else if (ready) { st.className = 'right status fix'; st.textContent = 'usable · check tips'; }
  else { st.className = 'right status fix'; st.textContent = 'needs fixing'; }

  ['copyBtn','qrBtn','checkBtn'].forEach(id => $(id).disabled = !ready);
  $('openBtn').classList.toggle('dis', !ready);
  $('openBtn').href = ready ? r.url : '#';

  const cn = $('chName');
  if (!ch) { cn.className = 'cn none'; cn.textContent = '—'; $('chWhy').textContent = 'Add a source and medium to see the channel.'; }
  else { cn.className = 'cn ' + (ch.bad ? 'bad' : 'good'); cn.textContent = ch.ch; $('chWhy').textContent = 'Because ' + ch.why; }

  const icon = {fail:'✕', warn:'!', info:'i'};
  const order = {fail:0, warn:1, info:2};
  const show = r.url || REQUIRED.some(f => r.v[f]) ? out : [];
  $('issues').innerHTML = show.sort((a, b) => order[a.lvl] - order[b.lvl]).map(i =>
    `<div class="iss ${i.lvl}"><div class="ic">${icon[i.lvl]}</div><div><b>${esc(i.t)}</b><br><span>${esc(i.d)}</span></div></div>`).join('');
  $('passed').textContent = show.length || r.url ? `✓ ${passed} check${passed === 1 ? '' : 's'} passed` : '';

  markPreset(r.v.source, r.v.medium);
  $('urlHint').textContent = r.b && !/^https?:\/\//i.test($('f_url').value.trim()) ? 'https:// will be added for you.' : 'Paste the full address of the page. Existing UTM tags on it will be replaced.';

  // hide panels that belong to an older link
  if ($('qrPanel').dataset.for !== current.url) $('qrPanel').classList.remove('show');
  if ($('checkPanel').dataset.for !== current.url) $('checkPanel').classList.remove('show');
}

// ------------------------------------------------------------ actions
function toast(t) { const el = $('toast'); el.textContent = t; el.classList.add('show'); clearTimeout(el._t); el._t = setTimeout(() => el.classList.remove('show'), 1600); }
async function copyText(t) {
  try { await navigator.clipboard.writeText(t); return true; }
  catch (e) {
    const ta = document.createElement('textarea'); ta.value = t; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select(); let ok = false; try { ok = document.execCommand('copy'); } catch (_) {}
    ta.remove(); return ok;
  }
}
$('copyBtn').onclick = async () => {
  if (!current.url) return;
  if (await copyText(current.url)) toast('Link copied ✓'); else toast('Copy failed — select the link and copy it');
  saveHist();
};
$('qrBtn').onclick = () => {
  if (!current.url) return;
  const p = $('qrPanel');
  if (p.classList.contains('show')) { p.classList.remove('show'); return; }
  const d = enc(current.url);
  $('qrImg').src = '/api/qr?fmt=svg&data=' + d;
  $('qrPng').href = '/api/qr?fmt=png&dl=1&data=' + d;
  $('qrSvg').href = '/api/qr?fmt=svg&dl=1&data=' + d;
  p.dataset.for = current.url; p.classList.add('show');
};
$('checkBtn').onclick = async () => {
  if (!current.url) return;
  const p = $('checkPanel'), out = $('checkOut'), btn = $('checkBtn');
  p.dataset.for = current.url; p.classList.add('show');
  out.innerHTML = '<div class="hint">Following the link and its redirects…</div>';
  btn.disabled = true; const old = btn.innerHTML; btn.innerHTML = '<span class="spin"></span> Testing';
  try {
    const res = await fetch('/api/check?url=' + enc(current.url));
    const j = await res.json();
    if (!j.ok) out.innerHTML = `<div class="iss warn"><div class="ic">!</div><div><b>Couldn't check this page</b><br><span>${esc(j.error || 'Unknown error')}</span></div></div>`;
    else {
      const icon = {pass:'✓', fail:'✕', warn:'!', info:'i'}[j.verdict];
      const title = {pass:'UTM tags arrive safely', fail:'Problem found', warn:'Worth a look', info:'Couldn\'t fully verify'}[j.verdict];
      let h = `<div class="iss ${j.verdict}"><div class="ic">${icon}</div><div><b>${title}</b><br><span>${esc(j.text)}</span></div></div>`;
      if (j.cross_domain) h += `<div class="iss info" style="margin-top:.45rem"><div class="ic">i</div><div><b>Ends on a different domain</b><br><span>Make sure GA4 cross-domain tracking is set up for it.</span></div></div>`;
      h += '<div class="chain">' + j.chain.map(c => `<div><span class="code">${c.status}</span>${esc(c.url)}</div>`).join('') + '</div>';
      out.innerHTML = h;
    }
  } catch (e) {
    out.innerHTML = '<div class="iss warn"><div class="ic">!</div><div><b>Couldn\'t reach the checker</b><br><span>Please try again in a moment.</span></div></div>';
  }
  btn.innerHTML = old; btn.disabled = !current.url;
};

$('moreBtn').onclick = () => { $('moreBtn').classList.toggle('open'); $('more').classList.toggle('show'); };
document.querySelectorAll('#sepTabs button').forEach(b => b.onclick = () => {
  SEP = b.dataset.sep;
  document.querySelectorAll('#sepTabs button').forEach(x => x.classList.toggle('on', x === b));
  render();
});
$('optClean').onchange = () => { $('sepTabs').classList.toggle('off', !$('optClean').checked); render(); };
$('resetBtn').onclick = () => { $('optClean').checked = true; $('sepTabs').classList.remove('off'); $('f_url').value = ''; FIELDS.forEach(f => $('f_' + f).value = ''); render(); $('f_url').focus(); };
['url'].concat(FIELDS).forEach(f => $('f_' + f).addEventListener('input', render));

// ------------------------------------------------------------ history (this browser only)
const HKEY = 'utmBuilderHistory';
function getHist() { try { return JSON.parse(localStorage.getItem(HKEY) || '[]'); } catch (e) { return []; } }
function setHist(h) { try { localStorage.setItem(HKEY, JSON.stringify(h)); } catch (e) {} }
function saveHist() {
  if (!current.url) return;
  let h = getHist().filter(x => x.url !== current.url);
  h.unshift({url: current.url, page: $('f_url').value.trim(), channel: current.ch ? current.ch.ch : '', v: current.v, at: new Date().toISOString()});
  setHist(h.slice(0, 50)); drawHist();
}
function drawHist() {
  const h = getHist();
  $('csvBtn').disabled = $('clearHist').disabled = !h.length;
  if (!h.length) { $('hist').innerHTML = '<div class="empty-hist">No links yet. Every link you copy shows up here so you can reuse or export it.</div>'; return; }
  $('hist').innerHTML = `<table><thead><tr><th>Campaign</th><th>Source / Medium</th><th>GA4 channel</th><th>Link</th><th></th></tr></thead><tbody>` +
    h.map((x, i) => `<tr>
      <td>${esc(x.v.campaign || '—')}</td>
      <td style="white-space:nowrap">${esc(x.v.source || '')} / ${esc(x.v.medium || '')}</td>
      <td><span class="badge ${x.channel === 'Unassigned' ? 'bad' : ''}">${esc(x.channel)}</span></td>
      <td class="url" title="${esc(x.url)}">${esc(x.url)}</td>
      <td class="act"><button class="btn ghost sm" data-copy="${i}">Copy</button> <button class="btn ghost sm" data-load="${i}">Edit</button></td>
    </tr>`).join('') + '</tbody></table>';
  document.querySelectorAll('[data-copy]').forEach(b => b.onclick = async () => { if (await copyText(h[+b.dataset.copy].url)) toast('Link copied ✓'); });
  document.querySelectorAll('[data-load]').forEach(b => b.onclick = () => {
    const x = h[+b.dataset.load];
    $('f_url').value = x.page || '';
    FIELDS.forEach(f => $('f_' + f).value = x.v[f] || '');
    if (['id','source_platform','creative_format','marketing_tactic'].some(f => x.v[f]) && !$('more').classList.contains('show')) $('moreBtn').click();
    render(); window.scrollTo({top: 0, behavior: 'smooth'});
  });
}
$('clearHist').onclick = () => { setHist([]); drawHist(); };
$('csvBtn').onclick = () => {
  const h = getHist(); if (!h.length) return;
  const cols = ['created','page'].concat(ORDER.map(f => 'utm_' + f), ['ga4_channel','full_link']);
  const q = s => '"' + String(s == null ? '' : s).replace(/"/g, '""') + '"';
  const rows = h.map(x => [x.at, x.page].concat(ORDER.map(f => x.v[f] || ''), [x.channel, x.url]).map(q).join(','));
  const blob = new Blob(['﻿' + cols.join(',') + '\n' + rows.join('\n')], {type: 'text/csv;charset=utf-8'});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'utm-links.csv'; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
};

// ------------------------------------------------------------ init
drawPresets(); drawHist(); render();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(debug=True, port=8501)
