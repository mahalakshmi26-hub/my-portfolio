import re
from collections import Counter

from flask import Flask, render_template_string, request, jsonify
import requests
from bs4 import BeautifulSoup

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Core analysis logic
# ---------------------------------------------------------------------------

STOPWORDS = set("""
a about above after again against all am an and any are aren't as at be
because been before being below between both but by can't cannot could
couldn't did didn't do does doesn't doing don't down during each few for
from further had hadn't has hasn't have haven't having he he'd he'll he's
her here here's hers herself him himself his how how's i i'd i'll i'm i've
if in into is isn't it it's its itself let's me more most mustn't my
myself no nor not of off on once only or other ought our ours ourselves
out over own same shan't she she'd she'll she's should shouldn't so some
such than that that's the their theirs them themselves then there
there's these they they'd they'll they're they've this those through to
too under until up very was wasn't we we'd we'll we're we've were
weren't what what's when when's where where's which while who who's
whom why why's with won't would wouldn't you you'd you'll you're you've
your yours yourself yourselves
""".split())

# A real browser UA so WAFs don't block the fetch outright; never identify
# this as a bot/checker tool.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*[A-Za-z]|[A-Za-z]")

MAX_CHARS = 300_000


def fetch_url_text(url, timeout=15):
    """Fetch a URL and return (visible_text, final_url). Retries once on
    403/429/503 in case of a transient WAF block."""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url

    headers = {
        "User-Agent": BROWSER_UA,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    last_error = None
    for attempt in range(2):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            if resp.status_code in (403, 429, 503) and attempt == 0:
                last_error = f"HTTP {resp.status_code}"
                continue
            resp.raise_for_status()
            return extract_visible_text(resp.text), resp.url
        except requests.exceptions.RequestException as exc:
            last_error = str(exc)
    raise RuntimeError(
        f"Couldn't fetch that URL ({last_error}). The site may be blocking "
        f"automated requests — try pasting the page text instead."
    )


def extract_visible_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "form",
                      "nav", "footer", "header", "button", "input", "title"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    return re.sub(r"\s+", " ", text).strip()


def tokenize(text):
    words = WORD_RE.findall(text.lower())
    cleaned = []
    for w in words:
        w = w.strip("'-")
        if len(w) > 1 or w.isalpha():
            cleaned.append(w)
    return cleaned


def ngram_counts(tokens, n, exclude_leading_stopword=True):
    counter = Counter()
    for i in range(len(tokens) - n + 1):
        gram = tokens[i:i + n]
        if n == 1 and exclude_leading_stopword and gram[0] in STOPWORDS:
            continue
        counter[" ".join(gram)] += 1
    return counter


def status_for_density(pct):
    if pct < 1:
        return "low"
    if pct <= 3:
        return "healthy"
    return "high"


def analyze_text(text, target_keyword=None, top_n=15):
    tokens = tokenize(text)
    total_words = len(tokens)
    if total_words == 0:
        raise ValueError("No readable text found to analyze.")

    result = {
        "total_words": total_words,
        "unique_words": len(set(tokens)),
    }

    for n, label in ((1, "unigrams"), (2, "bigrams"), (3, "trigrams"), (4, "fourgrams")):
        counts = ngram_counts(tokens, n)
        rows = []
        for phrase, count in counts.most_common(top_n):
            pct = round((count / total_words) * 100, 2)
            rows.append({
                "phrase": phrase,
                "count": count,
                "density": pct,
                "status": status_for_density(pct),
            })
        result[label] = rows

    target_keyword = (target_keyword or "").strip()
    if target_keyword:
        kw_tokens = tokenize(target_keyword)
        n = len(kw_tokens)
        if n == 0:
            result["target"] = None
        else:
            counts = ngram_counts(tokens, n, exclude_leading_stopword=False)
            phrase = " ".join(kw_tokens)
            count = counts.get(phrase, 0)
            pct = round((count / total_words) * 100, 2)
            result["target"] = {
                "phrase": target_keyword,
                "count": count,
                "density": pct,
                "status": status_for_density(pct),
            }
    else:
        result["target"] = None

    return result


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(PAGE_HTML)


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    data = request.get_json(force=True, silent=True) or {}
    mode = data.get("mode")
    target_keyword = data.get("target_keyword") or ""

    try:
        if mode == "url":
            url = (data.get("url") or "").strip()
            if not url:
                return jsonify({"error": "Please enter a URL."}), 400
            text, final_url = fetch_url_text(url)
            source = final_url
        else:
            text = (data.get("text") or "").strip()
            if not text:
                return jsonify({"error": "Please paste some text to analyze."}), 400
            source = "Pasted text"

        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS]

        result = analyze_text(text, target_keyword=target_keyword)
        result["source"] = source
        return jsonify(result)

    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception:
        return jsonify({"error": "Something went wrong analyzing that content."}), 500


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

PAGE_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Keyword Density Checker | SEO Tools by Mahalakshmi</title>
<meta name="description" content="Check keyword and keyword-phrase density in any text or web page, free.">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%237c6af7'/%3E%3Ctext x='50%25' y='50%25' dominant-baseline='central' text-anchor='middle' font-family='Georgia%2C serif' font-size='18' font-weight='900' fill='white'%3EM%3C/text%3E%3C/svg%3E">
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;600;700;800;900&family=Inter:wght@300;400;500;600&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="/static/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0a0a0f; --surface: #12121a; --surface2: #1a1a26;
    --accent: #7c6af7; --accent2: #f7a26a; --accent3: #6af7c8; --danger: #f76a6a;
    --text: #e8e8f0; --muted: #8888a8; --border: rgba(124,106,247,0.18);
    --card: rgba(18,18,28,0.85);
  }
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); font-size: 15px; line-height: 1.6; }
  a { color: inherit; }

  .layout { display: flex; min-height: 100vh; }

  /* SIDEBAR — shared pattern across all Flask+Render tools */
  .sidebar { width: 250px; flex-shrink: 0; background: var(--surface); border-right: 1px solid var(--border); display: flex; flex-direction: column; padding: 1.5rem 1.1rem; position: sticky; top: 0; height: 100vh; overflow-y: auto; }
  .brand { display: flex; align-items: center; gap: 0.6rem; text-decoration: none; margin-bottom: 2rem; padding: 0 0.3rem; }
  .brand-mark { width: 30px; height: 30px; border-radius: 8px; background: var(--accent); display: flex; align-items: center; justify-content: center; font-family: 'Sora', sans-serif; font-weight: 900; color: #fff; flex-shrink: 0; }
  .brand-text { font-family: 'Sora', sans-serif; font-weight: 700; color: var(--text); font-size: 0.95rem; }
  .sidebar-section { margin-bottom: 1.6rem; }
  .sidebar-label { font-family: 'DM Mono', monospace; font-size: 0.65rem; letter-spacing: 0.12em; text-transform: uppercase; color: var(--muted); padding: 0 0.6rem; margin-bottom: 0.5rem; }
  .sidebar-link { display: flex; align-items: center; gap: 0.6rem; padding: 0.55rem 0.6rem; border-radius: 8px; text-decoration: none; color: var(--muted); font-size: 0.85rem; font-weight: 500; margin-bottom: 0.15rem; border: 1px solid transparent; transition: all 0.15s; }
  .sidebar-link:hover { background: rgba(255,255,255,0.04); color: var(--text); }
  .sidebar-link.active { background: rgba(124,106,247,0.14); border: 1px solid rgba(124,106,247,0.4); color: var(--accent); font-weight: 700; }
  .sidebar-info { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 0.9rem; font-size: 0.76rem; color: var(--muted); line-height: 1.6; margin-bottom: 1.6rem; }
  .sidebar-info strong { color: var(--text); }
  .sidebar-footer { margin-top: auto; padding-top: 1.2rem; border-top: 1px solid var(--border); font-size: 0.72rem; color: var(--muted); line-height: 1.6; }
  .sidebar-footer .credit-name { color: var(--accent3); font-weight: 700; text-decoration: none; }
  .sidebar-social { display: flex; gap: 0.5rem; margin-top: 0.7rem; }
  .sidebar-social a { width: 28px; height: 28px; border-radius: 6px; background: rgba(106,247,200,0.08); border: 1px solid rgba(106,247,200,0.35); color: var(--accent3); display: flex; align-items: center; justify-content: center; font-family: 'DM Mono', monospace; font-size: 0.7rem; text-decoration: none; transition: all 0.15s; }
  .sidebar-social a:hover { background: rgba(106,247,200,0.18); }

  /* MAIN */
  .main { flex: 1; padding: 2.2rem 2.6rem 4rem; max-width: 1180px; }
  .page-head { margin-bottom: 1.8rem; }
  .page-eyebrow { font-family: 'DM Mono', monospace; font-size: 0.68rem; letter-spacing: 0.16em; text-transform: uppercase; color: var(--accent3); margin-bottom: 0.6rem; }
  .page-title { font-family: 'Sora', sans-serif; font-size: 1.9rem; font-weight: 800; margin-bottom: 0.4rem; }
  .page-sub { color: var(--muted); font-size: 0.93rem; max-width: 640px; }

  .card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 1.6rem; margin-bottom: 1.4rem; }
  .card h2 { font-family: 'Sora', sans-serif; font-size: 1.02rem; font-weight: 700; margin-bottom: 1rem; }

  /* INPUT MODE TABS */
  .mode-tabs { display: inline-flex; background: var(--surface2); border-radius: 9px; padding: 3px; margin-bottom: 1.1rem; border: 1px solid var(--border); }
  .mode-tab { border: none; background: transparent; color: var(--muted); font-family: 'Inter', sans-serif; font-size: 0.82rem; font-weight: 600; padding: 0.5rem 1.1rem; border-radius: 7px; cursor: pointer; transition: all 0.15s; }
  .mode-tab.active { background: var(--accent); color: #fff; }

  textarea, input[type="text"], input[type="url"] { width: 100%; background: var(--surface2); border: 1px solid var(--border); border-radius: 9px; color: var(--text); font-family: 'Inter', sans-serif; font-size: 0.88rem; padding: 0.75rem 0.9rem; resize: vertical; transition: border-color 0.15s; }
  textarea:focus, input:focus { outline: none; border-color: var(--accent); }
  textarea { min-height: 190px; line-height: 1.6; }
  .field { margin-bottom: 1rem; }
  .field label { display: block; font-size: 0.78rem; color: var(--muted); margin-bottom: 0.4rem; font-weight: 500; }
  .field-hint { font-size: 0.72rem; color: var(--muted); margin-top: 0.35rem; }
  .hidden { display: none !important; }

  .btn-primary { background: var(--accent); color: #fff; border: none; font-family: 'Inter', sans-serif; font-weight: 700; font-size: 0.9rem; padding: 0.75rem 1.6rem; border-radius: 9px; cursor: pointer; transition: background 0.15s, transform 0.15s; display: inline-flex; align-items: center; gap: 0.5rem; }
  .btn-primary:hover { background: #6a58e8; transform: translateY(-1px); }
  .btn-primary:disabled { opacity: 0.6; cursor: not-allowed; transform: none; }
  .btn-secondary { background: rgba(255,255,255,0.05); color: var(--text); border: 1px solid var(--border); font-family: 'Inter', sans-serif; font-weight: 600; font-size: 0.82rem; padding: 0.55rem 1.1rem; border-radius: 8px; cursor: pointer; transition: all 0.15s; }
  .btn-secondary:hover { border-color: var(--accent); color: var(--accent); }

  .error-box { background: rgba(247,106,106,0.1); border: 1px solid rgba(247,106,106,0.3); color: var(--danger); border-radius: 9px; padding: 0.75rem 1rem; font-size: 0.85rem; margin-top: 1rem; }

  /* STAT CARDS */
  .stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 0.9rem; margin-bottom: 1.4rem; }
  .stat-card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 1.1rem 1.2rem; }
  .stat-label { font-family: 'DM Mono', monospace; font-size: 0.65rem; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); margin-bottom: 0.5rem; }
  .stat-value { font-family: 'Sora', sans-serif; font-size: 1.55rem; font-weight: 800; }
  .stat-value.small { font-size: 1.1rem; word-break: break-word; }

  /* TARGET KEYWORD FOCUS CARD */
  .focus-card { display: flex; align-items: center; gap: 1.8rem; flex-wrap: wrap; }
  .ring-wrap { position: relative; width: 128px; height: 128px; flex-shrink: 0; }
  .ring-wrap svg { transform: rotate(-90deg); }
  .ring-center { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; }
  .ring-value { font-family: 'Sora', sans-serif; font-size: 1.3rem; font-weight: 800; }
  .ring-caption { font-size: 0.62rem; color: var(--muted); font-family: 'DM Mono', monospace; text-transform: uppercase; letter-spacing: 0.06em; }
  .focus-detail { flex: 1; min-width: 220px; }
  .focus-phrase { font-family: 'Sora', sans-serif; font-size: 1.15rem; font-weight: 700; margin-bottom: 0.4rem; }
  .focus-desc { color: var(--muted); font-size: 0.85rem; line-height: 1.7; }

  .badge { display: inline-flex; align-items: center; gap: 0.35rem; font-family: 'DM Mono', monospace; font-size: 0.68rem; font-weight: 500; padding: 0.28rem 0.65rem; border-radius: 100px; text-transform: uppercase; letter-spacing: 0.04em; }
  .badge.low { background: rgba(247,162,106,0.1); border: 1px solid rgba(247,162,106,0.3); color: var(--accent2); }
  .badge.healthy { background: rgba(106,247,200,0.1); border: 1px solid rgba(106,247,200,0.3); color: var(--accent3); }
  .badge.high { background: rgba(247,106,106,0.1); border: 1px solid rgba(247,106,106,0.3); color: var(--danger); }

  /* CHART */
  .chart-card canvas { max-height: 320px; }

  /* RESULT TABS + TABLE */
  .table-tabs { display: flex; gap: 0.4rem; margin-bottom: 1rem; flex-wrap: wrap; }
  .table-tab { border: 1px solid var(--border); background: var(--surface2); color: var(--muted); font-family: 'Inter', sans-serif; font-size: 0.8rem; font-weight: 600; padding: 0.45rem 0.95rem; border-radius: 8px; cursor: pointer; transition: all 0.15s; }
  .table-tab.active { background: rgba(124,106,247,0.14); border-color: rgba(124,106,247,0.4); color: var(--accent); }
  .table-head-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.9rem; flex-wrap: wrap; gap: 0.7rem; }

  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  thead th { text-align: left; font-family: 'DM Mono', monospace; font-size: 0.68rem; letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted); font-weight: 500; padding: 0.6rem 0.7rem; border-bottom: 1px solid var(--border); }
  tbody td { padding: 0.65rem 0.7rem; border-bottom: 1px solid rgba(255,255,255,0.04); }
  tbody tr:hover { background: rgba(255,255,255,0.02); }
  td.num { font-family: 'DM Mono', monospace; }
  .empty-note { color: var(--muted); font-size: 0.85rem; padding: 1rem 0; text-align: center; }

  .loading-note { color: var(--muted); font-size: 0.85rem; margin-top: 0.9rem; display: none; align-items: center; gap: 0.5rem; }
  .spinner { width: 14px; height: 14px; border: 2px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.7s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }

  @media (max-width: 860px) {
    .layout { flex-direction: column; }
    .sidebar { width: 100%; height: auto; position: relative; }
    .main { padding: 1.4rem 1.2rem 3rem; }
  }
</style>
</head>
<body>
<div class="layout">

  <aside class="sidebar">
    <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="brand">
      <span class="brand-mark">M</span>
      <span class="brand-text">SEO Tools</span>
    </a>
    <div class="sidebar-section">
      <div class="sidebar-label">Tool</div>
      <a href="#" class="sidebar-link active">🎯 Keyword Density Checker</a>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-label">Navigate</div>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="sidebar-link">🧰 All My Tools</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio" class="sidebar-link">👩‍💻 Portfolio</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/blog/index.html" class="sidebar-link">📝 Blog Posts</a>
    </div>
    <div class="sidebar-info">
      <strong>How it works:</strong> paste your content or a URL, optionally add a target keyword, and get single-word, 2-word and 3-word phrase density — plus a healthy-range check (1–3%).
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
    <div class="page-head">
      <div class="page-eyebrow">// free seo tool</div>
      <h1 class="page-title">🎯 Keyword Density Checker</h1>
      <p class="page-sub">See exactly how often your keywords and keyword phrases appear — paste content or a URL, check a target keyword's density, and spot thin or over-stuffed pages before Google does.</p>
    </div>

    <div class="card">
      <h2>Analyze content</h2>

      <div class="mode-tabs">
        <button type="button" class="mode-tab active" data-mode="text">Paste Text</button>
        <button type="button" class="mode-tab" data-mode="url">Fetch URL</button>
      </div>

      <div id="textMode" class="field">
        <label for="textInput">Content to analyze</label>
        <textarea id="textInput" placeholder="Paste your article, page copy, or any block of text here..."></textarea>
      </div>

      <div id="urlMode" class="field hidden">
        <label for="urlInput">Page URL</label>
        <input type="url" id="urlInput" placeholder="https://example.com/personal-loan">
        <div class="field-hint">We'll fetch the page and pull its visible text automatically.</div>
      </div>

      <div class="field">
        <label for="targetInput">Target keyword or phrase (optional)</label>
        <input type="text" id="targetInput" placeholder="e.g. personal loan interest rates">
        <div class="field-hint">Get an exact density reading and a healthy-range check for this specific keyword.</div>
      </div>

      <button type="button" class="btn-primary" id="analyzeBtn">🔍 Check Density</button>
      <div class="loading-note" id="loadingNote"><span class="spinner"></span> Analyzing…</div>
      <div class="error-box hidden" id="errorBox"></div>
    </div>

    <div id="resultsWrap" class="hidden">

      <div class="stat-grid" id="statGrid"></div>

      <div class="card hidden" id="focusCard">
        <h2>Target keyword focus</h2>
        <div class="focus-card">
          <div class="ring-wrap">
            <svg width="128" height="128" viewBox="0 0 128 128">
              <circle cx="64" cy="64" r="54" fill="none" stroke="rgba(255,255,255,0.06)" stroke-width="12"/>
              <circle id="ringProgress" cx="64" cy="64" r="54" fill="none" stroke="var(--accent3)" stroke-width="12" stroke-linecap="round" stroke-dasharray="339.3" stroke-dashoffset="339.3"/>
            </svg>
            <div class="ring-center">
              <div class="ring-value" id="ringValue">0%</div>
              <div class="ring-caption">density</div>
            </div>
          </div>
          <div class="focus-detail">
            <div class="focus-phrase" id="focusPhrase">—</div>
            <div id="focusBadge"></div>
            <p class="focus-desc" id="focusDesc" style="margin-top:0.6rem;"></p>
          </div>
        </div>
      </div>

      <div class="card chart-card">
        <div class="table-head-row">
          <h2 id="chartHeading">Top single-word density</h2>
          <div class="table-tabs">
            <button type="button" class="table-tab phrase-tab active" data-tab="unigrams">Single Keywords</button>
            <button type="button" class="table-tab phrase-tab" data-tab="bigrams">2-Word Phrases</button>
            <button type="button" class="table-tab phrase-tab" data-tab="trigrams">3-Word Phrases</button>
            <button type="button" class="table-tab phrase-tab" data-tab="fourgrams">4-Word Phrases</button>
          </div>
        </div>
        <canvas id="densityChart"></canvas>
      </div>

      <div class="card">
        <div class="table-head-row">
          <div class="table-tabs">
            <button type="button" class="table-tab phrase-tab active" data-tab="unigrams">Single Keywords</button>
            <button type="button" class="table-tab phrase-tab" data-tab="bigrams">2-Word Phrases</button>
            <button type="button" class="table-tab phrase-tab" data-tab="trigrams">3-Word Phrases</button>
            <button type="button" class="table-tab phrase-tab" data-tab="fourgrams">4-Word Phrases</button>
          </div>
          <button type="button" class="btn-secondary" id="exportBtn">⬇ Export CSV</button>
        </div>
        <table>
          <thead>
            <tr><th>#</th><th>Keyword / Phrase</th><th>Occurrences</th><th>Density</th><th>Status</th></tr>
          </thead>
          <tbody id="resultsBody"></tbody>
        </table>
        <div class="empty-note hidden" id="emptyNote">No repeated phrases of this length found.</div>
      </div>

    </div>

  </main>
</div>

<script>
let currentMode = 'text';
let lastResult = null;
let currentTab = 'unigrams';
let chartInstance = null;

document.querySelectorAll('.mode-tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.mode-tab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentMode = btn.dataset.mode;
    document.getElementById('textMode').classList.toggle('hidden', currentMode !== 'text');
    document.getElementById('urlMode').classList.toggle('hidden', currentMode !== 'url');
  });
});

const TAB_LABELS = {
  unigrams: 'single-word',
  bigrams: '2-word phrase',
  trigrams: '3-word phrase',
  fourgrams: '4-word phrase',
};

document.querySelectorAll('.phrase-tab').forEach(btn => {
  btn.addEventListener('click', () => setActiveTab(btn.dataset.tab));
});

function setActiveTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.phrase-tab').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === tab);
  });
  const heading = document.getElementById('chartHeading');
  if (heading) heading.textContent = 'Top ' + TAB_LABELS[tab] + ' density';
  if (lastResult) {
    try {
      renderChart((lastResult[tab] || []));
    } catch (e) {
      console.error('Chart render failed:', e);
      showChartFallback();
    }
    renderTable();
  }
}

document.getElementById('analyzeBtn').addEventListener('click', runAnalysis);
document.getElementById('exportBtn').addEventListener('click', exportCsv);

function statusLabel(status) {
  if (status === 'low') return 'Low';
  if (status === 'high') return 'Over-optimized';
  return 'Healthy';
}

async function runAnalysis() {
  const errorBox = document.getElementById('errorBox');
  const loadingNote = document.getElementById('loadingNote');
  const analyzeBtn = document.getElementById('analyzeBtn');
  errorBox.classList.add('hidden');
  errorBox.textContent = '';

  const payload = {
    mode: currentMode,
    target_keyword: document.getElementById('targetInput').value.trim(),
  };
  if (currentMode === 'url') {
    payload.url = document.getElementById('urlInput').value.trim();
  } else {
    payload.text = document.getElementById('textInput').value.trim();
  }

  loadingNote.style.display = 'flex';
  analyzeBtn.disabled = true;

  try {
    const resp = await fetch('/api/analyze', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.error || 'Something went wrong.');
    }
    lastResult = data;
    renderResults(data);
  } catch (err) {
    errorBox.textContent = err.message;
    errorBox.classList.remove('hidden');
  } finally {
    loadingNote.style.display = 'none';
    analyzeBtn.disabled = false;
  }
}

function renderResults(data) {
  document.getElementById('resultsWrap').classList.remove('hidden');

  const statGrid = document.getElementById('statGrid');
  statGrid.innerHTML = `
    <div class="stat-card">
      <div class="stat-label">Total Words</div>
      <div class="stat-value">${data.total_words.toLocaleString()}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Unique Words</div>
      <div class="stat-value">${data.unique_words.toLocaleString()}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Source</div>
      <div class="stat-value small">${escapeHtml(String(data.source || '').slice(0, 60))}</div>
    </div>
  `;

  const focusCard = document.getElementById('focusCard');
  if (data.target) {
    focusCard.classList.remove('hidden');
    const t = data.target;
    document.getElementById('focusPhrase').textContent = '"' + t.phrase + '"';
    document.getElementById('ringValue').textContent = t.density + '%';
    document.getElementById('focusBadge').innerHTML =
      `<span class="badge ${t.status}">${statusLabel(t.status)}</span>`;

    const desc = {
      low: `Appears ${t.count} time(s) — that's under the typical healthy range of 1–3%. Consider working it in a few more times naturally.`,
      healthy: `Appears ${t.count} time(s) — right in the healthy 1–3% range most SEO guidance recommends.`,
      high: `Appears ${t.count} time(s) — above 3% density, which risks reading as keyword stuffing. Consider trimming repeats or swapping in synonyms.`,
    }[t.status];
    document.getElementById('focusDesc').textContent = desc;

    const circumference = 339.3;
    const pct = Math.min(t.density / 6, 1); // scale 0-6% across the ring
    const ring = document.getElementById('ringProgress');
    ring.setAttribute('stroke-dashoffset', String(circumference * (1 - pct)));
    const color = { low: 'var(--accent2)', healthy: 'var(--accent3)', high: 'var(--danger)' }[t.status];
    ring.style.stroke = color;
  } else {
    focusCard.classList.add('hidden');
  }

  setActiveTab('unigrams');
}

function showChartFallback() {
  const canvas = document.getElementById('densityChart');
  const card = canvas.closest('.chart-card');
  canvas.style.display = 'none';
  let note = card.querySelector('.chart-fallback-note');
  if (!note) {
    note = document.createElement('div');
    note.className = 'chart-fallback-note empty-note';
    card.appendChild(note);
  }
  note.textContent = "Chart couldn't load (probably a blocked network request) — the full breakdown is still below in the table.";
}

function renderChart(rows) {
  if (typeof Chart === 'undefined') {
    showChartFallback();
    return;
  }
  const canvas = document.getElementById('densityChart');
  canvas.style.display = '';
  const existingNote = canvas.closest('.chart-card').querySelector('.chart-fallback-note');
  if (existingNote) existingNote.remove();

  const top = rows.slice(0, 10);
  const ctx = canvas.getContext('2d');
  if (chartInstance) chartInstance.destroy();
  chartInstance = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: top.map(r => r.phrase),
      datasets: [{
        label: 'Density %',
        data: top.map(r => r.density),
        backgroundColor: 'rgba(124,106,247,0.55)',
        borderRadius: 6,
        maxBarThickness: 26,
      }],
    },
    options: {
      indexAxis: 'y',
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: '#8888a8' }, grid: { color: 'rgba(255,255,255,0.05)' }, title: { display: true, text: 'Density %', color: '#8888a8' } },
        y: { ticks: { color: '#e8e8f0' }, grid: { display: false } },
      },
    },
  });
}

function renderTable() {
  if (!lastResult) return;
  const rows = lastResult[currentTab] || [];
  const body = document.getElementById('resultsBody');
  const emptyNote = document.getElementById('emptyNote');
  body.innerHTML = '';
  if (rows.length === 0) {
    emptyNote.classList.remove('hidden');
    return;
  }
  emptyNote.classList.add('hidden');
  rows.forEach((r, i) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="num">${i + 1}</td>
      <td>${escapeHtml(r.phrase)}</td>
      <td class="num">${r.count}</td>
      <td class="num">${r.density}%</td>
      <td><span class="badge ${r.status}">${statusLabel(r.status)}</span></td>
    `;
    body.appendChild(tr);
  });
}

function exportCsv() {
  if (!lastResult) return;
  const sections = [
    ['unigrams', 'Single Keyword'],
    ['bigrams', '2-Word Phrase'],
    ['trigrams', '3-Word Phrase'],
    ['fourgrams', '4-Word Phrase'],
  ];
  let csv = 'Type,Keyword/Phrase,Occurrences,Density %,Status\n';
  sections.forEach(([key, label]) => {
    (lastResult[key] || []).forEach(r => {
      csv += `${label},"${r.phrase.replace(/"/g, '""')}",${r.count},${r.density},${statusLabel(r.status)}\n`;
    });
  });
  if (lastResult.target) {
    const t = lastResult.target;
    csv += `Target Keyword,"${t.phrase.replace(/"/g, '""')}",${t.count},${t.density},${statusLabel(t.status)}\n`;
  }
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'keyword-density-report.csv';
  a.click();
  URL.revokeObjectURL(url);
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(debug=True, port=5000)
