import re
import math

from flask import Flask, request, jsonify
import requests
from bs4 import BeautifulSoup

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

# A real browser UA so WAFs don't block the fetch outright; never identify
# this as a bot/checker tool.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

MAX_CHARS = 150_000
READING_WPM = 238      # average silent reading speed (adults)
SPEAKING_WPM = 150     # average speaking pace
LONG_SENTENCE = 20     # words — flag as "long"
VERY_LONG_SENTENCE = 25  # words — flag as "very long"

# Abbreviations that end with a dot but don't end a sentence.
ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "e.g",
    "i.e", "eg", "ie", "no", "nos", "rs", "inc", "ltd", "pvt", "co", "corp",
    "approx", "dept", "est", "fig", "min", "max", "mt", "p.a", "pa", "yr",
    "yrs", "mo", "govt", "a.m", "p.m", "u.s", "u.k", "jan", "feb", "mar",
    "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
}

# Common irregular past participles for passive-voice detection.
IRREGULAR_PARTICIPLES = set("""
arisen awoken been borne beaten become begun bent bet bid bitten bled blown
broken bred brought built burnt burst bought cast caught chosen come cost crept
cut dealt dug done drawn dreamt drunk driven eaten fallen fed felt fought found
fled flung flown forbidden forgotten forgiven frozen got gotten given gone ground
grown hung had heard hidden hit held hurt kept knelt known laid led leapt left
lent let lain lit lost made meant met paid proven put quit read ridden rung
risen run said seen sought sold sent set shaken shed shot shown shut sung sunk
sat slain slept slid spoken spent spun spread sprung stood stolen stuck stung
struck sworn swept swum taken taught torn told thought thrown understood woken
worn won withdrawn written
""".split())

PASSIVE_RE = re.compile(
    r"\b(am|is|are|was|were|be|been|being)\s+(?:\w+ly\s+)?(\w+ed|"
    + "|".join(sorted(IRREGULAR_PARTICIPLES, key=len, reverse=True))
    + r")\b",
    re.I,
)

WORD_RE = re.compile(r"[A-Za-z0-9₹$€£%]+(?:['’\-.,][A-Za-z0-9]+)*")

# Syllable-count exceptions the heuristic gets wrong.
SYLLABLE_EXCEPTIONS = {
    "the": 1, "every": 3, "everyone": 3, "business": 2, "interest": 3,
    "different": 3, "area": 3, "idea": 3, "being": 2, "create": 2,
    "people": 2, "finance": 2, "financial": 3, "loan": 1, "loans": 1,
    "emi": 3, "emis": 3, "cibil": 2, "apply": 2, "eligible": 4,
    "eligibility": 6, "calculate": 3, "calculator": 4, "fire": 1,
    "hour": 1, "our": 1, "ours": 1, "real": 1, "really": 2, "science": 2,
    "quiet": 2, "poem": 2, "use": 1, "used": 1, "uses": 2, "rate": 1,
    "rates": 1, "insurance": 3, "average": 3, "family": 3, "several": 3,
    "evening": 2, "union": 2, "unions": 2, "whether": 2, "therefore": 2, "somewhere": 2,
}


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------

def count_syllables(word):
    w = word.lower().strip("'’.-,")
    if not w:
        return 0
    if any(ch.isdigit() for ch in w):
        # Numbers are read aloud as several syllables ("fifteen thousand").
        digits = sum(ch.isdigit() for ch in w)
        return min(max(digits, 1), 4)
    w = re.sub(r"[^a-z]", "", w)
    if not w:
        return 1
    if w in SYLLABLE_EXCEPTIONS:
        return SYLLABLE_EXCEPTIONS[w]
    if w.endswith("s") and w[:-1] in SYLLABLE_EXCEPTIONS:
        return SYLLABLE_EXCEPTIONS[w[:-1]]
    if len(w) <= 3:
        return 1

    base = w
    # Silent endings: "-es" / "-ed" (except "-ted"/"-ded"/"-ses" etc.) and a final "-e"
    if re.search(r"[^aeiouy](?:es|ed)$", base) and not re.search(r"(?:[td]ed|[szxc]es|[cs]hes|ges)$", base):
        base = base[:-2]
    elif base.endswith("e") and not re.search(r"(?:[^aeiouy]le|ee|ye)$", base):
        base = base[:-1]

    count = len(re.findall(r"[aeiouy]+", base))
    # Vowel pairs that are usually two syllables (e.g. "ia" in "India", "eo" in "video")
    count += len(re.findall(r"(?<![tscgxl])(?:ia|iu|io)|eo|(?<!q)ua|uo|ea(?=l[io])", base))
    if re.search(r"[aeiouy]ing$", base):
        count += 1
    if re.search(r"[^aeiouy]ely$", base):
        count -= 1
    count += len(re.findall(r"(?<=[^aeiou])ism$", base))
    return max(count, 1)


def tokenize_words(text):
    return [w for w in WORD_RE.findall(text) if re.search(r"[A-Za-z0-9]", w)]


def split_paragraphs(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    paras = [p.strip() for p in re.split(r"\n\s*\n", text)]
    return [p for p in paras if p]


def split_sentences(paragraph):
    """Split a paragraph into sentences. Each non-empty line is also its own
    boundary so headings and bullet points don't merge with body copy."""
    sentences = []
    for line in paragraph.split("\n"):
        line = re.sub(r"^\s*(?:[-*•▪◦–]|\d+[.)])\s+", "", line).strip()
        if not line:
            continue
        start = 0
        for m in re.finditer(r"[.!?]+[\"'”’)\]]*(?=\s+|$)", line):
            end = m.end()
            before = line[start:m.start()]
            last_token = before.split()[-1].lower() if before.split() else ""
            last_token = last_token.strip("(\"'“‘")
            if m.group(0).startswith(".") and len(m.group(0).rstrip("\"'”’)]")) == 1:
                if last_token in ABBREVIATIONS or re.fullmatch(r"[a-z]", last_token) \
                        or re.fullmatch(r"(?:[a-z]\.)+[a-z]", last_token):
                    continue
            chunk = line[start:end].strip()
            if chunk:
                sentences.append(chunk)
            start = end
        tail = line[start:].strip()
        if tail:
            sentences.append(tail)
    return [s for s in sentences if tokenize_words(s)]


def flesch_band(score):
    if score >= 90:
        return "Very easy", "Easily understood by an average 11-year-old."
    if score >= 80:
        return "Easy", "Conversational English for consumers."
    if score >= 70:
        return "Fairly easy", "Easy for most adult readers."
    if score >= 60:
        return "Plain English", "Easily understood by 13- to 15-year-olds — ideal for most web copy."
    if score >= 50:
        return "Fairly difficult", "Readable, but some readers will need to slow down."
    if score >= 30:
        return "Difficult", "Best suited to college-level readers."
    return "Very difficult", "Best understood by university graduates — too hard for general web copy."


def analyze_text(text, first_para_hint=None):
    paragraphs_raw = split_paragraphs(text)
    if not paragraphs_raw:
        raise ValueError("No readable text found to analyze.")

    sentences = []
    paragraphs = []
    total_syllables = 0
    complex_words = 0
    all_words = []

    for p in paragraphs_raw:
        idxs = []
        for s in split_sentences(p):
            words = tokenize_words(s)
            sylls = [count_syllables(w) for w in words]
            n = len(words)
            level = "ok"
            if n > VERY_LONG_SENTENCE:
                level = "very_long"
            elif n > LONG_SENTENCE:
                level = "long"
            passive = PASSIVE_RE.search(s)
            sentences.append({
                "text": s,
                "words": n,
                "syllables": sum(sylls),
                "level": level,
                "passive": bool(passive),
                "passive_phrase": passive.group(0) if passive else "",
            })
            idxs.append(len(sentences) - 1)
            total_syllables += sum(sylls)
            complex_words += sum(1 for w, sy in zip(words, sylls)
                                 if sy >= 3 and not any(c.isdigit() for c in w))
            all_words.extend(words)
        if idxs:
            paragraphs.append(idxs)

    total_words = len(all_words)
    total_sentences = len(sentences)
    if total_words == 0 or total_sentences == 0:
        raise ValueError("No readable text found to analyze.")

    wps = total_words / total_sentences
    spw = total_syllables / total_words
    flesch_raw = 206.835 - 1.015 * wps - 84.6 * spw
    fk_grade = 0.39 * wps + 11.8 * spw - 15.59
    flesch = max(0.0, min(100.0, flesch_raw))
    band, band_desc = flesch_band(flesch)

    chars_all = len(text)
    chars_no_space = len(re.sub(r"\s", "", text))
    letters = sum(len(re.sub(r"[^A-Za-z0-9]", "", w)) for w in all_words)

    long_count = sum(1 for s in sentences if s["level"] != "ok")
    very_long_count = sum(1 for s in sentences if s["level"] == "very_long")
    passive_count = sum(1 for s in sentences if s["passive"])

    para_word_counts = [sum(sentences[i]["words"] for i in p) for p in paragraphs]
    # The "opening paragraph" is the first real paragraph — skip short
    # headings / labels (under 12 words) that usually sit above it.
    first_para_words = next((c for c in para_word_counts if c >= 12),
                            para_word_counts[0] if para_word_counts else 0)

    # Sentence-length distribution buckets
    buckets = [("1–10", 1, 10), ("11–15", 11, 15), ("16–20", 16, 20),
               ("21–25", 21, 25), ("26–30", 26, 30), ("31+", 31, 10 ** 9)]
    distribution = [{"label": lbl, "count": sum(1 for s in sentences if lo <= s["words"] <= hi)}
                    for lbl, lo, hi in buckets]

    unique_words = len({w.lower() for w in all_words})

    stats = {
        "words": total_words,
        "unique_words": unique_words,
        "characters": chars_all,
        "characters_no_spaces": chars_no_space,
        "sentences": total_sentences,
        "paragraphs": len(paragraphs),
        "syllables": total_syllables,
        "avg_words_per_sentence": round(wps, 1),
        "avg_syllables_per_word": round(spw, 2),
        "avg_word_length": round(letters / total_words, 1),
        "avg_words_per_paragraph": round(total_words / max(len(paragraphs), 1), 1),
        "complex_words": complex_words,
        "complex_word_pct": round(complex_words / total_words * 100, 1),
        "long_sentences": long_count,
        "very_long_sentences": very_long_count,
        "long_sentence_pct": round(long_count / total_sentences * 100, 1),
        "passive_sentences": passive_count,
        "passive_pct": round(passive_count / total_sentences * 100, 1),
        "reading_time_sec": int(math.ceil(total_words / READING_WPM * 60)),
        "speaking_time_sec": int(math.ceil(total_words / SPEAKING_WPM * 60)),
        "first_paragraph_words": first_para_words,
        "longest_paragraph_words": max(para_word_counts) if para_word_counts else 0,
    }

    scores = {
        "flesch": round(flesch, 1),
        "flesch_raw": round(flesch_raw, 1),
        "fk_grade": round(max(fk_grade, 0), 1),
        "band": band,
        "band_desc": band_desc,
    }

    ai_checks = build_ai_checks(stats)

    return {
        "stats": stats,
        "scores": scores,
        "sentences": sentences,
        "paragraphs": paragraphs,
        "distribution": distribution,
        "ai_checks": ai_checks,
    }


def build_ai_checks(st):
    """AI-search / snippet readiness checks. Each: pass / warn / fail."""
    checks = []

    fp = st["first_paragraph_words"]
    if 40 <= fp <= 60:
        status, note = "pass", f"Your opening paragraph is {fp} words — perfect length for a featured snippet or AI answer."
    elif 25 <= fp <= 80:
        status, note = "warn", f"Your opening paragraph is {fp} words. Aim for 40–60 words that directly answer the main question."
    else:
        status, note = "fail", f"Your opening paragraph is {fp} words. Lead with a 40–60 word direct answer so Google and AI search can quote it."
    checks.append({"name": "Answer-first opening paragraph", "status": status, "note": note})

    ap = st["avg_words_per_paragraph"]
    if ap <= 70:
        status, note = "pass", f"Paragraphs average {ap} words — short and scannable."
    elif ap <= 110:
        status, note = "warn", f"Paragraphs average {ap} words. Break long blocks into 2–4 sentence chunks."
    else:
        status, note = "fail", f"Paragraphs average {ap} words — walls of text are hard to scan and harder for AI to extract."
    checks.append({"name": "Scannable paragraphs", "status": status, "note": note})

    lp = st["long_sentence_pct"]
    if lp <= 25:
        status, note = "pass", f"{lp}% of sentences are over {LONG_SENTENCE} words — within the 25% guideline."
    elif lp <= 40:
        status, note = "warn", f"{lp}% of sentences are over {LONG_SENTENCE} words. Keep this under 25%."
    else:
        status, note = "fail", f"{lp}% of sentences are over {LONG_SENTENCE} words. Split them — shorter sentences are easier to quote."
    checks.append({"name": "Sentence length", "status": status, "note": note})

    pp = st["passive_pct"]
    if pp <= 10:
        status, note = "pass", f"{pp}% passive voice — clear, direct writing."
    elif pp <= 20:
        status, note = "warn", f"{pp}% passive voice. Aim for under 10% so it's clear who does what."
    else:
        status, note = "fail", f"{pp}% passive voice. Rewrite in active voice (\"We calculate your EMI\", not \"Your EMI is calculated\")."
    checks.append({"name": "Active voice", "status": status, "note": note})

    cw = st["complex_word_pct"]
    if cw <= 15:
        status, note = "pass", f"{cw}% of words have 3+ syllables — easy vocabulary."
    elif cw <= 22:
        status, note = "warn", f"{cw}% of words have 3+ syllables. Swap jargon for plain words where you can."
    else:
        status, note = "fail", f"{cw}% of words have 3+ syllables — explain or replace jargon (e.g. \"use\" instead of \"utilise\")."
    checks.append({"name": "Plain vocabulary", "status": status, "note": note})

    return checks


# ---------------------------------------------------------------------------
# URL fetching
# ---------------------------------------------------------------------------

def fetch_url(url, timeout=15):
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
            return resp.text, resp.url
        except requests.exceptions.HTTPError as exc:
            last_error = f"HTTP {exc.response.status_code}" if exc.response is not None else "HTTP error"
        except requests.exceptions.Timeout:
            last_error = "the page took too long to respond"
        except requests.exceptions.ConnectionError:
            last_error = "couldn't connect to the site"
        except requests.exceptions.RequestException:
            last_error = "invalid URL or request"
    raise RuntimeError(
        f"Couldn't fetch that URL ({last_error}). The site may be blocking "
        f"automated requests — try pasting the page text instead."
    )


BLOCK_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote",
              "td", "th", "dt", "dd", "figcaption"]


def extract_page(html):
    """Return (main_text, seo) where main_text keeps paragraph breaks and
    seo holds title / meta description / h1 for the length checks."""
    soup = BeautifulSoup(html, "html.parser")

    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    md = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    meta_desc = (md.get("content") or "").strip() if md else ""
    h1_tag = soup.find("h1")
    h1 = h1_tag.get_text(" ", strip=True) if h1_tag else ""

    for tag in soup(["script", "style", "noscript", "svg", "iframe", "form",
                     "nav", "footer", "header", "aside", "button", "input",
                     "select", "template"]):
        tag.decompose()

    root = soup.find("article") or soup.find("main") or soup.body or soup
    blocks = []
    for el in root.find_all(BLOCK_TAGS):
        # Skip containers whose text is already captured by a nested block tag
        if el.find(BLOCK_TAGS):
            continue
        t = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
        if t:
            blocks.append(t)
    if not blocks:
        t = re.sub(r"\s+", " ", root.get_text(" ", strip=True)).strip()
        if t:
            blocks.append(t)

    return "\n\n".join(blocks), {"title": title, "meta_description": meta_desc, "h1": h1}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return PAGE_HTML


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    data = request.get_json(force=True, silent=True) or {}
    mode = data.get("mode")
    seo = None
    try:
        if mode == "url":
            url = (data.get("url") or "").strip()
            if not url:
                return jsonify({"error": "Please enter a URL."}), 400
            html, final_url = fetch_url(url)
            text, seo = extract_page(html)
            source = final_url
            if not text.strip():
                raise ValueError("Couldn't find readable body text on that page. "
                                 "It may be built with JavaScript — try pasting the text instead.")
        else:
            text = (data.get("text") or "").strip()
            if not text:
                return jsonify({"error": "Please paste some text to analyze."}), 400
            source = "Pasted text"

        truncated = len(text) > MAX_CHARS
        if truncated:
            text = text[:MAX_CHARS]

        result = analyze_text(text)
        result["source"] = source
        result["seo"] = seo
        result["truncated"] = truncated
        result["text_preview_chars"] = len(text)
        return jsonify(result)

    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502
    except Exception:
        app.logger.exception("analyze failed")
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
<title>Word Count Checker + Readability (Flesch) | SEO Tools by Mahalakshmi</title>
<meta name="description" content="Free word and character counter with Flesch Reading Ease, Flesch-Kincaid grade, hard-sentence highlighting, SEO length checks and AI-search readiness tips.">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%237c6af7'/%3E%3Ctext x='50%25' y='50%25' dominant-baseline='central' text-anchor='middle' font-family='Georgia%2C serif' font-size='18' font-weight='900' fill='white'%3EM%3C/text%3E%3C/svg%3E">
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;600;700;800;900&family=Inter:wght@300;400;500;600&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
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
  .main { flex: 1; padding: 2.2rem 2.6rem 4rem; max-width: 1180px; min-width: 0; }
  .page-head { margin-bottom: 1.8rem; }
  .page-eyebrow { font-family: 'DM Mono', monospace; font-size: 0.68rem; letter-spacing: 0.16em; text-transform: uppercase; color: var(--accent3); margin-bottom: 0.6rem; }
  .page-title { font-family: 'Sora', sans-serif; font-size: 1.9rem; font-weight: 800; margin-bottom: 0.4rem; }
  .page-sub { color: var(--muted); font-size: 0.93rem; max-width: 680px; }

  .card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 1.6rem; margin-bottom: 1.4rem; }
  .card h2 { font-family: 'Sora', sans-serif; font-size: 1.02rem; font-weight: 700; margin-bottom: 1rem; }
  .card-sub { color: var(--muted); font-size: 0.82rem; margin: -0.6rem 0 1.1rem; }

  /* INPUT MODE TABS */
  .mode-tabs { display: inline-flex; background: var(--surface2); border-radius: 9px; padding: 3px; margin-bottom: 1.1rem; border: 1px solid var(--border); }
  .mode-tab { border: none; background: transparent; color: var(--muted); font-family: 'Inter', sans-serif; font-size: 0.82rem; font-weight: 600; padding: 0.5rem 1.1rem; border-radius: 7px; cursor: pointer; transition: all 0.15s; }
  .mode-tab.active { background: var(--accent); color: #fff; }

  textarea, input[type="text"], input[type="url"], select { width: 100%; background: var(--surface2); border: 1px solid var(--border); border-radius: 9px; color: var(--text); font-family: 'Inter', sans-serif; font-size: 0.88rem; padding: 0.75rem 0.9rem; resize: vertical; transition: border-color 0.15s; }
  select { cursor: pointer; }
  textarea:focus, input:focus, select:focus { outline: none; border-color: var(--accent); }
  textarea { min-height: 220px; line-height: 1.6; }
  .field { margin-bottom: 1rem; }
  .field label { display: block; font-size: 0.78rem; color: var(--muted); margin-bottom: 0.4rem; font-weight: 500; }
  .field-hint { font-size: 0.72rem; color: var(--muted); margin-top: 0.35rem; }
  .hidden { display: none !important; }
  .row-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }

  /* LIVE COUNTER STRIP */
  .live-strip { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 0.6rem; }
  .live-pill { font-family: 'DM Mono', monospace; font-size: 0.72rem; color: var(--muted); background: var(--surface2); border: 1px solid var(--border); border-radius: 100px; padding: 0.25rem 0.7rem; }
  .live-pill b { color: var(--accent3); font-weight: 500; }

  .btn-primary { background: var(--accent); color: #fff; border: none; font-family: 'Inter', sans-serif; font-weight: 700; font-size: 0.9rem; padding: 0.75rem 1.6rem; border-radius: 9px; cursor: pointer; transition: background 0.15s, transform 0.15s; display: inline-flex; align-items: center; gap: 0.5rem; }
  .btn-primary:hover { background: #6a58e8; transform: translateY(-1px); }
  .btn-primary:disabled { opacity: 0.6; cursor: not-allowed; transform: none; }
  .btn-secondary { background: rgba(255,255,255,0.05); color: var(--text); border: 1px solid var(--border); font-family: 'Inter', sans-serif; font-weight: 600; font-size: 0.82rem; padding: 0.55rem 1.1rem; border-radius: 8px; cursor: pointer; transition: all 0.15s; }
  .btn-secondary:hover { border-color: var(--accent); color: var(--accent); }
  .btn-row { display: flex; gap: 0.7rem; align-items: center; flex-wrap: wrap; }

  .error-box { background: rgba(247,106,106,0.1); border: 1px solid rgba(247,106,106,0.3); color: var(--danger); border-radius: 9px; padding: 0.75rem 1rem; font-size: 0.85rem; margin-top: 1rem; }
  .note-box { background: rgba(247,162,106,0.08); border: 1px solid rgba(247,162,106,0.3); color: var(--accent2); border-radius: 9px; padding: 0.6rem 0.9rem; font-size: 0.8rem; margin-bottom: 1.2rem; }

  /* SCORE + CHART ROW */
  .top-grid { display: grid; grid-template-columns: 1.05fr 1fr; gap: 1.4rem; margin-bottom: 1.4rem; }
  .top-grid .card { margin-bottom: 0; }
  .score-card { display: flex; align-items: center; gap: 1.6rem; flex-wrap: wrap; }
  .ring-wrap { position: relative; width: 150px; height: 150px; flex-shrink: 0; }
  .ring-wrap svg { transform: rotate(-90deg); }
  .ring-center { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; }
  .ring-value { font-family: 'Sora', sans-serif; font-size: 2rem; font-weight: 800; line-height: 1.1; }
  .ring-caption { font-size: 0.6rem; color: var(--muted); font-family: 'DM Mono', monospace; text-transform: uppercase; letter-spacing: 0.06em; }
  .score-detail { flex: 1; min-width: 200px; }
  .score-band { font-family: 'Sora', sans-serif; font-size: 1.2rem; font-weight: 700; margin-bottom: 0.3rem; }
  .score-desc { color: var(--muted); font-size: 0.84rem; line-height: 1.6; margin-bottom: 0.8rem; }
  .mini-stats { display: flex; gap: 1.2rem; flex-wrap: wrap; margin-bottom: 0.8rem; }
  .mini-stat .k { font-family: 'DM Mono', monospace; font-size: 0.62rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
  .mini-stat .v { font-family: 'Sora', sans-serif; font-weight: 700; font-size: 1.05rem; }
  .target-line { font-size: 0.82rem; padding: 0.5rem 0.75rem; border-radius: 8px; }
  .target-line.ok { background: rgba(106,247,200,0.08); border: 1px solid rgba(106,247,200,0.3); color: var(--accent3); }
  .target-line.gap { background: rgba(247,162,106,0.08); border: 1px solid rgba(247,162,106,0.3); color: var(--accent2); }

  /* STAT CARDS */
  .stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 0.9rem; margin-bottom: 1.4rem; }
  .stat-card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 1rem 1.1rem; }
  .stat-label { font-family: 'DM Mono', monospace; font-size: 0.62rem; letter-spacing: 0.08em; text-transform: uppercase; color: var(--muted); margin-bottom: 0.45rem; }
  .stat-value { font-family: 'Sora', sans-serif; font-size: 1.4rem; font-weight: 800; }
  .stat-value .unit { font-size: 0.8rem; color: var(--muted); font-weight: 600; margin-left: 0.2rem; }

  /* BADGES */
  .badge { display: inline-flex; align-items: center; gap: 0.35rem; font-family: 'DM Mono', monospace; font-size: 0.66rem; font-weight: 500; padding: 0.26rem 0.62rem; border-radius: 100px; text-transform: uppercase; letter-spacing: 0.04em; white-space: nowrap; }
  .badge.pass { background: rgba(106,247,200,0.1); border: 1px solid rgba(106,247,200,0.3); color: var(--accent3); }
  .badge.warn, .badge.medium { background: rgba(247,162,106,0.1); border: 1px solid rgba(247,162,106,0.3); color: var(--accent2); }
  .badge.fail, .badge.high { background: rgba(247,106,106,0.1); border: 1px solid rgba(247,106,106,0.3); color: var(--danger); }
  .badge.low { background: rgba(124,106,247,0.12); border: 1px solid rgba(124,106,247,0.35); color: var(--accent); }
  .badge.empty { background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.1); color: var(--muted); }

  /* SEO LENGTH CHECKS */
  .seo-item { margin-bottom: 1.1rem; }
  .seo-item:last-child { margin-bottom: 0; }
  .seo-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.4rem; gap: 0.6rem; }
  .seo-head label { font-size: 0.8rem; color: var(--text); font-weight: 600; }
  .seo-meta { display: flex; align-items: center; gap: 0.6rem; }
  .seo-count { font-family: 'DM Mono', monospace; font-size: 0.72rem; color: var(--muted); }
  .bar { height: 5px; background: rgba(255,255,255,0.06); border-radius: 100px; margin-top: 0.45rem; overflow: hidden; }
  .bar-fill { height: 100%; width: 0; border-radius: 100px; background: var(--accent3); transition: width 0.2s, background 0.2s; }
  .seo-tip { font-size: 0.72rem; color: var(--muted); margin-top: 0.35rem; }

  /* AI CHECKS */
  .check-list { display: flex; flex-direction: column; gap: 0.7rem; }
  .check-item { display: flex; gap: 0.9rem; align-items: flex-start; padding: 0.8rem 0.9rem; background: var(--surface2); border: 1px solid var(--border); border-radius: 10px; }
  .check-item .badge { margin-top: 0.1rem; min-width: 58px; justify-content: center; }
  .check-name { font-weight: 600; font-size: 0.87rem; }
  .check-note { color: var(--muted); font-size: 0.8rem; }

  /* HIGHLIGHTED TEXT */
  .legend { display: flex; gap: 0.6rem; flex-wrap: wrap; margin-bottom: 1rem; }
  .legend label { display: inline-flex; align-items: center; gap: 0.4rem; font-size: 0.76rem; color: var(--muted); cursor: pointer; background: var(--surface2); border: 1px solid var(--border); border-radius: 100px; padding: 0.25rem 0.7rem; }
  .legend .sw { width: 10px; height: 10px; border-radius: 3px; display: inline-block; }
  .highlight-box { background: var(--surface2); border: 1px solid var(--border); border-radius: 10px; padding: 1.2rem 1.3rem; max-height: 460px; overflow-y: auto; font-size: 0.9rem; line-height: 1.85; }
  .highlight-box p { margin-bottom: 0.9rem; }
  .highlight-box p:last-child { margin-bottom: 0; }
  .s-very_long { background: rgba(247,106,106,0.2); border-radius: 3px; }
  .s-long { background: rgba(247,162,106,0.2); border-radius: 3px; }
  .s-passive { text-decoration: underline wavy rgba(124,106,247,0.9); text-underline-offset: 4px; }
  .highlight-box.no-long .s-long, .highlight-box.no-vlong .s-very_long { background: transparent; }
  .highlight-box.no-passive .s-passive { text-decoration: none; }

  /* TABLE */
  .table-head-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.9rem; flex-wrap: wrap; gap: 0.7rem; }
  .table-head-row h2 { margin-bottom: 0; }
  .table-tabs { display: flex; gap: 0.4rem; flex-wrap: wrap; }
  .table-tab { border: 1px solid var(--border); background: var(--surface2); color: var(--muted); font-family: 'Inter', sans-serif; font-size: 0.78rem; font-weight: 600; padding: 0.4rem 0.85rem; border-radius: 8px; cursor: pointer; transition: all 0.15s; }
  .table-tab.active { background: rgba(124,106,247,0.14); border-color: rgba(124,106,247,0.4); color: var(--accent); }
  .table-scroll { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 0.84rem; }
  thead th { text-align: left; font-family: 'DM Mono', monospace; font-size: 0.66rem; letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted); font-weight: 500; padding: 0.6rem 0.7rem; border-bottom: 1px solid var(--border); white-space: nowrap; }
  tbody td { padding: 0.65rem 0.7rem; border-bottom: 1px solid rgba(255,255,255,0.04); vertical-align: top; }
  tbody tr:hover { background: rgba(255,255,255,0.02); }
  td.num { font-family: 'DM Mono', monospace; white-space: nowrap; }
  td.fix { color: var(--muted); font-size: 0.78rem; min-width: 200px; }
  .empty-note { color: var(--muted); font-size: 0.85rem; padding: 1rem 0; text-align: center; }

  .loading-note { color: var(--muted); font-size: 0.85rem; display: none; align-items: center; gap: 0.5rem; }
  .spinner { width: 14px; height: 14px; border: 2px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.7s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }

  @media (max-width: 980px) {
    .top-grid { grid-template-columns: 1fr; }
  }
  @media (max-width: 860px) {
    .layout { flex-direction: column; }
    .sidebar { width: 100%; height: auto; position: relative; }
    .main { padding: 1.4rem 1rem 3rem; }
    .row-2 { grid-template-columns: 1fr; }
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
      <a href="#" class="sidebar-link active">📖 Word Count Checker</a>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-label">Navigate</div>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="sidebar-link">🧰 All My Tools</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio" class="sidebar-link">👩‍💻 Portfolio</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/blog/index.html" class="sidebar-link">📝 Blog Posts</a>
    </div>
    <div class="sidebar-info">
      <strong>Flesch Reading Ease</strong> runs 0–100 — higher is easier. Most web copy should score <strong>60+</strong>. The <strong>Flesch-Kincaid grade</strong> shows the school grade needed to follow your text; aim for grade 8 or below.
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
      <h1 class="page-title">📖 Word Count Checker</h1>
      <p class="page-sub">Count words and characters, get your Flesch Reading Ease and grade level, and see exactly which sentences to rewrite. Paste your copy or check a live page, then tighten it for readers, Google and AI search.</p>
    </div>

    <div class="card">
      <h2>Analyze content</h2>

      <div class="mode-tabs">
        <button type="button" class="mode-tab active" data-mode="text">Paste Text</button>
        <button type="button" class="mode-tab" data-mode="url">Fetch URL</button>
      </div>

      <div id="textMode" class="field">
        <label for="textInput">Content to analyze</label>
        <textarea id="textInput" placeholder="Paste your article, landing-page copy or blog draft here. Leave a blank line between paragraphs."></textarea>
        <div class="live-strip">
          <span class="live-pill"><b id="liveWords">0</b> words</span>
          <span class="live-pill"><b id="liveChars">0</b> characters</span>
          <span class="live-pill"><b id="liveCharsNs">0</b> without spaces</span>
          <span class="live-pill"><b id="liveRead">0 sec</b> read</span>
        </div>
      </div>

      <div id="urlMode" class="field hidden">
        <label for="urlInput">Page URL</label>
        <input type="url" id="urlInput" placeholder="https://www.example.com/personal-loan">
        <div class="field-hint">I'll pull the page's main body text (skipping menus and footers) and fill in its title, meta description and H1 below.</div>
      </div>

      <div class="field" style="max-width: 420px;">
        <label for="targetSelect">Target audience (sets your readability goal)</label>
        <select id="targetSelect">
          <option value="60" selected>General web readers — Flesch 60+</option>
          <option value="65">Finance &amp; loan customers — Flesch 65+</option>
          <option value="70">Mobile-first / quick answers — Flesch 70+</option>
          <option value="50">Expert / B2B audience — Flesch 50+</option>
        </select>
      </div>

      <div class="btn-row">
        <button type="button" class="btn-primary" id="analyzeBtn">🔍 Check Readability</button>
        <button type="button" class="btn-secondary" id="clearBtn">Clear</button>
        <div class="loading-note" id="loadingNote"><span class="spinner"></span> Analyzing…</div>
      </div>
      <div class="error-box hidden" id="errorBox"></div>
    </div>

    <div class="card">
      <h2>SEO length check</h2>
      <p class="card-sub">Type or paste your snippet text; counts update as you type. The URL mode fills these in for you.</p>
      <div class="seo-item" data-key="title" data-min="50" data-max="60" data-hard="65">
        <div class="seo-head"><label for="seoTitle">Title tag</label><div class="seo-meta"><span class="seo-count">0 / 60</span><span class="badge empty">empty</span></div></div>
        <input type="text" id="seoTitle" placeholder="Personal Loan Interest Rates 2026 – Compare & Apply">
        <div class="bar"><div class="bar-fill"></div></div>
        <div class="seo-tip">Best: 50–60 characters. Longer titles get cut off in Google results.</div>
      </div>
      <div class="seo-item" data-key="meta" data-min="120" data-max="155" data-hard="165">
        <div class="seo-head"><label for="seoMeta">Meta description</label><div class="seo-meta"><span class="seo-count">0 / 155</span><span class="badge empty">empty</span></div></div>
        <input type="text" id="seoMeta" placeholder="Compare personal loan rates from top banks, check eligibility in minutes and apply online.">
        <div class="bar"><div class="bar-fill"></div></div>
        <div class="seo-tip">Best: 120–155 characters. Put the benefit and a call to action early.</div>
      </div>
      <div class="seo-item" data-key="h1" data-min="20" data-max="70" data-hard="80">
        <div class="seo-head"><label for="seoH1">H1 heading</label><div class="seo-meta"><span class="seo-count">0 / 70</span><span class="badge empty">empty</span></div></div>
        <input type="text" id="seoH1" placeholder="Personal Loan Interest Rates">
        <div class="bar"><div class="bar-fill"></div></div>
        <div class="seo-tip">Best: 20–70 characters. One clear H1 that matches the search intent.</div>
      </div>
    </div>

    <div id="resultsWrap" class="hidden">

      <div class="note-box hidden" id="sourceNote"></div>

      <div class="top-grid">
        <div class="card">
          <h2>Readability score</h2>
          <div class="score-card">
            <div class="ring-wrap">
              <svg width="150" height="150" viewBox="0 0 150 150">
                <circle cx="75" cy="75" r="63" fill="none" stroke="rgba(255,255,255,0.06)" stroke-width="13"/>
                <circle id="ringProgress" cx="75" cy="75" r="63" fill="none" stroke="var(--accent3)" stroke-width="13" stroke-linecap="round" stroke-dasharray="395.84" stroke-dashoffset="395.84" style="transition: stroke-dashoffset 0.6s ease"/>
              </svg>
              <div class="ring-center">
                <div class="ring-value" id="ringValue">0</div>
                <div class="ring-caption">Flesch ease</div>
              </div>
            </div>
            <div class="score-detail">
              <div class="score-band" id="scoreBand">—</div>
              <div class="score-desc" id="scoreDesc"></div>
              <div class="mini-stats">
                <div class="mini-stat"><div class="k">FK grade</div><div class="v" id="fkGrade">—</div></div>
                <div class="mini-stat"><div class="k">Words / sentence</div><div class="v" id="wps">—</div></div>
                <div class="mini-stat"><div class="k">Syllables / word</div><div class="v" id="spw">—</div></div>
              </div>
              <div class="target-line" id="targetLine"></div>
            </div>
          </div>
        </div>
        <div class="card chart-card">
          <h2>Sentence length mix</h2>
          <div style="position: relative; height: 250px;"><canvas id="distChart"></canvas></div>
        </div>
      </div>

      <div class="stat-grid" id="statGrid"></div>

      <div class="card">
        <h2>AI-search &amp; snippet readiness</h2>
        <p class="card-sub">Checks that make your copy easy for Google featured snippets and AI answers to quote.</p>
        <div class="check-list" id="checkList"></div>
      </div>

      <div class="card">
        <h2>Your text, highlighted</h2>
        <div class="legend">
          <label><input type="checkbox" id="tgVlong" checked> <span class="sw" style="background: rgba(247,106,106,0.5)"></span> Very long (25+ words)</label>
          <label><input type="checkbox" id="tgLong" checked> <span class="sw" style="background: rgba(247,162,106,0.5)"></span> Long (21–25 words)</label>
          <label><input type="checkbox" id="tgPassive" checked> <span class="sw" style="background: var(--accent)"></span> Passive voice</label>
        </div>
        <div class="highlight-box" id="highlightBox"></div>
      </div>

      <div class="card">
        <div class="table-head-row">
          <h2>Sentences to fix</h2>
          <div class="btn-row">
            <div class="table-tabs" id="issueTabs">
              <button type="button" class="table-tab active" data-filter="all">All</button>
              <button type="button" class="table-tab" data-filter="high">High</button>
              <button type="button" class="table-tab" data-filter="medium">Medium</button>
              <button type="button" class="table-tab" data-filter="low">Low</button>
            </div>
            <button type="button" class="btn-secondary" id="csvBtn">⬇ Export CSV</button>
          </div>
        </div>
        <div class="table-scroll">
          <table>
            <thead><tr><th>#</th><th>Sentence</th><th>Words</th><th>Issue</th><th>Severity</th><th>How to fix</th></tr></thead>
            <tbody id="issueBody"></tbody>
          </table>
        </div>
      </div>

    </div>
  </main>
</div>

<script>
let mode = 'text';
let lastResult = null;
let issueFilter = 'all';
let distChart = null;
const READING_WPM = 238;

// ---------- Mode tabs ----------
document.querySelectorAll('.mode-tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.mode-tab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    mode = btn.dataset.mode;
    document.getElementById('textMode').classList.toggle('hidden', mode !== 'text');
    document.getElementById('urlMode').classList.toggle('hidden', mode !== 'url');
  });
});

// ---------- Live counter ----------
function fmtTime(sec) {
  if (sec < 60) return sec + ' sec';
  const m = Math.floor(sec / 60), s = sec % 60;
  return s ? `${m} min ${s} sec` : `${m} min`;
}
function liveCount() {
  const t = document.getElementById('textInput').value;
  const words = (t.match(/[A-Za-z0-9₹$€£%]+(?:['’\-.,][A-Za-z0-9]+)*/g) || []).length;
  document.getElementById('liveWords').textContent = words.toLocaleString();
  document.getElementById('liveChars').textContent = t.length.toLocaleString();
  document.getElementById('liveCharsNs').textContent = t.replace(/\s/g, '').length.toLocaleString();
  document.getElementById('liveRead').textContent = fmtTime(Math.ceil(words / READING_WPM * 60));
}
document.getElementById('textInput').addEventListener('input', liveCount);

// ---------- SEO length checks ----------
function evalSeo(item) {
  const input = item.querySelector('input');
  const len = input.value.trim().length;
  const min = +item.dataset.min, max = +item.dataset.max, hard = +item.dataset.hard;
  const badge = item.querySelector('.badge');
  const fill = item.querySelector('.bar-fill');
  item.querySelector('.seo-count').textContent = `${len} / ${max}`;
  let cls = 'empty', label = 'empty', color = 'var(--muted)';
  if (len === 0) { cls = 'empty'; label = 'empty'; }
  else if (len < min) { cls = 'warn'; label = 'too short'; color = 'var(--accent2)'; }
  else if (len <= max) { cls = 'pass'; label = 'good'; color = 'var(--accent3)'; }
  else if (len <= hard) { cls = 'warn'; label = 'slightly long'; color = 'var(--accent2)'; }
  else { cls = 'fail'; label = 'too long'; color = 'var(--danger)'; }
  badge.className = 'badge ' + cls;
  badge.textContent = label;
  fill.style.width = Math.min(len / hard * 100, 100) + '%';
  fill.style.background = color;
}
document.querySelectorAll('.seo-item').forEach(item => {
  item.querySelector('input').addEventListener('input', () => evalSeo(item));
});

// ---------- Analyze ----------
document.getElementById('analyzeBtn').addEventListener('click', analyze);
document.getElementById('clearBtn').addEventListener('click', () => {
  document.getElementById('textInput').value = '';
  document.getElementById('urlInput').value = '';
  liveCount();
  document.getElementById('resultsWrap').classList.add('hidden');
  document.getElementById('errorBox').classList.add('hidden');
});
document.getElementById('targetSelect').addEventListener('change', () => { if (lastResult) renderTarget(); });

async function analyze() {
  const btn = document.getElementById('analyzeBtn');
  const errorBox = document.getElementById('errorBox');
  const loading = document.getElementById('loadingNote');
  errorBox.classList.add('hidden');

  const payload = { mode };
  if (mode === 'url') {
    payload.url = document.getElementById('urlInput').value.trim();
    if (!payload.url) return showError('Please enter a URL.');
  } else {
    payload.text = document.getElementById('textInput').value;
    if (!payload.text.trim()) return showError('Please paste some text to analyze.');
  }

  btn.disabled = true;
  loading.style.display = 'flex';
  try {
    const res = await fetch('/api/analyze', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Something went wrong.');
    lastResult = data;
    render(data);
  } catch (e) {
    showError(e.message);
  } finally {
    btn.disabled = false;
    loading.style.display = 'none';
  }
}

function showError(msg) {
  const box = document.getElementById('errorBox');
  box.textContent = msg;
  box.classList.remove('hidden');
}

// ---------- Render ----------
function render(d) {
  document.getElementById('resultsWrap').classList.remove('hidden');

  // Source note
  const note = document.getElementById('sourceNote');
  if (d.seo) {
    note.innerHTML = `Analyzed the main body text of <b>${escapeHtml(d.source)}</b>. Check the highlighted text below to confirm the right content was picked up.`;
    note.classList.remove('hidden');
    const map = { title: 'seoTitle', meta_description: 'seoMeta', h1: 'seoH1' };
    Object.entries(map).forEach(([k, id]) => { document.getElementById(id).value = d.seo[k] || ''; });
    document.querySelectorAll('.seo-item').forEach(evalSeo);
  } else if (d.truncated) {
    note.textContent = 'Your text was very long, so only the first 150,000 characters were analyzed.';
    note.classList.remove('hidden');
  } else {
    note.classList.add('hidden');
  }

  const sc = d.scores, st = d.stats;

  // Ring
  const ring = document.getElementById('ringProgress');
  const circ = 395.84;
  ring.setAttribute('stroke-dashoffset', String(circ * (1 - sc.flesch / 100)));
  const col = sc.flesch >= 60 ? 'var(--accent3)' : sc.flesch >= 30 ? 'var(--accent2)' : 'var(--danger)';
  ring.setAttribute('stroke', col);
  const rv = document.getElementById('ringValue');
  rv.textContent = Math.round(sc.flesch);
  rv.style.color = col;
  document.getElementById('scoreBand').textContent = sc.band;
  document.getElementById('scoreDesc').textContent = sc.band_desc;
  document.getElementById('fkGrade').textContent = sc.fk_grade;
  document.getElementById('wps').textContent = st.avg_words_per_sentence;
  document.getElementById('spw').textContent = st.avg_syllables_per_word;
  renderTarget();

  // Stats
  const stats = [
    ['Words', st.words.toLocaleString()],
    ['Characters', st.characters.toLocaleString()],
    ['Chars (no spaces)', st.characters_no_spaces.toLocaleString()],
    ['Sentences', st.sentences.toLocaleString()],
    ['Paragraphs', st.paragraphs.toLocaleString()],
    ['Unique words', st.unique_words.toLocaleString()],
    ['Reading time', fmtTime(st.reading_time_sec)],
    ['Speaking time', fmtTime(st.speaking_time_sec)],
    ['Avg word length', st.avg_word_length + '<span class="unit">chars</span>'],
    ['Complex words', st.complex_word_pct + '<span class="unit">%</span>'],
    ['Long sentences', st.long_sentences + '<span class="unit">(' + st.long_sentence_pct + '%)</span>'],
    ['Passive voice', st.passive_sentences + '<span class="unit">(' + st.passive_pct + '%)</span>'],
  ];
  document.getElementById('statGrid').innerHTML = stats.map(([k, v]) =>
    `<div class="stat-card"><div class="stat-label">${k}</div><div class="stat-value">${v}</div></div>`).join('');

  // AI checks
  const lbl = { pass: 'pass', warn: 'improve', fail: 'fix' };
  document.getElementById('checkList').innerHTML = d.ai_checks.map(c =>
    `<div class="check-item"><span class="badge ${c.status}">${lbl[c.status]}</span>
      <div><div class="check-name">${escapeHtml(c.name)}</div><div class="check-note">${escapeHtml(c.note)}</div></div></div>`).join('');

  renderChart(d.distribution);
  renderHighlight(d);
  renderIssues();
}

function renderTarget() {
  const target = +document.getElementById('targetSelect').value;
  const f = lastResult.scores.flesch;
  const line = document.getElementById('targetLine');
  if (f >= target) {
    line.className = 'target-line ok';
    line.textContent = `✓ On target: ${Math.round(f)} meets your goal of ${target}+.`;
  } else {
    line.className = 'target-line gap';
    line.textContent = `${Math.round(target - f)} points below your goal of ${target}+. Shorten long sentences and swap complex words first.`;
  }
}

function renderChart(dist) {
  const colors = ['#6af7c8', '#6af7c8', '#6af7c8', '#f7a26a', '#f76a6a', '#f76a6a'];
  const ctx = document.getElementById('distChart');
  if (typeof Chart === 'undefined') return;
  if (distChart) distChart.destroy();
  distChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: dist.map(b => b.label + ' words'),
      datasets: [{ data: dist.map(b => b.count), backgroundColor: colors, borderRadius: 6, maxBarThickness: 42 }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: c => ` ${c.parsed.y} sentence${c.parsed.y === 1 ? '' : 's'}` } } },
      scales: {
        x: { ticks: { color: '#8888a8', font: { family: 'DM Mono', size: 10 } }, grid: { display: false } },
        y: { beginAtZero: true, ticks: { color: '#8888a8', precision: 0, font: { family: 'DM Mono', size: 10 } }, grid: { color: 'rgba(255,255,255,0.05)' } }
      }
    }
  });
}

function renderHighlight(d) {
  const box = document.getElementById('highlightBox');
  box.innerHTML = d.paragraphs.map(p => '<p>' + p.map(i => {
    const s = d.sentences[i];
    const cls = [];
    if (s.level !== 'ok') cls.push('s-' + s.level);
    if (s.passive) cls.push('s-passive');
    const title = [s.words + ' words', s.passive ? 'passive: "' + s.passive_phrase + '"' : ''].filter(Boolean).join(' · ');
    return `<span class="${cls.join(' ')}" title="${escapeHtml(title)}">${escapeHtml(s.text)}</span>`;
  }).join(' ') + '</p>').join('');
  applyToggles();
}
function applyToggles() {
  const box = document.getElementById('highlightBox');
  box.classList.toggle('no-vlong', !document.getElementById('tgVlong').checked);
  box.classList.toggle('no-long', !document.getElementById('tgLong').checked);
  box.classList.toggle('no-passive', !document.getElementById('tgPassive').checked);
}
['tgVlong', 'tgLong', 'tgPassive'].forEach(id => document.getElementById(id).addEventListener('change', applyToggles));

// ---------- Issues ----------
function buildIssues() {
  const rows = [];
  lastResult.sentences.forEach((s, i) => {
    if (s.level === 'very_long') rows.push({ n: i + 1, text: s.text, words: s.words, issue: 'Very long sentence', sev: 'high',
      fix: 'Split into two or three sentences of under 20 words each.' });
    else if (s.level === 'long') rows.push({ n: i + 1, text: s.text, words: s.words, issue: 'Long sentence', sev: 'medium',
      fix: 'Trim filler words or break at "and", "which" or "because".' });
    if (s.passive) rows.push({ n: i + 1, text: s.text, words: s.words, issue: `Passive voice ("${s.passive_phrase}")`, sev: 'low',
      fix: 'Name who does the action: "We approve your loan" beats "Your loan is approved".' });
  });
  return rows;
}
document.querySelectorAll('#issueTabs .table-tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#issueTabs .table-tab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    issueFilter = btn.dataset.filter;
    renderIssues();
  });
});
function renderIssues() {
  const rows = buildIssues().filter(r => issueFilter === 'all' || r.sev === issueFilter);
  const body = document.getElementById('issueBody');
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="6"><div class="empty-note">${issueFilter === 'all' ? '🎉 No long or passive sentences found. Nice, clean copy.' : 'Nothing at this severity.'}</div></td></tr>`;
    return;
  }
  body.innerHTML = rows.map(r => `<tr>
    <td class="num">${r.n}</td>
    <td>${escapeHtml(r.text.length > 220 ? r.text.slice(0, 220) + '…' : r.text)}</td>
    <td class="num">${r.words}</td>
    <td>${escapeHtml(r.issue)}</td>
    <td><span class="badge ${r.sev}">${r.sev}</span></td>
    <td class="fix">${escapeHtml(r.fix)}</td></tr>`).join('');
}

// ---------- CSV export ----------
document.getElementById('csvBtn').addEventListener('click', () => {
  if (!lastResult) return;
  const q = v => `"${String(v).replace(/"/g, '""')}"`;
  const st = lastResult.stats, sc = lastResult.scores;
  let csv = 'Section,Metric,Value\n';
  const summary = [
    ['Source', lastResult.source], ['Flesch Reading Ease', sc.flesch], ['Readability band', sc.band],
    ['Flesch-Kincaid grade', sc.fk_grade], ['Words', st.words], ['Characters', st.characters],
    ['Characters (no spaces)', st.characters_no_spaces], ['Sentences', st.sentences], ['Paragraphs', st.paragraphs],
    ['Unique words', st.unique_words], ['Syllables', st.syllables], ['Avg words per sentence', st.avg_words_per_sentence],
    ['Avg syllables per word', st.avg_syllables_per_word], ['Complex words %', st.complex_word_pct],
    ['Long sentences %', st.long_sentence_pct], ['Passive voice %', st.passive_pct],
    ['Reading time', fmtTime(st.reading_time_sec)], ['Speaking time', fmtTime(st.speaking_time_sec)]
  ];
  summary.forEach(([k, v]) => { csv += `Summary,${q(k)},${q(v)}\n`; });
  lastResult.ai_checks.forEach(c => { csv += `AI readiness,${q(c.name)},${q(c.status + ' - ' + c.note)}\n`; });
  csv += '\nSentence #,Sentence,Words,Issue,Severity,How to fix\n';
  buildIssues().forEach(r => { csv += `${r.n},${q(r.text)},${r.words},${q(r.issue)},${r.sev},${q(r.fix)}\n`; });
  const blob = new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'readability-report.csv';
  a.click();
  URL.revokeObjectURL(url);
});

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str == null ? '' : String(str);
  return div.innerHTML.replace(/"/g, '&quot;');
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(debug=True, port=5000)
