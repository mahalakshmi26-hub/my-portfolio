"""
Website Ping Test — Dashboard Edition
Pings a website several times over TCP + HTTP (the way online ping tools work,
since raw ICMP ping isn't allowed on cloud hosts) and reports uptime, DNS/IP,
response times (min / avg / max / jitter), packet loss, HTTP status, redirects
and SSL certificate health. Includes a bulk mode for up to 20 URLs.
Flask app | by Mahalakshmi Marimuthu
"""

import json
import socket
import ssl
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from flask import Flask, render_template_string, request

app = Flask(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8", "Connection": "close"}
TIMEOUT = 10          # seconds per ping
PING_GAP = 0.4        # pause between pings (seconds)
PING_CHOICES = [4, 8, 12]
BULK_PINGS = 3
BULK_LIMIT = 20

GOOD_MS = 600         # full HTTP response time thresholds
FAIR_MS = 1500

_domain_locks = {}
_domain_locks_guard = threading.Lock()


def domain_semaphore(host):
    with _domain_locks_guard:
        if host not in _domain_locks:
            _domain_locks[host] = threading.Semaphore(4)
        return _domain_locks[host]


# ---------------------------------------------------------------- helpers
def clean_url(raw: str) -> str:
    u = (raw or "").strip()
    if u and not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u


def rate_ms(ms):
    if ms is None:
        return "fail"
    if ms <= GOOD_MS:
        return "pass"
    if ms <= FAIR_MS:
        return "warn"
    return "fail"


def resolve_dns(host):
    start = time.perf_counter()
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        return {"ok": False, "ms": None, "ipv4": [], "ipv6": [], "error": f"DNS lookup failed ({e.strerror or e})"}
    ms = round((time.perf_counter() - start) * 1000, 1)
    ipv4, ipv6 = [], []
    for fam, _, _, _, addr in infos:
        ip = addr[0]
        if fam == socket.AF_INET and ip not in ipv4:
            ipv4.append(ip)
        elif fam == socket.AF_INET6 and ip not in ipv6:
            ipv6.append(ip)
    return {"ok": True, "ms": ms, "ipv4": ipv4, "ipv6": ipv6, "error": None}


def check_ssl(host, port=443):
    """Returns certificate details + TLS handshake time for an https host."""
    ctx = ssl.create_default_context()
    try:
        raw = socket.create_connection((host, port), timeout=TIMEOUT)
    except OSError as e:
        return {"ok": False, "error": f"Couldn't connect on port {port} ({type(e).__name__})"}
    try:
        start = time.perf_counter()
        with ctx.wrap_socket(raw, server_hostname=host) as s:
            handshake_ms = round((time.perf_counter() - start) * 1000, 1)
            cert = s.getpeercert()
            tls_version = s.version()
    except ssl.SSLCertVerificationError as e:
        return {"ok": False, "error": f"Invalid certificate: {e.verify_message or e}"}
    except (ssl.SSLError, OSError) as e:
        return {"ok": False, "error": f"TLS handshake failed ({type(e).__name__})"}
    finally:
        try:
            raw.close()
        except OSError:
            pass

    not_after = cert.get("notAfter")
    expires = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc) if not_after else None
    days_left = (expires - datetime.now(timezone.utc)).days if expires else None
    issuer = dict(x[0] for x in cert.get("issuer", ()))
    subject = dict(x[0] for x in cert.get("subject", ()))
    if days_left is None:
        rating = "warn"
    elif days_left < 0:
        rating = "fail"
    elif days_left <= 14:
        rating = "fail"
    elif days_left <= 30:
        rating = "warn"
    else:
        rating = "pass"
    return {
        "ok": True,
        "error": None,
        "issuer": issuer.get("organizationName") or issuer.get("commonName") or "Unknown",
        "subject": subject.get("commonName") or host,
        "expires": expires.strftime("%d %b %Y") if expires else "Unknown",
        "days_left": days_left,
        "tls_version": tls_version,
        "handshake_ms": handshake_ms,
        "rating": rating,
    }


def probe(url):
    """One full request following redirects — gives the final URL, status, server and redirect chain."""
    try:
        start = time.perf_counter()
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True, stream=True)
        ms = round((time.perf_counter() - start) * 1000, 1)
        chain = [{"url": h.url, "status": h.status_code} for h in r.history]
        info = {
            "ok": True,
            "final_url": r.url,
            "status": r.status_code,
            "reason": r.reason or "",
            "server": r.headers.get("Server", "Not disclosed"),
            "content_type": r.headers.get("Content-Type", "—"),
            "chain": chain,
            "ms": ms,
            "error": None,
        }
        r.close()
        return info
    except requests.exceptions.SSLError:
        return {"ok": False, "error": "SSL error — the certificate couldn't be verified."}
    except requests.exceptions.ConnectionError:
        return {"ok": False, "error": "Connection refused or host unreachable."}
    except requests.exceptions.Timeout:
        return {"ok": False, "error": f"No response within {TIMEOUT} seconds."}
    except requests.exceptions.RequestException as e:
        return {"ok": False, "error": f"Request failed ({type(e).__name__})."}


def one_ping(seq, url, host, port):
    """TCP connect time + full HTTP response time (fresh connection each time, no keep-alive)."""
    tcp_ms, http_ms, status, error = None, None, None, None
    try:
        start = time.perf_counter()
        s = socket.create_connection((host, port), timeout=TIMEOUT)
        tcp_ms = round((time.perf_counter() - start) * 1000, 1)
        s.close()
    except OSError as e:
        error = f"TCP connect failed ({type(e).__name__})"

    if error is None:
        try:
            start = time.perf_counter()
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=False, stream=True)
            http_ms = round((time.perf_counter() - start) * 1000, 1)
            status = r.status_code
            r.close()
        except requests.exceptions.SSLError:
            error = "SSL certificate error"
        except requests.exceptions.Timeout:
            error = "Request timed out"
        except requests.exceptions.RequestException as e:
            error = f"HTTP request failed ({type(e).__name__})"

    return {"seq": seq, "tcp_ms": tcp_ms, "http_ms": http_ms, "status": status, "ok": error is None, "error": error}


def summarize(pings):
    ok = [p for p in pings if p["ok"]]
    http = [p["http_ms"] for p in ok if p["http_ms"] is not None]
    tcp = [p["tcp_ms"] for p in ok if p["tcp_ms"] is not None]
    sent = len(pings)
    received = len(ok)
    loss = round((sent - received) / sent * 100) if sent else 100

    def st(vals):
        if not vals:
            return {"min": None, "avg": None, "max": None, "jitter": None}
        return {
            "min": round(min(vals), 1),
            "avg": round(statistics.mean(vals), 1),
            "max": round(max(vals), 1),
            "jitter": round(statistics.pstdev(vals), 1) if len(vals) > 1 else 0.0,
        }

    return {"sent": sent, "received": received, "loss": loss, "uptime": 100 - loss, "http": st(http), "tcp": st(tcp)}


def verdict(summary, final_status, pings=None):
    if summary["received"] == 0 and pings and all(p["tcp_ms"] is not None for p in pings) \
            and all(p["error"] == "SSL certificate error" for p in pings):
        return {"label": "SSL Error", "cls": "fail",
                "note": "The server is reachable, but its SSL certificate is invalid — browsers will show a security warning."}
    if summary["received"] == 0:
        return {"label": "Down", "cls": "fail", "note": "The website did not respond to any ping."}
    if final_status and final_status >= 500:
        return {"label": "Server Error", "cls": "fail", "note": f"The server responds, but returns HTTP {final_status}."}
    if summary["loss"] > 0:
        return {"label": "Unstable", "cls": "warn", "note": f"{summary['loss']}% of pings failed — the connection is flaky."}
    avg = summary["http"]["avg"]
    if avg is not None and avg > FAIR_MS:
        return {"label": "Slow", "cls": "warn", "note": f"Online, but the average response of {round(avg)} ms is slow."}
    return {"label": "Online", "cls": "pass", "note": "The website is up and responding normally."}


def status_class(code):
    if code is None:
        return "fail"
    if code < 300:
        return "pass"
    if code < 400:
        return "warn"
    return "fail"


def ping_site(raw_url, count, gap=PING_GAP, include_ssl=True):
    url = clean_url(raw_url)
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return {"url": raw_url, "error": "That doesn't look like a valid URL."}

    dns = resolve_dns(host)
    if not dns["ok"]:
        return {"url": url, "host": host, "error": dns["error"], "dns": dns}

    first = probe(url)
    if first["ok"]:
        target = first["final_url"]
    else:
        target = url
    tparsed = urlparse(target)
    thost = tparsed.hostname or host
    tport = tparsed.port or (443 if tparsed.scheme == "https" else 80)

    pings = []
    with domain_semaphore(thost):
        for i in range(1, count + 1):
            pings.append(one_ping(i, target, thost, tport))
            if i < count and gap:
                time.sleep(gap)

    summary = summarize(pings)
    final_status = first.get("status") if first["ok"] else (pings[-1]["status"] if pings else None)
    ssl_info = None
    if include_ssl and tparsed.scheme == "https":
        ssl_info = check_ssl(thost, tport)

    return {
        "url": url,
        "host": host,
        "target": target,
        "target_host": thost,
        "port": tport,
        "scheme": tparsed.scheme,
        "dns": dns,
        "probe": first,
        "pings": pings,
        "summary": summary,
        "status": final_status,
        "status_cls": status_class(final_status),
        "verdict": verdict(summary, final_status, pings),
        "speed_cls": rate_ms(summary["http"]["avg"]),
        "ssl": ssl_info,
        "tested_at": datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC"),
        "error": None,
    }


def ping_bulk(urls):
    def job(u):
        try:
            return ping_site(u, BULK_PINGS, gap=0.2)
        except Exception as e:  # never let one bad URL kill the batch
            return {"url": u, "error": f"Unexpected error ({type(e).__name__})"}

    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(job, urls))


# ---------------------------------------------------------------- template
PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Website Ping Test</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%237c6af7'/%3E%3Ctext x='50%25' y='50%25' dominant-baseline='central' text-anchor='middle' font-family='Georgia%2C serif' font-size='18' font-weight='900' fill='white'%3EM%3C/text%3E%3C/svg%3E">
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@600;700;800&family=Inter:wght@400;500;600&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root {
    --bg:#0a0a0f; --surface:#12121a; --surface2:#171722; --border:#23233a;
    --accent:#7c6af7; --accent-h:#6a58e8; --mint:#6af7c8; --orange:#f7a26a;
    --red:#f76a7c; --text:#e8e8f0; --muted:#8888a8;
  }
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);font-size:15px;line-height:1.6}
  a{color:var(--accent);text-decoration:none}

  .shell{display:flex;min-height:100vh}
  .sidebar{width:230px;background:var(--surface);border-right:1px solid var(--border);padding:1.4rem 1rem;position:fixed;top:0;bottom:0;display:flex;flex-direction:column;overflow-y:auto}
  .main{flex:1;margin-left:230px;padding:1.6rem 2rem 3rem;max-width:1400px}
  .brand{display:flex;align-items:center;gap:.6rem;font-family:'Sora',sans-serif;font-weight:800;font-size:.95rem;margin-bottom:2rem;color:var(--text);text-decoration:none}
  .brand:hover .brand-text{color:var(--accent)}
  .brand-mark{width:30px;height:30px;border-radius:8px;background:var(--accent);display:flex;align-items:center;justify-content:center;color:#fff;font-size:1rem}
  .sidebar-section{margin-bottom:1.4rem}
  .sidebar-label{font-family:'DM Mono',monospace;font-size:.62rem;letter-spacing:.16em;text-transform:uppercase;color:var(--muted);margin-bottom:.5rem;padding-left:.3rem}
  .sidebar-link{display:flex;align-items:center;gap:.6rem;padding:.55rem .8rem;border-radius:8px;color:var(--muted);font-size:.85rem;font-weight:500;margin-bottom:.2rem;border:1px solid transparent;text-decoration:none}
  .sidebar-link.active{background:rgba(124,106,247,.14);border:1px solid rgba(124,106,247,.4);color:var(--accent);font-weight:700}
  .sidebar-link:hover{color:var(--text)}
  .sidebar-link.active:hover{color:var(--accent)}
  .sidebar-info{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:.8rem .9rem;font-size:.72rem;color:var(--muted);line-height:1.55;margin-bottom:1.4rem}
  .sidebar-info b{color:var(--text);font-weight:600}
  .sidebar-footer{margin-top:auto;font-size:.68rem;color:var(--muted);line-height:1.5}
  .credit-name{color:var(--mint);font-weight:700;text-decoration:none}
  .credit-name:hover{text-decoration:underline}
  .sidebar-social{display:flex;gap:.5rem;margin-top:.7rem}
  .sidebar-social a{width:28px;height:28px;border-radius:6px;background:rgba(106,247,200,.08);border:1px solid rgba(106,247,200,.35);color:var(--mint);display:flex;align-items:center;justify-content:center;font-family:'DM Mono',monospace;font-size:.7rem;text-decoration:none;transition:background .2s}
  .sidebar-social a:hover{background:rgba(106,247,200,.18)}

  .topbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:1.4rem;flex-wrap:wrap;gap:.8rem}
  .topbar h1{font-family:'Sora',sans-serif;font-size:1.25rem;font-weight:700}
  .topbar h1 span{color:var(--accent)}
  .crumb{font-family:'DM Mono',monospace;font-size:.68rem;color:var(--muted);letter-spacing:.12em;text-transform:uppercase}

  .tabs{display:flex;gap:.5rem;margin-bottom:1rem}
  .tab{padding:.5rem 1.1rem;border-radius:8px;border:1px solid var(--border);background:var(--surface);color:var(--muted);font-size:.82rem;font-weight:600;cursor:pointer;font-family:'Inter',sans-serif}
  .tab.active{background:rgba(124,106,247,.14);border-color:rgba(124,106,247,.4);color:var(--accent)}
  .pane{display:none}
  .pane.show{display:block}

  .card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.2rem 1.4rem}
  .form-row{display:flex;gap:.8rem;flex-wrap:wrap;align-items:flex-end}
  .form-field{flex:1;min-width:260px}
  .form-field.small{flex:0 0 140px;min-width:140px}
  .form-field label{font-size:.8rem;color:var(--muted);display:block;margin-bottom:.4rem}
  input[type=text],select,textarea{width:100%;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:.7rem .9rem;font-family:'DM Mono',monospace;font-size:.82rem}
  textarea{min-height:140px;resize:vertical;line-height:1.6}
  input[type=text]:focus,select:focus,textarea:focus{outline:none;border-color:var(--accent)}
  .btn{display:inline-flex;align-items:center;gap:.5rem;background:var(--accent);color:#fff;border:none;border-radius:8px;padding:.7rem 1.5rem;font-size:.85rem;font-weight:600;cursor:pointer;font-family:'Inter',sans-serif}
  .btn:hover{background:var(--accent-h)}
  .btn.ghost{background:var(--surface);border:1px solid var(--border);color:var(--text)}
  .btn.ghost:hover{border-color:var(--accent);background:var(--surface)}
  .hint{font-size:.72rem;color:var(--muted);margin-top:.6rem;font-family:'DM Mono',monospace}
  .err{color:var(--red)}
  .spinner{display:none;margin-top:.8rem;font-size:.78rem;color:var(--muted);font-family:'DM Mono',monospace}
  .spinner.show{display:block}

  .verdict{display:flex;align-items:center;gap:1rem;margin:1.4rem 0 1rem;padding:1rem 1.3rem;border-radius:12px;border:1px solid var(--border);background:var(--surface)}
  .verdict .dot{width:14px;height:14px;border-radius:50%;flex-shrink:0}
  .verdict.pass .dot{background:var(--mint);box-shadow:0 0 0 6px rgba(106,247,200,.15)}
  .verdict.warn .dot{background:var(--orange);box-shadow:0 0 0 6px rgba(247,162,106,.15)}
  .verdict.fail .dot{background:var(--red);box-shadow:0 0 0 6px rgba(247,106,124,.15)}
  .verdict .v-title{font-family:'Sora',sans-serif;font-weight:800;font-size:1.05rem}
  .verdict.pass .v-title{color:var(--mint)} .verdict.warn .v-title{color:var(--orange)} .verdict.fail .v-title{color:var(--red)}
  .verdict .v-note{font-size:.82rem;color:var(--muted)}
  .verdict .v-meta{margin-left:auto;text-align:right;font-family:'DM Mono',monospace;font-size:.68rem;color:var(--muted)}

  .metrics{display:grid;grid-template-columns:repeat(5,1fr);gap:1rem;margin-bottom:1rem}
  .metric{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:.9rem 1.1rem}
  .metric .m-lbl{font-family:'DM Mono',monospace;font-size:.62rem;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
  .metric .m-val{font-family:'Sora',sans-serif;font-size:1.35rem;font-weight:800;margin-top:.2rem}
  .metric .m-sub{font-size:.7rem;color:var(--muted)}
  .c-pass{color:var(--mint)} .c-warn{color:var(--orange)} .c-fail{color:var(--red)}

  .chartrow{display:grid;grid-template-columns:1fr 2.4fr;gap:1rem;margin-bottom:1rem}
  .chart-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.1rem 1.2rem}
  .chart-card h3{font-family:'Sora',sans-serif;font-size:.82rem;font-weight:700;margin-bottom:.8rem;color:var(--muted)}
  .ring-wrap{position:relative;width:160px;height:160px;margin:0 auto}
  .ring-center{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
  .ring-center .score{font-family:'Sora',sans-serif;font-size:1.7rem;font-weight:800}
  .ring-center .lbl{font-size:.6rem;font-family:'DM Mono',monospace;color:var(--muted);text-transform:uppercase;letter-spacing:.1em}

  .sec-title{font-family:'Sora',sans-serif;font-size:1rem;font-weight:700;margin:1.8rem 0 .9rem}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
  .kv{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden}
  .kv-row{display:grid;grid-template-columns:150px 1fr;gap:.8rem;padding:.65rem 1.1rem;border-top:1px solid var(--border);font-size:.82rem;align-items:center}
  .kv-row:first-child{border-top:none}
  .kv-row .k{color:var(--muted);font-size:.76rem}
  .kv-row .v{font-family:'DM Mono',monospace;font-size:.78rem;word-break:break-all}
  .kv-head{padding:.7rem 1.1rem;background:var(--surface2);font-family:'Sora',sans-serif;font-weight:700;font-size:.85rem}

  .badge{display:inline-block;flex-shrink:0;font-size:.64rem;font-family:'DM Mono',monospace;padding:.2rem .55rem;border-radius:100px;white-space:nowrap}
  .badge.pass{background:rgba(106,247,200,.1);border:1px solid rgba(106,247,200,.3);color:var(--mint)}
  .badge.warn{background:rgba(247,162,106,.1);border:1px solid rgba(247,162,106,.35);color:var(--orange)}
  .badge.fail{background:rgba(247,106,124,.1);border:1px solid rgba(247,106,124,.35);color:var(--red)}
  .badge.info{background:rgba(124,106,247,.1);border:1px solid rgba(124,106,247,.35);color:var(--accent)}

  .terminal{background:#07070b;border:1px solid var(--border);border-radius:12px;padding:1rem 1.2rem;font-family:'DM Mono',monospace;font-size:.78rem;line-height:1.9;overflow-x:auto}
  .terminal .prompt{color:var(--accent)}
  .terminal .ok{color:var(--mint)} .terminal .bad{color:var(--red)} .terminal .dim{color:var(--muted)}

  table{width:100%;border-collapse:collapse;font-size:.8rem}
  .tbl{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:auto}
  th{text-align:left;font-family:'DM Mono',monospace;font-size:.64rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);background:var(--surface2);padding:.7rem .9rem;white-space:nowrap}
  td{padding:.65rem .9rem;border-top:1px solid var(--border);vertical-align:middle}
  td.mono{font-family:'DM Mono',monospace;font-size:.76rem}
  td.url{max-width:320px;word-break:break-all}

  .dl-row{display:flex;gap:.8rem;margin-top:1.2rem;flex-wrap:wrap}

  @media(max-width:1100px){.metrics{grid-template-columns:repeat(3,1fr)}}
  @media(max-width:960px){.chartrow,.grid2{grid-template-columns:1fr}.metrics{grid-template-columns:repeat(2,1fr)}.sidebar{display:none}.main{margin-left:0;padding:1.2rem}.verdict{flex-wrap:wrap}.verdict .v-meta{margin-left:0;text-align:left}}
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
      <a href="/" class="sidebar-link active">📡&nbsp; Website Ping Test</a>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-label">Navigate</div>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="sidebar-link">🧰&nbsp; All My Tools</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio" class="sidebar-link">👩‍💻&nbsp; Portfolio</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/blog/index.html" class="sidebar-link">📝&nbsp; Blog Posts</a>
    </div>
    <div class="sidebar-info">
      <b>How it pings:</b> cloud servers don't allow classic ICMP ping, so each ping opens a fresh TCP connection and makes a real HTTP request — the same thing Googlebot and your visitors experience.<br><br>
      <b>Limits:</b> up to 12 pings for one URL, or {{ bulk_limit }} URLs × {{ bulk_pings }} pings in bulk mode.
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
        <div class="crumb">// seo tool · free · no sign-up</div>
        <h1>📡 Website <span>Ping Test</span></h1>
      </div>
    </div>

    <div class="tabs">
      <button class="tab {{ 'active' if mode != 'bulk' }}" onclick="showTab('single')" id="tab-single">Single URL</button>
      <button class="tab {{ 'active' if mode == 'bulk' }}" onclick="showTab('bulk')" id="tab-bulk">Bulk Ping</button>
    </div>

    <!-- ============ SINGLE ============ -->
    <div class="pane {{ 'show' if mode != 'bulk' }}" id="pane-single">
      <form class="card" method="POST" action="/ping" onsubmit="document.getElementById('sp1').classList.add('show')">
        <div class="form-row">
          <div class="form-field">
            <label>Website URL or domain</label>
            <input type="text" name="url" placeholder="example.com or https://example.com/page" value="{{ raw_url or '' }}" required>
          </div>
          <div class="form-field small">
            <label>Number of pings</label>
            <select name="count">
              {% for c in ping_choices %}<option value="{{ c }}" {{ 'selected' if c == count }}>{{ c }} pings</option>{% endfor %}
            </select>
          </div>
          <button class="btn" type="submit">📡 Ping Website</button>
        </div>
        <div class="spinner" id="sp1">Pinging… each ping opens a fresh connection, so this takes a few seconds.</div>
        {% if error and mode != 'bulk' %}<div class="hint err">⚠ {{ error }}</div>{% endif %}
        <div class="hint">Checks DNS, TCP connect time, full HTTP response time, status code, redirects and SSL — all in one run.</div>
      </form>

      {% if r %}
      <div class="verdict {{ r.verdict.cls }}">
        <span class="dot"></span>
        <div>
          <div class="v-title">{{ r.verdict.label }}</div>
          <div class="v-note">{{ r.verdict.note }}</div>
        </div>
        <div class="v-meta">{{ r.target }}<br>tested {{ r.tested_at }}</div>
      </div>

      <div class="metrics">
        <div class="metric"><div class="m-lbl">Avg Response</div>
          <div class="m-val c-{{ r.speed_cls }}">{{ '%.0f'|format(r.summary.http.avg) if r.summary.http.avg is not none else '—' }}<span style="font-size:.8rem"> ms</span></div>
          <div class="m-sub">full HTTP round-trip</div></div>
        <div class="metric"><div class="m-lbl">Min / Max</div>
          <div class="m-val">{{ '%.0f'|format(r.summary.http.min) if r.summary.http.min is not none else '—' }}<span class="m-sub"> / </span>{{ '%.0f'|format(r.summary.http.max) if r.summary.http.max is not none else '—' }}</div>
          <div class="m-sub">milliseconds</div></div>
        <div class="metric"><div class="m-lbl">Jitter</div>
          <div class="m-val">{{ '%.0f'|format(r.summary.http.jitter) if r.summary.http.jitter is not none else '—' }}<span style="font-size:.8rem"> ms</span></div>
          <div class="m-sub">lower = more consistent</div></div>
        <div class="metric"><div class="m-lbl">Packet Loss</div>
          <div class="m-val c-{{ 'pass' if r.summary.loss == 0 else 'warn' if r.summary.loss < 50 else 'fail' }}">{{ r.summary.loss }}%</div>
          <div class="m-sub">{{ r.summary.received }} of {{ r.summary.sent }} replied</div></div>
        <div class="metric"><div class="m-lbl">HTTP Status</div>
          <div class="m-val c-{{ r.status_cls }}">{{ r.status or '—' }}</div>
          <div class="m-sub">{{ r.probe.reason if r.probe.ok else 'no response' }}</div></div>
      </div>

      <div class="chartrow">
        <div class="chart-card">
          <h3>UPTIME (THIS TEST)</h3>
          <div class="ring-wrap">
            <canvas id="ring"></canvas>
            <div class="ring-center">
              <div class="score c-{{ 'pass' if r.summary.uptime == 100 else 'warn' if r.summary.uptime >= 50 else 'fail' }}">{{ r.summary.uptime }}%</div>
              <div class="lbl">replies</div>
            </div>
          </div>
        </div>
        <div class="chart-card">
          <h3>RESPONSE TIME PER PING (MS)</h3>
          <canvas id="linechart" height="110"></canvas>
        </div>
      </div>

      <div class="sec-title">🖥️ Ping Log</div>
      <div class="terminal">
        <div><span class="prompt">$</span> ping {{ r.target_host }} <span class="dim">(port {{ r.port }}, {{ r.summary.sent }} requests)</span></div>
        <div class="dim">Resolved {{ r.target_host }} → {{ (r.dns.ipv4 + r.dns.ipv6)[0] if (r.dns.ipv4 + r.dns.ipv6) else '?' }} in {{ r.dns.ms }} ms</div>
        {% for p in r.pings %}
          {% if p.ok %}
          <div><span class="ok">Reply #{{ p.seq }}</span>: tcp={{ '%.0f'|format(p.tcp_ms) }}ms  http={{ '%.0f'|format(p.http_ms) }}ms  status={{ p.status }}</div>
          {% else %}
          <div><span class="bad">Ping #{{ p.seq }} failed</span>: {{ p.error }}</div>
          {% endif %}
        {% endfor %}
        <div class="dim">--- {{ r.target_host }} ping statistics ---</div>
        <div>{{ r.summary.sent }} sent, {{ r.summary.received }} received, {{ r.summary.loss }}% loss
        {% if r.summary.http.avg is not none %} · min/avg/max = {{ '%.0f'|format(r.summary.http.min) }}/{{ '%.0f'|format(r.summary.http.avg) }}/{{ '%.0f'|format(r.summary.http.max) }} ms{% endif %}</div>
      </div>

      <div class="grid2" style="margin-top:1.4rem">
        <div class="kv">
          <div class="kv-head">🌐 DNS &amp; Server</div>
          <div class="kv-row"><div class="k">Host</div><div class="v">{{ r.target_host }}</div></div>
          <div class="kv-row"><div class="k">IPv4</div><div class="v">{{ r.dns.ipv4|join(', ') or '—' }}</div></div>
          <div class="kv-row"><div class="k">IPv6</div><div class="v">{{ r.dns.ipv6|join(', ') or 'None' }}</div></div>
          <div class="kv-row"><div class="k">DNS lookup</div><div class="v">{{ r.dns.ms }} ms</div></div>
          <div class="kv-row"><div class="k">Avg TCP connect</div><div class="v">{{ r.summary.tcp.avg if r.summary.tcp.avg is not none else '—' }} ms</div></div>
          <div class="kv-row"><div class="k">Server</div><div class="v">{{ r.probe.server if r.probe.ok else '—' }}</div></div>
          <div class="kv-row"><div class="k">Content-Type</div><div class="v">{{ r.probe.content_type if r.probe.ok else '—' }}</div></div>
        </div>
        <div class="kv">
          <div class="kv-head">🔒 SSL Certificate</div>
          {% if r.scheme != 'https' %}
            <div class="kv-row"><div class="k">Status</div><div class="v"><span class="badge fail">No HTTPS</span> Site served over plain HTTP</div></div>
          {% elif r.ssl and r.ssl.ok %}
            <div class="kv-row"><div class="k">Status</div><div class="v"><span class="badge {{ r.ssl.rating }}">{{ 'Valid' if r.ssl.rating == 'pass' else 'Expiring soon' if r.ssl.days_left is not none and r.ssl.days_left >= 0 else 'Expired' }}</span></div></div>
            <div class="kv-row"><div class="k">Issued to</div><div class="v">{{ r.ssl.subject }}</div></div>
            <div class="kv-row"><div class="k">Issuer</div><div class="v">{{ r.ssl.issuer }}</div></div>
            <div class="kv-row"><div class="k">Expires</div><div class="v">{{ r.ssl.expires }} ({{ r.ssl.days_left }} days left)</div></div>
            <div class="kv-row"><div class="k">TLS version</div><div class="v">{{ r.ssl.tls_version }}</div></div>
            <div class="kv-row"><div class="k">TLS handshake</div><div class="v">{{ r.ssl.handshake_ms }} ms</div></div>
          {% else %}
            <div class="kv-row"><div class="k">Status</div><div class="v"><span class="badge fail">Problem</span> {{ r.ssl.error if r.ssl else 'Not checked' }}</div></div>
          {% endif %}
        </div>
      </div>

      <div class="sec-title">↪️ Redirect Chain</div>
      {% if r.probe.ok and r.probe.chain %}
      <div class="tbl"><table>
        <tr><th>#</th><th>URL</th><th>Status</th></tr>
        {% for h in r.probe.chain %}
        <tr><td class="mono">{{ loop.index }}</td><td class="mono url">{{ h.url }}</td><td><span class="badge warn">{{ h.status }}</span></td></tr>
        {% endfor %}
        <tr><td class="mono">✓</td><td class="mono url">{{ r.probe.final_url }}</td><td><span class="badge {{ r.status_cls }}">{{ r.status }}</span></td></tr>
      </table></div>
      <div class="hint">{{ r.probe.chain|length }} redirect(s) before the final page — each hop adds latency for users and crawl budget for Google.</div>
      {% elif r.probe.ok %}
      <div class="card" style="font-size:.84rem"><span class="badge pass">No redirects</span>&nbsp; The URL loads directly.</div>
      {% else %}
      <div class="card" style="font-size:.84rem"><span class="badge fail">Unknown</span>&nbsp; {{ r.probe.error }}</div>
      {% endif %}

      <div class="dl-row">
        <button class="btn ghost" onclick="downloadSingle()">⬇️ Download Ping Report (CSV)</button>
      </div>
      {% endif %}
    </div>

    <!-- ============ BULK ============ -->
    <div class="pane {{ 'show' if mode == 'bulk' }}" id="pane-bulk">
      <form class="card" method="POST" action="/bulk" onsubmit="document.getElementById('sp2').classList.add('show')">
        <div class="form-field">
          <label>URLs or domains — one per line (max {{ bulk_limit }})</label>
          <textarea name="urls" placeholder="bankbazaar.com&#10;https://example.com&#10;google.com" required>{{ bulk_raw or '' }}</textarea>
        </div>
        <div style="margin-top:.8rem"><button class="btn" type="submit">📡 Ping All</button></div>
        <div class="spinner" id="sp2">Pinging every URL {{ bulk_pings }} times in parallel — usually 10-30 seconds…</div>
        {% if error and mode == 'bulk' %}<div class="hint err">⚠ {{ error }}</div>{% endif %}
      </form>

      {% if bulk %}
      <div class="metrics" style="margin-top:1.4rem;grid-template-columns:repeat(4,1fr)">
        <div class="metric"><div class="m-lbl">URLs Checked</div><div class="m-val">{{ bulk_stats.total }}</div></div>
        <div class="metric"><div class="m-lbl">Online</div><div class="m-val c-pass">{{ bulk_stats.online }}</div></div>
        <div class="metric"><div class="m-lbl">Slow / Unstable</div><div class="m-val c-warn">{{ bulk_stats.warn }}</div></div>
        <div class="metric"><div class="m-lbl">Down / Errors</div><div class="m-val c-fail">{{ bulk_stats.down }}</div></div>
      </div>
      <div class="tbl"><table>
        <tr><th>URL</th><th>Verdict</th><th>Status</th><th>IP</th><th>Avg ms</th><th>Min / Max</th><th>Loss</th><th>SSL</th><th></th></tr>
        {% for b in bulk %}
        <tr>
          <td class="mono url">{{ b.target or b.url }}</td>
          {% if b.error %}
          <td><span class="badge fail">Error</span></td><td colspan="6" style="color:var(--muted);font-size:.78rem">{{ b.error }}</td><td></td>
          {% else %}
          <td><span class="badge {{ b.verdict.cls }}">{{ b.verdict.label }}</span></td>
          <td><span class="badge {{ b.status_cls }}">{{ b.status or '—' }}</span></td>
          <td class="mono">{{ b.dns.ipv4[0] if b.dns.ipv4 else (b.dns.ipv6[0] if b.dns.ipv6 else '—') }}</td>
          <td class="mono c-{{ b.speed_cls }}">{{ '%.0f'|format(b.summary.http.avg) if b.summary.http.avg is not none else '—' }}</td>
          <td class="mono">{{ '%.0f'|format(b.summary.http.min) if b.summary.http.min is not none else '—' }} / {{ '%.0f'|format(b.summary.http.max) if b.summary.http.max is not none else '—' }}</td>
          <td class="mono">{{ b.summary.loss }}%</td>
          <td>{% if b.scheme != 'https' %}<span class="badge fail">No HTTPS</span>{% elif b.ssl and b.ssl.ok %}<span class="badge {{ b.ssl.rating }}">{{ b.ssl.days_left }}d left</span>{% else %}<span class="badge fail">Invalid</span>{% endif %}</td>
          <td><a href="/?url={{ (b.target or b.url)|urlencode }}" style="font-size:.76rem;white-space:nowrap">Full test →</a></td>
          {% endif %}
        </tr>
        {% endfor %}
      </table></div>
      <div class="dl-row">
        <button class="btn ghost" onclick="downloadBulk()">⬇️ Download Bulk Report (CSV)</button>
      </div>
      {% endif %}
    </div>
  </main>
</div>

<script>
  function showTab(t){
    ['single','bulk'].forEach(x=>{
      document.getElementById('pane-'+x).classList.toggle('show', x===t);
      document.getElementById('tab-'+x).classList.toggle('active', x===t);
    });
  }
  function csvCell(v){ v = (v===null||v===undefined) ? '' : String(v); return /[",\n]/.test(v) ? '"'+v.replace(/"/g,'""')+'"' : v; }
  function saveCsv(rows, name){
    const text = '﻿' + rows.map(r=>r.map(csvCell).join(',')).join('\n');
    const blob = new Blob([text], {type:'text/csv;charset=utf-8'});
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
  }

  {% if r %}
  const R = {{ r_json|safe }};
  function downloadSingle(){
    const s = R.summary, rows = [['Section','Field','Value']];
    rows.push(['Summary','URL tested',R.target]);
    rows.push(['Summary','Verdict',R.verdict.label]);
    rows.push(['Summary','HTTP status',R.status]);
    rows.push(['Summary','Pings sent',s.sent]);
    rows.push(['Summary','Pings received',s.received]);
    rows.push(['Summary','Packet loss %',s.loss]);
    rows.push(['Summary','Avg response ms',s.http.avg]);
    rows.push(['Summary','Min response ms',s.http.min]);
    rows.push(['Summary','Max response ms',s.http.max]);
    rows.push(['Summary','Jitter ms',s.http.jitter]);
    rows.push(['Summary','Avg TCP connect ms',s.tcp.avg]);
    rows.push(['DNS','IPv4',R.dns.ipv4.join(' ')]);
    rows.push(['DNS','IPv6',R.dns.ipv6.join(' ')]);
    rows.push(['DNS','Lookup ms',R.dns.ms]);
    if (R.ssl && R.ssl.ok){
      rows.push(['SSL','Issuer',R.ssl.issuer]); rows.push(['SSL','Expires',R.ssl.expires]);
      rows.push(['SSL','Days left',R.ssl.days_left]); rows.push(['SSL','TLS version',R.ssl.tls_version]);
    } else if (R.ssl){ rows.push(['SSL','Error',R.ssl.error]); }
    R.pings.forEach(p=>rows.push(['Ping #'+p.seq, p.ok ? 'tcp '+p.tcp_ms+' ms / http '+p.http_ms+' ms' : 'failed', p.ok ? p.status : p.error]));
    saveCsv(rows, 'website_ping_test.csv');
  }
  if (window.Chart){
    const MUTED='#8888a8', GRID='rgba(136,136,168,0.12)';
    const up = R.summary.uptime;
    new Chart(document.getElementById('ring'), {
      type:'doughnut',
      data:{datasets:[{data:[up, 100-up], backgroundColor:[up===100?'#6af7c8':up>=50?'#f7a26a':'#f76a7c','#23233a'], borderWidth:0, cutout:'78%'}]},
      options:{plugins:{legend:{display:false},tooltip:{enabled:false}}}
    });
    new Chart(document.getElementById('linechart'), {
      type:'line',
      data:{
        labels: R.pings.map(p=>'#'+p.seq),
        datasets:[
          {label:'HTTP response', data:R.pings.map(p=>p.http_ms), borderColor:'#7c6af7', backgroundColor:'rgba(124,106,247,.15)', fill:true, tension:.3, pointRadius:4, spanGaps:false},
          {label:'TCP connect', data:R.pings.map(p=>p.tcp_ms), borderColor:'#6af7c8', backgroundColor:'transparent', tension:.3, pointRadius:3, borderDash:[4,4]}
        ]
      },
      options:{
        plugins:{legend:{position:'top',labels:{color:MUTED,boxWidth:10,font:{size:11}}}},
        scales:{x:{ticks:{color:MUTED},grid:{color:GRID}}, y:{beginAtZero:true,ticks:{color:MUTED,callback:v=>v+' ms'},grid:{color:GRID}}}
      }
    });
  }
  {% endif %}

  {% if bulk %}
  const B = {{ bulk_json|safe }};
  function downloadBulk(){
    const rows=[['URL','Verdict','HTTP status','IPv4','Avg ms','Min ms','Max ms','Packet loss %','SSL days left','Error']];
    B.forEach(b=>{
      if (b.error){ rows.push([b.url,'Error','','','','','','','',b.error]); return; }
      rows.push([b.target, b.verdict.label, b.status, b.dns.ipv4.join(' '), b.summary.http.avg, b.summary.http.min, b.summary.http.max, b.summary.loss,
        (b.ssl && b.ssl.ok) ? b.ssl.days_left : (b.scheme==='https' ? 'invalid' : 'no https'), '']);
    });
    saveCsv(rows, 'website_ping_bulk.csv');
  }
  {% endif %}

  {% if autorun %}
  document.querySelector('#pane-single form').requestSubmit();
  {% endif %}
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- routes
def safe_json(obj):
    # safe to drop inside a <script> block
    return json.dumps(obj).replace("</", "<\\/")


def render(**ctx):
    base = dict(
        r=None, bulk=None, error=None, raw_url="", bulk_raw="", mode="single", count=8, autorun=False,
        ping_choices=PING_CHOICES, bulk_limit=BULK_LIMIT, bulk_pings=BULK_PINGS,
    )
    base.update(ctx)
    return render_template_string(PAGE, **base)


@app.route("/", methods=["GET"])
def home():
    url = request.args.get("url", "").strip()
    return render(raw_url=url, autorun=bool(url))


@app.route("/ping", methods=["POST"])
def ping():
    raw_url = request.form.get("url", "")
    try:
        count = int(request.form.get("count", 8))
    except ValueError:
        count = 8
    if count not in PING_CHOICES:
        count = 8
    if not raw_url.strip():
        return render(error="Please enter a URL or domain.", count=count)

    result = ping_site(raw_url, count)
    if result.get("error"):
        return render(error=result["error"], raw_url=raw_url, count=count)
    return render(r=result, r_json=safe_json(result), raw_url=raw_url, count=count)


@app.route("/bulk", methods=["POST"])
def bulk():
    raw = request.form.get("urls", "")
    urls, seen = [], set()
    for line in raw.splitlines():
        u = line.strip()
        if u and u.lower() not in seen:
            seen.add(u.lower())
            urls.append(u)
    if not urls:
        return render(mode="bulk", error="Please paste at least one URL.", bulk_raw=raw)
    if len(urls) > BULK_LIMIT:
        return render(mode="bulk", error=f"You pasted {len(urls)} URLs — the limit is {BULK_LIMIT} per run.", bulk_raw=raw)

    results = ping_bulk(urls)
    stats = {"total": len(results), "online": 0, "warn": 0, "down": 0}
    for b in results:
        if b.get("error") or b["verdict"]["cls"] == "fail":
            stats["down"] += 1
        elif b["verdict"]["cls"] == "warn":
            stats["warn"] += 1
        else:
            stats["online"] += 1
    return render(mode="bulk", bulk=results, bulk_json=safe_json(results), bulk_stats=stats, bulk_raw=raw)


if __name__ == "__main__":
    app.run(debug=True, port=8501)
