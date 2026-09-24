"""
Robots.txt Tester  -  Flask app
Check whether Googlebot, Bingbot and AI crawlers are allowed or blocked
from any URL on a site - and see exactly which robots.txt rule decided it.

  * Fetch a live robots.txt (or paste your own)
  * Bulk test: many URLs x many bots in one grid
  * "Why" explainer: the exact line, group and longest-match reasoning
  * Follows Google's rules (RFC 9309): * and $ wildcards, longest match wins,
    Allow wins a tie, 4xx = allow all, 5xx = block all, 500 KiB limit
  * Health score + critical checks (whole site blocked, CSS/JS blocked,
    sitemap missing/unreachable, unsupported noindex / crawl-delay, typos)
  * AI search readiness: which AI crawlers you allow or block
  * CSV export of every result

Built by Mahalakshmi Marimuthu - Digital Marketing Strategist & AI-Powered SEO Expert
"""
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urljoin, urlparse

import requests
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

REQUEST_TIMEOUT = 12
GOOGLE_LIMIT = 500 * 1024          # Google ignores anything after 500 KiB
READ_LIMIT = 2 * 1024 * 1024
MAX_URLS = 100
MAX_SITEMAP_CHECKS = 5
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
GENERATOR_URL = "https://robots-txt-builder.onrender.com/"

# --------------------------------------------------------------------------- #
# Crawlers
#   tokens     - user-agent tokens the crawler obeys, most specific first
#   no_star    - ignores the "User-agent: *" group (must be named explicitly)
# --------------------------------------------------------------------------- #
BOTS = {
    # search engines
    "googlebot":        {"name": "Googlebot", "tokens": ["googlebot"], "kind": "search", "by": "Google",
                         "note": "Google Search (desktop + smartphone)"},
    "googlebot-image":  {"name": "Googlebot-Image", "tokens": ["googlebot-image", "googlebot"], "kind": "search", "by": "Google",
                         "note": "Google Images - falls back to the Googlebot group"},
    "googlebot-news":   {"name": "Googlebot-News", "tokens": ["googlebot-news", "googlebot"], "kind": "search", "by": "Google",
                         "note": "Google News - falls back to the Googlebot group"},
    "googlebot-video":  {"name": "Googlebot-Video", "tokens": ["googlebot-video", "googlebot"], "kind": "search", "by": "Google",
                         "note": "Google Video - falls back to the Googlebot group"},
    "storebot-google":  {"name": "Storebot-Google", "tokens": ["storebot-google"], "kind": "search", "by": "Google",
                         "note": "Google Shopping"},
    "adsbot-google":    {"name": "AdsBot-Google", "tokens": ["adsbot-google"], "kind": "search", "by": "Google",
                         "note": "Google Ads landing-page checks - ignores the * group", "no_star": True},
    "bingbot":          {"name": "Bingbot", "tokens": ["bingbot"], "kind": "search", "by": "Microsoft",
                         "note": "Bing Search (also feeds Copilot)"},
    "duckduckbot":      {"name": "DuckDuckBot", "tokens": ["duckduckbot"], "kind": "search", "by": "DuckDuckGo",
                         "note": "DuckDuckGo"},
    "yandex":           {"name": "YandexBot", "tokens": ["yandexbot", "yandex"], "kind": "search", "by": "Yandex",
                         "note": "Yandex Search"},
    "applebot":         {"name": "Applebot", "tokens": ["applebot", "googlebot"], "kind": "search", "by": "Apple",
                         "note": "Siri / Spotlight - follows Googlebot rules if Applebot isn't named"},
    # AI crawlers
    "gptbot":           {"name": "GPTBot", "tokens": ["gptbot"], "kind": "ai", "by": "OpenAI",
                         "note": "Collects content to train OpenAI models", "purpose": "Training"},
    "oai-searchbot":    {"name": "OAI-SearchBot", "tokens": ["oai-searchbot"], "kind": "ai", "by": "OpenAI",
                         "note": "Shows your site in ChatGPT search answers", "purpose": "AI search"},
    "chatgpt-user":     {"name": "ChatGPT-User", "tokens": ["chatgpt-user"], "kind": "ai", "by": "OpenAI",
                         "note": "Fetches a page when a ChatGPT user asks", "purpose": "User request"},
    "claudebot":        {"name": "ClaudeBot", "tokens": ["claudebot"], "kind": "ai", "by": "Anthropic",
                         "note": "Collects content to train Claude models", "purpose": "Training"},
    "claude-searchbot": {"name": "Claude-SearchBot", "tokens": ["claude-searchbot"], "kind": "ai", "by": "Anthropic",
                         "note": "Indexes pages for Claude's search answers", "purpose": "AI search"},
    "claude-user":      {"name": "Claude-User", "tokens": ["claude-user"], "kind": "ai", "by": "Anthropic",
                         "note": "Fetches a page when a Claude user asks", "purpose": "User request"},
    "perplexitybot":    {"name": "PerplexityBot", "tokens": ["perplexitybot"], "kind": "ai", "by": "Perplexity",
                         "note": "Indexes pages for Perplexity answers", "purpose": "AI search"},
    "google-extended":  {"name": "Google-Extended", "tokens": ["google-extended"], "kind": "ai", "by": "Google",
                         "note": "Control token: may Gemini use your content? (does not affect Google Search)",
                         "purpose": "Training"},
    "applebot-extended": {"name": "Applebot-Extended", "tokens": ["applebot-extended"], "kind": "ai", "by": "Apple",
                          "note": "Control token: may Apple Intelligence train on your content?", "purpose": "Training"},
    "ccbot":            {"name": "CCBot", "tokens": ["ccbot"], "kind": "ai", "by": "Common Crawl",
                         "note": "Open web dataset used by many AI models", "purpose": "Training"},
    "bytespider":       {"name": "Bytespider", "tokens": ["bytespider"], "kind": "ai", "by": "ByteDance",
                         "note": "Collects content for ByteDance AI models", "purpose": "Training"},
    "meta-externalagent": {"name": "Meta-ExternalAgent", "tokens": ["meta-externalagent"], "kind": "ai", "by": "Meta",
                           "note": "Collects content for Meta AI models", "purpose": "Training"},
    "amazonbot":        {"name": "Amazonbot", "tokens": ["amazonbot"], "kind": "ai", "by": "Amazon",
                         "note": "Alexa answers and Amazon AI", "purpose": "AI search"},
}
DEFAULT_BOTS = ["googlebot", "bingbot", "gptbot", "claudebot", "perplexitybot", "google-extended"]
AI_PANEL = [k for k, b in BOTS.items() if b["kind"] == "ai"]

# Typical CSS / JS / image paths - if Googlebot can't fetch them, it can't render the page
RENDER_SAMPLES = [
    "/wp-content/themes/theme/style.css", "/wp-includes/js/jquery/jquery.min.js",
    "/wp-content/plugins/plugin/script.js", "/assets/css/main.css", "/assets/js/app.js",
    "/static/css/main.css", "/static/js/main.js", "/_next/static/chunks/main.js",
    "/css/style.css", "/js/script.js", "/images/logo.png",
]

# --------------------------------------------------------------------------- #
# Parser  (follows Google's open-source robots.txt parser / RFC 9309)
# --------------------------------------------------------------------------- #
UA_KEYS = {"user-agent": None, "useragent": "useragent", "user agent": "user agent", "user_agent": "user_agent"}
ALLOW_KEYS = {"allow": None}
DISALLOW_KEYS = {"disallow": None, "dissallow": "dissallow", "dissalow": "dissalow", "disalow": "disalow",
                 "diasllow": "diasllow", "disallaw": "disallaw"}
SITEMAP_KEYS = {"sitemap": None, "site-map": "site-map"}
IGNORED_KNOWN = {"crawl-delay", "host", "clean-param", "noindex", "nofollow", "noarchive",
                 "request-rate", "visit-time", "cache-delay", "content-signal"}


def ua_token(value):
    """Google keeps only the leading product token: 'Googlebot/2.1 (+http...)' -> 'googlebot'."""
    value = value.strip()
    if value.startswith("*"):
        return "*"
    m = re.match(r"[A-Za-z_\-]+", value)
    return m.group(0).lower() if m else ""


def parse(text):
    """Return groups, sitemaps, raw lines and lint findings."""
    text = text.lstrip("﻿")
    lines = re.split(r"\r\n|\r|\n", text)
    groups, sitemaps, lint, other = [], [], [], []
    current = None
    last_was_ua = False
    for i, raw in enumerate(lines, start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            no_colon = False
        else:
            parts = line.split(None, 1)
            key, value = parts[0], (parts[1] if len(parts) > 1 else "")
            no_colon = True
        key_l = key.strip().lower()
        value = value.strip()

        kind = None
        typo = None
        if key_l in UA_KEYS:
            kind, typo = "ua", UA_KEYS[key_l]
        elif key_l in ALLOW_KEYS:
            kind = "allow"
        elif key_l in DISALLOW_KEYS:
            kind, typo = "disallow", DISALLOW_KEYS[key_l]
        elif key_l in SITEMAP_KEYS:
            kind, typo = "sitemap", SITEMAP_KEYS[key_l]

        if kind and no_colon:
            lint.append({"line": i, "sev": "warn", "code": "no_colon",
                         "msg": f"Line {i} is missing the colon (“{key.strip()}: …”). Google accepts it, but other crawlers may not."})
        if typo:
            lint.append({"line": i, "sev": "warn", "code": "typo",
                         "msg": f"Line {i}: “{key.strip()}” is a misspelling. Google tolerates it, but Bing and others may ignore the line — write it correctly."})

        if kind == "ua":
            tok = ua_token(value)
            if not tok:
                lint.append({"line": i, "sev": "warn", "code": "empty_ua",
                             "msg": f"Line {i}: User-agent has no valid name, so it is ignored."})
                continue
            if current is None or not last_was_ua:
                current = {"agents": [], "agent_lines": [], "rules": [], "start": i}
                groups.append(current)
            current["agents"].append(tok)
            current["agent_lines"].append(i)
            last_was_ua = True
            continue

        if kind in ("allow", "disallow"):
            last_was_ua = False
            if current is None:
                lint.append({"line": i, "sev": "warn", "code": "orphan",
                             "msg": f"Line {i} (“{raw.strip()[:60]}”) comes before any User-agent line, so every crawler ignores it."})
                continue
            if value and not value.startswith(("/", "*")):
                lint.append({"line": i, "sev": "warn", "code": "no_slash",
                             "msg": f"Line {i}: the path “{value[:60]}” should start with / (or *). Full URLs don't work here."})
            current["rules"].append({"type": kind, "path": value, "line": i, "raw": raw.strip()})
            continue

        if kind == "sitemap":
            sitemaps.append({"url": value, "line": i})
            continue

        # anything else
        if key_l in IGNORED_KNOWN:
            other.append({"key": key_l, "value": value, "line": i})
        else:
            lint.append({"line": i, "sev": "warn", "code": "unknown",
                         "msg": f"Line {i}: “{key.strip()[:40]}” isn't a robots.txt directive, so crawlers ignore it."})
    return {"groups": groups, "sitemaps": sitemaps, "other": other, "lint": lint, "lines": lines}


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
SAFE = "".join(chr(c) for c in range(33, 127) if chr(c) not in "%")


def norm(s):
    """Percent-encode non-ASCII / spaces and uppercase existing escapes so both sides compare alike."""
    s = re.sub(r"%[0-9a-fA-F]{2}", lambda m: m.group(0).upper(), s)
    return quote(s, safe=SAFE + "%")


def pattern_matches(pattern, path):
    pattern = norm(pattern)
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    rx = "".join(".*" if ch == "*" else re.escape(ch) for ch in pattern)
    rx = "^" + rx + ("$" if anchored else "")
    return re.match(rx, path, re.S) is not None


def target_path(url_or_path):
    """Return the path+query Google matches against."""
    s = url_or_path.strip()
    if re.match(r"^https?://", s, re.I):
        p = urlparse(s)
        path = p.path or "/"
        if p.query:
            path += "?" + p.query
        return norm(path)
    if not s.startswith("/"):
        s = "/" + s
    return norm(s)


def pick_groups(parsed, bot_key):
    bot = BOTS[bot_key]
    for tok in bot["tokens"]:
        found = [g for g in parsed["groups"] if tok in g["agents"]]
        if found:
            return found, tok
    if not bot.get("no_star"):
        found = [g for g in parsed["groups"] if "*" in g["agents"]]
        if found:
            return found, "*"
    return [], None


def decide(parsed, bot_key, path):
    groups, tok = pick_groups(parsed, bot_key)
    bot = BOTS[bot_key]
    base = {"group": tok, "group_lines": [l for g in groups for l in g["agent_lines"]]}
    if path.split("?")[0] == "/robots.txt":
        return {**base, "allowed": True, "rule": None,
                "why": "/robots.txt itself is always allowed — crawlers must be able to read it."}
    if not groups:
        why = (f"{bot['name']} ignores the “User-agent: *” group and isn't named anywhere, so nothing blocks it."
               if bot.get("no_star") else
               f"There's no group for {bot['name']} and no “User-agent: *” group, so everything is allowed.")
        return {**base, "allowed": True, "rule": None, "why": why}

    rules = [r for g in groups for r in g["rules"]]
    best = None
    for r in rules:
        if not r["path"]:
            continue       # empty Disallow = allow everything; empty Allow = no-op
        if pattern_matches(r["path"], path):
            length = len(r["path"])
            if (best is None or length > best[1]
                    or (length == best[1] and r["type"] == "allow" and best[0]["type"] == "disallow")):
                best = (r, length)

    who = "User-agent: *" if tok == "*" else f"User-agent: {tok}"
    fallback = ""
    if tok and tok != "*" and tok != bot["tokens"][0]:
        fallback = f" ({bot['name']} isn't named, so it follows the {tok} group.)"
    elif tok == "*":
        fallback = f" ({bot['name']} isn't named, so it follows the * group.)"
    merged = " (Google merges all groups with the same name.)" if len(groups) > 1 else ""

    if best is None:
        empty_dis = [r for r in rules if r["type"] == "disallow" and not r["path"]]
        extra = f" Line {empty_dis[0]['line']} is an empty “Disallow:”, which means allow everything." if empty_dis else ""
        return {**base, "allowed": True, "rule": None,
                "why": f"No rule in the “{who}” group matches this path, so it is allowed by default.{extra}{fallback}{merged}"}

    r, length = best
    allowed = r["type"] == "allow"
    rivals = [x for x in rules if x is not r and x["path"] and x["type"] != r["type"]
              and pattern_matches(x["path"], path)]
    why = f"Line {r['line']} “{r['raw']}” is the most specific rule that matches ({length} character{'s' if length != 1 else ''})"
    if rivals:
        rv = max(rivals, key=lambda x: len(x["path"]))
        if len(rv["path"]) == length:
            why += f". It ties with line {rv['line']} “{rv['raw']}”, and Google lets Allow win a tie"
        else:
            why += f", so it beats line {rv['line']} “{rv['raw']}” ({len(rv['path'])} characters)"
    why += f" → {'ALLOWED' if allowed else 'BLOCKED'}.{fallback}{merged}"
    return {**base, "allowed": allowed, "rule": {"line": r["line"], "raw": r["raw"], "type": r["type"]}, "why": why}


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def normalize_site(raw):
    raw = (raw or "").strip()
    if not raw:
        return ""
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    return raw


def fetch_robots(site_url):
    p = urlparse(site_url)
    if p.scheme not in ("http", "https") or not p.netloc:
        return {"ok": False, "error": "Please enter a valid website URL."}
    robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
    info = {"robots_url": robots_url, "origin": f"{p.scheme}://{p.netloc}"}
    try:
        s = requests.Session()
        s.max_redirects = 5            # Google follows up to 5 redirects
        r = s.get(robots_url, headers={"User-Agent": BROWSER_UA, "Accept": "text/plain,*/*;q=0.8"},
                  timeout=REQUEST_TIMEOUT, allow_redirects=True, stream=True)
        raw = r.raw.read(READ_LIMIT + 1, decode_content=True)
    except requests.exceptions.TooManyRedirects:
        return {**info, "ok": True, "status": None, "state": "unreachable", "text": "",
                "note": "robots.txt redirects more than 5 times. Google gives up and treats it as not found (allow all)."}
    except requests.exceptions.Timeout:
        return {**info, "ok": True, "status": None, "state": "unreachable", "text": "",
                "note": "robots.txt didn't respond in time. If Google sees this, it pauses crawling the site."}
    except requests.exceptions.RequestException:
        return {"ok": False, "error": "Could not connect to this website. Check the address, "
                                      "or use “Paste robots.txt” to test the file directly."}

    status = r.status_code
    info.update({"status": status, "final_url": r.url, "content_type": r.headers.get("Content-Type", ""),
                 "redirects": [h.url for h in r.history], "bytes": len(raw)})
    if 200 <= status < 300:
        enc = r.encoding or "utf-8"
        if enc.lower() in ("iso-8859-1", "ascii"):
            enc = "utf-8"
        info.update({"ok": True, "state": "ok", "text": raw[:READ_LIMIT].decode(enc, errors="replace")})
        return info
    if status == 429 or status >= 500:
        info.update({"ok": True, "state": "error", "text": "",
                     "note": f"robots.txt returned HTTP {status}. Google treats a server error as “block the whole site” "
                             f"until it can read the file again. If the site blocks automated checks, paste the file instead."})
        return info
    info.update({"ok": True, "state": "missing", "text": "",
                 "note": f"robots.txt returned HTTP {status}. Google treats a missing robots.txt as “no restrictions” "
                         f"— every URL is allowed."})
    return info


def check_sitemap(url):
    try:
        r = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=8, allow_redirects=True, stream=True)
        r.close()
        return {"url": url, "status": r.status_code, "ok": r.status_code < 400}
    except requests.exceptions.RequestException:
        return {"url": url, "status": None, "ok": False}


# --------------------------------------------------------------------------- #
# Health checks
# --------------------------------------------------------------------------- #
def health(parsed, text, meta, sitemap_results):
    checks = []
    add = lambda sev, title, detail: checks.append({"sev": sev, "title": title, "detail": detail})
    state = meta.get("state", "ok")

    if state == "missing":
        add("warn", "robots.txt not found", meta.get("note", ""))
    elif state in ("error", "unreachable"):
        add("fail", "robots.txt can't be read", meta.get("note", ""))

    if state == "ok" or meta.get("pasted"):
        size = len(text.encode("utf-8"))
        if size > GOOGLE_LIMIT:
            add("fail", "File is over Google's 500 KiB limit",
                f"The file is {size // 1024} KiB. Google ignores every rule after the first 500 KiB.")
        else:
            add("pass", "File size", f"{size:,} bytes — well within Google's 500 KiB limit.")

        head = text.lstrip("﻿ \t\r\n")[:200].lower()
        if head.startswith("<!doctype") or head.startswith("<html") or "<head" in head:
            add("fail", "Returns a web page, not a robots.txt",
                "The address serves HTML. Crawlers will find no valid rules — make sure /robots.txt returns plain text.")
        ct = (meta.get("content_type") or "").lower()
        if ct and "text/plain" not in ct and not meta.get("pasted"):
            add("warn", "Content-Type isn't text/plain",
                f"The server sends “{ct}”. Serve robots.txt as text/plain so every crawler reads it correctly.")
        if meta.get("final_url") and meta.get("redirects"):
            fh, oh = urlparse(meta["final_url"]).netloc, urlparse(meta["robots_url"]).netloc
            if fh != oh:
                add("warn", "robots.txt redirects to another host",
                    f"It redirects to {fh}. Google uses the redirected file, but it's cleaner to serve it on {oh} directly.")

        if not parsed["groups"]:
            add("warn", "No User-agent groups",
                "The file has no valid User-agent group, so nothing is blocked for anyone.")

        # whole site blocked?
        g_all = decide(parsed, "googlebot", "/")
        b_all = decide(parsed, "bingbot", "/")
        if not g_all["allowed"]:
            add("fail", "Googlebot is blocked from the whole site",
                f"The homepage “/” is blocked for Googlebot ({'line ' + str(g_all['rule']['line']) if g_all['rule'] else ''}). "
                "Your pages can drop out of Google. Remove “Disallow: /” unless this is a staging site.")
        else:
            add("pass", "Homepage is crawlable by Googlebot", "Googlebot can crawl “/”.")
        if not b_all["allowed"] and g_all["allowed"]:
            add("warn", "Bingbot is blocked from the whole site",
                "Bing (and Copilot, DuckDuckGo results that use Bing) can't crawl your site.")

        # rendering resources
        blocked = [p for p in RENDER_SAMPLES if not decide(parsed, "googlebot", norm(p))["allowed"]]
        g_groups, _ = pick_groups(parsed, "googlebot")
        css_js_rules = [r for g in g_groups for r in g["rules"]
                        if r["type"] == "disallow" and re.search(r"\.(css|js)\b|/(css|js|scripts?|styles?)/?$|/wp-includes|/_next",
                                                                 r["path"], re.I)]
        if blocked or css_js_rules:
            ex = blocked[0] if blocked else css_js_rules[0]["raw"]
            add("fail", "CSS / JS / image files may be blocked",
                f"For example “{ex}” is blocked for Googlebot. Google needs these files to render your pages "
                "and judge mobile-friendliness — allow them.")
        else:
            add("pass", "CSS, JS and images are crawlable", "Common style, script and image folders are open to Googlebot.")

        # sitemaps
        sms = parsed["sitemaps"]
        if not sms:
            add("warn", "No Sitemap line",
                "Add “Sitemap: https://yoursite.com/sitemap.xml” so every search engine finds your sitemap.")
        else:
            bad = [s for s in sms if not re.match(r"^https?://", s["url"], re.I)]
            for s in bad:
                add("fail", "Sitemap URL must be a full URL",
                    f"Line {s['line']}: “{s['url']}” — write the complete address starting with https://.")
            dead = [s for s in sitemap_results if not s["ok"]]
            for s in dead:
                add("warn", "Sitemap can't be reached",
                    f"{s['url']} returned {('HTTP ' + str(s['status'])) if s['status'] else 'no response'}.")
            if not bad and not dead:
                add("pass", "Sitemap listed", f"{len(sms)} sitemap{'s' if len(sms) != 1 else ''} found"
                    + (" and reachable." if sitemap_results else "."))

        # unsupported directives
        for o in parsed["other"]:
            if o["key"] in ("noindex", "nofollow", "noarchive"):
                add("fail", f"“{o['key'].capitalize()}:” doesn't work in robots.txt",
                    f"Line {o['line']}: Google stopped supporting this in 2019. Use a meta robots tag "
                    "or X-Robots-Tag header on the page instead.")
            elif o["key"] == "crawl-delay":
                add("info", "Crawl-delay is ignored by Google",
                    f"Line {o['line']}: Bing and Yandex respect it, but Google doesn't.")
            elif o["key"] == "host":
                add("info", "Host: is Yandex-only", f"Line {o['line']}: other search engines ignore it.")

        # duplicate groups
        seen = {}
        for g in parsed["groups"]:
            for a in g["agents"]:
                seen[a] = seen.get(a, 0) + 1
        dups = [a for a, n in seen.items() if n > 1]
        if dups:
            add("info", "Same bot named in more than one group",
                f"{', '.join(dups)} — Google merges them, but one group per bot is easier to manage.")

    for l in parsed["lint"]:
        add(l["sev"], "Syntax", l["msg"])

    score = 100
    for c in checks:
        score -= {"fail": 20, "warn": 7}.get(c["sev"], 0)
    if state in ("error", "unreachable"):
        score = min(score, 30)
    if any(c["title"].startswith("Googlebot is blocked from the whole") for c in checks):
        score = min(score, 20)
    return checks, max(0, min(100, score))


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/api/test", methods=["POST"])
def api_test():
    data = request.get_json(silent=True) or {}
    site = normalize_site(data.get("site", ""))
    pasted = data.get("robots_text")
    bots = [b for b in (data.get("bots") or DEFAULT_BOTS) if b in BOTS][:25] or DEFAULT_BOTS
    raw_urls = [u.strip() for u in (data.get("urls") or []) if u and u.strip()]

    if pasted is not None:
        text = str(pasted)
        meta = {"state": "ok", "pasted": True, "robots_url": (site.rstrip("/") + "/robots.txt") if site else ""}
        if len(text.encode("utf-8")) > READ_LIMIT:
            return jsonify({"ok": False, "error": "That's too much text — robots.txt files are limited to 500 KiB."}), 400
    else:
        if not site:
            return jsonify({"ok": False, "error": "Please enter a website URL, or paste a robots.txt."}), 400
        meta = fetch_robots(site)
        if not meta.get("ok"):
            return jsonify(meta), 400
        text = meta.pop("text", "")

    # Google only reads the first 500 KiB
    enc = text.encode("utf-8")
    used = enc[:GOOGLE_LIMIT].decode("utf-8", errors="ignore") if len(enc) > GOOGLE_LIMIT else text
    parsed = parse(used)

    # URLs to test
    if not raw_urls:
        raw_urls = ["/"]
        if site:
            sp = urlparse(site)
            if sp.path and sp.path != "/":
                raw_urls.append(site)
    truncated = len(raw_urls) > MAX_URLS
    raw_urls = raw_urls[:MAX_URLS]
    site_host = urlparse(site).netloc.lower() if site else ""

    sitemap_results = []
    if not meta.get("pasted") and parsed["sitemaps"]:
        urls = [s["url"] for s in parsed["sitemaps"] if re.match(r"^https?://", s["url"], re.I)][:MAX_SITEMAP_CHECKS]
        with ThreadPoolExecutor(max_workers=5) as ex:
            sitemap_results = list(ex.map(check_sitemap, urls))

    state = meta.get("state", "ok")
    rows = []
    for u in raw_urls:
        path = target_path(u)
        host_note = ""
        if re.match(r"^https?://", u, re.I) and site_host:
            h = urlparse(u).netloc.lower()
            if h and h != site_host:
                host_note = f"Different host ({h}) — this robots.txt only covers {site_host}."
        cells = {}
        for b in bots:
            if state == "missing":
                cells[b] = {"allowed": True, "rule": None, "group": None, "group_lines": [],
                            "why": "robots.txt wasn't found (4xx), so Google treats every URL as allowed."}
            elif state in ("error", "unreachable"):
                cells[b] = {"allowed": False, "rule": None, "group": None, "group_lines": [],
                            "why": "robots.txt couldn't be read (server error / timeout), so Google treats the whole site as blocked."}
            else:
                cells[b] = decide(parsed, b, path)
        rows.append({"input": u, "path": path, "host_note": host_note, "cells": cells})

    ai = []
    for b in AI_PANEL:
        if state == "missing":
            d = {"allowed": True, "group": None}
        elif state in ("error", "unreachable"):
            d = {"allowed": False, "group": None}
        else:
            d = decide(parsed, b, "/")
        named = any(t in g["agents"] for g in parsed["groups"] for t in BOTS[b]["tokens"][:1])
        ai.append({"key": b, "name": BOTS[b]["name"], "by": BOTS[b]["by"], "purpose": BOTS[b].get("purpose", ""),
                   "note": BOTS[b]["note"], "allowed": d["allowed"], "named": named, "group": d.get("group")})

    checks, score = health(parsed, text, meta, sitemap_results)
    rules_count = sum(len(g["rules"]) for g in parsed["groups"])
    return jsonify({
        "ok": True, "meta": meta, "text": text, "score": score, "checks": checks,
        "summary": {"groups": len(parsed["groups"]), "rules": rules_count, "sitemaps": len(parsed["sitemaps"]),
                    "bytes": len(enc), "lines": len(parsed["lines"]),
                    "agents": sorted({a for g in parsed["groups"] for a in g["agents"]})},
        "sitemaps": parsed["sitemaps"], "sitemap_results": sitemap_results,
        "bots": [{"key": b, "name": BOTS[b]["name"], "note": BOTS[b]["note"]} for b in bots],
        "rows": rows, "ai": ai, "truncated": truncated,
    })


@app.route("/api/bots")
def api_bots():
    return jsonify({"bots": [{"key": k, "name": v["name"], "kind": v["kind"], "by": v["by"], "note": v["note"]}
                             for k, v in BOTS.items()], "default": DEFAULT_BOTS})


@app.route("/")
def home():
    return Response(PAGE.replace("__GENERATOR__", GENERATOR_URL), mimetype="text/html")


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Robots.txt Tester – Check If Googlebot &amp; AI Crawlers Can Reach Your URLs</title>
<meta name="description" content="Free Robots.txt Tester: test many URLs against Googlebot, Bingbot and AI crawlers at once, see the exact rule that allows or blocks each one, and fix robots.txt issues before they cost you traffic.">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%237c6af7'/%3E%3Ctext x='50%25' y='50%25' dominant-baseline='central' text-anchor='middle' font-family='Georgia%2C serif' font-size='18' font-weight='900' fill='white'%3EM%3C/text%3E%3C/svg%3E">
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@600;700;800&family=Inter:wght@400;500;600;700&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
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

  .card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.2rem 1.4rem}
  label.lbl{font-size:.8rem;color:var(--muted);display:block;margin-bottom:.5rem}
  input[type=text]{width:100%;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:.75rem 1rem;font-family:'DM Mono',monospace;font-size:.82rem}
  input:focus{outline:none;border-color:var(--accent)}
  .btn{display:inline-flex;align-items:center;gap:.5rem;background:var(--accent);color:#fff;border:none;border-radius:8px;padding:.7rem 1.5rem;font-size:.85rem;font-weight:600;cursor:pointer;font-family:'Inter',sans-serif;white-space:nowrap}
  .btn:hover{background:var(--accent-h)}
  .btn.ghost{background:var(--surface);border:1px solid var(--border);color:var(--text);font-weight:500;padding:.55rem 1.1rem;font-size:.8rem}
  .btn.ghost:hover{border-color:var(--accent)}
  .row-inline{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center}
  .row-inline input{flex:1;min-width:240px}
  details.adv{margin-top:.9rem}
  details.adv summary{cursor:pointer;font-size:.78rem;color:var(--muted);font-family:'DM Mono',monospace}
  details.adv summary:hover{color:var(--text)}
  .adv-body{margin-top:.8rem;display:grid;gap:.7rem}
  .check{display:flex;gap:.5rem;align-items:center;font-size:.8rem;color:var(--muted)}
  .check input{accent-color:var(--accent)}
  .hint{font-size:.72rem;color:var(--muted);margin-top:.5rem;font-family:'DM Mono',monospace}
  .err{font-size:.8rem;color:var(--red);margin-top:.6rem}
  .spinner{display:none;margin-top:.8rem;font-size:.78rem;color:var(--muted);font-family:'DM Mono',monospace}
  .spinner.show{display:block}

  .overview{display:grid;grid-template-columns:220px 1fr 1fr;gap:1rem;margin:1.4rem 0}
  .chart-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.1rem 1.2rem;min-width:0}
  .chart-card h3{font-family:'DM Mono',monospace;font-size:.66rem;letter-spacing:.12em;font-weight:500;margin-bottom:.8rem;color:var(--muted);text-transform:uppercase}
  .ring-wrap{position:relative;width:160px;height:160px;margin:0 auto}
  .ring-center{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
  .ring-center .score{font-family:'Sora',sans-serif;font-size:1.9rem;font-weight:800}
  .ring-center .sub{font-size:.6rem;font-family:'DM Mono',monospace;color:var(--muted);text-transform:uppercase;letter-spacing:.1em}
  .bar-box{position:relative;height:190px}

  .metrics{display:grid;grid-template-columns:1fr 1fr;gap:.7rem}
  .metric{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:.7rem .9rem;min-width:0}
  .metric .k{font-family:'DM Mono',monospace;font-size:.6rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
  .metric .v{font-family:'Sora',sans-serif;font-size:1.05rem;font-weight:700;margin-top:.15rem;overflow-wrap:anywhere}
  .v.mint{color:var(--mint)} .v.orange{color:var(--orange)} .v.red{color:var(--red)} .v.purple{color:var(--accent)}

  .badge{display:inline-flex;align-items:center;gap:.3rem;font-size:.66rem;font-family:'DM Mono',monospace;padding:.22rem .6rem;border-radius:100px;white-space:nowrap;border:1px solid}
  .badge.ok,.badge.yes,.badge.self{background:rgba(106,247,200,.1);border-color:rgba(106,247,200,.3);color:var(--mint)}
  .badge.warn{background:rgba(247,162,106,.1);border-color:rgba(247,162,106,.35);color:var(--orange)}
  .badge.error,.badge.no{background:rgba(247,106,106,.1);border-color:rgba(247,106,106,.35);color:var(--red)}
  .badge.info,.badge.none,.badge.unk{background:rgba(136,136,168,.1);border-color:rgba(136,136,168,.3);color:var(--muted)}

  .sec-title{font-family:'Sora',sans-serif;font-size:1rem;font-weight:700;margin:2rem 0 .9rem;display:flex;align-items:center;gap:.6rem;flex-wrap:wrap}
  .sec-title .count{font-family:'DM Mono',monospace;font-size:.7rem;color:var(--muted);font-weight:400}

  .chips{display:flex;gap:.4rem;flex-wrap:wrap;margin-bottom:.8rem}
  .chip{background:var(--surface);border:1px solid var(--border);color:var(--muted);padding:.35rem .8rem;border-radius:100px;font-size:.72rem;cursor:pointer;font-family:'DM Mono',monospace}
  .chip.active{background:rgba(124,106,247,.14);border-color:rgba(124,106,247,.4);color:var(--accent)}
  .issues{display:flex;flex-direction:column;gap:.5rem}
  .issue{border-left:3px solid var(--border);background:var(--surface);border-radius:0 10px 10px 0;padding:.7rem 1rem;font-size:.82rem}
  .issue.error{border-color:var(--red)} .issue.warn{border-color:var(--orange)} .issue.info{border-color:var(--muted)}
  .issue .top{display:flex;gap:.5rem;align-items:flex-start}
  .issue .msg{font-weight:600;overflow-wrap:anywhere}
  .issue .fix{color:var(--muted);font-size:.76rem;margin-top:.3rem;overflow-wrap:anywhere}
  .issue .fix b{color:var(--mint);font-weight:600}
  .area-tag{font-family:'DM Mono',monospace;font-size:.6rem;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;margin-left:auto;white-space:nowrap}
  .all-good{color:var(--mint);font-size:.85rem;padding:.8rem 0}

  .tbl-tools{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center;margin-bottom:.7rem}
  .tbl-tools input{max-width:280px;padding:.5rem .8rem}
  .tbl-wrap{overflow-x:auto;background:var(--surface);border:1px solid var(--border);border-radius:12px}
  table{width:100%;border-collapse:collapse;font-size:.8rem}
  th{font-family:'DM Mono',monospace;font-size:.6rem;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);text-align:left;padding:.65rem .8rem;border-bottom:1px solid var(--border);white-space:nowrap}
  td{padding:.6rem .8rem;border-bottom:1px solid var(--border);vertical-align:top}
  tr:last-child td{border-bottom:none}
  tbody tr:hover td{background:rgba(124,106,247,.05)}
  td.mono{font-family:'DM Mono',monospace;font-size:.72rem;word-break:break-all}
  td .small{font-size:.7rem;color:var(--muted);margin-top:.2rem}
  .code-pill{font-family:'DM Mono',monospace;font-weight:500;font-size:.78rem;color:var(--text)}

  .matrix td,.matrix th{text-align:center;padding:.45rem .5rem}
  .matrix th.rowh,.matrix td.rowh{text-align:left}
  .cell{display:inline-flex;width:26px;height:26px;border-radius:6px;align-items:center;justify-content:center;font-size:.75rem;font-weight:700}
  .cell.yes,.cell.self-ok{background:rgba(106,247,200,.14);color:var(--mint)}
  .cell.no,.cell.self-miss{background:rgba(247,106,106,.14);color:var(--red)}
  .cell.unk{background:rgba(136,136,168,.12);color:var(--muted)}
  .legend{display:flex;gap:1rem;flex-wrap:wrap;font-size:.72rem;color:var(--muted);margin-top:.6rem}
  .legend span{display:inline-flex;align-items:center;gap:.35rem}

  pre.code{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:1rem;font-family:'DM Mono',monospace;font-size:.74rem;color:var(--mint);overflow-x:auto;white-space:pre}
  .dl-row{display:flex;gap:.6rem;flex-wrap:wrap;margin-top:.8rem}

  @media(max-width:1100px){.overview{grid-template-columns:220px 1fr}.overview .bars{grid-column:1/-1}}
  @media(max-width:960px){.overview{grid-template-columns:1fr}.sidebar{display:none}.main{margin-left:0;padding:1.2rem 1rem}}

  textarea,select{width:100%;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:.7rem 1rem;font-family:'DM Mono',monospace;font-size:.8rem;resize:vertical}
  select{cursor:pointer}
  textarea:focus,select:focus{outline:none;border-color:var(--accent)}
  .btn:disabled{opacity:.5;cursor:wait}
  pre.code{color:var(--text);max-height:420px;overflow:auto}
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
      <a href="#" class="sidebar-link active">🤖&nbsp; Robots.txt Tester</a>
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
      <div class="crumb">// seo tool · free</div>
      <h1>🤖 Robots.txt <span>Tester</span></h1>
    </div>

    <form class="card" id="form">
      <label class="lbl" for="site">Website URL</label>
      <div class="row-inline">
        <input type="text" id="site" placeholder="https://www.yourwebsite.com">
        <button class="btn" type="submit" id="runBtn">🔍 Test robots.txt</button>
      </div>
      <details class="adv">
        <summary>+ Advanced options</summary>
        <div class="adv-body">
          <div>
            <label class="lbl" for="urls">URLs or paths to test (one per line) — leave empty to test the homepage</label>
            <textarea id="urls" rows="4" placeholder="/blog/my-post&#10;/search?q=test"></textarea>
          </div>
          <div>
            <label class="lbl" for="botset">Crawlers to test</label>
            <select id="botset">
              <option value="default">Recommended — Googlebot, Bingbot, GPTBot, ClaudeBot, PerplexityBot, Google-Extended</option>
              <option value="google">All Google crawlers</option>
              <option value="search">All search engines</option>
              <option value="ai">All AI crawlers</option>
              <option value="all">Everything (23 crawlers)</option>
            </select>
          </div>
          <div>
            <label class="lbl" for="paste">Paste a robots.txt instead of fetching it (optional — handy for drafts or sites that block checks)</label>
            <textarea id="paste" rows="5" placeholder="User-agent: *&#10;Disallow: /admin/"></textarea>
          </div>
        </div>
      </details>
      <div class="spinner" id="sp">Reading robots.txt and testing your URLs…</div>
      <div class="err" id="err"></div>
    </form>

    <div id="results" style="display:none">
      <div class="overview">
        <div class="chart-card">
          <h3>Health score</h3>
          <div class="ring-wrap" id="ring"></div>
          <div style="display:flex;justify-content:center;gap:.4rem;margin-top:.8rem;flex-wrap:wrap" id="ringBadges"></div>
        </div>
        <div class="chart-card">
          <h3>At a glance</h3>
          <div class="metrics" id="metrics"></div>
          <div class="hint" style="overflow-wrap:anywhere" id="checked"></div>
        </div>
        <div class="chart-card bars">
          <h3>Allowed vs blocked</h3>
          <div class="bar-box"><canvas id="chart"></canvas></div>
        </div>
      </div>

      <div class="sec-title">🛠 Issues &amp; Fixes <span class="count" id="issueCount"></span></div>
      <div class="issues" id="issues"></div>

      <div class="sec-title">🔎 URL Test Results <span class="count" id="resCount"></span></div>
      <div class="tbl-tools">
        <input type="text" id="filter" placeholder="Filter by URL or crawler…">
        <label class="check"><input type="checkbox" id="blockedOnly"> Show blocked only</label>
        <button type="button" class="btn ghost" id="csvBtn" style="margin-left:auto">⬇ Download CSV</button>
      </div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>URL / path</th><th>Crawler</th><th>Result</th><th>Why</th></tr></thead>
          <tbody id="resBody"></tbody>
        </table>
      </div>

      <div class="sec-title">🧠 AI Crawlers <span class="count">can they crawl your homepage?</span></div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>Crawler</th><th>Company</th><th>Used for</th><th>Status</th></tr></thead>
          <tbody id="aiBody"></tbody>
        </table>
      </div>

      <div class="sec-title">📄 Your robots.txt</div>
      <pre class="code" id="code"></pre>
      <div class="dl-row">
        <a class="btn ghost" href="__GENERATOR__" target="_blank">🛠 Fix it in my Robots.txt Generator</a>
      </div>
    </div>
  </main>
</div>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const SETS = {
  default: ['googlebot','bingbot','gptbot','claudebot','perplexitybot','google-extended'],
  google: ['googlebot','googlebot-image','googlebot-news','googlebot-video','storebot-google','adsbot-google','google-extended'],
};
let BOTS = [], DATA = null, chart = null;
fetch('/api/bots').then(r => r.json()).then(j => { BOTS = j.bots; });
function botList() {
  const v = $('botset').value;
  if (SETS[v]) return SETS[v];
  if (v === 'all') return BOTS.map(b => b.key);
  return BOTS.filter(b => b.kind === v).map(b => b.key);
}

$('form').addEventListener('submit', async e => {
  e.preventDefault();
  const site = $('site').value.trim(), paste = $('paste').value;
  $('err').textContent = '';
  if (!site && !paste.trim()) { $('err').textContent = 'Please enter a website URL.'; return; }
  const body = { site, urls: $('urls').value.split('\n').map(s => s.trim()).filter(Boolean), bots: botList() };
  if (paste.trim()) body.robots_text = paste;
  $('sp').classList.add('show'); $('runBtn').disabled = true;
  try {
    const r = await fetch('/api/test', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'Something went wrong.');
    DATA = j; render();
    $('results').style.display = '';
  } catch (err) {
    $('err').textContent = err.message || 'Could not reach the server — please try again.';
  } finally {
    $('sp').classList.remove('show'); $('runBtn').disabled = false;
  }
});

function render() {
  const d = DATA, m = d.meta, s = d.summary;
  // ring
  const sc = d.score, col = sc >= 80 ? '#6af7c8' : sc >= 50 ? '#f7a26a' : '#f76a6a';
  $('ring').innerHTML = `<svg viewBox="0 0 160 160" width="160" height="160">
      <circle cx="80" cy="80" r="68" fill="none" stroke="#23233a" stroke-width="12"/>
      <circle cx="80" cy="80" r="68" fill="none" stroke="${col}" stroke-width="12" stroke-linecap="${sc > 0 ? 'round' : 'butt'}"
        stroke-dasharray="${(427.26 * sc / 100).toFixed(2)} 427.26" transform="rotate(-90 80 80)"/></svg>
    <div class="ring-center"><div class="score" style="color:${col}">${sc}</div><div class="sub">out of 100</div></div>`;
  const nErr = d.checks.filter(c => c.sev === 'fail').length, nWarn = d.checks.filter(c => c.sev === 'warn').length;
  $('ringBadges').innerHTML = `<span class="badge error">${nErr} errors</span><span class="badge warn">${nWarn} warnings</span>`;

  // metrics
  const status = m.pasted ? 'Pasted' : (m.status ? 'HTTP ' + m.status : 'No reply');
  const stCls = m.pasted || (m.status >= 200 && m.status < 300) ? 'mint' : m.state === 'missing' ? 'orange' : 'red';
  const home = d.rows.find(r => r.path === '/');
  const gHome = home && home.cells.googlebot ? home.cells.googlebot.allowed : !d.checks.some(c => c.title.startsWith('Googlebot is blocked'));
  const aiBlocked = d.ai.filter(a => !a.allowed).length;
  const kb = s.bytes >= 1024 ? (s.bytes / 1024).toFixed(1) + ' KB' : s.bytes + ' bytes';
  $('metrics').innerHTML = [
    ['robots.txt', status, stCls],
    ['Googlebot on homepage', gHome ? 'Allowed' : 'Blocked', gHome ? 'mint' : 'red'],
    ['Rules', `${s.rules} in ${s.groups} group${s.groups === 1 ? '' : 's'}`, 'purple'],
    ['Sitemaps', s.sitemaps || 'None', s.sitemaps ? 'mint' : 'orange'],
    ['File size', kb, ''],
    ['AI crawlers blocked', `${aiBlocked} of ${d.ai.length}`, 'purple'],
  ].map(([k, v, c]) => `<div class="metric"><div class="k">${k}</div><div class="v ${c}">${esc(v)}</div></div>`).join('');
  $('checked').innerHTML = m.pasted ? 'Checked: <span style="color:var(--text)">your pasted robots.txt</span>'
    : `Checked: <span style="color:var(--text)">${esc(m.robots_url)}</span>`;

  // chart
  let a = 0, b = 0;
  d.rows.forEach(r => Object.values(r.cells).forEach(c => c.allowed ? a++ : b++));
  if (typeof Chart !== 'undefined') {
    const data = { labels: ['Allowed', 'Blocked'], datasets: [{ data: [a, b], backgroundColor: ['#6af7c8', '#f76a6a'], borderColor: '#12121a', borderWidth: 3 }] };
    if (chart) { chart.data = data; chart.update(); }
    else chart = new Chart($('chart'), { type: 'doughnut', data, options: { cutout: '65%', maintainAspectRatio: false,
      plugins: { legend: { position: 'bottom', labels: { color: '#8888a8', font: { family: 'DM Mono', size: 11 }, boxWidth: 10 } } } } });
  }

  // issues
  const sevMap = { fail: 'error', warn: 'warn', info: 'info' };
  const label = { error: '✕ error', warn: '! warning', info: 'i info' };
  const order = { fail: 0, warn: 1, info: 2 };
  const issues = d.checks.filter(c => c.sev !== 'pass').sort((x, y) => order[x.sev] - order[y.sev]);
  const passed = d.checks.length - issues.length;
  $('issueCount').textContent = `${issues.length} found · ${passed} passed`;
  $('issues').innerHTML = issues.length ? issues.map(c => `<div class="issue ${sevMap[c.sev]}"><div class="top">
      <span class="badge ${sevMap[c.sev]}">${label[sevMap[c.sev]]}</span><div class="msg">${esc(c.title)}</div></div>
      <div class="fix">${esc(c.detail)}</div></div>`).join('')
    : '<div class="all-good">✓ No issues found — your robots.txt looks clean.</div>';

  renderResults();

  // AI table
  $('aiBody').innerHTML = d.ai.map(x => `<tr><td><div class="code-pill">${esc(x.name)}</div><div class="small">${esc(x.note)}</div></td>
      <td>${esc(x.by)}</td><td>${esc(x.purpose)}</td>
      <td><span class="badge ${x.allowed ? 'ok' : 'error'}">${x.allowed ? '✓ allowed' : '✕ blocked'}</span></td></tr>`).join('');

  // code
  const lines = (d.text || '').split(/\r\n|\r|\n/);
  $('code').innerHTML = d.text && d.text.trim()
    ? lines.map((l, i) => `<span style="color:#55557a;user-select:none">${String(i + 1).padStart(3, ' ')}  </span>${esc(l)}`).join('\n')
    : '<span style="color:var(--muted)">No robots.txt content.</span>';
}

function flatRows() {
  const out = [];
  DATA.rows.forEach(r => DATA.bots.forEach(b => out.push({ r, b, c: r.cells[b.key] })));
  return out;
}
function renderResults() {
  const q = $('filter').value.trim().toLowerCase(), only = $('blockedOnly').checked;
  const all = flatRows();
  const rows = all.filter(x => (!only || !x.c.allowed) && (!q || (x.r.input + ' ' + x.b.name).toLowerCase().includes(q)));
  $('resCount').textContent = `${DATA.rows.length} URL${DATA.rows.length === 1 ? '' : 's'} × ${DATA.bots.length} crawler${DATA.bots.length === 1 ? '' : 's'}`;
  $('resBody').innerHTML = rows.length ? rows.map(({ r, b, c }) => `<tr>
      <td class="mono">${esc(r.input)}${r.host_note ? `<div class="small" style="color:var(--orange)">${esc(r.host_note)}</div>` : ''}</td>
      <td><div class="code-pill">${esc(b.name)}</div></td>
      <td><span class="badge ${c.allowed ? 'ok' : 'error'}">${c.allowed ? '✓ allowed' : '✕ blocked'}</span></td>
      <td>${c.rule ? `<div class="code-pill">Line ${c.rule.line}: ${esc(c.rule.raw)}</div>` : ''}<div class="small">${esc(c.why)}</div></td></tr>`).join('')
    : '<tr><td colspan="4" style="color:var(--muted)">No results match this filter.</td></tr>';
}
$('filter').addEventListener('input', renderResults);
$('blockedOnly').addEventListener('change', renderResults);

$('csvBtn').addEventListener('click', () => {
  if (!DATA) return;
  const q = v => '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"';
  const out = [['URL', 'Crawler', 'Result', 'Matching line', 'Matching rule', 'Why'].map(q).join(',')];
  flatRows().forEach(({ r, b, c }) => out.push([r.input, b.name, c.allowed ? 'Allowed' : 'Blocked',
    c.rule ? c.rule.line : '', c.rule ? c.rule.raw : '', c.why].map(q).join(',')));
  const blob = new Blob(['﻿' + out.join('\r\n')], { type: 'text/csv;charset=utf-8' });
  let host = 'robots';
  try { host = new URL(DATA.meta.robots_url).hostname.replace(/^www\./, ''); } catch (e) {}
  const link = document.createElement('a'); link.href = URL.createObjectURL(blob);
  link.download = `robots-txt-test-${host}.csv`; link.click(); setTimeout(() => URL.revokeObjectURL(link.href), 1000);
});
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(debug=True, port=5000)
