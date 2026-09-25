"""
Core Web Vitals (CrUX) Checker
Shows how real Chrome users experience a page or a whole site, using
Google's Chrome UX Report (CrUX) API + CrUX History API.
Mobile and desktop side by side, pass/fail verdict, 6-month trend, fix tips.
Flask app | by Mahalakshmi Marimuthu
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from urllib.parse import urlparse

import requests
from flask import Flask, render_template_string, request

app = Flask(__name__)

# Same Google Cloud key as Page Speed Checker works — just enable "Chrome UX Report API" on it.
API_KEY = (os.environ.get("CRUX_API_KEY") or os.environ.get("PSI_API_KEY") or "").strip()
CRUX_URL = "https://chromeuxreport.googleapis.com/v1/records:queryRecord"
HISTORY_URL = "https://chromeuxreport.googleapis.com/v1/records:queryHistoryRecord"
TIMEOUT = 20
HISTORY_WEEKS = 25  # ~6 months of weekly data points

DEVICES = [("PHONE", "📱 Mobile"), ("DESKTOP", "🖥️ Desktop")]

# key, label, CrUX metric names (first match wins), unit, good limit, poor limit
METRICS = [
    ("lcp", "LCP", ["largest_contentful_paint"], "ms", 2500, 4000),
    ("inp", "INP", ["interaction_to_next_paint"], "ms", 200, 500),
    ("cls", "CLS", ["cumulative_layout_shift"], "", 0.1, 0.25),
    ("fcp", "FCP", ["first_contentful_paint"], "ms", 1800, 3000),
    ("ttfb", "TTFB", ["experimental_time_to_first_byte", "time_to_first_byte"], "ms", 800, 1800),
]
CORE = ("lcp", "inp", "cls")

HELP = {
    "lcp": "Largest Contentful Paint — how fast the main content shows up.",
    "inp": "Interaction to Next Paint — how quickly the page reacts to taps and clicks.",
    "cls": "Cumulative Layout Shift — how much the layout jumps while loading.",
    "fcp": "First Contentful Paint — when the first thing appears on screen.",
    "ttfb": "Time to First Byte — how fast the server starts responding.",
}

TIPS = {
    "lcp": [
        "Compress and resize the hero/banner image; serve WebP or AVIF.",
        "Preload the main image and don't lazy-load anything above the fold.",
        "Cut render-blocking CSS/JS and speed up the server response (caching/CDN).",
    ],
    "inp": [
        "Reduce heavy JavaScript — split long tasks and defer non-critical scripts.",
        "Trim third-party tags (chat widgets, trackers) that run on every tap.",
        "Keep calculators and forms light: update only what changes on input.",
    ],
    "cls": [
        "Set width and height on every image, video and iframe.",
        "Reserve fixed space for ads, banners and cookie bars.",
        "Use font-display: optional/swap and avoid inserting content above existing content.",
    ],
    "fcp": [
        "Inline critical CSS and defer the rest.",
        "Reduce server response time and use a CDN.",
    ],
    "ttfb": [
        "Enable server/page caching and use a CDN close to your users.",
        "Avoid redirect chains before the final page loads.",
    ],
}


class CruxError(Exception):
    pass


# ---------------------------------------------------------------- helpers
def clean_url(raw):
    u = (raw or "").strip()
    if u and not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u


def origin_of(url):
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}" if p.netloc else ""


def to_num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # drop NaN


def rate(value, good, poor):
    if value is None:
        return None
    if value <= good:
        return "good"
    if value <= poor:
        return "ni"
    return "poor"


def fmt(value, unit):
    if value is None:
        return "—"
    if unit == "ms":
        return f"{value / 1000:.2f} s" if value >= 1000 else f"{round(value)} ms"
    return f"{value:.2f}"


def fmt_date(d):
    try:
        return date(d["year"], d["month"], d["day"]).strftime("%d %b %Y")
    except (KeyError, TypeError, ValueError):
        return ""


def pick_metric(metrics, names):
    for n in names:
        if n in metrics:
            return metrics[n]
    return None


def crux_call(endpoint, key, value, device, extra=None):
    """Returns the 'record' dict, or None when CrUX has no data (404)."""
    if not API_KEY:
        raise CruxError("No Google API key is set on the server (CRUX_API_KEY).")
    body = {key: value, "formFactor": device}
    if extra:
        body.update(extra)
    try:
        r = requests.post(endpoint, params={"key": API_KEY}, json=body, timeout=TIMEOUT)
    except requests.exceptions.RequestException:
        raise CruxError("Couldn't reach Google's CrUX API. Please try again.")
    if r.status_code == 404:
        return None
    try:
        data = r.json()
    except ValueError:
        raise CruxError(f"CrUX API returned an unreadable response (HTTP {r.status_code}).")
    if r.status_code != 200:
        raise CruxError((data.get("error") or {}).get("message") or f"HTTP {r.status_code}")
    return data.get("record")


# ---------------------------------------------------------------- parsing
def parse_current(record):
    metrics = record.get("metrics") or {}
    out = {}
    for key, label, names, unit, good, poor in METRICS:
        m = pick_metric(metrics, names)
        if not m:
            out[key] = None
            continue
        p75 = to_num((m.get("percentiles") or {}).get("p75"))
        hist = m.get("histogram") or []
        dens = [round((to_num(b.get("density")) or 0) * 100) for b in hist[:3]]
        while len(dens) < 3:
            dens.append(0)
        out[key] = {
            "p75": p75,
            "display": fmt(p75, unit),
            "rating": rate(p75, good, poor),
            "good": dens[0], "ni": dens[1], "poor": dens[2],
        }
    period = record.get("collectionPeriod") or {}
    return out, f"{fmt_date(period.get('firstDate'))} – {fmt_date(period.get('lastDate'))}"


def parse_history(record):
    if not record:
        return None
    metrics = record.get("metrics") or {}
    periods = record.get("collectionPeriods") or []
    dates = [fmt_date(p.get("lastDate")) for p in periods]
    series = {}
    for key, label, names, unit, good, poor in METRICS[:3]:
        m = pick_metric(metrics, names)
        vals = ((m or {}).get("percentilesTimeseries") or {}).get("p75s") or []
        series[key] = [to_num(v) for v in vals]
    return {"dates": dates, **series}


def verdict(metrics):
    lcp, inp, cls = (metrics.get(k) for k in CORE)
    if not lcp or not cls or lcp["p75"] is None or cls["p75"] is None:
        return None  # not enough data to judge
    ok = lcp["rating"] == "good" and cls["rating"] == "good"
    if inp and inp["p75"] is not None:
        ok = ok and inp["rating"] == "good"
    return "pass" if ok else "fail"


# ---------------------------------------------------------------- per device
def check_device(url, scope, device, label):
    base = {"device": device, "label": label, "error": None, "metrics": {}, "history": None,
            "verdict": None, "period": "", "scope_used": None, "fallback": False}
    try:
        record, key, value = None, None, None
        if scope == "url":
            record = crux_call(CRUX_URL, "url", url, device)
            key, value = "url", url
        if record is None:
            origin = origin_of(url)
            record = crux_call(CRUX_URL, "origin", origin, device)
            key, value = "origin", origin
            base["fallback"] = scope == "url"
        if record is None:
            base["error"] = "Not enough real-user Chrome data for this device."
            return base
        base["metrics"], base["period"] = parse_current(record)
        base["verdict"] = verdict(base["metrics"])
        base["scope_used"] = key
        try:
            base["history"] = parse_history(
                crux_call(HISTORY_URL, key, value, device, {"collectionPeriodCount": HISTORY_WEEKS})
            )
        except CruxError:
            base["history"] = None  # trend is a nice-to-have; don't fail the whole check
    except CruxError as e:
        base["error"] = str(e)
    return base


def build_trend(devices):
    """Merge mobile + desktop history onto one shared date axis."""
    all_dates = []
    for d in devices:
        for dt in (d["history"] or {}).get("dates", []):
            if dt and dt not in all_dates:
                all_dates.append(dt)
    if not all_dates:
        return None
    all_dates.sort(key=lambda s: date.fromisoformat(_iso(s)))
    trend = {"dates": all_dates}
    for d in devices:
        h = d["history"] or {}
        lookup = {k: dict(zip(h.get("dates", []), h.get(k, []))) for k in CORE}
        trend[d["device"]] = {k: [lookup[k].get(dt) for dt in all_dates] for k in CORE}
    return trend


def _iso(s):
    return datetime.strptime(s, "%d %b %Y").date().isoformat()


def trend_arrow(devices):
    """Per device + core metric: compare latest p75 vs ~3 months earlier."""
    for d in devices:
        d["trend"] = {}
        h = d["history"] or {}
        for k in CORE:
            vals = [v for v in h.get(k, []) if v is not None]
            if len(vals) < 4:
                d["trend"][k] = None
                continue
            old, new = vals[max(0, len(vals) - 13)], vals[-1]
            if old == 0:
                d["trend"][k] = None
                continue
            change = (new - old) / old
            d["trend"][k] = "better" if change <= -0.05 else "worse" if change >= 0.05 else "flat"


def build_tips(devices):
    failing = []
    for key, label, *_ in METRICS:
        worst = None
        for d in devices:
            m = d["metrics"].get(key)
            if m and m["rating"] in ("ni", "poor"):
                if worst is None or m["rating"] == "poor":
                    worst = m["rating"]
        if worst:
            failing.append({"key": key, "label": label, "rating": worst, "tips": TIPS[key]})
    return failing


def export_rows(url, devices):
    rows = [["Device", "Data for", "Metric", "p75", "Rating", "Good %", "Needs improvement %", "Poor %", "Period"]]
    for d in devices:
        if d["error"]:
            rows.append([d["label"], "", "", "", d["error"], "", "", "", ""])
            continue
        scope = "Whole site" if d["scope_used"] == "origin" else "This URL"
        for key, label, *_ in METRICS:
            m = d["metrics"].get(key)
            if m:
                rows.append([d["label"], scope, label, m["display"], m["rating"] or "",
                             m["good"], m["ni"], m["poor"], d["period"]])
    return rows


# ---------------------------------------------------------------- template
PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Core Web Vitals (CrUX) Checker</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%237c6af7'/%3E%3Ctext x='50%25' y='50%25' dominant-baseline='central' text-anchor='middle' font-family='Georgia%2C serif' font-size='18' font-weight='900' fill='white'%3EM%3C/text%3E%3C/svg%3E">
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@600;700;800&family=Inter:wght@400;500;600&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root{--bg:#0a0a0f;--surface:#12121a;--surface2:#171722;--border:#23233a;--accent:#7c6af7;--accent-h:#6a58e8;--mint:#6af7c8;--orange:#f7a26a;--red:#f76a6a;--text:#e8e8f0;--muted:#8888a8}
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);font-size:15px;line-height:1.6}
  a{color:var(--accent);text-decoration:none}
  .shell{display:flex;min-height:100vh}
  .sidebar{width:230px;background:var(--surface);border-right:1px solid var(--border);padding:1.4rem 1rem;position:fixed;top:0;bottom:0;display:flex;flex-direction:column;overflow-y:auto}
  .main{flex:1;margin-left:230px;padding:1.6rem 2rem 3rem;max-width:1200px}
  .brand{display:flex;align-items:center;gap:.6rem;font-family:'Sora',sans-serif;font-weight:800;font-size:.95rem;margin-bottom:2rem;color:var(--text)}
  .brand:hover .brand-text{color:var(--accent)}
  .brand-mark{width:30px;height:30px;border-radius:8px;background:var(--accent);display:flex;align-items:center;justify-content:center;color:#fff;font-size:1rem}
  .sidebar-section{margin-bottom:1.4rem}
  .sidebar-label{font-family:'DM Mono',monospace;font-size:.62rem;letter-spacing:.16em;text-transform:uppercase;color:var(--muted);margin-bottom:.5rem;padding-left:.3rem}
  .sidebar-link{display:flex;align-items:center;gap:.6rem;padding:.55rem .8rem;border-radius:8px;color:var(--muted);font-size:.85rem;font-weight:500;margin-bottom:.2rem;border:1px solid transparent}
  .sidebar-link.active{background:rgba(124,106,247,.14);border:1px solid rgba(124,106,247,.4);color:var(--accent);font-weight:700}
  .sidebar-link:hover{color:var(--text)}
  .sidebar-link.active:hover{color:var(--accent)}
  .sidebar-info{font-size:.72rem;color:var(--muted);background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:.7rem .8rem;line-height:1.5}
  .sidebar-footer{margin-top:auto;padding-top:1rem;font-size:.68rem;color:var(--muted);line-height:1.5}
  .credit-name{color:var(--mint);font-weight:700}
  .credit-name:hover{text-decoration:underline}
  .sidebar-social{display:flex;gap:.5rem;margin-top:.7rem}
  .sidebar-social a{width:28px;height:28px;border-radius:6px;background:rgba(106,247,200,.08);border:1px solid rgba(106,247,200,.35);color:var(--mint);display:flex;align-items:center;justify-content:center;font-family:'DM Mono',monospace;font-size:.7rem;transition:background .2s}
  .sidebar-social a:hover{background:rgba(106,247,200,.18)}

  .crumb{font-family:'DM Mono',monospace;font-size:.68rem;color:var(--muted);letter-spacing:.12em;text-transform:uppercase}
  h1{font-family:'Sora',sans-serif;font-size:1.25rem;font-weight:700;margin-bottom:1.2rem}
  h1 span{color:var(--accent)}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.2rem 1.4rem}
  .form-row{display:flex;gap:.8rem;flex-wrap:wrap;align-items:flex-end}
  .form-field{flex:1;min-width:260px}
  .form-field label,.seg-label{font-size:.8rem;color:var(--muted);display:block;margin-bottom:.4rem}
  input[type=text]{width:100%;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:.7rem .9rem;font-family:'DM Mono',monospace;font-size:.82rem}
  input[type=text]:focus{outline:none;border-color:var(--accent)}
  .seg{display:inline-flex;background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:3px}
  .seg input{display:none}
  .seg label{padding:.5rem .9rem;border-radius:8px;font-size:.8rem;color:var(--muted);cursor:pointer;margin:0}
  .seg input:checked+label{background:rgba(124,106,247,.18);color:var(--accent);font-weight:600}
  .btn{background:var(--accent);color:#fff;border:none;border-radius:8px;padding:.72rem 1.5rem;font-size:.85rem;font-weight:600;cursor:pointer;font-family:'Inter',sans-serif}
  .btn:hover{background:var(--accent-h)}
  .hint{font-size:.72rem;color:var(--muted);margin-top:.7rem;font-family:'DM Mono',monospace}
  .err{color:var(--red)}
  .progress{display:none;margin-top:1rem}
  .progress.show{display:block}
  .bar{height:8px;background:var(--surface2);border:1px solid var(--border);border-radius:100px;overflow:hidden}
  .bar-fill{height:100%;width:0;background:linear-gradient(90deg,var(--accent),var(--mint));border-radius:100px;transition:width .4s ease}
  .p-row{display:flex;justify-content:space-between;margin-top:.45rem;font-family:'DM Mono',monospace;font-size:.72rem;color:var(--muted)}
  .p-row b{color:var(--accent);font-weight:500}
  .btn:disabled{opacity:.6;cursor:wait}

  .sec-title{font-family:'Sora',sans-serif;font-size:1rem;font-weight:700;margin:1.8rem 0 .9rem}
  .verdicts{display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-top:1.4rem}
  .vcard{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.1rem 1.3rem}
  .vcard .dev{font-family:'DM Mono',monospace;font-size:.7rem;color:var(--muted);text-transform:uppercase;letter-spacing:.1em}
  .vcard .big{font-family:'Sora',sans-serif;font-size:1.35rem;font-weight:800;margin:.35rem 0}
  .big.pass{color:var(--mint)}.big.fail{color:var(--red)}.big.none{color:var(--muted)}
  .vcard .meta{font-size:.74rem;color:var(--muted)}
  .note{display:inline-block;margin-top:.5rem;font-size:.7rem;font-family:'DM Mono',monospace;color:var(--orange);background:rgba(247,162,106,.08);border:1px solid rgba(247,162,106,.3);padding:.2rem .55rem;border-radius:100px}

  .table{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden}
  .row{display:grid;grid-template-columns:1.3fr 1fr 1fr;gap:1rem;padding:.85rem 1.2rem;border-top:1px solid var(--border);align-items:center}
  .row.head{border-top:none;background:var(--surface2);font-family:'DM Mono',monospace;font-size:.66rem;color:var(--muted);text-transform:uppercase;letter-spacing:.1em;padding:.6rem 1.2rem}
  .m-name{font-weight:700;font-size:.88rem}
  .m-name .core{font-family:'DM Mono',monospace;font-size:.58rem;color:var(--accent);border:1px solid rgba(124,106,247,.4);border-radius:100px;padding:.05rem .4rem;margin-left:.35rem;vertical-align:middle}
  .m-help{font-size:.72rem;color:var(--muted);margin-top:.1rem}
  .cell-top{display:flex;align-items:center;gap:.5rem;margin-bottom:.35rem}
  .badge{font-size:.72rem;font-family:'DM Mono',monospace;padding:.18rem .55rem;border-radius:100px;white-space:nowrap}
  .badge.good{background:rgba(106,247,200,.1);border:1px solid rgba(106,247,200,.3);color:var(--mint)}
  .badge.ni{background:rgba(247,162,106,.1);border:1px solid rgba(247,162,106,.35);color:var(--orange)}
  .badge.poor{background:rgba(247,106,106,.1);border:1px solid rgba(247,106,106,.35);color:var(--red)}
  .arrow{font-size:.68rem;font-family:'DM Mono',monospace;color:var(--muted)}
  .arrow.better{color:var(--mint)}.arrow.worse{color:var(--red)}
  .dist{display:flex;height:6px;border-radius:4px;overflow:hidden;background:var(--border)}
  .dist span:nth-child(1){background:var(--mint)}.dist span:nth-child(2){background:var(--orange)}.dist span:nth-child(3){background:var(--red)}
  .dist-lbl{font-size:.64rem;color:var(--muted);font-family:'DM Mono',monospace;margin-top:.2rem}
  .dash{color:var(--muted);font-size:.8rem}

  .trend-head{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:.6rem;margin-bottom:.8rem}
  .tabs button{background:var(--surface2);border:1px solid var(--border);color:var(--muted);border-radius:8px;padding:.35rem .8rem;font-size:.75rem;cursor:pointer;font-family:'DM Mono',monospace}
  .tabs button.on{border-color:rgba(124,106,247,.5);color:var(--accent);background:rgba(124,106,247,.14)}

  .tip{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1rem 1.2rem;margin-bottom:.6rem}
  .tip-h{display:flex;align-items:center;gap:.6rem;font-weight:700;font-size:.88rem;margin-bottom:.4rem}
  .tip ul{padding-left:1.1rem}
  .tip li{font-size:.8rem;color:#c8c8e0;margin:.15rem 0}
  .allgood{font-size:.85rem;color:var(--mint)}
  .dl{display:inline-flex;align-items:center;gap:.5rem;background:var(--surface);border:1px solid var(--border);color:var(--text);padding:.55rem 1.2rem;border-radius:8px;font-size:.8rem;cursor:pointer;margin-top:1.6rem;font-family:'Inter',sans-serif}
  .dl:hover{border-color:var(--accent)}
  @media(max-width:900px){.sidebar{display:none}.main{margin-left:0;padding:1.2rem}.verdicts{grid-template-columns:1fr}.row{grid-template-columns:1fr;gap:.5rem}.row.head{display:none}}
</style>
</head>
<body>
<div class="shell">
  <aside class="sidebar">
    <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="brand">
      <span class="brand-mark">M</span><span class="brand-text">SEO Tools</span>
    </a>
    <div class="sidebar-section">
      <div class="sidebar-label">Tool</div>
      <a href="#" class="sidebar-link active">🎯&nbsp; Core Web Vitals</a>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-label">Navigate</div>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="sidebar-link">🧰&nbsp; All My Tools</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio" class="sidebar-link">👩‍💻&nbsp; Portfolio</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/blog/index.html" class="sidebar-link">📝&nbsp; Blog Posts</a>
    </div>
    <div class="sidebar-info">Real-user data from Google's Chrome UX Report — the last 28 days of actual Chrome visits. Small or new sites may not have enough traffic to show data.</div>
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
    <div class="crumb">// seo tool · real-user data</div>
    <h1>🎯 Core Web Vitals <span>(CrUX) Checker</span></h1>

    <form class="card" method="POST" action="/check" onsubmit="startProgress()">
      <div class="form-row">
        <div class="form-field">
          <label>Website or page URL</label>
          <input type="text" name="url" placeholder="https://example.com/page" value="{{ raw_url }}" required>
        </div>
        <div>
          <span class="seg-label">Check</span>
          <div class="seg">
            <input type="radio" id="s1" name="scope" value="url" {{ 'checked' if scope == 'url' }}><label for="s1">This page</label>
            <input type="radio" id="s2" name="scope" value="origin" {{ 'checked' if scope == 'origin' }}><label for="s2">Whole site</label>
          </div>
        </div>
        <button class="btn" type="submit" id="go">Check Vitals</button>
      </div>
      <div class="progress" id="prog">
        <div class="bar"><div class="bar-fill" id="pfill"></div></div>
        <div class="p-row"><span id="pstep">Starting…</span><b id="ppct">0%</b></div>
      </div>
      <script>
        function startProgress(){
          const steps = [
            [8,  'Connecting to Google Chrome UX Report…'],
            [25, 'Fetching 📱 mobile real-user data…'],
            [45, 'Fetching 🖥️ desktop real-user data…'],
            [65, 'Loading 6-month trend history…'],
            [82, 'Rating LCP, INP & CLS against Google thresholds…'],
            [94, 'Building your report…']
          ];
          const fill = document.getElementById('pfill'), step = document.getElementById('pstep'), pct = document.getElementById('ppct');
          document.getElementById('prog').classList.add('show');
          const btn = document.getElementById('go'); btn.textContent = 'Checking…';
          setTimeout(() => btn.disabled = true, 0);
          let i = 0, cur = 0;
          const tick = () => {
            if (i < steps.length) { cur = steps[i][0]; step.textContent = steps[i][1]; i++; }
            else if (cur < 98) { cur += 1; }
            fill.style.width = cur + '%'; pct.textContent = cur + '%';
          };
          tick(); setInterval(tick, 900);
        }
        window.addEventListener('pageshow', () => {
          document.getElementById('prog').classList.remove('show');
          const b = document.getElementById('go'); b.disabled = false; b.textContent = 'Check Vitals';
        });
      </script>
      {% if error %}<div class="hint err">⚠ {{ error }}</div>{% endif %}
      <div class="hint">Checks mobile and desktop together. If a page has no data, it automatically shows the whole site instead.</div>
    </form>

    {% if devices %}
    <div class="verdicts">
      {% for d in devices %}
      <div class="vcard">
        <div class="dev">{{ d.label }}</div>
        {% if d.error %}
          <div class="big none">No data</div>
          <div class="meta">{{ d.error }}</div>
        {% else %}
          <div class="big {{ d.verdict or 'none' }}">{{ '✓ Passes Core Web Vitals' if d.verdict == 'pass' else '✕ Fails Core Web Vitals' if d.verdict == 'fail' else 'Not enough data' }}</div>
          <div class="meta">{{ 'Whole site' if d.scope_used == 'origin' else 'This page' }} · {{ d.period }}</div>
          {% if d.fallback %}<span class="note">No page-level data — showing whole site</span>{% endif %}
        {% endif %}
      </div>
      {% endfor %}
    </div>

    <div class="sec-title">Metrics at the 75th percentile</div>
    <div class="table">
      <div class="row head"><div>Metric</div><div>📱 Mobile</div><div>🖥️ Desktop</div></div>
      {% for key, label in metric_list %}
      <div class="row">
        <div><div class="m-name">{{ label }}{% if key in core %}<span class="core">CORE</span>{% endif %}</div><div class="m-help">{{ help[key] }}</div></div>
        {% for d in devices %}
        {% set m = d.metrics.get(key) %}
        <div>
          {% if m and m.p75 is not none %}
            <div class="cell-top">
              <span class="badge {{ m.rating }}">{{ m.display }}</span>
              {% set t = (d.trend or {}).get(key) %}
              {% if t %}<span class="arrow {{ t }}">{{ '↓ improving' if t == 'better' else '↑ worsening' if t == 'worse' else '→ steady' }}</span>{% endif %}
            </div>
            <div class="dist"><span style="width:{{ m.good }}%"></span><span style="width:{{ m.ni }}%"></span><span style="width:{{ m.poor }}%"></span></div>
            <div class="dist-lbl">{{ m.good }}% good · {{ m.ni }}% ok · {{ m.poor }}% poor</div>
          {% else %}<span class="dash">—</span>{% endif %}
        </div>
        {% endfor %}
      </div>
      {% endfor %}
    </div>

    {% if trend %}
    <div class="sec-title">6-month trend</div>
    <div class="card">
      <div class="trend-head">
        <div class="hint" style="margin:0">Weekly p75 values · dashed line = Google's "good" limit</div>
        <div class="tabs">
          <button type="button" class="on" data-k="lcp">LCP</button>
          <button type="button" data-k="inp">INP</button>
          <button type="button" data-k="cls">CLS</button>
        </div>
      </div>
      <canvas id="trend" height="90"></canvas>
    </div>
    {% endif %}

    <div class="sec-title">What to fix</div>
    {% if tips %}
      {% for t in tips %}
      <div class="tip">
        <div class="tip-h"><span class="badge {{ t.rating }}">{{ 'Poor' if t.rating == 'poor' else 'Needs improvement' }}</span>{{ t.label }}</div>
        <ul>{% for x in t.tips %}<li>{{ x }}</li>{% endfor %}</ul>
      </div>
      {% endfor %}
    {% else %}
      <div class="allgood">✓ Every metric is in the "good" range — nothing urgent to fix.</div>
    {% endif %}

    <button class="dl" type="button" onclick="downloadCSV()">⬇️ Download report (CSV)</button>

    <script>
      const ROWS = {{ rows_json|safe }};
      function downloadCSV(){
        const csv = ROWS.map(r => r.map(v => '"' + String(v).replace(/"/g,'""') + '"').join(',')).join('\n');
        const blob = new Blob(['﻿' + csv], {type:'text/csv'});
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob); a.download = 'core_web_vitals_report.csv'; a.click();
      }
      {% if trend %}
      const TREND = {{ trend_json|safe }};
      const GOOD = {lcp:2500, inp:200, cls:0.1};
      const MUTED = '#8888a8', GRID = 'rgba(136,136,168,0.12)';
      const unit = k => k === 'cls' ? v => v.toFixed(2) : v => (v >= 1000 ? (v/1000).toFixed(1)+'s' : Math.round(v)+'ms');
      function ds(k){
        const out = [];
        if (TREND.PHONE) out.push({label:'Mobile', data:TREND.PHONE[k], borderColor:'#7c6af7', backgroundColor:'#7c6af7', tension:.3, pointRadius:2, spanGaps:true});
        if (TREND.DESKTOP) out.push({label:'Desktop', data:TREND.DESKTOP[k], borderColor:'#6af7c8', backgroundColor:'#6af7c8', tension:.3, pointRadius:2, spanGaps:true});
        out.push({label:'Good limit', data:TREND.dates.map(() => GOOD[k]), borderColor:'rgba(136,136,168,.6)', borderDash:[5,5], pointRadius:0, borderWidth:1});
        return out;
      }
      const chart = new Chart(document.getElementById('trend'), {
        type:'line',
        data:{labels:TREND.dates, datasets:ds('lcp')},
        options:{
          plugins:{legend:{labels:{color:MUTED, boxWidth:10, font:{size:11}}}},
          scales:{
            x:{ticks:{color:MUTED, maxTicksLimit:8, font:{size:10}}, grid:{color:GRID}},
            y:{beginAtZero:true, ticks:{color:MUTED, callback:unit('lcp')}, grid:{color:GRID}}
          }
        }
      });
      document.querySelectorAll('.tabs button').forEach(b => b.onclick = () => {
        document.querySelectorAll('.tabs button').forEach(x => x.classList.remove('on'));
        b.classList.add('on');
        chart.data.datasets = ds(b.dataset.k);
        chart.options.scales.y.ticks.callback = unit(b.dataset.k);
        chart.update();
      });
      {% endif %}
    </script>
    {% endif %}
  </main>
</div>
</body>
</html>
"""


def render(**kw):
    ctx = dict(devices=None, error=None, raw_url="", scope="url", trend=None, tips=None,
               rows_json="[]", trend_json="null", metric_list=[(m[0], m[1]) for m in METRICS],
               core=CORE, help=HELP)
    ctx.update(kw)
    return render_template_string(PAGE, **ctx)


# ---------------------------------------------------------------- routes
@app.route("/", methods=["GET"])
def home():
    return render()


@app.route("/check", methods=["GET", "POST"])
def check():
    if request.method == "GET":
        return render()
    raw_url = request.form.get("url", "")
    scope = "origin" if request.form.get("scope") == "origin" else "url"
    url = clean_url(raw_url)
    if not url or not origin_of(url):
        return render(error="Please enter a valid URL.", raw_url=raw_url, scope=scope)
    if not API_KEY:
        return render(error="No Google API key is set on the server (CRUX_API_KEY).", raw_url=raw_url, scope=scope)

    with ThreadPoolExecutor(max_workers=2) as pool:
        devices = list(pool.map(lambda d: check_device(url, scope, *d), DEVICES))

    if all(d["error"] for d in devices):
        msg = devices[0]["error"]
        if "Not enough" in msg:
            msg = ("Google doesn't have enough real-user Chrome data for this page or its site yet. "
                   "CrUX only covers sites with meaningful traffic — try a larger site, "
                   "or use a lab test like PageSpeed Insights instead.")
        return render(error=msg, raw_url=raw_url, scope=scope)

    trend_arrow(devices)
    trend = build_trend(devices)
    return render(
        devices=devices, raw_url=raw_url, scope=scope,
        trend=trend, trend_json=json.dumps(trend),
        tips=build_tips(devices), rows_json=json.dumps(export_rows(url, devices)),
    )


if __name__ == "__main__":
    app.run(debug=True, port=8501)
