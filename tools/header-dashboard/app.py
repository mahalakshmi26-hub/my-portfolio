"""
Header Tags Extractor & Checker — Dashboard Edition
Extract H1–H6 tags from multiple URLs, view heading structure & flag issues.
Flask app | by Mahalakshmi Marimuthu
"""

import concurrent.futures
import io
import uuid

import pandas as pd
import requests
from bs4 import BeautifulSoup
from flask import Flask, abort, render_template_string, request, send_file

app = Flask(__name__)

# in-memory store for download exports (keeps the last 20 runs)
STORE = {}
STORE_LIMIT = 20

FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}

MAX_URLS = 200


# ---------------------------------------------------------------- core logic
def analyze_headings(headings: list) -> dict:
    """Run SEO checks on an ordered list of (level, text) heading tuples."""
    h1_count = sum(1 for lvl, _ in headings if lvl == 1)
    skips = []
    prev = None
    for lvl, _ in headings:
        if prev is not None and lvl > prev + 1:
            skips.append(f"H{prev} → H{lvl}")
        prev = lvl
    issues = []
    if h1_count == 0:
        issues.append("Missing H1")
    if h1_count > 1:
        issues.append(f"Multiple H1s ({h1_count})")
    if skips:
        issues.append("Skipped levels: " + ", ".join(skips))
    return {"h1_count": h1_count, "skips": skips, "issues": issues}


def extract_headings(url: str) -> dict:
    """Fetch one URL and pull out its H1–H6 tags in document order."""
    row = {"URL": url, "Status": "", "Headings": [], "Checks": {}}
    try:
        resp = requests.get(url, headers=FETCH_HEADERS, timeout=15, allow_redirects=True)
        row["Status"] = str(resp.status_code)
        soup = BeautifulSoup(resp.text, "html.parser")
        headings = [
            (int(tag.name[1]), tag.get_text(strip=True))
            for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
        ]
        row["Headings"] = headings
        row["Checks"] = analyze_headings(headings)
    except requests.exceptions.Timeout:
        row["Status"] = "Timeout"
        row["Checks"] = {"h1_count": 0, "skips": [], "issues": ["Could not fetch"]}
    except requests.exceptions.RequestException as e:
        row["Status"] = f"Error: {type(e).__name__}"
        row["Checks"] = {"h1_count": 0, "skips": [], "issues": ["Could not fetch"]}
    return row


def clean_urls(raw: str) -> list:
    urls = []
    for line in raw.splitlines():
        u = line.strip()
        if not u:
            continue
        if not u.startswith(("http://", "https://")):
            u = "https://" + u
        if u not in urls:
            urls.append(u)
    return urls


def run_analysis(urls: list) -> list:
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(extract_headings, u): u for u in urls}
        for fut in concurrent.futures.as_completed(futures):
            results.append(fut.result())
    order = {u: i for i, u in enumerate(urls)}
    results.sort(key=lambda r: order[r["URL"]])
    return results


def build_view(results: list) -> dict:
    """Everything the dashboard template needs."""
    pages = []
    level_totals = {n: 0 for n in range(1, 7)}
    counts = {"missing": 0, "multiple": 0, "skipped": 0, "fetch_err": 0, "clean": 0}
    for r in results:
        c = r["Checks"]
        lvls = {n: 0 for n in range(1, 7)}
        for lvl, _ in r["Headings"]:
            lvls[lvl] += 1
            level_totals[lvl] += 1
        badges = []
        if "Could not fetch" in c.get("issues", []):
            badges.append(("error", "Fetch failed"))
            counts["fetch_err"] += 1
        else:
            if c.get("h1_count", 0) == 0:
                badges.append(("error", "Missing H1"))
                counts["missing"] += 1
            if c.get("h1_count", 0) > 1:
                badges.append(("warn", f"{c['h1_count']}× H1"))
                counts["multiple"] += 1
            if c.get("skips"):
                badges.append(("warn", "Skipped: " + ", ".join(c["skips"])))
                counts["skipped"] += 1
        if not badges:
            badges.append(("ok", "All good"))
            counts["clean"] += 1
        pages.append(
            {
                "url": r["URL"],
                "status": r["Status"],
                "total": len(r["Headings"]),
                "lvls": lvls,
                "badges": badges,
                "headings": r["Headings"],
                "has_issues": badges[0][0] != "ok",
            }
        )
    n = len(pages)
    health = round(100 * counts["clean"] / n) if n else 0
    return {
        "pages": pages,
        "n": n,
        "ok_fetch": sum(1 for p in pages if p["status"] == "200"),
        "health": health,
        "counts": counts,
        "level_totals": level_totals,
    }


def build_export_frames(results: list):
    summary_rows, flat_rows = [], []
    for r in results:
        c = r["Checks"]
        lvls = {n: 0 for n in range(1, 7)}
        by_level = {n: [] for n in range(1, 7)}
        for lvl, txt in r["Headings"]:
            lvls[lvl] += 1
            by_level[lvl].append(txt or "(empty)")
        summary_rows.append(
            {
                "URL": r["URL"],
                "Status": r["Status"],
                "Total Headings": len(r["Headings"]),
                "H1": lvls[1], "H2": lvls[2], "H3": lvls[3],
                "H4": lvls[4], "H5": lvls[5], "H6": lvls[6],
                "Missing H1": "Yes" if c.get("h1_count", 0) == 0 else "No",
                "Multiple H1s": "Yes" if c.get("h1_count", 0) > 1 else "No",
                "Skipped Levels": ", ".join(c.get("skips", [])) or "None",
                "Issues": "; ".join(c.get("issues", [])) or "All good",
            }
        )
        flat_rows.append(
            {
                "URL": r["URL"],
                **{f"H{n}": " | ".join(by_level[n]) for n in range(1, 7)},
            }
        )
    return pd.DataFrame(summary_rows), pd.DataFrame(flat_rows)


# ---------------------------------------------------------------- template
PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Header Tags Extractor & Checker</title>
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

  /* layout */
  .shell{display:flex;min-height:100vh}
  .sidebar{width:230px;background:var(--surface);border-right:1px solid var(--border);padding:1.4rem 1rem;position:fixed;top:0;bottom:0;display:flex;flex-direction:column}
  .main{flex:1;margin-left:230px;padding:1.6rem 2rem 3rem;max-width:1400px}
  .logo{display:flex;align-items:center;gap:.6rem;font-family:'Sora',sans-serif;font-weight:800;font-size:.95rem;margin-bottom:2rem}
  .logo-mark{width:30px;height:30px;border-radius:8px;background:var(--accent);display:flex;align-items:center;justify-content:center;color:#fff;font-size:1rem}
  .nav-item{display:flex;align-items:center;gap:.6rem;padding:.55rem .8rem;border-radius:8px;color:var(--muted);font-size:.85rem;font-weight:500;margin-bottom:.2rem}
  .nav-item.active{background:rgba(124,106,247,.14);color:var(--text);border:1px solid rgba(124,106,247,.3)}
  .nav-item:hover{color:var(--text)}
  .sidebar-foot{margin-top:auto;font-size:.68rem;color:var(--muted);line-height:1.5}
  .sidebar-foot a{color:var(--mint)}

  .topbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:1.4rem;flex-wrap:wrap;gap:.8rem}
  .topbar h1{font-family:'Sora',sans-serif;font-size:1.25rem;font-weight:700}
  .topbar h1 span{color:var(--accent)}
  .crumb{font-family:'DM Mono',monospace;font-size:.68rem;color:var(--muted);letter-spacing:.12em;text-transform:uppercase}

  /* cards */
  .card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.2rem 1.4rem}
  textarea{width:100%;min-height:130px;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:.8rem 1rem;font-family:'DM Mono',monospace;font-size:.8rem;resize:vertical}
  textarea:focus{outline:none;border-color:var(--accent)}
  .btn{display:inline-flex;align-items:center;gap:.5rem;background:var(--accent);color:#fff;border:none;border-radius:8px;padding:.6rem 1.5rem;font-size:.85rem;font-weight:600;cursor:pointer;font-family:'Inter',sans-serif;margin-top:.8rem}
  .btn:hover{background:var(--accent-h)}
  .hint{font-size:.72rem;color:var(--muted);margin-top:.5rem;font-family:'DM Mono',monospace}

  /* metric row */
  .metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1rem;margin:1.4rem 0}
  .metric{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1rem 1.2rem}
  .metric .k{font-family:'DM Mono',monospace;font-size:.66rem;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
  .metric .v{font-family:'Sora',sans-serif;font-size:1.6rem;font-weight:800;margin-top:.2rem}
  .metric .v.purple{color:var(--accent)} .metric .v.mint{color:var(--mint)}
  .metric .v.orange{color:var(--orange)} .metric .v.red{color:var(--red)}

  /* charts row */
  .charts{display:grid;grid-template-columns:280px 1fr 1fr;gap:1rem;margin-bottom:1.4rem}
  .chart-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.1rem 1.2rem}
  .chart-card h3{font-family:'Sora',sans-serif;font-size:.82rem;font-weight:700;margin-bottom:.8rem;color:var(--muted)}
  .ring-wrap{position:relative;width:170px;height:170px;margin:0 auto}
  .ring-center{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
  .ring-center .score{font-family:'Sora',sans-serif;font-size:2rem;font-weight:800}
  .ring-center .lbl{font-size:.62rem;font-family:'DM Mono',monospace;color:var(--muted);text-transform:uppercase;letter-spacing:.12em}

  /* table */
  .tbl-wrap{overflow-x:auto;background:var(--surface);border:1px solid var(--border);border-radius:12px}
  table{width:100%;border-collapse:collapse;font-size:.8rem;min-width:900px}
  th{font-family:'DM Mono',monospace;font-size:.62rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);text-align:left;padding:.7rem .9rem;border-bottom:1px solid var(--border);white-space:nowrap}
  td{padding:.65rem .9rem;border-bottom:1px solid var(--border);vertical-align:top}
  tr:last-child td{border-bottom:none}
  tr:hover td{background:rgba(124,106,247,.05)}
  td.url{max-width:340px;word-break:break-all;font-family:'DM Mono',monospace;font-size:.72rem}
  td.num{text-align:center;font-family:'DM Mono',monospace}
  .badge{display:inline-block;font-size:.64rem;font-family:'DM Mono',monospace;padding:.18rem .55rem;border-radius:100px;margin:.1rem .15rem .1rem 0;white-space:nowrap}
  .badge.ok{background:rgba(106,247,200,.1);border:1px solid rgba(106,247,200,.3);color:var(--mint)}
  .badge.warn{background:rgba(247,162,106,.1);border:1px solid rgba(247,162,106,.35);color:var(--orange)}
  .badge.error{background:rgba(247,106,124,.1);border:1px solid rgba(247,106,124,.35);color:var(--red)}
  .status-pill{font-family:'DM Mono',monospace;font-size:.68rem}
  .status-pill.good{color:var(--mint)} .status-pill.bad{color:var(--red)}

  /* heading trees */
  .sec-title{font-family:'Sora',sans-serif;font-size:1rem;font-weight:700;margin:1.8rem 0 .9rem}
  details{background:var(--surface);border:1px solid var(--border);border-radius:12px;margin-bottom:.6rem}
  summary{cursor:pointer;padding:.8rem 1.1rem;font-size:.8rem;display:flex;align-items:center;gap:.6rem;flex-wrap:wrap;list-style:none}
  summary::-webkit-details-marker{display:none}
  summary .u{font-family:'DM Mono',monospace;font-size:.72rem;word-break:break-all}
  .htree{padding:.4rem 1.3rem 1rem;font-family:'DM Mono',monospace;font-size:.76rem;line-height:2.1}
  .htree .lvl{color:var(--accent);font-weight:700}
  .htree .empty{color:var(--muted);font-style:italic}
  .dl-row{display:flex;gap:.8rem;margin-top:1.4rem;flex-wrap:wrap}
  .dl-row a{display:inline-flex;align-items:center;gap:.5rem;background:var(--surface);border:1px solid var(--border);color:var(--text);padding:.55rem 1.2rem;border-radius:8px;font-size:.8rem;font-weight:500}
  .dl-row a:hover{border-color:var(--accent)}

  .spinner{display:none;margin-top:.8rem;font-size:.78rem;color:var(--muted);font-family:'DM Mono',monospace}
  .spinner.show{display:block}

  @media(max-width:960px){.charts{grid-template-columns:1fr}.sidebar{display:none}.main{margin-left:0;padding:1.2rem}}
</style>
</head>
<body>
<div class="shell">

  <aside class="sidebar">
    <div class="logo"><div class="logo-mark">M</div>SEO Tools</div>
    <div class="nav-item active">📊&nbsp; Header Tag Audit</div>
    <a class="nav-item" href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/" target="_blank">🧰&nbsp; All My Tools</a>
    <a class="nav-item" href="https://mahalakshmi26-hub.github.io/my-portfolio/" target="_blank">👩‍💻&nbsp; Portfolio</a>
    <div class="sidebar-foot">
      Built by <a href="https://mahalakshmi26-hub.github.io/my-portfolio/" target="_blank">Mahalakshmi Marimuthu</a><br>
      Digital Marketing Strategist &amp; AI-Powered SEO Expert
    </div>
  </aside>

  <main class="main">
    <div class="topbar">
      <div>
        <div class="crumb">// seo tool · free</div>
        <h1>🏷️ Header Tags <span>Extractor &amp; Checker</span></h1>
      </div>
    </div>

    <form class="card" method="POST" action="/analyze" onsubmit="document.getElementById('sp').classList.add('show')">
      <label style="font-size:.8rem;color:var(--muted);display:block;margin-bottom:.5rem">Enter URLs (one per line, up to {{ max_urls }})</label>
      <textarea name="urls" placeholder="https://example.com&#10;https://example.com/page-2&#10;example.com/page-3">{{ raw_input or "" }}</textarea>
      <button class="btn" type="submit">🚀 Run Audit</button>
      <div class="spinner" id="sp">Fetching pages… this can take a moment for many URLs</div>
      {% if error %}<div class="hint" style="color:var(--red)">{{ error }}</div>{% endif %}
    </form>

    {% if view %}
    <!-- metrics -->
    <div class="metrics">
      <div class="metric"><div class="k">URLs Audited</div><div class="v purple">{{ view.n }}</div></div>
      <div class="metric"><div class="k">Fetched OK (200)</div><div class="v mint">{{ view.ok_fetch }}</div></div>
      <div class="metric"><div class="k">Missing H1</div><div class="v red">{{ view.counts.missing }}</div></div>
      <div class="metric"><div class="k">Multiple H1s</div><div class="v orange">{{ view.counts.multiple }}</div></div>
      <div class="metric"><div class="k">Skipped Levels</div><div class="v orange">{{ view.counts.skipped }}</div></div>
      <div class="metric"><div class="k">Clean Pages</div><div class="v mint">{{ view.counts.clean }}</div></div>
    </div>

    <!-- charts -->
    <div class="charts">
      <div class="chart-card">
        <h3>STRUCTURE HEALTH</h3>
        <div class="ring-wrap">
          <canvas id="ring"></canvas>
          <div class="ring-center">
            <div class="score" style="color:{{ '#6af7c8' if view.health >= 80 else '#f7a26a' if view.health >= 50 else '#f76a7c' }}">{{ view.health }}%</div>
            <div class="lbl">clean pages</div>
          </div>
        </div>
      </div>
      <div class="chart-card">
        <h3>ISSUE BREAKDOWN</h3>
        <canvas id="donut" height="170"></canvas>
      </div>
      <div class="chart-card">
        <h3>HEADING DISTRIBUTION (ALL PAGES)</h3>
        <canvas id="bars" height="170"></canvas>
      </div>
    </div>

    <!-- table -->
    <div class="tbl-wrap">
      <table>
        <thead><tr>
          <th>URL</th><th>Status</th><th>Total</th>
          <th>H1</th><th>H2</th><th>H3</th><th>H4</th><th>H5</th><th>H6</th>
          <th>Issues</th>
        </tr></thead>
        <tbody>
        {% for p in view.pages %}
          <tr>
            <td class="url">{{ p.url }}</td>
            <td><span class="status-pill {{ 'good' if p.status == '200' else 'bad' }}">{{ p.status }}</span></td>
            <td class="num">{{ p.total }}</td>
            {% for n in range(1, 7) %}<td class="num">{{ p.lvls[n] }}</td>{% endfor %}
            <td>{% for cls, label in p.badges %}<span class="badge {{ cls }}">{{ label }}</span>{% endfor %}</td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
    </div>

    <div class="dl-row">
      <a href="/download/{{ token }}/csv">⬇️ Summary CSV</a>
      <a href="/download/{{ token }}/xlsx">⬇️ Excel (Summary + Headings)</a>
    </div>

    <!-- heading trees -->
    <div class="sec-title">🌳 Heading Structure</div>
    {% for p in view.pages %}
    <details>
      <summary>
        {% if p.has_issues %}<span class="badge warn">⚠</span>{% else %}<span class="badge ok">✓</span>{% endif %}
        <span class="u">{{ p.url }}</span>
        <span style="color:var(--muted);font-size:.7rem">· {{ p.total }} headings</span>
      </summary>
      <div class="htree">
        {% if not p.headings %}<span class="empty">No heading tags found on this page.</span>{% endif %}
        {% for lvl, txt in p.headings %}
          <div style="padding-left:{{ (lvl - 1) * 26 }}px"><span class="lvl">H{{ lvl }}</span> {% if txt %}{{ txt }}{% else %}<span class="empty">(empty)</span>{% endif %}</div>
        {% endfor %}
      </div>
    </details>
    {% endfor %}

    <script>
      const P = {
        health: {{ view.health }},
        issues: [{{ view.counts.missing }}, {{ view.counts.multiple }}, {{ view.counts.skipped }}, {{ view.counts.fetch_err }}, {{ view.counts.clean }}],
        levels: [{{ view.level_totals[1] }}, {{ view.level_totals[2] }}, {{ view.level_totals[3] }}, {{ view.level_totals[4] }}, {{ view.level_totals[5] }}, {{ view.level_totals[6] }}]
      };
      const MUTED = '#8888a8', GRID = 'rgba(136,136,168,0.12)';
      new Chart(document.getElementById('ring'), {
        type: 'doughnut',
        data: { datasets: [{ data: [P.health, 100 - P.health],
          backgroundColor: [P.health >= 80 ? '#6af7c8' : P.health >= 50 ? '#f7a26a' : '#f76a7c', '#23233a'],
          borderWidth: 0, cutout: '78%' }] },
        options: { plugins: { legend: { display: false }, tooltip: { enabled: false } } }
      });
      new Chart(document.getElementById('donut'), {
        type: 'doughnut',
        data: { labels: ['Missing H1', 'Multiple H1s', 'Skipped levels', 'Fetch failed', 'Clean'],
          datasets: [{ data: P.issues, borderWidth: 0, cutout: '60%',
            backgroundColor: ['#f76a7c', '#f7a26a', '#f7d06a', '#8888a8', '#6af7c8'] }] },
        options: { plugins: { legend: { position: 'right', labels: { color: MUTED, boxWidth: 10, font: { size: 11 } } } } }
      });
      new Chart(document.getElementById('bars'), {
        type: 'bar',
        data: { labels: ['H1', 'H2', 'H3', 'H4', 'H5', 'H6'],
          datasets: [{ data: P.levels, backgroundColor: '#7c6af7', borderRadius: 6 }] },
        options: { plugins: { legend: { display: false } },
          scales: { x: { ticks: { color: MUTED }, grid: { display: false } },
                    y: { ticks: { color: MUTED, precision: 0 }, grid: { color: GRID } } } }
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
    return render_template_string(PAGE, view=None, error=None, raw_input="", max_urls=MAX_URLS)


@app.route("/analyze", methods=["POST"])
def analyze():
    raw = request.form.get("urls", "")
    urls = clean_urls(raw)
    if not urls:
        return render_template_string(
            PAGE, view=None, error="Please enter at least one URL.", raw_input=raw, max_urls=MAX_URLS
        )
    urls = urls[:MAX_URLS]
    results = run_analysis(urls)

    token = uuid.uuid4().hex[:12]
    STORE[token] = results
    while len(STORE) > STORE_LIMIT:
        STORE.pop(next(iter(STORE)))

    return render_template_string(
        PAGE, view=build_view(results), error=None, raw_input=raw, token=token, max_urls=MAX_URLS
    )


@app.route("/download/<token>/<fmt>")
def download(token, fmt):
    results = STORE.get(token)
    if results is None:
        abort(404)
    summary_df, flat_df = build_export_frames(results)
    if fmt == "csv":
        buf = io.BytesIO(summary_df.to_csv(index=False).encode("utf-8-sig"))
        return send_file(buf, as_attachment=True, download_name="header_check_summary.csv", mimetype="text/csv")
    if fmt == "xlsx":
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            summary_df.to_excel(writer, index=False, sheet_name="Summary")
            flat_df.to_excel(writer, index=False, sheet_name="All Headings")
        buf.seek(0)
        return send_file(
            buf, as_attachment=True, download_name="header_tags_report.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    abort(404)


if __name__ == "__main__":
    app.run(debug=True, port=8501)
