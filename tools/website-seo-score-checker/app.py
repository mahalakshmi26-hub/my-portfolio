"""
Website SEO Score Checker — Dashboard Edition
Audits a single URL against ~25 free on-page/technical SEO factors and
produces a weighted 0-100 score with a full checklist.
Flask app | by Mahalakshmi Marimuthu
"""

import csv
import io
import re
import uuid
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from flask import Flask, abort, render_template_string, request, send_file

app = Flask(__name__)

# in-memory store for CSV exports (keeps the last 20 runs)
STORE = {}
STORE_LIMIT = 20

FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}
TIMEOUT = 15
ROBOTS_TIMEOUT = 8

GENERIC_ANCHORS = {
    "click here", "here", "read more", "learn more", "more info",
    "more", "link", "this page", "this", "website", "page", "click",
}

# 5 categories, weighted equally (20% each) toward the overall score
CATEGORIES = [
    ("meta", "Meta & Indexing"),
    ("content", "Content & Mobile"),
    ("structure", "Heading Structure"),
    ("links", "Link Profile"),
    ("technical", "Technical Setup"),
]
CATEGORY_LABELS = dict(CATEGORIES)
POINTS = {"pass": 1.0, "warn": 0.5, "fail": 0.0}


# ---------------------------------------------------------------- core logic
def add(checks, category, cid, label, status, detail, tip=""):
    checks.append(
        {
            "category": category,
            "id": cid,
            "label": label,
            "status": status,  # pass | warn | fail
            "detail": detail,
            "tip": tip,
        }
    )


def fetch_robots(root_url):
    """Returns (reachable, allowed, sitemap_urls)."""
    robots_url = urljoin(root_url, "/robots.txt")
    try:
        resp = requests.get(robots_url, headers=FETCH_HEADERS, timeout=ROBOTS_TIMEOUT)
    except requests.exceptions.RequestException:
        return False, True, []
    if resp.status_code != 200:
        return False, True, []
    lines = resp.text.splitlines()
    rp = RobotFileParser()
    rp.parse(lines)
    sitemap_urls = [
        line.split(":", 1)[1].strip()
        for line in lines
        if line.strip().lower().startswith("sitemap:") and ":" in line
    ]
    try:
        allowed = rp.can_fetch(FETCH_HEADERS["User-Agent"], root_url) and rp.can_fetch("*", root_url)
    except Exception:
        allowed = True
    return True, allowed, sitemap_urls


def check_sitemap(root_url, robots_reachable, sitemap_urls):
    if sitemap_urls:
        return True, sitemap_urls[0]
    try:
        resp = requests.get(urljoin(root_url, "/sitemap.xml"), headers=FETCH_HEADERS, timeout=ROBOTS_TIMEOUT)
        body = resp.text[:300].lower()
        if resp.status_code == 200 and ("<urlset" in body or "<sitemapindex" in body or "<?xml" in body):
            return True, urljoin(root_url, "/sitemap.xml")
    except requests.exceptions.RequestException:
        pass
    return False, None


def run_audit(url: str, keyword: str = "") -> dict:
    parsed = urlparse(url)
    root_url = f"{parsed.scheme}://{parsed.netloc}"
    domain = parsed.netloc.lower().replace("www.", "")
    keyword = keyword.strip().lower()

    checks = []

    resp = requests.get(url, headers=FETCH_HEADERS, timeout=TIMEOUT, allow_redirects=True)
    html = resp.text
    soup = BeautifulSoup(html, "html.parser")

    # -------------------- TECHNICAL --------------------
    if resp.status_code == 200:
        add(checks, "technical", "http_status", "HTTP status code", "pass", f"Page responded {resp.status_code} OK.")
    elif 300 <= resp.status_code < 400:
        add(checks, "technical", "http_status", "HTTP status code", "warn", f"Page responded {resp.status_code} (redirected).", "Point internal links straight to the final URL instead of relying on a redirect.")
    else:
        add(checks, "technical", "http_status", "HTTP status code", "fail", f"Page responded {resp.status_code}.", "Fix the server error or broken URL — Google can't index a page it can't fetch cleanly.")

    robots_reachable, robots_allowed, sitemap_hint_urls = fetch_robots(root_url)
    if not robots_reachable:
        add(checks, "technical", "robots_txt", "robots.txt", "warn", "Could not find/reach robots.txt at the site root.", "Add a robots.txt file at the domain root so crawlers get clear crawl instructions.")
    elif not robots_allowed:
        add(checks, "technical", "robots_txt", "robots.txt", "fail", "robots.txt is blocking this exact page from crawlers.", "Remove the Disallow rule covering this URL if you want it indexed.")
    else:
        add(checks, "technical", "robots_txt", "robots.txt", "pass", "robots.txt exists and doesn't block this page.")

    sitemap_found, sitemap_url = check_sitemap(root_url, robots_reachable, sitemap_hint_urls)
    if sitemap_found:
        add(checks, "technical", "xml_sitemap", "XML sitemap", "pass", f"Found at {sitemap_url}.")
    else:
        add(checks, "technical", "xml_sitemap", "XML sitemap", "warn", "No sitemap referenced in robots.txt and none found at /sitemap.xml.", "Generate an XML sitemap and reference it in robots.txt so search engines can discover your pages faster.")

    page_kb = len(resp.content) / 1024
    if page_kb < 150:
        add(checks, "technical", "page_size", "HTML page size", "pass", f"{page_kb:.0f} KB — lean and fast to load.")
    elif page_kb < 300:
        add(checks, "technical", "page_size", "HTML page size", "warn", f"{page_kb:.0f} KB — getting heavy.", "Trim unused markup/inline scripts to keep the HTML payload light.")
    else:
        add(checks, "technical", "page_size", "HTML page size", "fail", f"{page_kb:.0f} KB — very heavy HTML.", "A large HTML document slows first render — audit for bloated inline scripts/styles or excessive markup.")

    # -------------------- META & INDEXING --------------------
    title_tag = soup.find("title")
    title_text = title_tag.get_text(strip=True) if title_tag else ""
    if not title_text:
        add(checks, "meta", "title", "Title tag", "fail", "No <title> tag found.", "Add a unique, descriptive title tag — it's one of the strongest on-page ranking signals.")
    elif 30 <= len(title_text) <= 60:
        add(checks, "meta", "title", "Title tag", "pass", f"\"{title_text}\" ({len(title_text)} chars).")
    else:
        add(checks, "meta", "title", "Title tag", "warn", f"\"{title_text}\" ({len(title_text)} chars) — outside the 30-60 char sweet spot.", "Aim for 30-60 characters so the title doesn't get truncated in search results.")

    desc_tag = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    desc_text = desc_tag.get("content", "").strip() if desc_tag else ""
    if not desc_text:
        add(checks, "meta", "meta_description", "Meta description", "fail", "No meta description found.", "Add a meta description (70-160 chars) summarizing the page to improve click-through from search results.")
    elif 70 <= len(desc_text) <= 160:
        add(checks, "meta", "meta_description", "Meta description", "pass", f"\"{desc_text}\" ({len(desc_text)} chars).")
    else:
        add(checks, "meta", "meta_description", "Meta description", "warn", f"\"{desc_text}\" ({len(desc_text)} chars) — outside the 70-160 char sweet spot.", "Rewrite to land between 70-160 characters so it isn't cut off in search results.")

    robots_meta = soup.find("meta", attrs={"name": re.compile("^robots$", re.I)})
    robots_content = robots_meta.get("content", "").lower() if robots_meta else ""
    if "noindex" in robots_content:
        add(checks, "meta", "meta_robots", "Indexability (meta robots)", "fail", "Page has a noindex directive — it's telling search engines to skip it.", "Remove the noindex tag if you want this page to appear in search results.")
    else:
        add(checks, "meta", "meta_robots", "Indexability (meta robots)", "pass", "No noindex directive found.")

    canonical = soup.find("link", attrs={"rel": re.compile("canonical", re.I)})
    if canonical and canonical.get("href", "").strip():
        add(checks, "meta", "canonical", "Canonical tag", "pass", f"Points to {canonical.get('href').strip()}.")
    else:
        add(checks, "meta", "canonical", "Canonical tag", "warn", "No canonical tag found.", "Add a self-referencing canonical tag to avoid duplicate-content issues.")

    html_tag = soup.find("html")
    if html_tag and html_tag.get("lang", "").strip():
        add(checks, "meta", "html_lang", "Language declared", "pass", f"lang=\"{html_tag.get('lang').strip()}\".")
    else:
        add(checks, "meta", "html_lang", "Language declared", "warn", "No lang attribute on <html>.", "Add lang=\"en\" (or the right code) to the <html> tag to help search engines and screen readers.")

    charset = soup.find("meta", attrs={"charset": True}) or soup.find("meta", attrs={"http-equiv": re.compile("content-type", re.I)})
    if charset:
        add(checks, "meta", "charset", "Character encoding", "pass", "Charset declared.")
    else:
        add(checks, "meta", "charset", "Character encoding", "warn", "No charset meta tag found.", "Add <meta charset=\"UTF-8\"> near the top of <head>.")

    favicon = soup.find("link", attrs={"rel": re.compile("icon", re.I)})
    if favicon:
        add(checks, "meta", "favicon", "Favicon", "pass", "Favicon link found.")
    else:
        add(checks, "meta", "favicon", "Favicon", "warn", "No favicon link tag found.", "Add a favicon — small polish signal for trust and brand recall in browser tabs/bookmarks.")

    og_title = soup.find("meta", attrs={"property": "og:title"})
    og_image = soup.find("meta", attrs={"property": "og:image"})
    if og_title and og_image:
        add(checks, "meta", "open_graph", "Open Graph tags", "pass", "og:title and og:image present.")
    elif og_title or og_image:
        add(checks, "meta", "open_graph", "Open Graph tags", "warn", "Only partial Open Graph tags found.", "Add both og:title and og:image so shared links look right on social media.")
    else:
        add(checks, "meta", "open_graph", "Open Graph tags", "fail", "No Open Graph tags found.", "Add og:title, og:description and og:image so the page previews well when shared.")

    ld_json = soup.find_all("script", attrs={"type": "application/ld+json"})
    if ld_json:
        add(checks, "meta", "structured_data", "Structured data (schema.org)", "pass", f"{len(ld_json)} JSON-LD block(s) found.")
    else:
        add(checks, "meta", "structured_data", "Structured data (schema.org)", "warn", "No JSON-LD structured data found.", "Add schema.org markup (Article, Product, FAQ, etc.) to become eligible for rich results.")

    # -------------------- CONTENT & MOBILE --------------------
    if parsed.scheme == "https":
        add(checks, "content", "https", "HTTPS", "pass", "Page is served over HTTPS.")
    else:
        add(checks, "content", "https", "HTTPS", "fail", "Page is served over plain HTTP.", "Move to HTTPS — it's a confirmed ranking signal and a browser trust indicator.")

    viewport = soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)})
    if viewport:
        add(checks, "content", "viewport", "Mobile viewport tag", "pass", "Viewport meta tag present.")
    else:
        add(checks, "content", "viewport", "Mobile viewport tag", "fail", "No viewport meta tag found.", "Add <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"> for mobile SEO.")

    text_soup = BeautifulSoup(html, "html.parser")
    for tag in text_soup(["script", "style", "noscript"]):
        tag.decompose()
    body_text = text_soup.get_text(separator=" ")
    words = re.findall(r"[A-Za-z0-9']+", body_text)
    word_count = len(words)
    if word_count >= 600:
        add(checks, "content", "word_count", "Content length", "pass", f"~{word_count} words.")
    elif word_count >= 300:
        add(checks, "content", "word_count", "Content length", "warn", f"~{word_count} words — on the thin side.", "Aim for 600+ words of genuinely useful content where the topic supports it.")
    else:
        add(checks, "content", "word_count", "Content length", "fail", f"~{word_count} words — thin content.", "Thin pages struggle to rank. Expand with real detail the reader needs.")

    images = soup.find_all("img")
    if not images:
        add(checks, "content", "image_alt", "Image alt text", "pass", "No images on this page.")
    else:
        with_alt = sum(1 for img in images if (img.get("alt") or "").strip())
        frac = with_alt / len(images)
        detail = f"{with_alt}/{len(images)} images have alt text."
        if frac >= 0.9:
            add(checks, "content", "image_alt", "Image alt text", "pass", detail)
        elif frac >= 0.5:
            add(checks, "content", "image_alt", "Image alt text", "warn", detail, "Add descriptive alt text to the remaining images — it helps accessibility and image search.")
        else:
            add(checks, "content", "image_alt", "Image alt text", "fail", detail, "Most images are missing alt text — add it for accessibility and image SEO.")

    if keyword:
        first_words = " ".join(words[:120]).lower()
        body_lower = body_text.lower()
        if keyword in first_words:
            add(checks, "content", "keyword_placement", "Focus keyword in opening content", "pass", f"\"{keyword}\" appears early in the page.")
        elif keyword in body_lower:
            add(checks, "content", "keyword_placement", "Focus keyword in opening content", "warn", f"\"{keyword}\" appears later on the page, not near the top.", "Work the focus keyword into the first ~100 words so intent is clear immediately.")
        else:
            add(checks, "content", "keyword_placement", "Focus keyword in opening content", "fail", f"\"{keyword}\" wasn't found anywhere in the page text.", "Make sure the focus keyword actually appears in the visible content.")

    # -------------------- HEADING STRUCTURE --------------------
    headings = [
        (int(tag.name[1]), tag.get_text(strip=True))
        for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
    ]
    h1s = [txt for lvl, txt in headings if lvl == 1]
    if len(h1s) == 1:
        add(checks, "structure", "h1_presence", "Single H1", "pass", f"\"{h1s[0]}\".")
    elif len(h1s) == 0:
        add(checks, "structure", "h1_presence", "Single H1", "fail", "No H1 tag found.", "Every page needs exactly one H1 that states what the page is about.")
    else:
        add(checks, "structure", "h1_presence", "Single H1", "warn", f"{len(h1s)} H1 tags found.", "Keep just one H1 per page — use H2s for the rest of the structure.")

    skips = []
    prev = None
    empties = 0
    for lvl, txt in headings:
        if prev is not None and lvl > prev + 1:
            skips.append(f"H{prev}→H{lvl}")
        prev = lvl
        if not txt:
            empties += 1
    if skips:
        add(checks, "structure", "heading_hierarchy", "Heading hierarchy", "warn", "Skipped levels: " + ", ".join(skips) + ".", "Don't skip heading levels (e.g. H2 straight to H4) — keep the outline nested in order.")
    else:
        add(checks, "structure", "heading_hierarchy", "Heading hierarchy", "pass", "No skipped heading levels.")

    if empties:
        add(checks, "structure", "empty_headings", "Empty headings", "warn", f"{empties} empty heading tag(s) found.", "Remove or fill in headings with no text — empty headings confuse both users and crawlers.")
    else:
        add(checks, "structure", "empty_headings", "Empty headings", "pass", "No empty heading tags.")

    if keyword:
        if any(keyword in txt.lower() for txt in h1s):
            add(checks, "structure", "keyword_in_h1", "Focus keyword in H1", "pass", "Keyword found in the H1.")
        else:
            add(checks, "structure", "keyword_in_h1", "Focus keyword in H1", "warn", "Keyword not found in the H1.", "Work the focus keyword into the H1 where it reads naturally.")

    # -------------------- LINK PROFILE --------------------
    anchors = [a for a in soup.find_all("a", href=True)]
    internal, external, broken_placeholders, generic = 0, 0, 0, 0
    counted = 0
    for a in anchors:
        href = a["href"].strip()
        if href.lower().startswith(("mailto:", "tel:")):
            continue
        if href in ("#", "") or href.lower().startswith("javascript:"):
            broken_placeholders += 1
            continue
        resolved = urljoin(url, href)
        link_domain = urlparse(resolved).netloc.lower().replace("www.", "")
        if link_domain == "" or link_domain == domain:
            internal += 1
        else:
            external += 1
        counted += 1
        anchor_text = a.get_text(strip=True).lower()
        if not anchor_text or anchor_text in GENERIC_ANCHORS:
            generic += 1

    if internal > 0:
        add(checks, "links", "internal_links", "Internal links", "pass", f"{internal} internal link(s) found.")
    else:
        add(checks, "links", "internal_links", "Internal links", "fail", "No internal links found.", "Link to other relevant pages on your own site — it spreads authority and helps crawlers discover content.")

    if external > 0:
        add(checks, "links", "external_links", "External links", "pass", f"{external} external link(s) found.")
    else:
        add(checks, "links", "external_links", "External links", "warn", "No external links found.", "Linking out to credible, relevant sources can support trust — not mandatory, but usually a healthy signal.")

    if counted > 0:
        generic_frac = generic / counted
        detail = f"{generic} of {counted} links use generic or empty anchor text."
        if generic_frac < 0.10:
            add(checks, "links", "anchor_text_quality", "Descriptive anchor text", "pass", detail)
        elif generic_frac < 0.30:
            add(checks, "links", "anchor_text_quality", "Descriptive anchor text", "warn", detail, "Replace vague anchors like \"click here\" with text that describes the destination.")
        else:
            add(checks, "links", "anchor_text_quality", "Descriptive anchor text", "fail", detail, "Most links use generic anchor text — rewrite them to describe where they go.")
    else:
        add(checks, "links", "anchor_text_quality", "Descriptive anchor text", "warn", "No linkable anchors found to evaluate.")

    if broken_placeholders == 0:
        add(checks, "links", "broken_anchor_placeholders", "Placeholder links (href=\"#\")", "pass", "No placeholder/empty href links found.")
    else:
        add(checks, "links", "broken_anchor_placeholders", "Placeholder links (href=\"#\")", "warn", f"{broken_placeholders} link(s) point nowhere (href=\"#\" or empty).", "Point these to a real destination or remove the anchor if it isn't a working link.")

    return {"checks": checks, "resp": resp}


def score_view(checks: list) -> dict:
    by_cat = {key: [] for key, _ in CATEGORIES}
    for c in checks:
        by_cat[c["category"]].append(c)

    category_rows = []
    cat_scores = []
    for key, label in CATEGORIES:
        items = by_cat[key]
        if items:
            pts = sum(POINTS[c["status"]] for c in items)
            score = round(100 * pts / len(items))
        else:
            score = 100
        cat_scores.append(score)
        category_rows.append({"key": key, "label": label, "score": score, "checks": items})

    overall = round(sum(cat_scores) / len(cat_scores)) if cat_scores else 0
    counts = {"pass": 0, "warn": 0, "fail": 0}
    for c in checks:
        counts[c["status"]] += 1

    return {
        "overall": overall,
        "categories": category_rows,
        "counts": counts,
        "total": len(checks),
    }


def build_export_rows(checks: list) -> list:
    return [
        {
            "Category": CATEGORY_LABELS.get(c["category"], c["category"]),
            "Check": c["label"],
            "Status": c["status"].upper(),
            "Detail": c["detail"],
            "Suggested Fix": c["tip"],
        }
        for c in checks
    ]


def clean_url(raw: str) -> str:
    u = raw.strip()
    if u and not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u


# ---------------------------------------------------------------- template
PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Website SEO Score Checker</title>
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
  .sidebar{width:230px;background:var(--surface);border-right:1px solid var(--border);padding:1.4rem 1rem;position:fixed;top:0;bottom:0;display:flex;flex-direction:column}
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
  .form-row{display:flex;gap:.8rem;flex-wrap:wrap}
  .form-field{flex:1;min-width:220px}
  .form-field label{font-size:.8rem;color:var(--muted);display:block;margin-bottom:.4rem}
  input[type=text]{width:100%;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:.7rem .9rem;font-family:'DM Mono',monospace;font-size:.82rem}
  input[type=text]:focus{outline:none;border-color:var(--accent)}
  .btn{display:inline-flex;align-items:center;gap:.5rem;background:var(--accent);color:#fff;border:none;border-radius:8px;padding:.6rem 1.5rem;font-size:.85rem;font-weight:600;cursor:pointer;font-family:'Inter',sans-serif;margin-top:.9rem}
  .btn:hover{background:var(--accent-h)}
  .hint{font-size:.72rem;color:var(--muted);margin-top:.6rem;font-family:'DM Mono',monospace}

  .metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1rem;margin:1.4rem 0}
  .metric{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1rem 1.2rem}
  .metric .k{font-family:'DM Mono',monospace;font-size:.66rem;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
  .metric .v{font-family:'Sora',sans-serif;font-size:1.6rem;font-weight:800;margin-top:.2rem}
  .metric .v.purple{color:var(--accent)} .metric .v.mint{color:var(--mint)}
  .metric .v.orange{color:var(--orange)} .metric .v.red{color:var(--red)}

  .charts{display:grid;grid-template-columns:280px 1fr 1fr;gap:1rem;margin-bottom:1.4rem}
  .chart-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.1rem 1.2rem}
  .chart-card h3{font-family:'Sora',sans-serif;font-size:.82rem;font-weight:700;margin-bottom:.8rem;color:var(--muted)}
  .ring-wrap{position:relative;width:170px;height:170px;margin:0 auto}
  .ring-center{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
  .ring-center .score{font-family:'Sora',sans-serif;font-size:2rem;font-weight:800}
  .ring-center .lbl{font-size:.62rem;font-family:'DM Mono',monospace;color:var(--muted);text-transform:uppercase;letter-spacing:.12em}

  .sec-title{font-family:'Sora',sans-serif;font-size:1rem;font-weight:700;margin:1.8rem 0 .9rem}
  details{background:var(--surface);border:1px solid var(--border);border-radius:12px;margin-bottom:.6rem}
  summary{cursor:pointer;padding:.85rem 1.1rem;font-size:.85rem;display:flex;align-items:center;gap:.7rem;flex-wrap:wrap;list-style:none}
  summary::-webkit-details-marker{display:none}
  summary .cat-name{font-family:'Sora',sans-serif;font-weight:700}
  summary .cat-score{margin-left:auto;font-family:'DM Mono',monospace;font-size:.75rem;color:var(--muted)}
  .check-list{padding:.2rem 1.1rem 1rem}
  .check-row{padding:.7rem 0;border-top:1px solid var(--border);display:flex;gap:.8rem;align-items:flex-start}
  .check-row:first-child{border-top:none}
  .badge{display:inline-block;flex-shrink:0;font-size:.64rem;font-family:'DM Mono',monospace;padding:.2rem .55rem;border-radius:100px;white-space:nowrap;margin-top:.1rem}
  .badge.pass{background:rgba(106,247,200,.1);border:1px solid rgba(106,247,200,.3);color:var(--mint)}
  .badge.warn{background:rgba(247,162,106,.1);border:1px solid rgba(247,162,106,.35);color:var(--orange)}
  .badge.fail{background:rgba(247,106,124,.1);border:1px solid rgba(247,106,124,.35);color:var(--red)}
  .check-body .lbl{font-weight:600;font-size:.86rem}
  .check-body .det{font-size:.78rem;color:var(--muted);margin-top:.15rem}
  .check-body .tip{font-size:.76rem;color:var(--accent2, var(--orange));margin-top:.3rem}

  .dl-row{display:flex;gap:.8rem;margin-top:1.4rem;flex-wrap:wrap}
  .dl-row a{display:inline-flex;align-items:center;gap:.5rem;background:var(--surface);border:1px solid var(--border);color:var(--text);padding:.55rem 1.2rem;border-radius:8px;font-size:.8rem;font-weight:500}
  .dl-row a:hover{border-color:var(--accent)}

  .metric-clickable{cursor:pointer;transition:border-color .15s,transform .15s}
  .metric-clickable:hover{border-color:var(--accent);transform:translateY(-1px)}
  .filter-row{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;margin-bottom:.9rem}
  .filter-pill{cursor:pointer;font-family:'DM Mono',monospace;font-size:.72rem;padding:.35rem .85rem;border-radius:100px;background:var(--surface);border:1px solid var(--border);color:var(--muted)}
  .filter-pill:hover{color:var(--text);border-color:var(--accent)}
  .filter-pill.active{color:var(--bg);background:var(--muted);border-color:var(--muted)}
  .filter-pill.active.fail{background:var(--red);border-color:var(--red)}
  .filter-pill.active.warn{background:var(--orange);border-color:var(--orange)}
  .filter-pill.active.pass{background:var(--mint);border-color:var(--mint)}
  .filter-pill.active.all{background:var(--accent);border-color:var(--accent)}
  .check-row.is-hidden{display:none}
  details.cat-details.is-hidden{display:none}

  .spinner{display:none;margin-top:.8rem;font-size:.78rem;color:var(--muted);font-family:'DM Mono',monospace}
  .spinner.show{display:block}

  @media(max-width:960px){.charts{grid-template-columns:1fr}.sidebar{display:none}.main{margin-left:0;padding:1.2rem}}
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
      <a href="#" class="sidebar-link active">🧪&nbsp; SEO Score Checker</a>
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
        <div class="crumb">// seo tool · free</div>
        <h1>🧪 Website <span>SEO Score Checker</span></h1>
      </div>
    </div>

    <form class="card" method="POST" action="/analyze" onsubmit="document.getElementById('sp').classList.add('show')">
      <div class="form-row">
        <div class="form-field">
          <label>Website URL</label>
          <input type="text" name="url" placeholder="https://example.com/page" value="{{ raw_url or '' }}" required>
        </div>
        <div class="form-field">
          <label>Focus keyword (optional)</label>
          <input type="text" name="keyword" placeholder="e.g. personal loan" value="{{ raw_keyword or '' }}">
        </div>
      </div>
      <button class="btn" type="submit">🚀 Run SEO Check</button>
      <div class="spinner" id="sp">Auditing the page against ~25 on-page &amp; technical checks…</div>
      {% if error %}<div class="hint" style="color:var(--red)">{{ error }}</div>{% endif %}
      <div class="hint">Checks title/meta tags, headings, links, mobile &amp; technical setup — 100% free, no sign-up, no paid APIs.</div>
    </form>

    {% if view %}
    <div class="metrics">
      <div class="metric"><div class="k">Overall Score</div><div class="v {{ 'mint' if view.overall >= 80 else 'orange' if view.overall >= 50 else 'red' }}">{{ view.overall }}/100</div></div>
      <div class="metric metric-clickable" onclick="filterChecks('pass')" title="Jump to passed checks"><div class="k">Checks Passed</div><div class="v mint">{{ view.counts.pass }}</div></div>
      <div class="metric metric-clickable" onclick="filterChecks('warn')" title="Jump to warnings"><div class="k">Warnings</div><div class="v orange">{{ view.counts.warn }}</div></div>
      <div class="metric metric-clickable" onclick="filterChecks('fail')" title="Jump to failed checks"><div class="k">Failed</div><div class="v red">{{ view.counts.fail }}</div></div>
      <div class="metric metric-clickable" onclick="filterChecks('all')" title="Jump to full checklist"><div class="k">Total Checks</div><div class="v purple">{{ view.total }}</div></div>
    </div>

    <div class="charts">
      <div class="chart-card">
        <h3>OVERALL SEO HEALTH</h3>
        <div class="ring-wrap">
          <canvas id="ring"></canvas>
          <div class="ring-center">
            <div class="score" style="color:{{ '#6af7c8' if view.overall >= 80 else '#f7a26a' if view.overall >= 50 else '#f76a7c' }}">{{ view.overall }}</div>
            <div class="lbl">out of 100</div>
          </div>
        </div>
      </div>
      <div class="chart-card">
        <h3>SCORE BY CATEGORY</h3>
        <canvas id="bars" height="170"></canvas>
      </div>
      <div class="chart-card">
        <h3>PASS / WARN / FAIL</h3>
        <canvas id="donut" height="170"></canvas>
      </div>
    </div>

    <div class="dl-row">
      <a href="/download/{{ token }}">⬇️ Download Full Report (CSV)</a>
    </div>

    <div class="sec-title" id="checklist">📋 Detailed Checklist</div>
    <div class="filter-row">
      <span class="filter-pill active all" data-status="all" onclick="filterChecks('all')">All ({{ view.total }})</span>
      <span class="filter-pill fail" data-status="fail" onclick="filterChecks('fail')">✕ Failed ({{ view.counts.fail }})</span>
      <span class="filter-pill warn" data-status="warn" onclick="filterChecks('warn')">⚠ Warnings ({{ view.counts.warn }})</span>
      <span class="filter-pill pass" data-status="pass" onclick="filterChecks('pass')">✓ Passed ({{ view.counts.pass }})</span>
    </div>
    {% for cat in view.categories %}
    <details class="cat-details" {% if cat.score < 100 %}open{% endif %}>
      <summary>
        {% if cat.score >= 80 %}<span class="badge pass">✓</span>{% elif cat.score >= 50 %}<span class="badge warn">⚠</span>{% else %}<span class="badge fail">✕</span>{% endif %}
        <span class="cat-name">{{ cat.label }}</span>
        <span class="cat-score">{{ cat.score }}/100</span>
      </summary>
      <div class="check-list">
        {% for c in cat.checks %}
        <div class="check-row" data-status="{{ c.status }}">
          <span class="badge {{ c.status }}">{{ c.status }}</span>
          <div class="check-body">
            <div class="lbl">{{ c.label }}</div>
            <div class="det">{{ c.detail }}</div>
            {% if c.tip %}<div class="tip">💡 {{ c.tip }}</div>{% endif %}
          </div>
        </div>
        {% endfor %}
      </div>
    </details>
    {% endfor %}

    <script>
      const V = {
        overall: {{ view.overall }},
        catLabels: [{% for cat in view.categories %}"{{ cat.label }}",{% endfor %}],
        catScores: [{% for cat in view.categories %}{{ cat.score }},{% endfor %}],
        pass: {{ view.counts.pass }}, warn: {{ view.counts.warn }}, fail: {{ view.counts.fail }}
      };
      const MUTED = '#8888a8', GRID = 'rgba(136,136,168,0.12)';
      new Chart(document.getElementById('ring'), {
        type: 'doughnut',
        data: { datasets: [{ data: [V.overall, 100 - V.overall],
          backgroundColor: [V.overall >= 80 ? '#6af7c8' : V.overall >= 50 ? '#f7a26a' : '#f76a7c', '#23233a'],
          borderWidth: 0, cutout: '78%' }] },
        options: { plugins: { legend: { display: false }, tooltip: { enabled: false } } }
      });
      new Chart(document.getElementById('bars'), {
        type: 'bar',
        data: { labels: V.catLabels, datasets: [{ data: V.catScores,
          backgroundColor: V.catScores.map(s => s >= 80 ? '#6af7c8' : s >= 50 ? '#f7a26a' : '#f76a7c'),
          borderRadius: 6 }] },
        options: { indexAxis: 'y', plugins: { legend: { display: false } },
          scales: { x: { min: 0, max: 100, ticks: { color: MUTED }, grid: { color: GRID } },
                    y: { ticks: { color: MUTED, font: { size: 10.5 } }, grid: { display: false } } } }
      });
      const donutStatuses = ['pass', 'warn', 'fail'];
      new Chart(document.getElementById('donut'), {
        type: 'doughnut',
        data: { labels: ['Passed', 'Warnings', 'Failed'],
          datasets: [{ data: [V.pass, V.warn, V.fail], borderWidth: 0, cutout: '60%',
            backgroundColor: ['#6af7c8', '#f7a26a', '#f76a7c'] }] },
        options: {
          onClick: (evt, elements) => { if (elements.length) filterChecks(donutStatuses[elements[0].index]); },
          onHover: (evt, elements) => { evt.native.target.style.cursor = elements.length ? 'pointer' : 'default'; },
          plugins: { legend: { position: 'right', labels: { color: MUTED, boxWidth: 10, font: { size: 11 } } } }
        }
      });

      function filterChecks(status) {
        document.querySelectorAll('.check-row').forEach(row => {
          row.classList.toggle('is-hidden', status !== 'all' && row.dataset.status !== status);
        });
        document.querySelectorAll('details.cat-details').forEach(det => {
          const rows = det.querySelectorAll('.check-row');
          const anyVisible = Array.from(rows).some(r => !r.classList.contains('is-hidden'));
          det.classList.toggle('is-hidden', !anyVisible);
          if (status !== 'all' && anyVisible) det.open = true;
        });
        document.querySelectorAll('.filter-pill').forEach(p => p.classList.toggle('active', p.dataset.status === status));
        document.getElementById('checklist').scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
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
    return render_template_string(PAGE, view=None, error=None, raw_url="", raw_keyword="")


@app.route("/analyze", methods=["POST"])
def analyze():
    raw_url = request.form.get("url", "")
    raw_keyword = request.form.get("keyword", "")
    url = clean_url(raw_url)
    if not url:
        return render_template_string(PAGE, view=None, error="Please enter a URL.", raw_url=raw_url, raw_keyword=raw_keyword)

    try:
        result = run_audit(url, raw_keyword)
    except requests.exceptions.Timeout:
        return render_template_string(PAGE, view=None, error="The page took too long to respond. Please try again.", raw_url=raw_url, raw_keyword=raw_keyword)
    except requests.exceptions.RequestException as e:
        return render_template_string(PAGE, view=None, error=f"Couldn't fetch that URL ({type(e).__name__}). Double-check it's correct and publicly reachable.", raw_url=raw_url, raw_keyword=raw_keyword)
    except Exception:
        return render_template_string(PAGE, view=None, error="Something went wrong analyzing that page. Please try again.", raw_url=raw_url, raw_keyword=raw_keyword)

    checks = result["checks"]
    view = score_view(checks)

    token = uuid.uuid4().hex[:12]
    STORE[token] = checks
    while len(STORE) > STORE_LIMIT:
        STORE.pop(next(iter(STORE)))

    return render_template_string(PAGE, view=view, error=None, raw_url=raw_url, raw_keyword=raw_keyword, token=token)


@app.route("/download/<token>")
def download(token):
    checks = STORE.get(token)
    if checks is None:
        abort(404)
    rows = build_export_rows(checks)
    text_buf = io.StringIO()
    writer = csv.DictWriter(text_buf, fieldnames=["Category", "Check", "Status", "Detail", "Suggested Fix"])
    writer.writeheader()
    writer.writerows(rows)
    buf = io.BytesIO(text_buf.getvalue().encode("utf-8-sig"))
    return send_file(buf, as_attachment=True, download_name="seo_score_report.csv", mimetype="text/csv")


if __name__ == "__main__":
    app.run(debug=True, port=8501)
