"""
Open Graph Checker  -  Flask app
Checks a page's Open Graph + Twitter Card tags, previews how the link will look
when shared on Twitter/X, Facebook, LinkedIn, WhatsApp, Slack, Discord, Telegram
and Google Search, validates the og:image (dimensions / file size / crop safety
per platform), cross-checks Twitter Card tags against their OG fallbacks, checks
whether robots.txt blocks the social-media crawlers themselves, and scores the
page with concrete fixes. Also supports bulk-checking up to 20 URLs at once.

Built by Mahalakshmi Marimuthu - Digital Marketing Strategist & AI-Powered SEO Expert
"""
import csv
import io
import re
import time
import uuid
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, render_template_string, request, send_file

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

app = Flask(__name__)

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
MAX_BULK_URLS = 20
REQUEST_TIMEOUT = 10
IMAGE_MAX_BYTES = 6 * 1024 * 1024  # 6 MB cap while downloading an og:image
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Real crawler user-agents used to see if robots.txt blocks the social bots
# themselves (a very common, invisible reason a page's card fails to render).
BOT_AGENTS = [
    ("facebookexternalhit", "Facebook / Instagram"),
    ("Twitterbot", "Twitter / X"),
    ("LinkedInBot", "LinkedIn"),
    ("WhatsApp", "WhatsApp"),
    ("Slackbot-LinkExpanding", "Slack"),
    ("Discordbot", "Discord"),
    ("TelegramBot", "Telegram"),
]

STORE = {}          # token -> bulk results, for CSV export
STORE_LIMIT = 40

# --------------------------------------------------------------------------- #
# Platform preview specs: aspect ratio for the image crop box + how much of the
# title/description that platform typically shows before truncating.
# --------------------------------------------------------------------------- #
PLATFORMS = [
    {"key": "facebook",  "label": "Facebook",   "icon": "f",  "ratio": 1.91, "title_len": 100, "desc_len": 135, "shows_domain": True},
    {"key": "linkedin",  "label": "LinkedIn",   "icon": "in", "ratio": 1.91, "title_len": 70,  "desc_len": 100, "shows_domain": True},
    {"key": "twitter",   "label": "Twitter / X", "icon": "X", "ratio": 2.0,  "title_len": 70,  "desc_len": 125, "shows_domain": True},
    {"key": "whatsapp",  "label": "WhatsApp",   "icon": "W",  "ratio": 1.91, "title_len": 65,  "desc_len": 65,  "shows_domain": True},
    {"key": "slack",     "label": "Slack",      "icon": "S",  "ratio": 1.91, "title_len": 70,  "desc_len": 160, "shows_domain": True},
    {"key": "discord",   "label": "Discord",    "icon": "D",  "ratio": 1.91, "title_len": 256, "desc_len": 300, "shows_domain": False},
    {"key": "telegram",  "label": "Telegram",   "icon": "T",  "ratio": 1.91, "title_len": 90,  "desc_len": 180, "shows_domain": True},
    {"key": "google",    "label": "Google Search", "icon": "G", "ratio": None, "title_len": 60, "desc_len": 160, "shows_domain": True},
]


# --------------------------------------------------------------------------- #
# URL helpers
# --------------------------------------------------------------------------- #
def normalize_url(raw):
    raw = (raw or "").strip()
    if not raw:
        return ""
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    return raw


def clean_bulk_urls(raw_text):
    seen, out = set(), []
    for line in (raw_text or "").splitlines():
        u = normalize_url(line)
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def domain_of(url):
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return url


def truncate(text, n):
    text = text or ""
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# --------------------------------------------------------------------------- #
# Fetch + parse
# --------------------------------------------------------------------------- #
def fetch_html(url):
    try:
        r = requests.get(
            url, headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"},
            timeout=REQUEST_TIMEOUT, allow_redirects=True,
        )
        return {
            "ok": True, "final_url": r.url, "status_code": r.status_code,
            "html": r.text if r.status_code < 400 else "", "error": None,
        }
    except requests.exceptions.Timeout:
        return {"ok": False, "final_url": url, "status_code": None, "html": "", "error": "Request timed out."}
    except requests.exceptions.SSLError:
        return {"ok": False, "final_url": url, "status_code": None, "html": "", "error": "SSL certificate error."}
    except requests.exceptions.ConnectionError:
        return {"ok": False, "final_url": url, "status_code": None, "html": "", "error": "Could not connect to this URL."}
    except requests.exceptions.RequestException as e:
        return {"ok": False, "final_url": url, "status_code": None, "html": "", "error": f"Request failed ({e.__class__.__name__})."}


def parse_tags(html, base_url):
    soup = BeautifulSoup(html or "", "html.parser")

    def meta(prop_or_name, attr="property"):
        tag = soup.find("meta", attrs={attr: prop_or_name})
        if not tag or not tag.get("content"):
            tag = soup.find("meta", attrs={"property" if attr == "name" else "name": prop_or_name})
        return (tag.get("content").strip() if tag and tag.get("content") else "")

    og = {}
    for t in soup.find_all("meta", attrs={"property": re.compile(r"^og:", re.I)}):
        key = t.get("property", "").lower()
        val = (t.get("content") or "").strip()
        if key and val and key not in og:
            og[key] = val

    tw = {}
    for t in soup.find_all("meta", attrs={"name": re.compile(r"^twitter:", re.I)}):
        key = t.get("name", "").lower()
        val = (t.get("content") or "").strip()
        if key and val and key not in tw:
            tw[key] = val

    title_tag = (soup.title.string.strip() if soup.title and soup.title.string else "")
    meta_desc = meta("description", "name")
    canonical_tag = soup.find("link", rel="canonical")
    canonical = urljoin(base_url, canonical_tag.get("href").strip()) if canonical_tag and canonical_tag.get("href") else ""

    if og.get("og:image"):
        og["og:image"] = urljoin(base_url, og["og:image"])
    if tw.get("twitter:image"):
        tw["twitter:image"] = urljoin(base_url, tw["twitter:image"])

    return {"og": og, "twitter": tw, "title_tag": title_tag, "meta_description": meta_desc, "canonical": canonical}


def check_image(image_url):
    if not image_url:
        return {"present": False}
    info = {"present": True, "url": image_url, "ok": False, "width": None, "height": None,
            "size_bytes": None, "content_type": None, "error": None, "ratio": None}
    try:
        r = requests.get(
            image_url, headers={"User-Agent": BROWSER_UA}, timeout=REQUEST_TIMEOUT, stream=True,
        )
        if r.status_code >= 400:
            info["error"] = f"Image request returned {r.status_code}."
            return info
        info["content_type"] = r.headers.get("Content-Type", "").split(";")[0].strip()
        chunks = []
        total = 0
        for chunk in r.iter_content(8192):
            total += len(chunk)
            chunks.append(chunk)
            if total > IMAGE_MAX_BYTES:
                break
        data = b"".join(chunks)
        info["size_bytes"] = total
        if HAS_PIL:
            try:
                img = Image.open(io.BytesIO(data))
                info["width"], info["height"] = img.size
                info["ok"] = True
                if info["height"]:
                    info["ratio"] = round(info["width"] / info["height"], 2)
            except Exception:
                info["error"] = "Could not read image dimensions (unsupported or corrupted format)."
        else:
            info["ok"] = True
    except requests.exceptions.Timeout:
        info["error"] = "Image request timed out."
    except requests.exceptions.RequestException:
        info["error"] = "Could not download the image."
    return info


def check_robots(final_url):
    try:
        origin = urlparse(final_url)
        robots_url = f"{origin.scheme}://{origin.netloc}/robots.txt"
        r = requests.get(robots_url, headers={"User-Agent": BROWSER_UA}, timeout=REQUEST_TIMEOUT)
        if r.status_code >= 400:
            return {"checked": True, "found": False, "blocked": []}
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(r.text.splitlines())
        blocked = [label for agent, label in BOT_AGENTS if not rp.can_fetch(agent, final_url)]
        return {"checked": True, "found": True, "blocked": blocked}
    except requests.exceptions.RequestException:
        return {"checked": False, "found": False, "blocked": []}


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def add_issue(issues, severity, message, fix=None):
    issues.append({"severity": severity, "message": message, "fix": fix or ""})


def score_result(data):
    og, tw = data["og"], data["twitter"]
    issues = []
    score = 0
    checks = {}

    def mark(key, ok, warn_msg=None):
        checks[key] = "ok" if ok else ("warn" if warn_msg else "error")

    # og:title (15)
    title = og.get("og:title") or ""
    if title:
        if 15 <= len(title) <= 95:
            score += 15
            checks["title"] = "ok"
        else:
            score += 8
            checks["title"] = "warn"
            add_issue(issues, "warn", f"og:title is {len(title)} characters.",
                      "Aim for roughly 15-95 characters so it isn't cut off or oddly short on any platform.")
    else:
        checks["title"] = "error"
        add_issue(issues, "error", "og:title is missing.", "Add <meta property=\"og:title\" content=\"...\">.")

    # og:description (15)
    desc = og.get("og:description") or ""
    if desc:
        if 40 <= len(desc) <= 300:
            score += 15
            checks["description"] = "ok"
        else:
            score += 8
            checks["description"] = "warn"
            add_issue(issues, "warn", f"og:description is {len(desc)} characters.",
                      "Aim for roughly 40-300 characters - most platforms truncate well before 300.")
    else:
        checks["description"] = "error"
        add_issue(issues, "error", "og:description is missing.", "Add <meta property=\"og:description\" content=\"...\">.")

    # og:image (30: presence 10, loads 10, dimensions 10)
    img = data["image_info"]
    if img.get("present"):
        score += 10
        if img.get("ok"):
            score += 10
            checks["image_loads"] = "ok"
            w, h = img.get("width"), img.get("height")
            if w and h:
                if w < 200 or h < 200:
                    checks["image_size"] = "error"
                    add_issue(issues, "error", f"og:image is only {w}x{h}px - several platforms won't show an image this small.",
                              "Use at least 600x315px; 1200x630px is the safe, universal size.")
                elif w < 600:
                    checks["image_size"] = "warn"
                    score += 5
                    add_issue(issues, "warn", f"og:image is {w}x{h}px - usable but small.",
                              "1200x630px looks sharp on every platform, including retina screens.")
                else:
                    score += 10
                    checks["image_size"] = "ok"
                ratio = img.get("ratio") or 0
                if ratio and not (1.5 <= ratio <= 2.1):
                    add_issue(issues, "warn", f"og:image aspect ratio is {ratio}:1 - most platforms crop to roughly 1.91:1.",
                              "Use a 1200x630px (1.91:1) image so platform cropping doesn't cut off important parts.")
            size_kb = round((img.get("size_bytes") or 0) / 1024)
            if size_kb > 5000:
                add_issue(issues, "warn", f"og:image is about {size_kb}KB - large images can be skipped or load slowly.",
                          "Keep it under 5MB (ideally well under 1MB) so crawlers fetch it reliably.")
        else:
            checks["image_loads"] = "error"
            add_issue(issues, "error", f"og:image could not be loaded: {img.get('error') or 'unknown error'}.",
                      "Make sure the image URL is public, returns a real image, and isn't blocked for crawlers.")
    else:
        checks["image_loads"] = "error"
        add_issue(issues, "error", "og:image is missing.", "Add <meta property=\"og:image\" content=\"https://.../image.jpg\"> - without it most platforms show a blank card.")

    # og:url (10)
    og_url = og.get("og:url") or ""
    if og_url:
        score += 5
        norm_final = data["final_url"].split("#")[0].rstrip("/")
        if og_url.split("#")[0].rstrip("/") == norm_final:
            score += 5
            checks["url_tag"] = "ok"
        else:
            checks["url_tag"] = "warn"
            add_issue(issues, "warn", "og:url doesn't match the page's actual URL.",
                      "Point og:url at the exact canonical URL so shares always consolidate to one link.")
    else:
        checks["url_tag"] = "warn"
        add_issue(issues, "warn", "og:url is missing.", "Add <meta property=\"og:url\" content=\"https://your-canonical-url\">.")

    # og:type (5)
    if og.get("og:type"):
        score += 5
        checks["type_tag"] = "ok"
    else:
        checks["type_tag"] = "warn"
        add_issue(issues, "warn", "og:type is missing (defaults to \"website\" on most platforms).",
                  "Add <meta property=\"og:type\" content=\"website\"> (or \"article\" for blog posts).")

    # og:site_name (5)
    if og.get("og:site_name"):
        score += 5
        checks["site_name"] = "ok"
    else:
        checks["site_name"] = "warn"
        add_issue(issues, "warn", "og:site_name is missing.", "Add <meta property=\"og:site_name\" content=\"Your Brand\"> - shown next to the title on Facebook/LinkedIn.")

    # Twitter Card cross-check (10)
    card = tw.get("twitter:card") or ""
    tw_image = tw.get("twitter:image") or ""
    if card:
        score += 5
        checks["twitter_card"] = "ok"
    else:
        checks["twitter_card"] = "warn"
        add_issue(issues, "warn", "twitter:card is missing.",
                  "Add <meta name=\"twitter:card\" content=\"summary_large_image\"> for a big-image preview on X - without it, X falls back to a small thumbnail card even when og:image is set.")
    if not tw_image and not img.get("present"):
        checks["twitter_image"] = "error"
    elif not tw_image and img.get("present"):
        score += 5
        checks["twitter_image"] = "ok"
    else:
        score += 5
        checks["twitter_image"] = "ok"
    if card and card not in ("summary", "summary_large_image", "app", "player"):
        add_issue(issues, "warn", f"twitter:card value \"{card}\" isn't a recognised type.",
                  "Use \"summary_large_image\" (big image) or \"summary\" (small thumbnail).")

    # robots.txt bot block (10)
    robots = data["robots"]
    if robots.get("blocked"):
        checks["robots"] = "error"
        add_issue(issues, "error",
                  f"robots.txt blocks: {', '.join(robots['blocked'])}. These crawlers can't fetch the page to build a preview at all.",
                  "Add an explicit Allow rule (or remove the Disallow) for the listed bot names in robots.txt.")
    else:
        score += 10
        checks["robots"] = "ok"

    score = max(0, min(100, score))
    issues.sort(key=lambda i: {"error": 0, "warn": 1, "info": 2}[i["severity"]])
    return {"score": score, "checks": checks, "issues": issues}


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def analyze_url(raw_url, fetch_image=True):
    url = normalize_url(raw_url)
    result = {"input_url": raw_url, "url": url, "final_url": url, "status_code": None,
              "fetch_ok": False, "fetch_error": None}
    if not url:
        result["fetch_error"] = "Empty URL."
        return result

    fetched = fetch_html(url)
    result["final_url"] = fetched["final_url"]
    result["status_code"] = fetched["status_code"]
    result["fetch_ok"] = fetched["ok"] and fetched["status_code"] is not None and fetched["status_code"] < 400
    result["fetch_error"] = fetched["error"] if not fetched["ok"] else (
        None if result["fetch_ok"] else f"Page returned HTTP {fetched['status_code']}."
    )

    if not result["fetch_ok"]:
        result.update({"og": {}, "twitter": {}, "title_tag": "", "meta_description": "", "canonical": "",
                       "image_info": {"present": False}, "robots": {"checked": False, "found": False, "blocked": []}})
        result["score"], result["checks"], result["issues"] = 0, {}, [
            {"severity": "error", "message": result["fetch_error"] or "Could not fetch this page.", "fix": "Check the URL is correct and publicly reachable."}
        ]
        return result

    parsed = parse_tags(fetched["html"], fetched["final_url"])
    result.update(parsed)

    image_url = parsed["og"].get("og:image") or parsed["twitter"].get("twitter:image")
    result["image_info"] = check_image(image_url) if fetch_image else {"present": bool(image_url), "url": image_url, "ok": bool(image_url)}
    result["robots"] = check_robots(fetched["final_url"])

    scored = score_result(result)
    result["score"], result["checks"], result["issues"] = scored["score"], scored["checks"], scored["issues"]

    # Build the per-platform preview data
    og, tw = result["og"], result["twitter"]
    prev_title = og.get("og:title") or parsed["title_tag"] or domain_of(result["final_url"])
    prev_desc = og.get("og:description") or parsed["meta_description"] or ""
    prev_image = result["image_info"].get("url") if result["image_info"].get("present") else ""
    prev_domain = domain_of(result["final_url"])

    previews = []
    for p in PLATFORMS:
        if p["key"] == "twitter":
            t_title = tw.get("twitter:title") or prev_title
            t_desc = tw.get("twitter:description") or prev_desc
            t_image = tw.get("twitter:image") or prev_image
            show_image = bool(t_image) and (tw.get("twitter:card") != "summary" or True)
        else:
            t_title, t_desc, t_image = prev_title, prev_desc, prev_image
            show_image = bool(t_image)
        previews.append({
            **p,
            "title": truncate(t_title, p["title_len"]),
            "desc": truncate(t_desc, p["desc_len"]),
            "image": t_image if show_image else "",
            "domain": prev_domain,
        })
    result["previews"] = previews
    return result


def run_bulk(urls):
    # Bulk mode still fetches each og:image (not just the page) so the score
    # matches what a single check would show - only concurrency makes it fast.
    results = [None] * len(urls)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(analyze_url, u, True): i for i, u in enumerate(urls)}
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                results[i] = fut.result()
            except Exception as e:
                results[i] = {"input_url": urls[i], "url": urls[i], "final_url": urls[i], "fetch_ok": False,
                              "fetch_error": f"Unexpected error: {e}", "score": 0, "issues": [], "checks": {}}
    return results


def bulk_summary(results):
    n = len(results)
    ok = sum(1 for r in results if r.get("fetch_ok"))
    avg = round(sum(r.get("score", 0) for r in results) / n) if n else 0
    good = sum(1 for r in results if r.get("score", 0) >= 80)
    warn = sum(1 for r in results if 50 <= r.get("score", 0) < 80)
    bad = sum(1 for r in results if r.get("score", 0) < 50)
    blocked = sum(1 for r in results if r.get("robots", {}).get("blocked"))
    return {"n": n, "ok": ok, "avg": avg, "good": good, "warn": warn, "bad": bad, "blocked": blocked}


# --------------------------------------------------------------------------- #
# Template
# --------------------------------------------------------------------------- #
PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Open Graph Checker</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%237c6af7'/%3E%3Ctext x='50%25' y='50%25' dominant-baseline='central' text-anchor='middle' font-family='Georgia%2C serif' font-size='18' font-weight='900' fill='white'%3EM%3C/text%3E%3C/svg%3E">
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@600;700;800&family=Inter:wght@400;500;600&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
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

  .tabs{display:flex;gap:.5rem;margin-bottom:1.2rem}
  .tab-btn{background:var(--surface);border:1px solid var(--border);color:var(--muted);padding:.55rem 1.1rem;border-radius:8px;font-size:.82rem;font-weight:600;cursor:pointer;font-family:'Inter',sans-serif}
  .tab-btn.active{background:rgba(124,106,247,.14);border-color:rgba(124,106,247,.4);color:var(--accent)}
  .tab-panel{display:none}
  .tab-panel.show{display:block}

  .card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.2rem 1.4rem}
  input[type=text],input[type=url],textarea{width:100%;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:.75rem 1rem;font-family:'DM Mono',monospace;font-size:.82rem}
  textarea{min-height:130px;resize:vertical}
  input:focus,textarea:focus{outline:none;border-color:var(--accent)}
  .btn{display:inline-flex;align-items:center;gap:.5rem;background:var(--accent);color:#fff;border:none;border-radius:8px;padding:.65rem 1.5rem;font-size:.85rem;font-weight:600;cursor:pointer;font-family:'Inter',sans-serif;margin-top:.8rem}
  .btn:hover{background:var(--accent-h)}
  .hint{font-size:.72rem;color:var(--muted);margin-top:.5rem;font-family:'DM Mono',monospace}
  .err{font-size:.78rem;color:var(--red);margin-top:.5rem}
  .row-inline{display:flex;gap:.6rem;flex-wrap:wrap;align-items:flex-start}
  .row-inline input{flex:1;min-width:240px}

  .metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:1rem;margin:1.4rem 0}
  .metric{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1rem 1.2rem}
  .metric .k{font-family:'DM Mono',monospace;font-size:.64rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
  .metric .v{font-family:'Sora',sans-serif;font-size:1.5rem;font-weight:800;margin-top:.2rem}
  .metric .v.purple{color:var(--accent)} .metric .v.mint{color:var(--mint)}
  .metric .v.orange{color:var(--orange)} .metric .v.red{color:var(--red)}

  .charts{display:grid;grid-template-columns:220px 1fr;gap:1rem;margin-bottom:1.4rem}
  .chart-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.1rem 1.2rem}
  .chart-card h3{font-family:'Sora',sans-serif;font-size:.78rem;font-weight:700;margin-bottom:.8rem;color:var(--muted)}
  .ring-wrap{position:relative;width:160px;height:160px;margin:0 auto}
  .ring-center{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
  .ring-center .score{font-family:'Sora',sans-serif;font-size:1.9rem;font-weight:800}
  .ring-center .lbl{font-size:.6rem;font-family:'DM Mono',monospace;color:var(--muted);text-transform:uppercase;letter-spacing:.1em}

  .badges-row{display:flex;flex-wrap:wrap;gap:.4rem;margin-bottom:1.3rem}
  .badge{display:inline-flex;align-items:center;gap:.35rem;font-size:.68rem;font-family:'DM Mono',monospace;padding:.3rem .65rem;border-radius:100px;white-space:nowrap}
  .badge.ok{background:rgba(106,247,200,.1);border:1px solid rgba(106,247,200,.3);color:var(--mint)}
  .badge.warn{background:rgba(247,162,106,.1);border:1px solid rgba(247,162,106,.35);color:var(--orange)}
  .badge.error{background:rgba(247,106,124,.1);border:1px solid rgba(247,106,124,.35);color:var(--red)}

  .sec-title{font-family:'Sora',sans-serif;font-size:1rem;font-weight:700;margin:1.8rem 0 .9rem}

  .issues{display:flex;flex-direction:column;gap:.5rem;margin-bottom:1rem}
  .issue{border-left:3px solid var(--border);background:var(--surface);border-radius:0 10px 10px 0;padding:.7rem 1rem;font-size:.82rem}
  .issue.error{border-color:var(--red)} .issue.warn{border-color:var(--orange)}
  .issue .msg{font-weight:600}
  .issue .fix{color:var(--muted);font-size:.76rem;margin-top:.25rem}
  .all-good{color:var(--mint);font-size:.85rem;padding:.8rem 0}

  .preview-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:1rem;margin-bottom:1.4rem}
  .pcard{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden}
  .pcard .phead{display:flex;align-items:center;gap:.5rem;padding:.6rem .9rem;border-bottom:1px solid var(--border);font-family:'DM Mono',monospace;font-size:.68rem;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
  .pcard .picon{width:20px;height:20px;border-radius:5px;background:var(--surface2);display:flex;align-items:center;justify-content:center;font-size:.62rem;font-weight:700;color:var(--accent)}
  .pcard .pimg{width:100%;background:var(--surface2);display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:.7rem;overflow:hidden}
  .pcard .pimg img{width:100%;height:100%;object-fit:cover;display:block}
  .pcard .pbody{padding:.7rem .9rem}
  .pcard .pdomain{font-size:.66rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:.2rem}
  .pcard .ptitle{font-size:.85rem;font-weight:700;color:var(--text);margin-bottom:.2rem;line-height:1.35}
  .pcard .pdesc{font-size:.76rem;color:var(--muted);line-height:1.4}
  .pcard.google .pbody .ptitle{color:#8ab4f8}
  .pcard.google .pdomain{color:var(--mint)}

  .tbl-wrap{overflow-x:auto;background:var(--surface);border:1px solid var(--border);border-radius:12px;margin-bottom:1.4rem}
  table{width:100%;border-collapse:collapse;font-size:.8rem}
  th{font-family:'DM Mono',monospace;font-size:.6rem;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);text-align:left;padding:.65rem .8rem;border-bottom:1px solid var(--border);white-space:nowrap}
  td{padding:.6rem .8rem;border-bottom:1px solid var(--border);vertical-align:top}
  tr:last-child td{border-bottom:none}
  tr:hover td{background:rgba(124,106,247,.05)}
  td.mono{font-family:'DM Mono',monospace;font-size:.72rem;word-break:break-all}
  td.num{text-align:center;font-family:'DM Mono',monospace}
  .score-pill{font-family:'Sora',sans-serif;font-weight:800;font-size:.85rem;display:inline-block;padding:.15rem .55rem;border-radius:7px}
  .score-pill.good{background:rgba(106,247,200,.12);color:var(--mint)}
  .score-pill.mid{background:rgba(247,162,106,.12);color:var(--orange)}
  .score-pill.bad{background:rgba(247,106,124,.12);color:var(--red)}

  .dl-row{display:flex;gap:.8rem;margin:0 0 1.4rem;flex-wrap:wrap}
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
    <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="brand">
      <span class="brand-mark">M</span>
      <span class="brand-text">SEO Tools</span>
    </a>
    <div class="sidebar-section">
      <div class="sidebar-label">Tool</div>
      <a href="#" class="sidebar-link active">🔗&nbsp; Open Graph Checker</a>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-label">Navigate</div>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="sidebar-link">🧰&nbsp; All My Tools</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio" class="sidebar-link">👩‍💻&nbsp; Portfolio</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/blog/index.html" class="sidebar-link">📝&nbsp; Blog Posts</a>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-label">This tool checks</div>
      <div style="font-size:.72rem;color:var(--muted);line-height:1.6;padding:0 .3rem">
        Open Graph &amp; Twitter Card tags · previews on 8 platforms · image size &amp; crop safety · robots.txt crawler blocks · bulk check up to {{ max_urls }} URLs
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
        <h1>🔗 Open Graph <span>Checker</span></h1>
      </div>
    </div>

    <div class="tabs">
      <button type="button" class="tab-btn {{ 'active' if active_tab != 'bulk' else '' }}" onclick="showTab('single')" id="tab-btn-single">Single URL</button>
      <button type="button" class="tab-btn {{ 'active' if active_tab == 'bulk' else '' }}" onclick="showTab('bulk')" id="tab-btn-bulk">Bulk Check (up to {{ max_urls }})</button>
    </div>

    <div id="panel-single" class="tab-panel {{ 'show' if active_tab != 'bulk' else '' }}">
      <form class="card" method="POST" action="/check" onsubmit="document.getElementById('sp1').classList.add('show')">
        <label style="font-size:.8rem;color:var(--muted);display:block;margin-bottom:.5rem">Page URL</label>
        <div class="row-inline">
          <input type="text" name="url" placeholder="https://example.com/page" value="{{ raw_input or '' }}">
          <button class="btn" type="submit" style="margin-top:0">🔍 Check Tags</button>
        </div>
        <div class="spinner" id="sp1">Fetching the page and its og:image…</div>
        {% if error and active_tab != 'bulk' %}<div class="err">{{ error }}</div>{% endif %}
      </form>

      {% if view %}
        {% if not view.fetch_ok %}
          <div class="card" style="margin-top:1.2rem;border-color:rgba(247,106,124,.4)">
            <div class="err" style="margin-top:0">Couldn't check this URL: {{ view.fetch_error }}</div>
          </div>
        {% else %}

        <div class="charts">
          <div class="chart-card">
            <h3>OG SCORE</h3>
            <div class="ring-wrap">
              <canvas id="ring"></canvas>
              <div class="ring-center">
                <div class="score" style="color:{{ '#6af7c8' if view.score >= 80 else '#f7a26a' if view.score >= 50 else '#f76a7c' }}">{{ view.score }}</div>
                <div class="lbl">out of 100</div>
              </div>
            </div>
          </div>
          <div class="chart-card">
            <h3>WHAT WE CHECKED</h3>
            <div class="badges-row">
              {% for key, val in view.checks.items() %}
                <span class="badge {{ val }}">{{ '✓' if val == 'ok' else ('!' if val == 'warn' else '✕') }} {{ key.replace('_',' ') }}</span>
              {% endfor %}
            </div>
            <div style="font-size:.72rem;color:var(--muted);font-family:'DM Mono',monospace">
              Final URL: <span style="color:var(--text)">{{ view.final_url }}</span> · HTTP {{ view.status_code }}
            </div>
          </div>
        </div>

        <div class="sec-title">🛠 Issues &amp; Fixes</div>
        {% if view.issues %}
        <div class="issues">
          {% for i in view.issues %}
          <div class="issue {{ i.severity }}">
            <div class="msg">{{ i.message }}</div>
            {% if i.fix %}<div class="fix">Fix: {{ i.fix }}</div>{% endif %}
          </div>
          {% endfor %}
        </div>
        {% else %}
        <div class="all-good">✓ No issues found — every check passed.</div>
        {% endif %}

        <div class="sec-title">👀 How it looks when shared — 8 platforms</div>
        <div class="preview-grid">
          {% for p in view.previews %}
          <div class="pcard {{ p.key }}">
            <div class="phead"><span class="picon">{{ p.icon }}</span> {{ p.label }}</div>
            {% if p.key != 'google' %}
              <div class="pimg" style="aspect-ratio:{{ p.ratio }}">
                {% if p.image %}<img src="{{ p.image }}" loading="lazy" alt="">{% else %}No image{% endif %}
              </div>
            {% endif %}
            <div class="pbody">
              <div class="pdomain">{{ p.domain }}</div>
              <div class="ptitle">{{ p.title or '(no title)' }}</div>
              <div class="pdesc">{{ p.desc }}</div>
            </div>
          </div>
          {% endfor %}
        </div>
        <p class="hint">Crop boxes above match each platform's real aspect ratio, so you can see what gets cropped out of og:image before you publish.</p>

        <div class="sec-title">🖼 Image Details</div>
        <div class="tbl-wrap">
          <table>
            <tbody>
              <tr><th>Image URL</th><td class="mono">{{ view.image_info.url or '—' }}</td></tr>
              <tr><th>Loads OK</th><td>{{ '✓ Yes' if view.image_info.ok else '✕ No' }}{% if view.image_info.error %} — {{ view.image_info.error }}{% endif %}</td></tr>
              {% if view.image_info.width %}
              <tr><th>Dimensions</th><td>{{ view.image_info.width }} × {{ view.image_info.height }}px (ratio {{ view.image_info.ratio }}:1)</td></tr>
              {% endif %}
              {% if view.image_info.size_bytes %}
              <tr><th>File Size</th><td>{{ (view.image_info.size_bytes / 1024) | round(1) }} KB</td></tr>
              {% endif %}
              {% if view.image_info.content_type %}
              <tr><th>Content Type</th><td>{{ view.image_info.content_type }}</td></tr>
              {% endif %}
            </tbody>
          </table>
        </div>

        <div class="sec-title">🐦 Twitter Card Cross-Check</div>
        <div class="tbl-wrap">
          <table>
            <thead><tr><th>Tag</th><th>Twitter-specific value</th><th>Falls back to (og:)</th></tr></thead>
            <tbody>
              <tr><td>twitter:card</td><td class="mono">{{ view.twitter.get('twitter:card','—') }}</td><td class="mono">n/a — required for a large image card</td></tr>
              <tr><td>twitter:title</td><td class="mono">{{ view.twitter.get('twitter:title','—') }}</td><td class="mono">{{ view.og.get('og:title','—') }}</td></tr>
              <tr><td>twitter:description</td><td class="mono">{{ view.twitter.get('twitter:description','—') }}</td><td class="mono">{{ view.og.get('og:description','—') }}</td></tr>
              <tr><td>twitter:image</td><td class="mono">{{ view.twitter.get('twitter:image','—') }}</td><td class="mono">{{ view.og.get('og:image','—') }}</td></tr>
            </tbody>
          </table>
        </div>
        <p class="hint">X (Twitter) uses its own twitter:* tags first and only falls back to og:* tags when a twitter:* tag is missing.</p>

        <div class="sec-title">🤖 Crawler / robots.txt Check</div>
        <div class="tbl-wrap">
          <table>
            <thead><tr><th>Bot</th><th>Status</th></tr></thead>
            <tbody>
              {% for agent, label in bot_agents %}
              <tr><td>{{ label }}</td><td>{% if label in view.robots.blocked %}<span class="badge error">✕ blocked in robots.txt</span>{% else %}<span class="badge ok">✓ allowed</span>{% endif %}</td></tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
        {% if not view.robots.checked %}<p class="hint">Couldn't fetch robots.txt for this domain — assuming allowed.</p>{% endif %}

        <div class="sec-title">📋 All Detected Tags</div>
        <div class="tbl-wrap">
          <table>
            <thead><tr><th>Tag</th><th>Value</th></tr></thead>
            <tbody>
              {% for k, v in view.og.items() %}<tr><td class="mono">{{ k }}</td><td class="mono">{{ v }}</td></tr>{% endfor %}
              {% for k, v in view.twitter.items() %}<tr><td class="mono">{{ k }}</td><td class="mono">{{ v }}</td></tr>{% endfor %}
              {% if not view.og and not view.twitter %}<tr><td colspan="2">No og: or twitter: tags found on this page.</td></tr>{% endif %}
            </tbody>
          </table>
        </div>

        <script>
          new Chart(document.getElementById('ring'), {
            type: 'doughnut',
            data: { datasets: [{ data: [{{ view.score }}, {{ 100 - view.score }}],
              backgroundColor: ['{{ "#6af7c8" if view.score >= 80 else "#f7a26a" if view.score >= 50 else "#f76a7c" }}', '#23233a'],
              borderWidth: 0, cutout: '78%' }] },
            options: { plugins: { legend: { display: false }, tooltip: { enabled: false } } }
          });
        </script>
        {% endif %}
      {% endif %}
    </div>

    <div id="panel-bulk" class="tab-panel {{ 'show' if active_tab == 'bulk' else '' }}">
      <form class="card" method="POST" action="/bulk" onsubmit="document.getElementById('sp2').classList.add('show')">
        <label style="font-size:.8rem;color:var(--muted);display:block;margin-bottom:.5rem">Enter URLs (one per line, up to {{ max_urls }})</label>
        <textarea name="urls" placeholder="https://example.com/page-1&#10;https://example.com/page-2&#10;example.com/page-3">{{ bulk_raw or "" }}</textarea>
        <button class="btn" type="submit">🚀 Check All</button>
        <div class="spinner" id="sp2">Checking every URL — this can take a moment for a full batch…</div>
        {% if error and active_tab == 'bulk' %}<div class="err">{{ error }}</div>{% endif %}
      </form>

      {% if bulk_view %}
      <div class="metrics">
        <div class="metric"><div class="k">URLs Checked</div><div class="v purple">{{ bulk_view.summary.n }}</div></div>
        <div class="metric"><div class="k">Fetched OK</div><div class="v mint">{{ bulk_view.summary.ok }}</div></div>
        <div class="metric"><div class="k">Average Score</div><div class="v {{ 'mint' if bulk_view.summary.avg >= 80 else 'orange' if bulk_view.summary.avg >= 50 else 'red' }}">{{ bulk_view.summary.avg }}</div></div>
        <div class="metric"><div class="k">Score ≥ 80</div><div class="v mint">{{ bulk_view.summary.good }}</div></div>
        <div class="metric"><div class="k">Score 50–79</div><div class="v orange">{{ bulk_view.summary.warn }}</div></div>
        <div class="metric"><div class="k">Score &lt; 50</div><div class="v red">{{ bulk_view.summary.bad }}</div></div>
        <div class="metric"><div class="k">Robots-Blocked</div><div class="v red">{{ bulk_view.summary.blocked }}</div></div>
      </div>

      <div class="dl-row">
        <a href="/download/{{ bulk_view.token }}/csv">⬇️ Download CSV</a>
      </div>

      <div class="tbl-wrap">
        <table>
          <thead><tr><th>URL</th><th>Score</th><th>Status</th><th>og:image</th><th>Twitter Card</th><th>Blocked Bots</th><th></th></tr></thead>
          <tbody>
          {% for r in bulk_view.results %}
            <tr>
              <td class="mono">{{ r.input_url }}</td>
              <td><span class="score-pill {{ 'good' if r.score >= 80 else 'mid' if r.score >= 50 else 'bad' }}">{{ r.score }}</span></td>
              <td>{% if r.fetch_ok %}<span class="badge ok">✓ {{ r.status_code }}</span>{% else %}<span class="badge error">✕ {{ r.fetch_error or 'failed' }}</span>{% endif %}</td>
              <td>{% if r.get('image_info',{}).get('present') %}<span class="badge ok">present</span>{% else %}<span class="badge error">missing</span>{% endif %}</td>
              <td class="mono">{{ r.get('twitter',{}).get('twitter:card','—') }}</td>
              <td>{% if r.get('robots',{}).get('blocked') %}<span class="badge error">{{ r.robots.blocked | join(', ') }}</span>{% else %}<span class="badge ok">none</span>{% endif %}</td>
              <td><a href="/?url={{ r.input_url | urlencode }}">Full report →</a></td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% endif %}
    </div>
  </main>
</div>

<script>
  function showTab(name) {
    document.getElementById('panel-single').classList.toggle('show', name === 'single');
    document.getElementById('panel-bulk').classList.toggle('show', name === 'bulk');
    document.getElementById('tab-btn-single').classList.toggle('active', name === 'single');
    document.getElementById('tab-btn-bulk').classList.toggle('active', name === 'bulk');
  }
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/", methods=["GET"])
def home():
    url_param = request.args.get("url", "").strip()
    view = analyze_url(url_param, fetch_image=True) if url_param else None
    return render_template_string(
        PAGE, view=view, bulk_view=None, error=None, raw_input=url_param, bulk_raw="",
        max_urls=MAX_BULK_URLS, active_tab="single", bot_agents=BOT_AGENTS,
    )


@app.route("/check", methods=["POST"])
def check():
    raw = request.form.get("url", "")
    if not raw.strip():
        return render_template_string(
            PAGE, view=None, bulk_view=None, error="Please enter a URL.", raw_input=raw, bulk_raw="",
            max_urls=MAX_BULK_URLS, active_tab="single", bot_agents=BOT_AGENTS,
        )
    view = analyze_url(raw, fetch_image=True)
    return render_template_string(
        PAGE, view=view, bulk_view=None, error=None, raw_input=raw, bulk_raw="",
        max_urls=MAX_BULK_URLS, active_tab="single", bot_agents=BOT_AGENTS,
    )


@app.route("/bulk", methods=["POST"])
def bulk():
    raw = request.form.get("urls", "")
    urls = clean_bulk_urls(raw)[:MAX_BULK_URLS]
    if not urls:
        return render_template_string(
            PAGE, view=None, bulk_view=None, error="Please enter at least one URL.", raw_input="", bulk_raw=raw,
            max_urls=MAX_BULK_URLS, active_tab="bulk", bot_agents=BOT_AGENTS,
        )
    results = run_bulk(urls)
    summary = bulk_summary(results)
    token = uuid.uuid4().hex[:12]
    STORE[token] = results
    while len(STORE) > STORE_LIMIT:
        STORE.pop(next(iter(STORE)))
    bulk_view = {"results": results, "summary": summary, "token": token}
    return render_template_string(
        PAGE, view=None, bulk_view=bulk_view, error=None, raw_input="", bulk_raw=raw,
        max_urls=MAX_BULK_URLS, active_tab="bulk", bot_agents=BOT_AGENTS,
    )


@app.route("/download/<token>/csv")
def download_csv(token):
    results = STORE.get(token)
    if results is None:
        return Response("This report has expired. Please run the bulk check again.", status=404)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["URL", "Final URL", "Score", "Fetch OK", "og:title", "og:description", "og:image",
                     "Image Width", "Image Height", "Twitter Card", "Blocked Bots", "Top Issue"])
    for r in results:
        og = r.get("og", {}) or {}
        img = r.get("image_info", {}) or {}
        top_issue = (r.get("issues") or [{}])[0].get("message", "") if r.get("issues") else ""
        writer.writerow([
            r.get("input_url", ""), r.get("final_url", ""), r.get("score", ""), r.get("fetch_ok", ""),
            og.get("og:title", ""), og.get("og:description", ""), og.get("og:image", ""),
            img.get("width") or "", img.get("height") or "",
            (r.get("twitter", {}) or {}).get("twitter:card", ""),
            "; ".join((r.get("robots", {}) or {}).get("blocked", [])),
            top_issue,
        ])
    mem = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    return send_file(mem, as_attachment=True, download_name="open_graph_check.csv", mimetype="text/csv")


if __name__ == "__main__":
    app.run(debug=True, port=8501)
