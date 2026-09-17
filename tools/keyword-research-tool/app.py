import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, render_template_string, request, jsonify
import requests
from bs4 import BeautifulSoup

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Shared text-processing helpers
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


def tokenize(text):
    words = WORD_RE.findall(text.lower())
    cleaned = []
    for w in words:
        w = w.strip("'-")
        if len(w) > 1 or w.isalpha():
            cleaned.append(w)
    return cleaned


def stem(token):
    """Naive suffix-stripping so 'card' matches 'cards', 'loan' matches
    'loans', etc. Good enough for topic-relevance filtering; not meant to be
    a real stemmer."""
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def stemmed_token_set(text):
    return {stem(t) for t in tokenize(text)}


def clean_ngrams(tokens, n):
    """Yield n-gram phrases, dropping ones that start or end on a stopword
    (for n>1) or that are themselves a stopword (n==1) — keeps phrases like
    'personal loan' instead of noise like 'the personal' or 'loan is'."""
    counter = Counter()
    for i in range(len(tokens) - n + 1):
        gram = tokens[i:i + n]
        if n == 1:
            if gram[0] in STOPWORDS:
                continue
        else:
            if gram[0] in STOPWORDS or gram[-1] in STOPWORDS:
                continue
        counter[" ".join(gram)] += 1
    return counter


# ---------------------------------------------------------------------------
# Page fetch + on-page candidate extraction — used to auto-detect a root
# keyword when no Primary Keyword is given, and to boost anything that also
# appears on the page itself.
# ---------------------------------------------------------------------------

def fetch_html(url, timeout=15):
    """Fetch a URL and return (html, final_url). Retries once on
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
                time.sleep(0.6)
                continue
            resp.raise_for_status()
            return resp.text, resp.url
        except requests.exceptions.RequestException as exc:
            last_error = str(exc)
    raise RuntimeError(
        f"Couldn't fetch that URL ({last_error}). The site may be blocking "
        f"automated requests, or the address might be incorrect."
    )


def extract_seo_fields(html):
    soup = BeautifulSoup(html, "html.parser")

    title = soup.title.get_text(" ", strip=True) if soup.title else ""

    meta_desc = ""
    meta_tag = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    if meta_tag and meta_tag.get("content"):
        meta_desc = meta_tag["content"]

    h1s = [h.get_text(" ", strip=True) for h in soup.find_all("h1")]
    h2s = [h.get_text(" ", strip=True) for h in soup.find_all("h2")]
    h3s = [h.get_text(" ", strip=True) for h in soup.find_all("h3")]

    for tag in soup(["script", "style", "noscript", "svg", "iframe", "form",
                      "nav", "footer", "header", "button", "input"]):
        tag.decompose()
    body_text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()[:MAX_CHARS]

    return {
        "title": title,
        "meta_description": meta_desc,
        "h1": h1s,
        "h2": h2s,
        "h3": h3s,
        "body_text": body_text,
    }


# Prominence weights — appearing in the title or an H1 is a much stronger
# relevance signal than a passing mention buried in body copy.
FIELD_WEIGHTS = {
    "title": 6,
    "meta_description": 4,
    "h1": 5,
    "h2": 3,
    "body_text": 1,
}


def score_page_candidates(fields):
    """Returns (weighted_scores, raw_counts) Counters keyed by phrase."""
    weighted = Counter()
    raw_counts = Counter()

    def add(text, weight):
        if not text:
            return
        tokens = tokenize(text)
        for n in (1, 2, 3):
            for phrase, count in clean_ngrams(tokens, n).items():
                weighted[phrase] += count * weight
                raw_counts[phrase] += count

    add(fields["title"], FIELD_WEIGHTS["title"])
    add(fields["meta_description"], FIELD_WEIGHTS["meta_description"])
    add(" . ".join(fields["h1"]), FIELD_WEIGHTS["h1"])
    add(" . ".join(fields["h2"] + fields["h3"]), FIELD_WEIGHTS["h2"])
    add(fields["body_text"], FIELD_WEIGHTS["body_text"])

    return weighted, raw_counts


def auto_detect_root(weighted_scores):
    """When no Primary Keyword is given, pick the single strongest short
    phrase from the page to use as the root keyword — prefer a 2-word
    phrase (closer to how people actually search) over a bare single word."""
    ranked = weighted_scores.most_common(20)
    two_word = [p for p, _ in ranked if len(p.split()) == 2]
    if two_word:
        return two_word[0]
    return ranked[0][0] if ranked else ""


# ---------------------------------------------------------------------------
# Google Autocomplete expansion (free, keyless — real query phrases, not a
# licensed keyword database, so it carries no search-volume numbers)
# ---------------------------------------------------------------------------

def get_autocomplete_suggestions(query, limit=10, timeout=4):
    try:
        resp = requests.get(
            "https://suggestqueries.google.com/complete/search",
            params={"client": "firefox", "q": query, "hl": "en"},
            headers={"User-Agent": BROWSER_UA},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        suggestions = data[1] if len(data) > 1 else []
        return [str(s).strip().lower() for s in suggestions[:limit] if str(s).strip()]
    except Exception:
        return []


QUESTION_WORDS = ["what", "how", "why", "when", "where", "which", "who", "is"]

# Generic, domain-agnostic modifiers used to template prefix/suffix keyword
# ideas directly — these always appear in the results even if Google
# Autocomplete is slow/rate-limited, and get a score boost whenever Google
# independently suggests the same combination.
PREFIX_MODIFIERS = ["best", "top", "cheap", "free", "new", "compare", "buy",
                     "apply for", "how to get", "what is", "why choose", "types of"]
SUFFIX_MODIFIERS = ["apply", "eligibility", "benefits", "offers", "review",
                     "alternatives", "price", "cost", "near me",
                     "customer care", "login", "vs", "for beginners",
                     "documents required"]
ALPHABET = "abcdefghijklmnopqrstuvwxyz"


def gather_autocomplete_pool(root, max_workers=8):
    """Runs the root alone, root + each question word, and root + each
    letter of the alphabet (the standard 'alphabet soup' keyword-research
    technique) through Google Autocomplete concurrently. Returns a dict of
    every raw suggestion seen -> the best (lowest) rank it appeared at."""
    queries = [root]
    queries += [f"{w} {root}" for w in QUESTION_WORDS]
    queries += [f"{root} {letter}" for letter in ALPHABET]

    pool = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(get_autocomplete_suggestions, q): q for q in queries}
        for future in as_completed(futures):
            try:
                suggestions = future.result()
            except Exception:
                suggestions = []
            for rank, suggestion in enumerate(suggestions):
                if suggestion not in pool or rank < pool[suggestion]:
                    pool[suggestion] = rank
    return pool


# ---------------------------------------------------------------------------
# Classification + combined scoring
# ---------------------------------------------------------------------------

def classify_category(phrase, root):
    if phrase == root:
        return "root"
    if len(phrase.split()) >= 5:
        return "longtail"
    if phrase.startswith(root + " "):
        return "suffix"
    if phrase.endswith(" " + root):
        return "prefix"
    return "secondary"


CATEGORY_BASE_SCORE = {"root": 100, "suffix": 80, "prefix": 75, "secondary": 65, "longtail": 60}


def research_keywords(url, primary_keyword, top_per_category=12):
    primary_keyword = re.sub(r"\s+", " ", (primary_keyword or "").strip().lower())
    url = (url or "").strip()

    fields = None
    final_url = None
    weighted_scores = Counter()
    raw_counts = Counter()

    if url:
        html, final_url = fetch_html(url)
        fields = extract_seo_fields(html)
        weighted_scores, raw_counts = score_page_candidates(fields)

    if primary_keyword:
        root = primary_keyword
    elif weighted_scores:
        root = auto_detect_root(weighted_scores)
    else:
        raise ValueError("Enter a URL, a primary keyword, or both.")

    if not root:
        raise ValueError("Couldn't find enough readable text on that page — try adding a primary keyword.")

    root_stems = stemmed_token_set(root)
    if not root_stems:
        raise ValueError("That primary keyword didn't contain any usable words.")

    autocomplete_pool = gather_autocomplete_pool(root)

    curated = set()
    for mod in PREFIX_MODIFIERS:
        curated.add(f"{mod} {root}")
    for mod in SUFFIX_MODIFIERS:
        curated.add(f"{root} {mod}")

    all_phrases = set(autocomplete_pool) | curated | {root}

    rows = []
    for raw_phrase in all_phrases:
        phrase = re.sub(r"\s+", " ", raw_phrase).strip()
        if not phrase:
            continue
        if not root_stems.issubset(stemmed_token_set(phrase)):
            continue  # drifted off-topic (e.g. shares no real words with the root)

        category = classify_category(phrase, root)
        rank = autocomplete_pool.get(phrase)
        verified = phrase in autocomplete_pool
        in_page = raw_counts.get(phrase, 0) > 0

        score = CATEGORY_BASE_SCORE[category]
        if rank is not None:
            score = max(score, 92 - rank * 4)
        if verified and phrase in curated:
            score += 8
        if in_page:
            score += 12
        score = min(100, round(score, 1))

        rows.append({
            "phrase": phrase,
            "category": category,
            "score": score,
            "word_count": len(phrase.split()),
            "verified": verified,
            "on_page": in_page,
        })

    rows.sort(key=lambda r: r["score"], reverse=True)

    buckets = {"root": [], "secondary": [], "prefix": [], "suffix": [], "longtail": []}
    for row in rows:
        buckets[row["category"]].append(row)
    for key in ("secondary", "prefix", "suffix", "longtail"):
        buckets[key] = buckets[key][:top_per_category]

    all_capped = []
    for key in ("root", "secondary", "prefix", "suffix", "longtail"):
        all_capped.extend(buckets[key])
    all_capped.sort(key=lambda r: r["score"], reverse=True)

    return {
        "source_url": final_url,
        "page_title": (fields["title"] if fields else "") or "",
        "root_keyword": root,
        "primary_keyword_supplied": bool(primary_keyword),
        "keywords": all_capped,
        "buckets": buckets,
        "stats": {
            "on_page_candidates": len(weighted_scores),
            "autocomplete_suggestions": len(autocomplete_pool),
            "total_ideas": len(all_capped),
        },
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(PAGE_HTML)


@app.route("/api/research", methods=["POST"])
def api_research():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    primary_keyword = (data.get("primary_keyword") or "").strip()
    if not url and not primary_keyword:
        return jsonify({"error": "Please enter a URL, a primary keyword, or both."}), 400
    try:
        result = research_keywords(url, primary_keyword)
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception:
        return jsonify({"error": "Something went wrong researching that keyword."}), 500


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

PAGE_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Keyword Research Tool | SEO Tools by Mahalakshmi</title>
<meta name="description" content="Enter a URL and/or a primary keyword and get root, secondary, prefix, suffix and long-tail keyword ideas — from on-page content plus real Google Autocomplete data.">
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

  .field-row { display: grid; grid-template-columns: 1.3fr 1fr; gap: 1rem; }
  @media (max-width: 700px) { .field-row { grid-template-columns: 1fr; } }
  input[type="url"], input[type="text"] { width: 100%; background: var(--surface2); border: 1px solid var(--border); border-radius: 9px; color: var(--text); font-family: 'Inter', sans-serif; font-size: 0.9rem; padding: 0.8rem 0.95rem; transition: border-color 0.15s; }
  input:focus { outline: none; border-color: var(--accent); }
  .field { margin-bottom: 1rem; }
  .field label { display: block; font-size: 0.78rem; color: var(--muted); margin-bottom: 0.4rem; font-weight: 500; }
  .field-hint { font-size: 0.72rem; color: var(--muted); margin-top: 0.35rem; }

  .btn-primary { background: var(--accent); color: #fff; border: none; font-family: 'Inter', sans-serif; font-weight: 700; font-size: 0.9rem; padding: 0.75rem 1.6rem; border-radius: 9px; cursor: pointer; transition: background 0.15s, transform 0.15s; display: inline-flex; align-items: center; gap: 0.5rem; }
  .btn-primary:hover { background: #6a58e8; transform: translateY(-1px); }
  .btn-primary:disabled { opacity: 0.6; cursor: not-allowed; transform: none; }
  .btn-secondary { background: rgba(255,255,255,0.05); color: var(--text); border: 1px solid var(--border); font-family: 'Inter', sans-serif; font-weight: 600; font-size: 0.82rem; padding: 0.55rem 1.1rem; border-radius: 8px; cursor: pointer; transition: all 0.15s; }
  .btn-secondary:hover { border-color: var(--accent); color: var(--accent); }

  .error-box { background: rgba(247,106,106,0.1); border: 1px solid rgba(247,106,106,0.3); color: var(--danger); border-radius: 9px; padding: 0.75rem 1rem; font-size: 0.85rem; margin-top: 1rem; }

  .stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 0.9rem; margin-bottom: 1.4rem; }
  .stat-card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 1.1rem 1.2rem; }
  .stat-label { font-family: 'DM Mono', monospace; font-size: 0.65rem; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); margin-bottom: 0.5rem; }
  .stat-value { font-family: 'Sora', sans-serif; font-size: 1.55rem; font-weight: 800; }
  .stat-value.small { font-size: 1rem; word-break: break-word; }

  .badge { display: inline-flex; align-items: center; gap: 0.35rem; font-family: 'DM Mono', monospace; font-size: 0.68rem; font-weight: 500; padding: 0.28rem 0.65rem; border-radius: 100px; text-transform: uppercase; letter-spacing: 0.04em; white-space: nowrap; }
  .badge.root { background: rgba(124,106,247,0.3); border: 1px solid rgba(124,106,247,0.6); color: #fff; font-weight: 700; }
  .badge.secondary { background: rgba(106,247,200,0.1); border: 1px solid rgba(106,247,200,0.3); color: var(--accent3); }
  .badge.prefix { background: rgba(247,162,106,0.1); border: 1px solid rgba(247,162,106,0.3); color: var(--accent2); }
  .badge.suffix { background: rgba(124,106,247,0.12); border: 1px solid rgba(124,106,247,0.35); color: var(--accent); }
  .badge.longtail { background: rgba(255,255,255,0.05); border: 1px solid var(--border); color: var(--text); }

  .table-tabs { display: flex; gap: 0.4rem; margin-bottom: 1rem; flex-wrap: wrap; }
  .table-tab { border: 1px solid var(--border); background: var(--surface2); color: var(--muted); font-family: 'Inter', sans-serif; font-size: 0.8rem; font-weight: 600; padding: 0.45rem 0.95rem; border-radius: 8px; cursor: pointer; transition: all 0.15s; }
  .table-tab.active { background: rgba(124,106,247,0.14); border-color: rgba(124,106,247,0.4); color: var(--accent); }

  .chart-card canvas { max-height: 320px; }

  .table-head-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.9rem; flex-wrap: wrap; gap: 0.7rem; }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  thead th { text-align: left; font-family: 'DM Mono', monospace; font-size: 0.68rem; letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted); font-weight: 500; padding: 0.6rem 0.7rem; border-bottom: 1px solid var(--border); }
  tbody td { padding: 0.65rem 0.7rem; border-bottom: 1px solid rgba(255,255,255,0.04); }
  tbody tr:hover { background: rgba(255,255,255,0.02); }
  td.num { font-family: 'DM Mono', monospace; }
  .on-page-tag { font-size: 0.68rem; color: var(--accent3); font-family: 'DM Mono', monospace; }
  .score-bar-wrap { display: flex; align-items: center; gap: 0.6rem; }
  .score-bar-track { flex: 1; height: 6px; border-radius: 4px; background: rgba(255,255,255,0.06); overflow: hidden; min-width: 60px; }
  .score-bar-fill { height: 100%; background: var(--accent); border-radius: 4px; }
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
      <a href="#" class="sidebar-link active">🔍 Keyword Research Tool</a>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-label">Navigate</div>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="sidebar-link">🧰 All My Tools</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio" class="sidebar-link">👩‍💻 Portfolio</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/blog/index.html" class="sidebar-link">📝 Blog Posts</a>
    </div>
    <div class="sidebar-info">
      <strong>How it works:</strong> we pick one root keyword (yours, or the page's strongest phrase), run it through Google Autocomplete's full alphabet sweep plus common question words, and template generic prefix/suffix ideas on top. Anything that drifts off-topic is filtered out. Ranked by relevance — not verified search volume.
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
      <h1 class="page-title">🔍 Keyword Research Tool</h1>
      <p class="page-sub">Give it a URL and/or a primary keyword and get a full keyword map — root, secondary, prefix, suffix and long-tail ideas — built from real Google Autocomplete data plus the page's own content.</p>
    </div>

    <div class="card">
      <h2>Research keywords</h2>
      <div class="field-row">
        <div class="field">
          <label for="urlInput">Page URL (optional if you give a primary keyword)</label>
          <input type="url" id="urlInput" placeholder="https://example.com/personal-loan">
        </div>
        <div class="field">
          <label for="primaryInput">Primary keyword (optional)</label>
          <input type="text" id="primaryInput" placeholder="e.g. credit card">
        </div>
      </div>
      <div class="field-hint" style="margin-bottom:1rem;">Give both for the best results — the primary keyword sets the root, the URL boosts anything that's already on the page. A short root (1-3 words) spreads better across categories than a long phrase.</div>
      <button type="button" class="btn-primary" id="researchBtn">🔍 Find Keywords</button>
      <div class="loading-note" id="loadingNote"><span class="spinner"></span> Researching keywords — this runs a full Autocomplete sweep, can take 15-20s…</div>
      <div class="error-box hidden" id="errorBox" style="display:none;"></div>
    </div>

    <div id="resultsWrap" style="display:none;">

      <div class="stat-grid" id="statGrid"></div>

      <div class="card chart-card">
        <h2 id="chartHeading">Top 10 by relevance</h2>
        <canvas id="keywordChart"></canvas>
      </div>

      <div class="card">
        <div class="table-head-row">
          <h2 style="margin:0;">Keyword ideas</h2>
          <button type="button" class="btn-secondary" id="exportBtn">⬇ Export CSV</button>
        </div>
        <div class="table-tabs">
          <button type="button" class="table-tab active" data-tab="all">All</button>
          <button type="button" class="table-tab" data-tab="root">Root</button>
          <button type="button" class="table-tab" data-tab="secondary">Secondary</button>
          <button type="button" class="table-tab" data-tab="prefix">Prefix</button>
          <button type="button" class="table-tab" data-tab="suffix">Suffix</button>
          <button type="button" class="table-tab" data-tab="longtail">Long-tail</button>
        </div>
        <table>
          <thead>
            <tr><th>#</th><th>Keyword</th><th>Category</th><th>Words</th><th>Relevance</th></tr>
          </thead>
          <tbody id="resultsBody"></tbody>
        </table>
        <div class="empty-note hidden" id="emptyNote" style="display:none;">No keyword ideas in this category.</div>
      </div>

    </div>

  </main>
</div>

<script>
let lastResult = null;
let currentTab = 'all';
let chartInstance = null;

document.getElementById('researchBtn').addEventListener('click', runResearch);
document.getElementById('exportBtn').addEventListener('click', exportCsv);
['urlInput', 'primaryInput'].forEach(id => {
  document.getElementById(id).addEventListener('keydown', (e) => { if (e.key === 'Enter') runResearch(); });
});
document.querySelectorAll('.table-tab').forEach(btn => {
  btn.addEventListener('click', () => setActiveTab(btn.dataset.tab));
});

const CATEGORY_LABELS = { root: 'Root', secondary: 'Secondary', prefix: 'Prefix', suffix: 'Suffix', longtail: 'Long-tail' };

function rowsForTab(tab) {
  if (!lastResult) return [];
  if (tab === 'all') return lastResult.keywords;
  return lastResult.buckets[tab] || [];
}

function setActiveTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.table-tab').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  const heading = document.getElementById('chartHeading');
  const rows = rowsForTab(tab);
  heading.textContent = 'Top ' + Math.min(10, rows.length) + ' by relevance' + (tab === 'all' ? '' : ' — ' + CATEGORY_LABELS[tab]);
  try {
    renderChart(rows.slice(0, 10));
  } catch (e) {
    console.error('Chart render failed:', e);
    showChartFallback();
  }
  renderTable(rows);
}

async function runResearch() {
  const errorBox = document.getElementById('errorBox');
  const loadingNote = document.getElementById('loadingNote');
  const researchBtn = document.getElementById('researchBtn');
  errorBox.style.display = 'none';
  errorBox.textContent = '';

  const url = document.getElementById('urlInput').value.trim();
  const primaryKeyword = document.getElementById('primaryInput').value.trim();
  if (!url && !primaryKeyword) {
    errorBox.textContent = 'Please enter a URL, a primary keyword, or both.';
    errorBox.style.display = 'block';
    return;
  }

  loadingNote.style.display = 'flex';
  researchBtn.disabled = true;

  try {
    const resp = await fetch('/api/research', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, primary_keyword: primaryKeyword }),
    });
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.error || 'Something went wrong.');
    }
    lastResult = data;
    renderResults(data);
  } catch (err) {
    errorBox.textContent = err.message;
    errorBox.style.display = 'block';
  } finally {
    loadingNote.style.display = 'none';
    researchBtn.disabled = false;
  }
}

function renderResults(data) {
  document.getElementById('resultsWrap').style.display = 'block';

  const pageStat = data.source_url
    ? `<div class="stat-card"><div class="stat-label">Page Analyzed</div><div class="stat-value small">${escapeHtml(String(data.page_title || data.source_url).slice(0, 60))}</div></div>`
    : '';

  const statGrid = document.getElementById('statGrid');
  statGrid.innerHTML = `
    <div class="stat-card">
      <div class="stat-label">Root Keyword</div>
      <div class="stat-value small">${escapeHtml(data.root_keyword)}</div>
    </div>
    ${pageStat}
    <div class="stat-card">
      <div class="stat-label">Total Ideas</div>
      <div class="stat-value">${data.stats.total_ideas}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Secondary</div>
      <div class="stat-value">${data.buckets.secondary.length}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Prefix</div>
      <div class="stat-value">${data.buckets.prefix.length}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Suffix</div>
      <div class="stat-value">${data.buckets.suffix.length}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Long-tail</div>
      <div class="stat-value">${data.buckets.longtail.length}</div>
    </div>
  `;

  setActiveTab('all');
}

function showChartFallback() {
  const canvas = document.getElementById('keywordChart');
  const card = canvas.closest('.chart-card');
  canvas.style.display = 'none';
  let note = card.querySelector('.chart-fallback-note');
  if (!note) {
    note = document.createElement('div');
    note.className = 'chart-fallback-note empty-note';
    card.appendChild(note);
  }
  note.textContent = "Chart couldn't load (probably a blocked network request) — the full list is still below in the table.";
}

function renderChart(rows) {
  if (typeof Chart === 'undefined') {
    showChartFallback();
    return;
  }
  const canvas = document.getElementById('keywordChart');
  canvas.style.display = '';
  const existingNote = canvas.closest('.chart-card').querySelector('.chart-fallback-note');
  if (existingNote) existingNote.remove();

  const ctx = canvas.getContext('2d');
  if (chartInstance) chartInstance.destroy();
  if (rows.length === 0) {
    if (chartInstance) { chartInstance.destroy(); chartInstance = null; }
    canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height);
    return;
  }
  chartInstance = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: rows.map(r => r.phrase),
      datasets: [{
        label: 'Relevance',
        data: rows.map(r => r.score),
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
        x: { min: 0, max: 100, ticks: { color: '#8888a8' }, grid: { color: 'rgba(255,255,255,0.05)' }, title: { display: true, text: 'Relevance score', color: '#8888a8' } },
        y: { ticks: { color: '#e8e8f0' }, grid: { display: false } },
      },
    },
  });
}

function renderTable(rows) {
  const body = document.getElementById('resultsBody');
  const emptyNote = document.getElementById('emptyNote');
  body.innerHTML = '';
  if (!rows || rows.length === 0) {
    emptyNote.style.display = 'block';
    return;
  }
  emptyNote.style.display = 'none';
  rows.forEach((r, i) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="num">${i + 1}</td>
      <td>${escapeHtml(r.phrase)} ${r.on_page ? '<span class="on-page-tag">● on-page</span>' : ''}</td>
      <td><span class="badge ${r.category}">${CATEGORY_LABELS[r.category]}</span></td>
      <td class="num">${r.word_count}</td>
      <td>
        <div class="score-bar-wrap">
          <div class="score-bar-track"><div class="score-bar-fill" style="width:${r.score}%"></div></div>
          <span class="num">${r.score}</span>
        </div>
      </td>
    `;
    body.appendChild(tr);
  });
}

function exportCsv() {
  if (!lastResult) return;
  let csv = 'Rank,Keyword,Category,Words,On Page,Relevance Score\n';
  lastResult.keywords.forEach((r, i) => {
    csv += `${i + 1},"${r.phrase.replace(/"/g, '""')}",${CATEGORY_LABELS[r.category]},${r.word_count},${r.on_page ? 'Yes' : 'No'},${r.score}\n`;
  });
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'keyword-research-report.csv';
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
