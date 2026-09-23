"""
Page Speed Checker — Dashboard Edition
Runs a URL through Google's PageSpeed Insights API (Lighthouse) for both
mobile and desktop, side by side, and shows Core Web Vitals + top fix
opportunities for each.
Flask app | by Mahalakshmi Marimuthu
"""

import csv
import io
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from flask import Flask, abort, render_template_string, request, send_file

app = Flask(__name__)

PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
PSI_API_KEY = os.environ.get("PSI_API_KEY", "").strip()
PSI_TIMEOUT = 60

STORE = {}
STORE_LIMIT = 20

STRATEGIES = [("mobile", "📱 Mobile"), ("desktop", "🖥️ Desktop")]

VITAL_ROWS = [("lcp", "LCP"), ("inp", "INP"), ("cls", "CLS"), ("fcp", "FCP"), ("ttfb", "TTFB")]
VITAL_HELP = {
    "lcp": "Largest Contentful Paint — how fast the main content loads.",
    "inp": "Interaction to Next Paint — how responsive the page feels to clicks/taps.",
    "cls": "Cumulative Layout Shift — how much the page jumps around while loading.",
    "fcp": "First Contentful Paint — how fast something first appears on screen.",
    "ttfb": "Time to First Byte — how fast the server starts responding.",
}


class PSIError(Exception):
    pass


# ---------------------------------------------------------------- helpers
def clean_url(raw: str) -> str:
    u = raw.strip()
    if u and not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u


def strip_markdown_links(text: str) -> str:
    # Lighthouse descriptions use [label](url) markdown — keep just the label.
    return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text or "")


def rate_metric(value, good, needs_improvement):
    """Ascending thresholds where lower is better. Returns pass | warn | fail."""
    if value is None:
        return None
    if value <= good:
        return "pass"
    if value <= needs_improvement:
        return "warn"
    return "fail"


def ms_to_s(ms):
    if ms is None:
        return 0
    return round(ms / 1000, 2)


def parse_psi_response(data: dict, strategy: str) -> dict:
    """Pulls the fields we care about out of a raw PageSpeed Insights v5 JSON response."""
    lighthouse = data.get("lighthouseResult") or {}
    runtime_error = lighthouse.get("runtimeError")
    if runtime_error and runtime_error.get("code"):
        raise PSIError(runtime_error.get("message") or runtime_error.get("code"))

    audits = lighthouse.get("audits") or {}
    categories = lighthouse.get("categories") or {}
    perf = categories.get("performance") or {}
    score = perf.get("score")
    perf_score = round(score * 100) if score is not None else None

    def numeric(aid):
        a = audits.get(aid) or {}
        return a.get("numericValue")

    lab = {
        "fcp_ms": numeric("first-contentful-paint"),
        "lcp_ms": numeric("largest-contentful-paint"),
        "tbt_ms": numeric("total-blocking-time"),
        "cls": (audits.get("cumulative-layout-shift") or {}).get("numericValue"),
        "si_ms": numeric("speed-index"),
        "tti_ms": numeric("interactive"),
        "ttfb_ms": numeric("server-response-time"),
    }

    field = None
    le = data.get("loadingExperience") or data.get("originLoadingExperience")
    if le and le.get("metrics"):
        m = le["metrics"]

        def pct(key):
            v = m.get(key)
            return v.get("percentile") if v else None

        cls_raw = pct("CUMULATIVE_LAYOUT_SHIFT_SCORE")
        field = {
            "lcp_ms": pct("LARGEST_CONTENTFUL_PAINT_MS"),
            "inp_ms": pct("INTERACTION_TO_NEXT_PAINT"),
            "cls": (cls_raw / 100) if cls_raw is not None else None,
            "fcp_ms": pct("FIRST_CONTENTFUL_PAINT_MS"),
            "category": le.get("overall_category"),
        }

    opportunities = []
    for aid, a in audits.items():
        details = a.get("details") or {}
        score_val = a.get("score")
        if details.get("type") == "opportunity" and score_val is not None and score_val < 0.9:
            opportunities.append(
                {
                    "id": aid,
                    "title": a.get("title", aid),
                    "description": strip_markdown_links(a.get("description", "")),
                    "savings_ms": details.get("overallSavingsMs") or 0,
                }
            )
    opportunities.sort(key=lambda o: o["savings_ms"], reverse=True)

    return {
        "strategy": strategy,
        "score": perf_score,
        "lab": lab,
        "field": field,
        "opportunities": opportunities[:8],
        "final_url": lighthouse.get("finalUrl"),
    }


def fetch_psi(url: str, strategy: str) -> dict:
    if not PSI_API_KEY:
        raise PSIError(
            "No PageSpeed Insights API key is configured on the server "
            "(set the PSI_API_KEY environment variable)."
        )
    params = {"url": url, "key": PSI_API_KEY, "strategy": strategy, "category": "performance"}
    try:
        resp = requests.get(PSI_ENDPOINT, params=params, timeout=PSI_TIMEOUT)
    except requests.exceptions.Timeout:
        raise PSIError("PageSpeed Insights took too long to respond. Please try again.")
    except requests.exceptions.RequestException as e:
        raise PSIError(f"Couldn't reach PageSpeed Insights ({type(e).__name__}).")

    try:
        data = resp.json()
    except ValueError:
        raise PSIError(f"PageSpeed Insights returned an unreadable response (HTTP {resp.status_code}).")

    if resp.status_code != 200:
        message = (data.get("error") or {}).get("message") or f"HTTP {resp.status_code}"
        raise PSIError(message)

    return parse_psi_response(data, strategy)


def run_both(url: str) -> dict:
    results, errors = {}, {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(fetch_psi, url, strategy): strategy for strategy, _ in STRATEGIES}
        for fut in as_completed(futures):
            strategy = futures[fut]
            try:
                results[strategy] = fut.result()
            except PSIError as e:
                errors[strategy] = str(e)
    return {"results": results, "errors": errors}


def vitals_for(r: dict) -> dict:
    field = r.get("field") or {}
    lab = r.get("lab") or {}
    out = {}

    def make(key, unit, good, warn, value, source, metric_name):
        rating = rate_metric(value, good, warn) if value is not None else None
        if value is None:
            display = "—"
        elif unit == "s":
            display = f"{round(value / 1000, 2)}s"
        elif unit == "ms":
            display = f"{round(value)}ms"
        else:
            display = f"{round(value, 3)}"
        out[key] = {"value": value, "display": display, "source": source, "rating": rating, "metric_name": metric_name}

    if field.get("lcp_ms") is not None:
        make("lcp", "s", 2500, 4000, field["lcp_ms"], "field", "LCP")
    else:
        make("lcp", "s", 2500, 4000, lab.get("lcp_ms"), "lab", "LCP")

    if field.get("inp_ms") is not None:
        make("inp", "ms", 200, 500, field["inp_ms"], "field", "INP")
    else:
        make("inp", "ms", 200, 600, lab.get("tbt_ms"), "lab", "TBT")

    if field.get("cls") is not None:
        make("cls", "", 0.1, 0.25, field["cls"], "field", "CLS")
    else:
        make("cls", "", 0.1, 0.25, lab.get("cls"), "lab", "CLS")

    if field.get("fcp_ms") is not None:
        make("fcp", "s", 1800, 3000, field["fcp_ms"], "field", "FCP")
    else:
        make("fcp", "s", 1800, 3000, lab.get("fcp_ms"), "lab", "FCP")

    make("ttfb", "ms", 800, 1800, lab.get("ttfb_ms"), "lab", "TTFB")

    return out


def build_view(url: str, run: dict) -> dict:
    strategies = []
    for key, label in STRATEGIES:
        r = run["results"].get(key)
        if r:
            strategies.append(
                {
                    "key": key,
                    "label": label,
                    "score": r["score"],
                    "vitals": vitals_for(r),
                    "opportunities": r["opportunities"],
                    "has_field": bool(r["field"]),
                    "final_url": r.get("final_url"),
                    "lab_chart": {
                        "fcp_s": ms_to_s((r["lab"] or {}).get("fcp_ms")),
                        "lcp_s": ms_to_s((r["lab"] or {}).get("lcp_ms")),
                        "si_s": ms_to_s((r["lab"] or {}).get("si_ms")),
                        "tti_s": ms_to_s((r["lab"] or {}).get("tti_ms")),
                    },
                    "error": None,
                }
            )
        else:
            strategies.append(
                {
                    "key": key,
                    "label": label,
                    "score": None,
                    "vitals": {},
                    "opportunities": [],
                    "has_field": False,
                    "final_url": None,
                    "lab_chart": {"fcp_s": 0, "lcp_s": 0, "si_s": 0, "tti_s": 0},
                    "error": run["errors"].get(key, "Unknown error"),
                }
            )

    combined_vitals = []
    for vkey, vlabel in VITAL_ROWS:
        combined_vitals.append(
            {
                "key": vkey,
                "label": vlabel,
                "help": VITAL_HELP[vkey],
                "mobile": strategies[0]["vitals"].get(vkey),
                "desktop": strategies[1]["vitals"].get(vkey),
            }
        )

    return {"url": url, "strategies": strategies, "vitals": combined_vitals}


def build_export_rows(view: dict) -> list:
    rows = []
    for s in view["strategies"]:
        if s["error"]:
            rows.append({"Strategy": s["label"], "Type": "Error", "Name": "—", "Value": s["error"], "Detail": ""})
            continue
        rows.append({"Strategy": s["label"], "Type": "Score", "Name": "Performance Score", "Value": s["score"], "Detail": ""})
        for vkey, vlabel in VITAL_ROWS:
            v = s["vitals"].get(vkey)
            if v:
                rows.append(
                    {
                        "Strategy": s["label"],
                        "Type": "Core Web Vital",
                        "Name": v["metric_name"],
                        "Value": v["display"],
                        "Detail": f"{v['source']} data, rated {v['rating']}",
                    }
                )
        for o in s["opportunities"]:
            rows.append(
                {
                    "Strategy": s["label"],
                    "Type": "Opportunity",
                    "Name": o["title"],
                    "Value": f"~{round(o['savings_ms'])} ms potential savings",
                    "Detail": o["description"],
                }
            )
    return rows


# ---------------------------------------------------------------- template
PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Page Speed Checker</title>
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

  .card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.2rem 1.4rem}
  .form-row{display:flex;gap:.8rem;flex-wrap:wrap;align-items:flex-end}
  .form-field{flex:1;min-width:260px}
  .form-field label{font-size:.8rem;color:var(--muted);display:block;margin-bottom:.4rem}
  input[type=text]{width:100%;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:.7rem .9rem;font-family:'DM Mono',monospace;font-size:.82rem}
  input[type=text]:focus{outline:none;border-color:var(--accent)}
  .btn{display:inline-flex;align-items:center;gap:.5rem;background:var(--accent);color:#fff;border:none;border-radius:8px;padding:.7rem 1.5rem;font-size:.85rem;font-weight:600;cursor:pointer;font-family:'Inter',sans-serif}
  .btn:hover{background:var(--accent-h)}
  .hint{font-size:.72rem;color:var(--muted);margin-top:.6rem;font-family:'DM Mono',monospace}

  .scorerow{display:grid;grid-template-columns:1fr 1fr 1.4fr;gap:1rem;margin:1.4rem 0}
  .chart-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.1rem 1.2rem}
  .chart-card h3{font-family:'Sora',sans-serif;font-size:.82rem;font-weight:700;margin-bottom:.8rem;color:var(--muted)}
  .ring-wrap{position:relative;width:150px;height:150px;margin:0 auto}
  .ring-center{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
  .ring-center .score{font-family:'Sora',sans-serif;font-size:1.8rem;font-weight:800}
  .ring-center .lbl{font-size:.6rem;font-family:'DM Mono',monospace;color:var(--muted);text-transform:uppercase;letter-spacing:.1em}
  .strategy-error{font-size:.8rem;color:var(--red);text-align:center;padding:2rem 0}

  .sec-title{font-family:'Sora',sans-serif;font-size:1rem;font-weight:700;margin:1.8rem 0 .9rem}
  .vitals-table{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden}
  .vt-row{display:grid;grid-template-columns:1.4fr 1fr 1fr;gap:.8rem;padding:.8rem 1.1rem;border-top:1px solid var(--border);align-items:center}
  .vt-row:first-child{border-top:none;background:var(--surface2)}
  .vt-metric .m-name{font-weight:700;font-size:.86rem}
  .vt-metric .m-help{font-size:.72rem;color:var(--muted);margin-top:.15rem}
  .vt-val{display:flex;align-items:center;gap:.5rem;font-family:'DM Mono',monospace;font-size:.85rem}
  .vt-val .note{font-size:.65rem;color:var(--muted)}
  .badge{display:inline-block;flex-shrink:0;font-size:.64rem;font-family:'DM Mono',monospace;padding:.2rem .55rem;border-radius:100px;white-space:nowrap}
  .badge.pass{background:rgba(106,247,200,.1);border:1px solid rgba(106,247,200,.3);color:var(--mint)}
  .badge.warn{background:rgba(247,162,106,.1);border:1px solid rgba(247,162,106,.35);color:var(--orange)}
  .badge.fail{background:rgba(247,106,124,.1);border:1px solid rgba(247,106,124,.35);color:var(--red)}

  details{background:var(--surface);border:1px solid var(--border);border-radius:12px;margin-bottom:.6rem}
  summary{cursor:pointer;padding:.85rem 1.1rem;font-size:.85rem;display:flex;align-items:center;gap:.7rem;flex-wrap:wrap;list-style:none;font-family:'Sora',sans-serif;font-weight:700}
  summary::-webkit-details-marker{display:none}
  summary .cnt{margin-left:auto;font-family:'DM Mono',monospace;font-size:.75rem;color:var(--muted);font-weight:400}
  .opp-list{padding:.2rem 1.1rem 1rem}
  .opp-row{padding:.7rem 0;border-top:1px solid var(--border);display:flex;gap:.8rem;align-items:flex-start}
  .opp-row:first-child{border-top:none}
  .opp-body .lbl{font-weight:600;font-size:.86rem}
  .opp-body .det{font-size:.78rem;color:var(--muted);margin-top:.15rem}
  .empty-note{font-size:.8rem;color:var(--muted);padding:.9rem 1.1rem}

  .dl-row{display:flex;gap:.8rem;margin-top:1.4rem;flex-wrap:wrap}
  .dl-row a{display:inline-flex;align-items:center;gap:.5rem;background:var(--surface);border:1px solid var(--border);color:var(--text);padding:.55rem 1.2rem;border-radius:8px;font-size:.8rem;font-weight:500}
  .dl-row a:hover{border-color:var(--accent)}

  .spinner{display:none;margin-top:.8rem;font-size:.78rem;color:var(--muted);font-family:'DM Mono',monospace}
  .spinner.show{display:block}

  @media(max-width:960px){.scorerow{grid-template-columns:1fr}.vt-row{grid-template-columns:1fr;gap:.3rem}.sidebar{display:none}.main{margin-left:0;padding:1.2rem}}
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
      <a href="#" class="sidebar-link active">⚡&nbsp; Page Speed Checker</a>
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
      <div>
        <div class="crumb">// seo tool · free (needs a one-time free Google API key)</div>
        <h1>⚡ Page <span>Speed Checker</span></h1>
      </div>
    </div>

    <form class="card" method="POST" action="/analyze" onsubmit="document.getElementById('sp').classList.add('show')">
      <div class="form-row">
        <div class="form-field">
          <label>Website URL</label>
          <input type="text" name="url" placeholder="https://example.com/page" value="{{ raw_url or '' }}" required>
        </div>
        <button class="btn" type="submit">🚀 Run Speed Test</button>
      </div>
      <div class="spinner" id="sp">Running Lighthouse on mobile &amp; desktop via Google PageSpeed Insights — usually 15-25 seconds…</div>
      {% if error %}<div class="hint" style="color:var(--red)">{{ error }}</div>{% endif %}
      <div class="hint">Powered by Google's free PageSpeed Insights API — tests both mobile and desktop in one run.</div>
    </form>

    {% if view %}
    <div class="scorerow">
      {% for s in view.strategies %}
      <div class="chart-card">
        <h3>{{ s.label }} PERFORMANCE</h3>
        {% if s.error %}
        <div class="strategy-error">⚠ {{ s.error }}</div>
        {% else %}
        <div class="ring-wrap">
          <canvas id="ring_{{ s.key }}"></canvas>
          <div class="ring-center">
            <div class="score" style="color:{{ '#6af7c8' if s.score >= 90 else '#f7a26a' if s.score >= 50 else '#f76a7c' }}">{{ s.score }}</div>
            <div class="lbl">/ 100</div>
          </div>
        </div>
        {% if not s.has_field %}<div class="hint" style="text-align:center;margin-top:.6rem">Lab data only — not enough real-user traffic in Chrome UX Report yet.</div>{% endif %}
        {% endif %}
      </div>
      {% endfor %}
      <div class="chart-card">
        <h3>LAB TIMING (SECONDS) — MOBILE VS DESKTOP</h3>
        <canvas id="labchart" height="150"></canvas>
      </div>
    </div>

    <div class="sec-title">🎯 Core Web Vitals — Mobile vs Desktop</div>
    <div class="vitals-table">
      <div class="vt-row">
        <div></div><div style="font-family:'DM Mono',monospace;font-size:.68rem;color:var(--muted);text-transform:uppercase">📱 Mobile</div><div style="font-family:'DM Mono',monospace;font-size:.68rem;color:var(--muted);text-transform:uppercase">🖥️ Desktop</div>
      </div>
      {% for v in view.vitals %}
      <div class="vt-row">
        <div class="vt-metric"><div class="m-name">{{ v.label }}</div><div class="m-help">{{ v.help }}</div></div>
        {% for m in [v.mobile, v.desktop] %}
        <div class="vt-val">
          {% if m %}
            <span class="badge {{ m.rating or '' }}">{{ m.display }}</span>
            <span class="note">{{ m.metric_name }} · {{ m.source }}</span>
          {% else %}
            <span class="note">—</span>
          {% endif %}
        </div>
        {% endfor %}
      </div>
      {% endfor %}
    </div>

    <div class="dl-row">
      <a href="/download/{{ token }}">⬇️ Download Full Report (CSV)</a>
    </div>

    <div class="sec-title">💡 Top Opportunities</div>
    {% for s in view.strategies %}
    <details {% if not loop.first %}{% endif %} open>
      <summary>{{ s.label }} <span class="cnt">{{ s.opportunities|length }} suggestion(s)</span></summary>
      {% if s.error %}
      <div class="empty-note">Couldn't run this strategy: {{ s.error }}</div>
      {% elif s.opportunities %}
      <div class="opp-list">
        {% for o in s.opportunities %}
        <div class="opp-row">
          <span class="badge warn">~{{ '%.0f'|format(o.savings_ms) }} ms</span>
          <div class="opp-body">
            <div class="lbl">{{ o.title }}</div>
            <div class="det">{{ o.description }}</div>
          </div>
        </div>
        {% endfor %}
      </div>
      {% else %}
      <div class="empty-note">No major opportunities flagged — nice and lean already.</div>
      {% endif %}
    </details>
    {% endfor %}

    <script>
      const MUTED = '#8888a8', GRID = 'rgba(136,136,168,0.12)';

      {% for s in view.strategies %}
      {% if not s.error %}
      new Chart(document.getElementById('ring_{{ s.key }}'), {
        type: 'doughnut',
        data: { datasets: [{ data: [{{ s.score }}, {{ 100 - s.score }}],
          backgroundColor: ['{{ "#6af7c8" if s.score >= 90 else "#f7a26a" if s.score >= 50 else "#f76a7c" }}', '#23233a'],
          borderWidth: 0, cutout: '78%' }] },
        options: { plugins: { legend: { display: false }, tooltip: { enabled: false } } }
      });
      {% endif %}
      {% endfor %}

      new Chart(document.getElementById('labchart'), {
        type: 'bar',
        data: {
          labels: ['FCP', 'LCP', 'Speed Index', 'Time to Interactive'],
          datasets: [
            {
              label: 'Mobile',
              data: [
                {{ view.strategies[0].lab_chart.fcp_s }}, {{ view.strategies[0].lab_chart.lcp_s }},
                {{ view.strategies[0].lab_chart.si_s }}, {{ view.strategies[0].lab_chart.tti_s }}
              ],
              backgroundColor: '#7c6af7', borderRadius: 5
            },
            {
              label: 'Desktop',
              data: [
                {{ view.strategies[1].lab_chart.fcp_s }}, {{ view.strategies[1].lab_chart.lcp_s }},
                {{ view.strategies[1].lab_chart.si_s }}, {{ view.strategies[1].lab_chart.tti_s }}
              ],
              backgroundColor: '#6af7c8', borderRadius: 5
            }
          ]
        },
        options: {
          indexAxis: 'y',
          plugins: { legend: { position: 'top', labels: { color: MUTED, boxWidth: 10, font: { size: 11 } } } },
          scales: {
            x: { ticks: { color: MUTED, callback: (v) => v + 's' }, grid: { color: GRID } },
            y: { ticks: { color: MUTED, font: { size: 10.5 } }, grid: { display: false } }
          }
        }
      });
    </script>
    {% endif %}
  </main>
</div>
</body>
</html>
"""


# ---------------------------------------------------------------- routes
@app.route("/", methods=["GET"])
def home():
    return render_template_string(PAGE, view=None, error=None, raw_url="")


@app.route("/analyze", methods=["POST"])
def analyze():
    raw_url = request.form.get("url", "")
    url = clean_url(raw_url)
    if not url:
        return render_template_string(PAGE, view=None, error="Please enter a URL.", raw_url=raw_url)

    run = run_both(url)
    if not run["results"]:
        # both strategies failed — surface the first error as the top-level message
        first_error = next(iter(run["errors"].values()), "Something went wrong running the speed test.")
        return render_template_string(PAGE, view=None, error=first_error, raw_url=raw_url)

    view = build_view(url, run)

    token = uuid.uuid4().hex[:12]
    STORE[token] = build_export_rows(view)
    while len(STORE) > STORE_LIMIT:
        STORE.pop(next(iter(STORE)))

    return render_template_string(PAGE, view=view, error=None, raw_url=raw_url, token=token)


@app.route("/download/<token>")
def download(token):
    rows = STORE.get(token)
    if rows is None:
        abort(404)
    text_buf = io.StringIO()
    writer = csv.DictWriter(text_buf, fieldnames=["Strategy", "Type", "Name", "Value", "Detail"])
    writer.writeheader()
    writer.writerows(rows)
    buf = io.BytesIO(text_buf.getvalue().encode("utf-8-sig"))
    return send_file(buf, as_attachment=True, download_name="page_speed_report.csv", mimetype="text/csv")


if __name__ == "__main__":
    app.run(debug=True, port=8501)
