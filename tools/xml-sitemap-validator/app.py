"""
XML Sitemap Validator + URL Status Bulk Check — Flask dashboard
Built by Mahalakshmi Marimuthu · Digital Marketing Strategist & AI-Powered SEO Expert

1. Fetches a sitemap (or sitemap index, .xml or .xml.gz), or takes pasted XML / a
   plain URL list.
2. Validates it against the sitemaps.org protocol + Google's sitemap rules
   (namespace, 50,000 URL / 50 MB limits, <loc> format, host, duplicates,
   <lastmod> quality, robots.txt declaration and blocking).
3. Bulk-checks each URL (in small batches from the browser, so Render's request
   timeout is never hit): status code, redirect chain, noindex (meta + header)
   and canonical — i.e. "does this URL actually belong in a sitemap?"
"""

import gzip
import io
import ipaddress
import os
import random
import re
import socket
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from defusedxml import DefusedXmlException
from defusedxml import ElementTree as DET
from flask import Flask, jsonify, render_template_string, request
from xml.etree.ElementTree import ParseError

app = Flask(__name__)
app.json.sort_keys = False  # keep chart bucket order

# ---------------------------------------------------------------------------
# Config / limits (sized for Render's free tier)
# ---------------------------------------------------------------------------
SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
MAX_SITEMAP_BYTES = 50 * 1024 * 1024       # Google's uncompressed limit
DOWNLOAD_CAP = 60 * 1024 * 1024            # hard stop while downloading
MAX_URLS_PER_FILE = 50000
MAX_CHILD_SITEMAPS = 25                    # child sitemaps analysed from an index
CHILD_TIME_BUDGET = 70                     # seconds spent fetching children
MAX_ENTRIES_ANALYSED = 200000              # URLs validated in one run
MAX_URLS_PER_CHILD_RETURNED = 1000         # URLs sent to the browser per child
MAX_URLS_RETURNED = 5000                   # URLs sent to the browser in total
MAX_STATUS_CHECKS = 500                    # URL status checks per run
MAX_BATCH = 50                             # URLs per /api/check request
FETCH_TIMEOUT = 20
CHECK_TIMEOUT = 10
HTML_READ_LIMIT = 400_000                  # bytes of HTML read per page (head is enough)
WORKERS = 12
DOMAIN_CONCURRENCY = 6

# Local testing only: set ALLOW_PRIVATE_HOSTS=1 to allow 127.0.0.1 etc.
ALLOW_PRIVATE = os.environ.get("ALLOW_PRIVATE_HOSTS") == "1"

# A normal browser UA — self-identifying as a bot gets blocked by most WAFs.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

UNRELIABLE_DOMAINS = {
    "facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com",
    "threads.net", "tiktok.com",
    "bit.ly", "tinyurl.com", "t.co", "goo.gl", "rebrand.ly", "ow.ly", "bnkbzr.co",
}

AI_CRAWLERS = ["GPTBot", "OAI-SearchBot", "ClaudeBot", "PerplexityBot", "Google-Extended", "CCBot"]

VALID_CHANGEFREQ = {"always", "hourly", "daily", "weekly", "monthly", "yearly", "never"}
W3C_DATE_RE = re.compile(
    r"^\d{4}(-\d{2}(-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2}))?)?)?$"
)


class FetchError(Exception):
    pass


# ---------------------------------------------------------------------------
# Check registry: code -> (severity, label when passing, problem title, how to fix)
# ---------------------------------------------------------------------------
CHECKS = {
    # Fetch / file level
    "fetch_status": ("critical", "Sitemap URL returns 200 OK", "Sitemap URL doesn't return 200 OK",
                     "Google can only read a sitemap that loads with a 200 status. Fix the URL or the server response."),
    "sitemap_redirect": ("warning", "Sitemap URL doesn't redirect", "Sitemap URL redirects",
                         "Submit the final sitemap URL in Search Console and robots.txt, not one that redirects."),
    "not_xml_html": ("critical", "Returns XML (not an HTML page)", "Returns an HTML page instead of XML",
                     "The URL serves a web page (often a 404 or login page). Point to the real sitemap file."),
    "content_type": ("warning", "Served with an XML Content-Type", "Served with a non-XML Content-Type",
                     "Serve the file as application/xml or text/xml (or application/gzip for .gz)."),
    "leading_content": ("warning", "Nothing before the XML declaration", "Text or spaces before <?xml ...?>",
                        "Remove the blank lines/spaces before the XML declaration — strict parsers reject the file."),
    "xml_malformed": ("critical", "XML is well-formed", "XML isn't well-formed",
                      "Fix the syntax error shown (unclosed tag, unescaped &, bad character). Google can't read a broken file."),
    "bad_root": ("critical", "Root is <urlset> or <sitemapindex>", "Root element isn't <urlset> or <sitemapindex>",
                 "A sitemap must start with <urlset> (URLs) or <sitemapindex> (list of sitemaps)."),
    "bad_namespace": ("critical", "Correct sitemap namespace", "Missing or wrong sitemap namespace",
                      'Use xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" on the root element.'),
    "empty": ("critical", "Sitemap contains URLs", "Sitemap contains no URLs",
              "An empty sitemap does nothing. Add your indexable URLs or remove it from Search Console/robots.txt."),
    "too_many_urls": ("critical", "Under 50,000 URLs per file", "More than 50,000 URLs in one file",
                      "Split it into several sitemaps and list them in a sitemap index."),
    "too_large": ("critical", "Under 50 MB uncompressed", "File is larger than 50 MB uncompressed",
                  "Split it into smaller sitemaps and list them in a sitemap index."),
    # Index level
    "nested_index": ("critical", "No nested sitemap indexes", "Sitemap index points to another sitemap index",
                     "A sitemap index can only list sitemaps of URLs, not other indexes. Flatten it."),
    "child_failed": ("critical", "All child sitemaps load and parse", "Child sitemaps that failed to load or parse",
                     "Fix or remove the broken child sitemaps listed in the index."),
    "children_capped": ("info", "All child sitemaps analysed", "Only part of the index was analysed",
                        "Free-tier limit: the first child sitemaps were analysed. Paste a single child sitemap URL to check the rest."),
    # URL entries
    "missing_loc": ("critical", "Every entry has a <loc>", "Entries with no <loc>",
                    "Every <url>/<sitemap> entry needs one <loc> with the full URL."),
    "invalid_loc": ("critical", "Every <loc> is a full http(s) URL", "<loc> values that aren't full http(s) URLs",
                    "Use absolute URLs including https:// and the domain — relative paths are not allowed."),
    "loc_whitespace": ("warning", "No spaces inside URLs", "Spaces inside URLs",
                       "Remove or percent-encode (%20) spaces inside the URL."),
    "loc_too_long": ("warning", "URLs under 2,048 characters", "URLs longer than 2,048 characters",
                     "The protocol allows at most 2,048 characters per URL. Shorten or drop these."),
    "loc_fragment": ("warning", "No #fragments in URLs", "URLs containing #fragments",
                     "Search engines ignore everything after #. List the clean URL only once."),
    "cross_host": ("warning", "URLs are on the sitemap's own host", "URLs on a different host than the sitemap",
                   "A sitemap should only list URLs on its own host (incl. www vs non-www) unless cross-submission is verified in Search Console."),
    "mixed_protocol": ("warning", "One protocol (http/https) used", "Mix of http:// and https:// URLs",
                       "List only the canonical protocol version (normally https://)."),
    "duplicates": ("warning", "No duplicate URLs", "Duplicate URLs",
                   "Each URL should appear only once across your sitemaps."),
    "blocked_robots": ("warning", "No URLs blocked by robots.txt", "URLs blocked by robots.txt for Googlebot",
                       "Listing a URL you also block sends mixed signals. Unblock it or remove it from the sitemap."),
    # Lastmod & optional tags
    "lastmod_invalid": ("warning", "<lastmod> dates use W3C format", "Invalid <lastmod> date format",
                        "Use W3C Datetime, e.g. 2026-09-24 or 2026-09-24T10:30:00+05:30."),
    "lastmod_future": ("warning", "No <lastmod> dates in the future", "<lastmod> dates in the future",
                       "Future dates look auto-generated; Google may stop trusting your lastmod values."),
    "lastmod_same": ("warning", "<lastmod> values look genuine", "Every URL has the same <lastmod>",
                     "Identical dates suggest the build time, not the real content update. Google only uses lastmod when it's consistently accurate."),
    "lastmod_missing": ("info", "Every URL has a <lastmod>", "URLs without <lastmod>",
                        "Optional, but an accurate lastmod helps Google recrawl updated pages faster."),
    "optional_invalid": ("warning", "<priority>/<changefreq> values are valid", "Invalid <priority> or <changefreq> values",
                         "priority must be 0.0–1.0 and changefreq one of always/hourly/daily/weekly/monthly/yearly/never."),
    "priority_changefreq": ("info", "No ignored tags", "<priority> / <changefreq> are ignored by Google",
                            "Harmless, but Google ignores these. Spend the effort on accurate <lastmod> instead."),
    # robots.txt
    "robots_missing": ("info", "robots.txt found", "No robots.txt found",
                       "Add a robots.txt with a 'Sitemap:' line so every crawler can find your sitemap."),
    "robots_not_declared": ("warning", "Sitemap declared in robots.txt", "Sitemap not declared in robots.txt",
                            "Add a line like 'Sitemap: https://example.com/sitemap.xml' to robots.txt."),
}

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2, "pass": 3}

FILE_CHECKS = ["fetch_status", "sitemap_redirect", "not_xml_html", "content_type", "leading_content",
               "xml_malformed", "bad_root", "bad_namespace", "empty", "too_many_urls", "too_large"]
INDEX_CHECKS = ["nested_index", "child_failed", "children_capped"]
URL_CHECKS = ["missing_loc", "invalid_loc", "loc_whitespace", "loc_too_long", "loc_fragment",
              "cross_host", "mixed_protocol", "duplicates"]
LASTMOD_CHECKS = ["lastmod_invalid", "lastmod_future", "lastmod_same", "lastmod_missing",
                  "optional_invalid", "priority_changefreq"]
ROBOTS_CHECKS = ["robots_missing", "robots_not_declared", "blocked_robots"]


class Issues:
    def __init__(self):
        self.items = {}

    def add(self, code, example=None, detail=None, n=1):
        it = self.items.setdefault(code, {"count": 0, "examples": [], "detail": None})
        it["count"] += n
        if example and len(it["examples"]) < 5 and example not in it["examples"]:
            it["examples"].append(example)
        if detail and not it["detail"]:
            it["detail"] = detail

    def has(self, code):
        return code in self.items

    def report(self, applicable):
        codes = list(dict.fromkeys(list(applicable) + list(self.items.keys())))
        out = []
        for code in codes:
            sev, label, problem, fix = CHECKS[code]
            it = self.items.get(code)
            out.append({
                "code": code,
                "severity": sev if it else "pass",
                "title": problem if it else label,
                "fix": fix if it else "",
                "count": it["count"] if it else 0,
                "examples": it["examples"] if it else [],
                "detail": it["detail"] if it else None,
            })
        out.sort(key=lambda c: SEVERITY_ORDER[c["severity"]])
        return out


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------
_domain_semaphores = {}
_domain_semaphores_lock = threading.Lock()


def _get_domain_semaphore(url):
    netloc = urlparse(url).netloc.lower()
    with _domain_semaphores_lock:
        sem = _domain_semaphores.get(netloc)
        if sem is None:
            sem = threading.Semaphore(DOMAIN_CONCURRENCY)
            _domain_semaphores[netloc] = sem
        return sem


def is_public_host(url):
    """Basic SSRF guard: refuse URLs that resolve to private/loopback addresses."""
    if ALLOW_PRIVATE:
        return True
    host = urlparse(url).hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True  # let the request fail naturally as "Connection failed"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def is_unreliable_domain(url):
    netloc = urlparse(url).netloc.lower()
    netloc = netloc[4:] if netloc.startswith("www.") else netloc
    return any(netloc == d or netloc.endswith("." + d) for d in UNRELIABLE_DOMAINS)


def ensure_scheme(url):
    url = url.strip()
    if url and not re.match(r"^https?://", url, re.I):
        url = "https://" + url.lstrip("/")
    return url


def norm_url(u):
    """Normalise for comparison: lowercase scheme/host, drop default port + fragment."""
    p = urlparse(u.strip())
    scheme = p.scheme.lower()
    netloc = p.netloc.lower()
    if (scheme == "http" and netloc.endswith(":80")) or (scheme == "https" and netloc.endswith(":443")):
        netloc = netloc.rsplit(":", 1)[0]
    return urlunparse((scheme, netloc, p.path or "/", "", p.query, ""))


def fetch_raw(url):
    """Download a sitemap with a size cap, transparently un-gzipping .gz files."""
    if not is_public_host(url):
        raise FetchError("That address points to a private network and can't be checked.")
    try:
        resp = requests.get(url, headers=HEADERS, timeout=FETCH_TIMEOUT, allow_redirects=True, stream=True)
        if resp.status_code in (403, 429, 503):
            resp.close()
            time.sleep(1.2 + random.random())
            resp = requests.get(url, headers=HEADERS, timeout=FETCH_TIMEOUT, allow_redirects=True, stream=True)
        chunks, total = [], 0
        for chunk in resp.iter_content(65536):
            total += len(chunk)
            if total > DOWNLOAD_CAP:
                resp.close()
                raise FetchError("The file is larger than 60 MB — too big to analyse.")
            chunks.append(chunk)
        resp.close()
    except requests.exceptions.Timeout:
        raise FetchError("Timed out while downloading the sitemap.")
    except requests.exceptions.SSLError:
        raise FetchError("SSL certificate error on this URL.")
    except requests.exceptions.ConnectionError:
        raise FetchError("Couldn't connect to this site.")
    except requests.exceptions.RequestException as exc:
        raise FetchError(f"Request failed: {str(exc)[:120]}")

    body = b"".join(chunks)
    info = {
        "status_code": resp.status_code,
        "final_url": resp.url,
        "redirected": len(resp.history) > 0,
        "content_type": resp.headers.get("Content-Type", ""),
        "compressed_bytes": len(body),
        "gzip": False,
    }
    if body[:2] == b"\x1f\x8b":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as gz:
                body = gz.read(DOWNLOAD_CAP + 1)
        except OSError:
            raise FetchError("The .gz file is corrupted and couldn't be decompressed.")
        info["gzip"] = True
    info["bytes"] = len(body)
    return body, info


# ---------------------------------------------------------------------------
# robots.txt (Google-style matching: longest rule wins, * and $ wildcards)
# ---------------------------------------------------------------------------
class Robots:
    def __init__(self, text):
        self.groups = []
        self.sitemaps = []
        agents, rules, last_was_agent = [], [], False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, val = line.split(":", 1)
            key, val = key.strip().lower(), val.strip()
            if key == "user-agent":
                if not last_was_agent and agents:
                    self.groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(val.lower())
                last_was_agent = True
            elif key in ("allow", "disallow"):
                last_was_agent = False
                if agents:
                    rules.append((key == "allow", val))
            elif key == "sitemap":
                self.sitemaps.append(val)
        if agents:
            self.groups.append((agents, rules))
        self._cache = {}

    def _rules_for(self, token):
        token = token.lower()
        if token in self._cache:
            return self._cache[token]
        best_len, chosen = -1, []
        for agents, rules in self.groups:
            for a in agents:
                if a != "*" and token.startswith(a):
                    if len(a) > best_len:
                        best_len, chosen = len(a), list(rules)
                    elif len(a) == best_len:
                        chosen += rules
        if best_len < 0:
            chosen = [r for agents, rules in self.groups if "*" in agents for r in rules]
        compiled = []
        for allow, pattern in chosen:
            if not pattern:
                continue
            anchored = pattern.endswith("$")
            body = pattern[:-1] if anchored else pattern
            rx = re.escape(body).replace(r"\*", ".*") + ("$" if anchored else "")
            compiled.append((allow, len(pattern), re.compile(rx)))
        self._cache[token] = compiled
        return compiled

    def can_fetch(self, token, url):
        p = urlparse(url)
        path = (p.path or "/") + (("?" + p.query) if p.query else "")
        best = None  # (length, allow)
        for allow, length, rx in self._rules_for(token):
            if rx.match(path):
                if best is None or length > best[0] or (length == best[0] and allow):
                    best = (length, allow)
        return True if best is None else best[1]


def load_robots(root):
    url = root.rstrip("/") + "/robots.txt"
    out = {"url": url, "found": False, "robots": None, "sitemaps": []}
    if not is_public_host(url):
        return out
    try:
        resp = requests.get(url, headers=HEADERS, timeout=8)
        ctype = resp.headers.get("Content-Type", "").lower()
        if resp.status_code == 200 and "html" not in ctype:
            robots = Robots(resp.text[:500_000])
            out.update(found=True, robots=robots, sitemaps=robots.sitemaps)
    except requests.exceptions.RequestException:
        pass
    return out


# ---------------------------------------------------------------------------
# XML parsing + validation
# ---------------------------------------------------------------------------
def _local(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _namespace(tag):
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def parse_sitemap_xml(body, issues, label=None):
    """Stream-parse sitemap XML. Returns (kind, entries). Records file-level issues."""
    head = body[:1000].lstrip(b"\xef\xbb\xbf").lstrip().lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        issues.add("not_xml_html", example=label)
        return None, []

    stripped = body.lstrip(b"\xef\xbb\xbf").lstrip()
    if stripped.startswith(b"<?xml") and not body.lstrip(b"\xef\xbb\xbf").startswith(b"<?xml"):
        issues.add("leading_content", example=label)
        body = stripped

    kind, ns, entries, depth, root = None, None, [], 0, None
    try:
        for event, elem in DET.iterparse(io.BytesIO(body), events=("start", "end")):
            if event == "start":
                depth += 1
                if root is None:
                    root, kind, ns = elem, _local(elem.tag), _namespace(elem.tag)
                continue
            depth -= 1
            if depth != 1:
                continue
            tag = _local(elem.tag)
            if tag in ("url", "sitemap"):
                entry = {"loc": None, "lastmod": None, "changefreq": None, "priority": None}
                for child in elem:
                    ctag = _local(child.tag)
                    if ctag in entry and entry[ctag] is None:
                        entry[ctag] = child.text or ""
                entries.append(entry)
                if len(entries) >= MAX_ENTRIES_ANALYSED:
                    break
            root.clear()
    except ParseError as exc:
        issues.add("xml_malformed", example=label, detail=str(exc))
    except DefusedXmlException:
        issues.add("xml_malformed", example=label,
                   detail="The file uses DTD/entity declarations, which sitemaps must not contain.")

    if kind is None:
        if not issues.has("xml_malformed"):
            issues.add("xml_malformed", example=label, detail="No XML content found.")
        return None, entries
    if kind not in ("urlset", "sitemapindex"):
        issues.add("bad_root", example=label, detail=f"Found <{kind}>")
    if ns != SITEMAP_NS:
        issues.add("bad_namespace", example=label, detail=f"Found: {ns or '(none)'}")
    return kind, entries


def validate_entries(entries, issues, base_host=None, source=None, check_hosts=True):
    """URL-level + lastmod checks. Adds cleaned fields to each entry in place."""
    today = datetime.now(timezone.utc).date()
    schemes = Counter()
    lastmods = []
    has_optional = False
    for e in entries:
        raw = e.get("loc")
        if raw is None or not raw.strip():
            issues.add("missing_loc", example=source)
            e["loc"] = ""
            continue
        loc = raw.strip()
        e["loc"] = loc
        if source:
            e["source"] = source
        if re.search(r"\s", loc):
            issues.add("loc_whitespace", example=loc)
        p = urlparse(loc)
        if p.scheme.lower() not in ("http", "https") or not p.netloc:
            issues.add("invalid_loc", example=loc)
            e["bad"] = True
            continue
        schemes[p.scheme.lower()] += 1
        if len(loc) > 2048:
            issues.add("loc_too_long", example=loc[:120] + "…")
        if "#" in loc:
            issues.add("loc_fragment", example=loc)
        if check_hosts and base_host and p.netloc.lower() != base_host:
            issues.add("cross_host", example=loc)

        lm = (e.get("lastmod") or "").strip()
        e["lastmod"] = lm or None
        e["lastmod_valid"] = False
        if lm:
            if not W3C_DATE_RE.match(lm):
                issues.add("lastmod_invalid", example=f"{lm}  ({loc})")
            else:
                e["lastmod_valid"] = True
                lastmods.append(lm)
                if len(lm) >= 10:
                    try:
                        d = datetime.strptime(lm[:10], "%Y-%m-%d").date()
                        if d > today + timedelta(days=1):
                            issues.add("lastmod_future", example=f"{lm}  ({loc})")
                        e["lastmod_date"] = d.isoformat()
                    except ValueError:
                        issues.add("lastmod_invalid", example=f"{lm}  ({loc})")
                        e["lastmod_valid"] = False
        else:
            issues.add("lastmod_missing", example=loc)

        pr, cf = e.get("priority"), e.get("changefreq")
        if pr is not None or cf is not None:
            has_optional = True
        if pr is not None:
            try:
                if not 0.0 <= float(pr.strip()) <= 1.0:
                    issues.add("optional_invalid", example=f"priority {pr.strip()}  ({loc})")
            except ValueError:
                issues.add("optional_invalid", example=f"priority {pr.strip()}  ({loc})")
        if cf is not None and cf.strip().lower() not in VALID_CHANGEFREQ:
            issues.add("optional_invalid", example=f"changefreq {cf.strip()}  ({loc})")

    if len(schemes) > 1:
        issues.add("mixed_protocol", detail=", ".join(f"{k}: {v}" for k, v in schemes.items()))
    if has_optional:
        issues.add("priority_changefreq")
    if len(lastmods) >= 5 and len(set(lastmods)) == 1:
        issues.add("lastmod_same", detail=f"All {len(lastmods)} URLs use {lastmods[0]}", n=len(lastmods))


def mark_duplicates(entries, issues):
    counts = Counter(e["loc"] for e in entries if e.get("loc"))
    for loc, c in counts.items():
        if c > 1:
            issues.add("duplicates", example=f"{loc}  (×{c})", n=c - 1)


def lastmod_buckets(entries):
    today = datetime.now(timezone.utc).date()
    buckets = {"< 30 days": 0, "1–6 months": 0, "6–12 months": 0, "> 1 year": 0, "Missing / invalid": 0}
    for e in entries:
        d = e.get("lastmod_date")
        if not d:
            buckets["Missing / invalid"] += 1
            continue
        age = (today - datetime.strptime(d, "%Y-%m-%d").date()).days
        if age < 30:
            buckets["< 30 days"] += 1
        elif age < 183:
            buckets["1–6 months"] += 1
        elif age < 366:
            buckets["6–12 months"] += 1
        else:
            buckets["> 1 year"] += 1
    return buckets


def check_file_info(info, issues, label):
    if info["status_code"] != 200:
        issues.add("fetch_status", example=label, detail=f"HTTP {info['status_code']}")
    if info["redirected"]:
        issues.add("sitemap_redirect", example=f"{label} → {info['final_url']}")
    ctype = info["content_type"].lower()
    if info["status_code"] == 200 and ctype and not any(t in ctype for t in ("xml", "gzip", "x-gzip", "octet-stream")):
        issues.add("content_type", example=label, detail=info["content_type"])
    if info["bytes"] > MAX_SITEMAP_BYTES:
        issues.add("too_large", example=label, detail=f"{info['bytes'] / 1048576:.1f} MB")


def process_child(loc):
    """Fetch + parse one child sitemap of an index. Returns (child_summary, entries, local_issues)."""
    local = Issues()
    summary = {"loc": loc, "status": None, "url_count": 0, "bytes": 0, "gzip": False, "error": None, "kind": None}
    try:
        body, info = fetch_raw(loc)
    except FetchError as exc:
        summary["error"] = str(exc)
        local.add("child_failed", example=loc, detail=str(exc))
        return summary, [], local
    summary.update(status=info["status_code"], bytes=info["bytes"], gzip=info["gzip"])
    check_file_info(info, local, loc)
    local.items.pop("fetch_status", None)  # reported once, as child_failed
    if info["status_code"] != 200:
        summary["error"] = f"HTTP {info['status_code']}"
        local.add("child_failed", example=loc, detail=f"HTTP {info['status_code']}")
        return summary, [], local
    kind, entries = parse_sitemap_xml(body, local, label=loc)
    summary["kind"] = kind
    if kind == "sitemapindex":
        local.add("nested_index", example=loc)
        summary["error"] = "Nested sitemap index"
        return summary, [], local
    if kind != "urlset" or local.has("xml_malformed") or local.has("not_xml_html"):
        summary["error"] = "Couldn't parse" if kind != "urlset" else "XML errors (partially read)"
        local.add("child_failed", example=loc, detail=summary["error"])
    if len(entries) > MAX_URLS_PER_FILE:
        local.add("too_many_urls", example=loc, detail=f"{len(entries):,} URLs")
    if not entries and kind == "urlset" and not local.has("xml_malformed"):
        local.add("empty", example=loc)
    validate_entries(entries, local, base_host=urlparse(info["final_url"]).netloc.lower(), source=loc)
    summary["url_count"] = len(entries)
    return summary, entries, local


def merge_issues(target, src):
    for code, it in src.items.items():
        t = target.items.setdefault(code, {"count": 0, "examples": [], "detail": None})
        t["count"] += it["count"]
        for ex in it["examples"]:
            if len(t["examples"]) < 5 and ex not in t["examples"]:
                t["examples"].append(ex)
        if it["detail"] and not t["detail"]:
            t["detail"] = it["detail"]


# ---------------------------------------------------------------------------
# URL status + indexability check
# ---------------------------------------------------------------------------
def _get(url):
    return requests.get(url, headers=HEADERS, timeout=CHECK_TIMEOUT, allow_redirects=True, stream=True)


def check_url(url):
    start = time.time()
    base = {"url": url, "status_code": None, "final_url": None, "redirect_chain": [], "redirect_count": 0,
            "noindex": None, "canonical": None, "canonical_match": None, "response_ms": None,
            "error": None, "verdict": "error"}
    if not is_public_host(url):
        base.update(error="Private address", response_ms=0)
        return base
    sem = _get_domain_semaphore(url)
    sem.acquire()
    try:
        resp = _get(url)
        if resp.status_code in (403, 429, 503):
            resp.close()
            time.sleep(1.2 + random.random())
            resp = _get(url)

        status = resp.status_code
        chain = [{"status_code": h.status_code, "url": h.url} for h in resp.history]
        noindex, canonical = None, None

        xrt = resp.headers.get("X-Robots-Tag", "")
        if xrt:
            for part in xrt.split(","):
                part = part.strip().lower()
                if ":" in part and not part.startswith(("unavailable_after",)):
                    bot, _, directive = part.partition(":")
                    if bot.strip() not in ("googlebot", "*"):
                        continue
                    part = directive.strip()
                if part in ("noindex", "none"):
                    noindex = "X-Robots-Tag header"

        link_canon = resp.links.get("canonical") if hasattr(resp, "links") else None
        if link_canon and link_canon.get("url"):
            canonical = urljoin(resp.url, link_canon["url"])

        ctype = resp.headers.get("Content-Type", "").lower()
        if 200 <= status < 300 and "html" in ctype:
            raw, total = [], 0
            for chunk in resp.iter_content(32768):
                raw.append(chunk)
                total += len(chunk)
                if total >= HTML_READ_LIMIT or b"</head>" in chunk.lower():
                    break
            html = b"".join(raw).decode(resp.encoding or "utf-8", errors="replace")
            cut = html.lower().find("</head>")
            if cut > 0:
                html = html[:cut + 7]
            soup = BeautifulSoup(html, "html.parser")
            for m in soup.find_all("meta", attrs={"name": True}):
                if m["name"].strip().lower() in ("robots", "googlebot"):
                    directives = [d.strip() for d in (m.get("content") or "").lower().split(",")]
                    if "noindex" in directives or "none" in directives:
                        noindex = noindex or "meta robots tag"
            if canonical is None:
                for link in soup.find_all("link", href=True):
                    rel = link.get("rel") or []
                    rels = [r.lower() for r in (rel if isinstance(rel, list) else rel.split())]
                    if "canonical" in rels:
                        canonical = urljoin(resp.url, link["href"].strip())
                        break
        resp.close()

        canonical_match = None
        if canonical:
            # compare with the page that actually served the tag (the final URL after redirects)
            canonical_match = norm_url(canonical) == norm_url(resp.url)

        if 500 <= status < 600:
            verdict = "server_error"
        elif 400 <= status < 500:
            verdict = "unverifiable" if is_unreliable_domain(url) else "client_error"
        elif chain or 300 <= status < 400:
            verdict = "redirect"
        elif noindex:
            verdict = "noindex"
        elif canonical and not canonical_match:
            verdict = "canonicalised"
        else:
            verdict = "valid"

        base.update(status_code=status, final_url=resp.url, redirect_chain=chain, redirect_count=len(chain),
                    noindex=noindex, canonical=canonical, canonical_match=canonical_match, verdict=verdict,
                    response_ms=round((time.time() - start) * 1000))
        return base
    except requests.exceptions.Timeout:
        msg = "Timeout"
    except requests.exceptions.TooManyRedirects:
        msg = "Too many redirects"
    except requests.exceptions.SSLError:
        msg = "SSL error"
    except requests.exceptions.ConnectionError:
        msg = "Connection failed"
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)[:120]
    finally:
        sem.release()
    base.update(error=msg, verdict="unverifiable" if is_unreliable_domain(url) else "error",
                response_ms=round((time.time() - start) * 1000))
    return base


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template_string(PAGE_TEMPLATE, max_checks=MAX_STATUS_CHECKS,
                                  max_children=MAX_CHILD_SITEMAPS, batch=25)


@app.route("/api/parse", methods=["POST"])
def api_parse():
    data = request.get_json(force=True, silent=True) or {}
    mode = data.get("mode", "url")
    value = (data.get("value") or "").strip()
    if not value:
        return jsonify({"error": "Please enter a sitemap URL, paste XML, or paste a list of URLs."}), 400

    issues = Issues()
    applicable = []
    meta = {"mode": mode, "sitemap_url": None, "final_url": None, "kind": None, "bytes": None,
            "gzip": False, "children": [], "total_urls": 0}
    entries = []
    base_host = None

    if mode == "list":
        applicable = ["invalid_loc", "loc_whitespace", "loc_too_long", "loc_fragment", "duplicates"]
        lines = [ln.strip() for ln in re.split(r"[\r\n]+", value) if ln.strip()]
        entries = [{"loc": ensure_scheme(ln) if not re.match(r"^[a-z][a-z0-9+.-]*://", ln, re.I) else ln}
                   for ln in lines[:MAX_URLS_RETURNED]]
        meta["kind"] = "list"
        validate_entries(entries, issues, check_hosts=False)
        for code in ("lastmod_missing", "mixed_protocol"):
            issues.items.pop(code, None)
    else:
        if mode == "xml":
            body = value.encode("utf-8")
            meta["bytes"] = len(body)
            applicable = ["leading_content", "xml_malformed", "bad_root", "bad_namespace", "empty",
                          "too_many_urls", "too_large"]
            kind, entries = parse_sitemap_xml(body, issues, label="Pasted XML")
            if len(body) > MAX_SITEMAP_BYTES:
                issues.add("too_large")
        else:
            url = ensure_scheme(value)
            meta["sitemap_url"] = url
            applicable = list(FILE_CHECKS)
            try:
                body, info = fetch_raw(url)
            except FetchError as exc:
                return jsonify({"error": str(exc)}), 400
            meta.update(final_url=info["final_url"], bytes=info["bytes"], gzip=info["gzip"])
            check_file_info(info, issues, url)
            base_host = urlparse(info["final_url"]).netloc.lower()
            if info["status_code"] != 200 and not body.strip():
                kind = None
            else:
                kind, entries = parse_sitemap_xml(body, issues, label=url)
        meta["kind"] = kind

        if kind == "sitemapindex":
            applicable += INDEX_CHECKS
            child_locs = []
            for e in entries:
                loc = (e.get("loc") or "").strip()
                if not loc:
                    issues.add("missing_loc", example="(sitemap index entry)")
                    continue
                child_locs.append({"loc": loc, "lastmod": (e.get("lastmod") or "").strip() or None})
            if len(child_locs) > MAX_URLS_PER_FILE:
                issues.add("too_many_urls", detail=f"{len(child_locs):,} sitemaps listed")
            if not child_locs:
                issues.add("empty")
            to_fetch = child_locs[:MAX_CHILD_SITEMAPS]
            skipped = child_locs[MAX_CHILD_SITEMAPS:]
            all_entries, started = [], time.time()
            with ThreadPoolExecutor(max_workers=5) as pool:
                for i in range(0, len(to_fetch), 5):
                    batch = to_fetch[i:i + 5]
                    if time.time() - started > CHILD_TIME_BUDGET or len(all_entries) >= MAX_ENTRIES_ANALYSED:
                        skipped = to_fetch[i:] + skipped
                        break
                    for child, (summary, child_entries, local) in zip(
                            batch, pool.map(process_child, [c["loc"] for c in batch])):
                        summary["lastmod"] = child["lastmod"]
                        meta["children"].append(summary)
                        merge_issues(issues, local)
                        all_entries.extend(child_entries)
            analysed = len(meta["children"])
            for child in skipped:
                meta["children"].append({"loc": child["loc"], "lastmod": child["lastmod"], "status": None,
                                         "url_count": None, "error": "Not analysed (free-tier limit)",
                                         "skipped": True})
            if skipped:
                issues.add("children_capped", n=len(skipped),
                           detail=f"{analysed} of {len(child_locs)} child sitemaps analysed")
            entries = all_entries
            applicable += URL_CHECKS + LASTMOD_CHECKS
        elif kind == "urlset":
            applicable += URL_CHECKS + LASTMOD_CHECKS
            if len(entries) > MAX_URLS_PER_FILE:
                issues.add("too_many_urls", detail=f"{len(entries):,} URLs")
            if not entries and not issues.has("xml_malformed"):
                issues.add("empty")
            if base_host is None and entries:
                hosts = Counter(urlparse((e.get("loc") or "").strip()).netloc.lower() for e in entries)
                base_host = hosts.most_common(1)[0][0] if hosts else None
            validate_entries(entries, issues, base_host=base_host, source=meta.get("sitemap_url"))

    entries = [e for e in entries if e.get("loc") and not e.get("bad")]
    mark_duplicates(entries, issues)
    meta["total_urls"] = len(entries)

    # ---- robots.txt: declaration + Googlebot blocking + AI crawler access ----
    robots_info = None
    root_url = meta.get("final_url") or meta.get("sitemap_url")
    if not root_url and entries:
        root_url = Counter(
            f"{urlparse(e['loc']).scheme}://{urlparse(e['loc']).netloc}" for e in entries).most_common(1)[0][0]
    if root_url and (entries or mode == "url"):
        p = urlparse(root_url)
        root = f"{p.scheme}://{p.netloc}"
        rb = load_robots(root)
        applicable += ["robots_missing", "blocked_robots"] + (["robots_not_declared"] if mode == "url" else [])
        robots_info = {"url": rb["url"], "found": rb["found"], "declared_sitemaps": rb["sitemaps"],
                       "declared": None, "ai": []}
        if not rb["found"]:
            issues.add("robots_missing", example=rb["url"])
        else:
            robots = rb["robots"]
            if mode == "url":
                targets = {norm_url(meta["sitemap_url"]), norm_url(meta.get("final_url") or meta["sitemap_url"])}
                declared = any(norm_url(s) in targets for s in rb["sitemaps"])
                robots_info["declared"] = declared
                if not declared:
                    issues.add("robots_not_declared", example=meta["sitemap_url"],
                               detail=f"{len(rb['sitemaps'])} other sitemap(s) declared" if rb["sitemaps"] else None)
            host = p.netloc.lower()
            for e in entries:
                if urlparse(e["loc"]).netloc.lower() == host and not robots.can_fetch("googlebot", e["loc"]):
                    e["blocked"] = True
                    issues.add("blocked_robots", example=e["loc"])
            robots_info["ai"] = [{"bot": b, "allowed": robots.can_fetch(b, root + "/")} for b in AI_CRAWLERS]

    # ---- Trim what we send to the browser ----
    returned, per_source = [], Counter()
    for e in entries:
        src = e.get("source") or ""
        if meta["kind"] == "sitemapindex" and per_source[src] >= MAX_URLS_PER_CHILD_RETURNED:
            continue
        per_source[src] += 1
        returned.append({"loc": e["loc"], "lastmod": e.get("lastmod"), "source": e.get("source"),
                         "blocked": bool(e.get("blocked"))})
        if len(returned) >= MAX_URLS_RETURNED:
            break

    checklist = issues.report(applicable)
    crit = sum(1 for c in checklist if c["severity"] == "critical")
    warn = sum(1 for c in checklist if c["severity"] == "warning")
    structure_score = max(0, 100 - 20 * crit - 6 * warn)

    return jsonify({
        "meta": meta,
        "checklist": checklist,
        "structure_score": structure_score,
        "counts": {"critical": crit, "warning": warn,
                   "info": sum(1 for c in checklist if c["severity"] == "info"),
                   "pass": sum(1 for c in checklist if c["severity"] == "pass")},
        "lastmod_buckets": lastmod_buckets(entries) if meta["kind"] != "list" else None,
        "robots": robots_info,
        "urls": returned,
        "returned_urls": len(returned),
    })


@app.route("/api/check", methods=["POST"])
def api_check():
    data = request.get_json(force=True, silent=True) or {}
    urls = [u for u in (data.get("urls") or []) if isinstance(u, str) and u.strip()][:MAX_BATCH]
    urls = [u.strip() for u in urls if re.match(r"^https?://", u.strip(), re.I)]
    if not urls:
        return jsonify({"error": "No URLs to check."}), 400
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(check_url, urls))
    return jsonify({"results": results})


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Frontend (single-file dashboard template)
# ---------------------------------------------------------------------------
PAGE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>XML Sitemap Validator & URL Status Checker | SEO Tools by Mahalakshmi</title>
<meta name="description" content="Free XML sitemap validator. Check sitemap structure against Google's rules, then bulk-check every URL's status code, redirects, noindex and canonical — and download a clean sitemap.">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%237c6af7'/%3E%3Ctext x='50%25' y='50%25' dominant-baseline='central' text-anchor='middle' font-family='Georgia%2C serif' font-size='18' font-weight='900' fill='white'%3EM%3C/text%3E%3C/svg%3E">
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;600;700;800;900&family=Inter:wght@300;400;500;600&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  :root {
    --bg: #0a0a0f; --surface: #12121a; --surface2: #1a1a26;
    --accent: #7c6af7; --accent2: #f7a26a; --accent3: #6af7c8;
    --danger: #f76a6a; --text: #e8e8f0; --muted: #8888a8;
    --border: rgba(124,106,247,0.18); --card: rgba(18,18,28,0.85);
  }
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); font-size: 15px; line-height: 1.6; }
  a { color: inherit; }
  .layout { display: flex; min-height: 100vh; }

  /* SIDEBAR */
  .sidebar { width: 240px; flex-shrink: 0; background: var(--surface); border-right: 1px solid var(--border); padding: 1.6rem 1.2rem; position: sticky; top: 0; height: 100vh; display: flex; flex-direction: column; gap: 2rem; }
  .brand { display: flex; align-items: center; gap: 0.55rem; text-decoration: none; }
  .brand-mark { width: 30px; height: 30px; border-radius: 8px; background: var(--accent); display: flex; align-items: center; justify-content: center; font-family: 'Sora', sans-serif; font-weight: 900; font-size: 1rem; color: #fff; flex-shrink: 0; }
  .brand-text { font-family: 'Sora', sans-serif; font-weight: 700; font-size: 0.95rem; }
  .sidebar-section { display: flex; flex-direction: column; gap: 0.3rem; }
  .sidebar-label { font-family: 'DM Mono', monospace; font-size: 0.65rem; letter-spacing: 0.14em; text-transform: uppercase; color: var(--muted); margin-bottom: 0.5rem; }
  .sidebar-link { display: flex; align-items: center; gap: 0.6rem; padding: 0.55rem 0.7rem; border-radius: 8px; border: 1px solid transparent; font-size: 0.85rem; font-weight: 500; color: var(--muted); text-decoration: none; transition: all 0.15s; }
  .sidebar-link:hover { background: rgba(124,106,247,0.12); color: var(--text); }
  .sidebar-link.active { background: rgba(124,106,247,0.14); border: 1px solid rgba(124,106,247,0.4); color: var(--accent); font-weight: 700; }
  .sidebar-limits { margin-top: auto; background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 0.9rem; font-size: 0.72rem; color: var(--muted); line-height: 1.6; }
  .sidebar-limits strong { color: var(--text); }
  .sidebar-footer { padding-top: 1rem; border-top: 1px solid var(--border); }
  .sidebar-footer p { font-size: 0.7rem; color: var(--muted); line-height: 1.5; margin-bottom: 0.7rem; }
  .credit-name { color: var(--accent3); font-weight: 700; text-decoration: none; }
  .credit-name:hover { text-decoration: underline; }
  .sidebar-social { display: flex; gap: 0.5rem; }
  .sidebar-social a { width: 28px; height: 28px; border-radius: 6px; background: rgba(106,247,200,0.08); border: 1px solid rgba(106,247,200,0.35); display: flex; align-items: center; justify-content: center; color: var(--accent3); text-decoration: none; font-size: 0.68rem; font-family: 'DM Mono', monospace; transition: all 0.2s; }
  .sidebar-social a:hover { background: rgba(106,247,200,0.2); border-color: var(--accent3); }

  /* MAIN */
  .main { flex: 1; padding: 2.2rem 3rem 4rem; max-width: 1220px; min-width: 0; }
  .page-head { margin-bottom: 1.8rem; }
  .page-eyebrow { font-family: 'DM Mono', monospace; font-size: 0.68rem; letter-spacing: 0.16em; text-transform: uppercase; color: var(--accent3); margin-bottom: 0.5rem; }
  .page-title { font-family: 'Sora', sans-serif; font-size: 1.7rem; font-weight: 800; margin-bottom: 0.4rem; }
  .page-sub { color: var(--muted); font-size: 0.92rem; max-width: 680px; }

  /* INPUT CARD */
  .scan-card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 1.6rem; margin-bottom: 2rem; }
  .tabs { display: flex; gap: 0.4rem; margin-bottom: 1rem; flex-wrap: wrap; }
  .tab { background: var(--surface2); border: 1px solid var(--border); color: var(--muted); padding: 0.45rem 1rem; border-radius: 8px; font-size: 0.8rem; font-weight: 600; cursor: pointer; font-family: 'Inter', sans-serif; transition: all 0.15s; }
  .tab:hover { color: var(--text); }
  .tab.active { background: rgba(124,106,247,0.14); border-color: rgba(124,106,247,0.45); color: var(--accent); }
  .scan-input { width: 100%; background: var(--surface2); border: 1px solid var(--border); border-radius: 8px; color: var(--text); padding: 0.75rem 0.9rem; font-family: 'Inter', sans-serif; font-size: 0.9rem; }
  .scan-input:focus { outline: none; border-color: var(--accent); }
  textarea.scan-input { resize: vertical; min-height: 160px; font-family: 'DM Mono', monospace; font-size: 0.78rem; }
  .scan-hint { font-size: 0.74rem; color: var(--muted); margin-top: 0.5rem; font-family: 'DM Mono', monospace; }
  .options-row { display: flex; align-items: center; gap: 0.7rem; flex-wrap: wrap; margin-top: 1.1rem; font-size: 0.82rem; color: var(--muted); }
  .options-row select { background: var(--surface2); border: 1px solid var(--border); color: var(--text); padding: 0.4rem 0.6rem; border-radius: 7px; font-size: 0.8rem; font-family: 'Inter', sans-serif; }
  .scan-actions { margin-top: 1.2rem; display: flex; align-items: center; gap: 1rem; flex-wrap: wrap; }
  .btn-primary { background: var(--accent); color: #fff; border: none; padding: 0.7rem 1.7rem; border-radius: 8px; font-family: 'Inter', sans-serif; font-weight: 600; font-size: 0.88rem; cursor: pointer; transition: background 0.2s, transform 0.2s; }
  .btn-primary:hover { background: #6a58e8; transform: translateY(-1px); }
  .btn-primary:disabled { opacity: 0.55; cursor: not-allowed; transform: none; }
  .btn-ghost { background: var(--surface2); border: 1px solid var(--border); color: var(--text); padding: 0.5rem 1rem; border-radius: 8px; font-size: 0.8rem; font-weight: 600; cursor: pointer; font-family: 'Inter', sans-serif; }
  .btn-ghost:hover { border-color: var(--accent); color: var(--accent); }
  .btn-ghost:disabled { opacity: 0.5; cursor: not-allowed; }
  .spinner { width: 16px; height: 16px; border: 2px solid rgba(255,255,255,0.35); border-top-color: #fff; border-radius: 50%; animation: spin 0.7s linear infinite; display: inline-block; vertical-align: -3px; margin-right: 0.5rem; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .scan-status { font-size: 0.82rem; color: var(--muted); }
  .error-box { margin-top: 1rem; background: rgba(247,106,106,0.1); border: 1px solid rgba(247,106,106,0.3); color: #ff9d9d; padding: 0.7rem 1rem; border-radius: 8px; font-size: 0.85rem; display: none; }

  /* RESULTS */
  #results { display: none; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 1.2rem 1.3rem; margin-bottom: 1.4rem; }
  .card h3 { font-family: 'Sora', sans-serif; font-size: 0.92rem; font-weight: 700; margin-bottom: 0.9rem; display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
  .card h3 .sub { font-family: 'Inter', sans-serif; font-weight: 400; font-size: 0.76rem; color: var(--muted); }
  .summary-row { display: grid; grid-template-columns: 210px repeat(5, 1fr); gap: 1rem; margin-bottom: 1.4rem; }
  .health-card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 1.1rem; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 0.35rem; }
  .health-ring-wrap { position: relative; width: 100px; height: 100px; }
  .health-ring-wrap svg { transform: rotate(-90deg); }
  .health-ring-bg { fill: none; stroke: var(--surface2); stroke-width: 9; }
  .health-ring-fg { fill: none; stroke: var(--accent3); stroke-width: 9; stroke-linecap: round; transition: stroke-dashoffset 0.6s ease, stroke 0.3s; }
  .health-score { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; font-family: 'Sora', sans-serif; font-size: 1.3rem; font-weight: 800; }
  .health-label { font-size: 0.7rem; color: var(--muted); font-family: 'DM Mono', monospace; text-transform: uppercase; letter-spacing: 0.08em; }
  .health-sub { font-size: 0.68rem; color: var(--muted); text-align: center; font-family: 'DM Mono', monospace; }
  .stat-card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 1.1rem 1.2rem; display: flex; flex-direction: column; justify-content: center; gap: 0.3rem; }
  .stat-num { font-family: 'Sora', sans-serif; font-size: 1.6rem; font-weight: 800; }
  .stat-label { font-size: 0.75rem; color: var(--muted); }
  .stat-card.clickable { cursor: pointer; transition: border-color 0.15s, transform 0.15s, background 0.15s; position: relative; }
  .stat-card.clickable:hover { border-color: rgba(124,106,247,0.55); background: rgba(124,106,247,0.07); transform: translateY(-2px); }
  .stat-card.clickable::after { content: 'View →'; position: absolute; top: 0.7rem; right: 0.9rem; font-family: 'DM Mono', monospace; font-size: 0.62rem; color: var(--muted); opacity: 0; transition: opacity 0.15s; }
  .stat-card.clickable:hover::after { opacity: 1; color: var(--accent); }
  .flash { animation: flash 1.2s ease; }
  @keyframes flash { 0% { box-shadow: 0 0 0 0 rgba(124,106,247,0.0); } 25% { box-shadow: 0 0 0 3px rgba(124,106,247,0.55); } 100% { box-shadow: 0 0 0 0 rgba(124,106,247,0.0); } }
  .stat-card.good .stat-num { color: var(--accent3); }
  .stat-card.warn .stat-num { color: var(--accent2); }
  .stat-card.bad .stat-num { color: var(--danger); }

  .chips { display: flex; flex-wrap: wrap; gap: 0.5rem; }
  .chip { font-family: 'DM Mono', monospace; font-size: 0.7rem; padding: 0.3rem 0.7rem; border-radius: 100px; background: var(--surface2); border: 1px solid var(--border); color: var(--text); overflow-wrap: anywhere; }
  .chip.ok { border-color: rgba(106,247,200,0.35); color: var(--accent3); }
  .chip.no { border-color: rgba(247,106,106,0.35); color: var(--danger); }
  .chip.mid { border-color: rgba(247,162,106,0.35); color: var(--accent2); }
  .chip-label { font-size: 0.7rem; color: var(--muted); font-family: 'DM Mono', monospace; text-transform: uppercase; letter-spacing: 0.08em; margin: 0.9rem 0 0.45rem; }
  .chip-label:first-child { margin-top: 0; }

  .progress-card { display: none; }
  .progress-top { display: flex; justify-content: space-between; align-items: center; gap: 1rem; font-size: 0.82rem; color: var(--muted); margin-bottom: 0.6rem; flex-wrap: wrap; }
  .progress-bar { height: 8px; background: var(--surface2); border-radius: 100px; overflow: hidden; }
  .progress-fill { height: 100%; width: 0; background: linear-gradient(90deg, var(--accent), var(--accent3)); transition: width 0.3s; }

  .check-item { display: flex; gap: 0.8rem; padding: 0.8rem 0; border-bottom: 1px solid rgba(255,255,255,0.05); align-items: flex-start; }
  .check-item:last-child { border-bottom: none; }
  .check-body { flex: 1; min-width: 0; }
  .check-title { font-weight: 600; font-size: 0.86rem; }
  .check-count { font-family: 'DM Mono', monospace; font-size: 0.72rem; color: var(--muted); margin-left: 0.3rem; }
  .check-detail { font-family: 'DM Mono', monospace; font-size: 0.72rem; color: var(--accent2); margin-top: 0.15rem; overflow-wrap: anywhere; }
  .check-fix { font-size: 0.78rem; color: var(--muted); margin-top: 0.2rem; }
  .check-item details { margin-top: 0.35rem; }
  .check-item summary { font-size: 0.74rem; color: var(--accent); cursor: pointer; font-family: 'DM Mono', monospace; }
  .check-item details ul { list-style: none; margin-top: 0.35rem; }
  .check-item details li { font-family: 'DM Mono', monospace; font-size: 0.7rem; color: #c8c8e0; overflow-wrap: anywhere; padding: 0.15rem 0; }
  .pass-toggle { margin-top: 0.6rem; }
  .group-label { font-family: 'DM Mono', monospace; font-size: 0.66rem; letter-spacing: 0.12em; text-transform: uppercase; color: var(--accent3); margin: 0.6rem 0 0.1rem; }

  .badge { display: inline-block; font-family: 'DM Mono', monospace; font-size: 0.66rem; padding: 0.2rem 0.6rem; border-radius: 100px; font-weight: 600; white-space: nowrap; border: 1px solid transparent; }
  .badge.critical { background: rgba(247,106,106,0.12); color: var(--danger); border-color: rgba(247,106,106,0.3); }
  .badge.warning { background: rgba(247,162,106,0.12); color: var(--accent2); border-color: rgba(247,162,106,0.3); }
  .badge.info { background: rgba(124,106,247,0.12); color: #a99cf9; border-color: rgba(124,106,247,0.3); }
  .badge.pass { background: rgba(106,247,200,0.1); color: var(--accent3); border-color: rgba(106,247,200,0.25); }

  .chart-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; margin-bottom: 1.4rem; }
  .chart-row .card { margin-bottom: 0; }
  .chart-box { position: relative; height: 210px; }

  .results-head { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 0.8rem; margin-bottom: 1rem; }
  .filter-row { display: flex; gap: 0.4rem; flex-wrap: wrap; }
  .filter-btn { background: var(--surface2); border: 1px solid var(--border); color: var(--muted); padding: 0.35rem 0.8rem; border-radius: 100px; font-size: 0.72rem; font-family: 'DM Mono', monospace; cursor: pointer; transition: all 0.15s; }
  .filter-btn.active { background: var(--accent); border-color: var(--accent); color: #fff; }
  .filter-btn[hidden] { display: none; }
  .table-tools { display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: center; }
  .search-input { background: var(--surface2); border: 1px solid var(--border); color: var(--text); padding: 0.45rem 0.8rem; border-radius: 8px; font-size: 0.8rem; min-width: 200px; }
  .search-input:focus { outline: none; border-color: var(--accent); }
  .export-note { font-size: 0.72rem; color: var(--muted); margin-bottom: 0.8rem; }

  table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
  thead th { text-align: left; padding: 0.6rem 0.7rem; color: var(--muted); font-size: 0.66rem; text-transform: uppercase; letter-spacing: 0.06em; font-family: 'DM Mono', monospace; border-bottom: 1px solid var(--border); white-space: nowrap; }
  tbody td { padding: 0.6rem 0.7rem; border-bottom: 1px solid rgba(255,255,255,0.04); vertical-align: top; }
  tbody tr:hover { background: rgba(124,106,247,0.05); }
  .url-cell { max-width: 340px; overflow-wrap: anywhere; }
  .url-cell a { text-decoration: none; color: var(--text); }
  .url-cell a:hover { color: var(--accent); }
  .small-muted { font-size: 0.7rem; color: var(--muted); margin-top: 0.2rem; overflow-wrap: anywhere; }
  .vbadge { display: inline-block; font-family: 'DM Mono', monospace; font-size: 0.66rem; padding: 0.2rem 0.6rem; border-radius: 100px; font-weight: 600; white-space: nowrap; }
  .empty-state { text-align: center; padding: 2.5rem 1rem; color: var(--muted); font-size: 0.86rem; }
  .more-row { text-align: center; margin-top: 1rem; }
  .child-table td:first-child, .child-table th:first-child { width: 34px; }
  .child-actions { display: flex; gap: 0.6rem; flex-wrap: wrap; margin-top: 1rem; align-items: center; }
  input[type=checkbox] { accent-color: var(--accent); width: 15px; height: 15px; }

  @media (max-width: 1100px) {
    .summary-row { grid-template-columns: repeat(3, 1fr); }
    .health-card { grid-row: span 2; }
    .chart-row { grid-template-columns: 1fr 1fr; }
  }
  @media (max-width: 900px) {
    .sidebar { display: none; }
    .main { padding: 1.6rem 1rem 3rem; }
    .summary-row { grid-template-columns: 1fr 1fr; }
    .health-card { grid-column: span 2; grid-row: auto; }
    .chart-row { grid-template-columns: 1fr; }
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
      <a href="#" class="sidebar-link active">🗺️ Sitemap Validator</a>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-label">Navigate</div>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="sidebar-link">🧰 All My Tools</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio" class="sidebar-link">👩‍💻 Portfolio</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/blog/index.html" class="sidebar-link">📝 Blog Posts</a>
    </div>
    <div class="sidebar-limits">
      Validates <strong>every URL</strong> in the sitemap (up to <strong>{{ max_children }} child sitemaps</strong> from an index) and status-checks up to <strong>{{ max_checks }} URLs</strong> per run — free and fast.
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
      <div class="page-eyebrow">// seo tool</div>
      <div class="page-title">XML Sitemap Validator &amp; URL Status Checker</div>
      <div class="page-sub">Check your sitemap against Google's rules, then bulk-check every URL in it — status code, redirects, noindex and canonical — to find the URLs that don't belong there. Download a clean sitemap when you're done.</div>
    </div>

    <div class="scan-card">
      <div class="tabs">
        <button class="tab active" data-mode="url" onclick="setMode('url')">🌐 Sitemap URL</button>
        <button class="tab" data-mode="xml" onclick="setMode('xml')">📋 Paste XML</button>
        <button class="tab" data-mode="list" onclick="setMode('list')">🔗 Paste URL list</button>
      </div>
      <div id="inputUrlWrap">
        <input id="sitemapUrl" class="scan-input" type="text" placeholder="https://example.com/sitemap.xml" onkeydown="if(event.key==='Enter')startValidate()">
        <div class="scan-hint">Works with a regular sitemap, a sitemap index, or a .xml.gz file.</div>
      </div>
      <div id="inputTextWrap" style="display:none;">
        <textarea id="sitemapText" class="scan-input"></textarea>
        <div class="scan-hint" id="textHint"></div>
      </div>

      <div class="options-row">
        <span>Bulk status check:</span>
        <select id="checkCount">
          <option value="100">first 100 URLs</option>
          <option value="250">first 250 URLs</option>
          <option value="500">first 500 URLs (max)</option>
          <option value="0">skip — structure only</option>
        </select>
        <select id="checkPick">
          <option value="first">in sitemap order</option>
          <option value="random">random sample</option>
        </select>
      </div>

      <div class="scan-actions">
        <button class="btn-primary" id="validateBtn" onclick="startValidate()">Validate Sitemap</button>
        <span class="scan-status" id="scanStatus"></span>
      </div>
      <div class="error-box" id="errorBox"></div>
    </div>

    <div id="results">
      <div class="summary-row">
        <div class="health-card">
          <div class="health-ring-wrap">
            <svg width="100" height="100" viewBox="0 0 100 100">
              <circle class="health-ring-bg" cx="50" cy="50" r="42"></circle>
              <circle class="health-ring-fg" id="healthRing" cx="50" cy="50" r="42" stroke-dasharray="264" stroke-dashoffset="264"></circle>
            </svg>
            <div class="health-score" id="healthScoreText">0</div>
          </div>
          <div class="health-label">Sitemap Health</div>
          <div class="health-sub" id="healthSub"></div>
        </div>
        <div class="stat-card clickable" onclick="jumpToTable('all')" title="Show all URLs"><div class="stat-num" id="statUrls">0</div><div class="stat-label">URLs found</div></div>
        <div class="stat-card bad clickable" onclick="jumpToReport()" title="Go to the validation report"><div class="stat-num" id="statCritical">0</div><div class="stat-label">Critical issues</div></div>
        <div class="stat-card warn clickable" onclick="jumpToReport()" title="Go to the validation report"><div class="stat-num" id="statWarnings">0</div><div class="stat-label">Warnings</div></div>
        <div class="stat-card good clickable" onclick="jumpToTable('valid')" title="Show the valid URLs"><div class="stat-num" id="statValid">–</div><div class="stat-label">Valid URLs (checked)</div></div>
        <div class="stat-card bad clickable" onclick="jumpToTable('remove')" title="Show the URLs that need fixing"><div class="stat-num" id="statRemove">–</div><div class="stat-label">Shouldn't be in sitemap</div></div>
      </div>

      <div class="card progress-card" id="progressCard">
        <div class="progress-top">
          <span id="progressText">Checking URLs…</span>
          <button class="btn-ghost" id="stopBtn" onclick="stopCheck()">Stop</button>
        </div>
        <div class="progress-bar"><div class="progress-fill" id="progressFill"></div></div>
      </div>

      <div class="card" id="overviewCard">
        <h3>Sitemap overview</h3>
        <div id="overview"></div>
      </div>

      <div class="card" id="childCard" style="display:none;">
        <h3>Child sitemaps <span class="sub" id="childSub"></span></h3>
        <div style="overflow-x:auto;">
          <table class="child-table">
            <thead><tr><th><input type="checkbox" id="childAll" checked onchange="toggleAllChildren(this.checked)"></th><th>Sitemap</th><th>URLs</th><th>Lastmod</th><th>Status</th></tr></thead>
            <tbody id="childBody"></tbody>
          </table>
        </div>
        <div class="child-actions">
          <button class="btn-ghost" id="recheckBtn" onclick="runStatusCheck()">Status-check URLs from selected sitemaps</button>
          <span class="scan-status">Uses the count and sample options above.</span>
        </div>
      </div>

      <div class="card" id="reportCard">
        <h3>Validation report <span class="sub" id="checkSub"></span></h3>
        <div id="urlFindings"></div>
        <div id="checklist"></div>
      </div>

      <div class="chart-row">
        <div class="card"><h3>URL verdicts</h3><div class="chart-box"><canvas id="verdictChart"></canvas></div></div>
        <div class="card"><h3>Status codes</h3><div class="chart-box"><canvas id="codesChart"></canvas></div></div>
        <div class="card"><h3>Lastmod freshness</h3><div class="chart-box"><canvas id="lastmodChart"></canvas></div></div>
      </div>

      <div class="card" id="tableCard">
        <div class="results-head">
          <div class="filter-row" id="filterRow"></div>
          <div class="table-tools">
            <input class="search-input" id="searchInput" placeholder="Search URLs…" oninput="renderTable(true)">
            <button class="btn-ghost" onclick="exportCsv()">⬇ Export CSV</button>
            <button class="btn-ghost" id="cleanBtn" onclick="exportCleanSitemap()">⬇ Clean sitemap.xml</button>
          </div>
        </div>
        <div class="export-note" id="exportNote"></div>
        <div style="overflow-x:auto;">
          <table>
            <thead><tr><th>URL</th><th>Status</th><th>Verdict</th><th>Final URL / redirect chain</th><th>Indexability</th><th>Lastmod</th></tr></thead>
            <tbody id="resultsBody"></tbody>
          </table>
        </div>
        <div class="empty-state" id="emptyState" style="display:none;">No URLs match this filter.</div>
        <div class="more-row" id="moreRow" style="display:none;"><button class="btn-ghost" onclick="showMore()">Show more</button></div>
      </div>
    </div>
  </main>
</div>

<script>
const VERDICTS = {
  valid:         { label: 'Valid',            color: '#6af7c8' },
  redirect:      { label: 'Redirect',         color: '#f7a26a' },
  client_error:  { label: '4xx error',        color: '#f76a6a' },
  server_error:  { label: '5xx error',        color: '#c94a4a' },
  noindex:       { label: 'Noindex',          color: '#b58cff' },
  canonicalised: { label: 'Canonicalised',    color: '#7c6af7' },
  blocked:       { label: 'Blocked (robots)', color: '#f7d46a' },
  error:         { label: 'Unreachable',      color: '#8f3a3a' },
  unverifiable:  { label: 'Unverifiable',     color: '#8888a8' },
  unchecked:     { label: 'Not checked',      color: '#3a3a52' },
};
const FIX_TEXT = {
  redirect: ['URLs redirect', 'Replace each with its final URL (see the Final URL column) — sitemaps should only list destination URLs.'],
  client_error: ['URLs return 4xx (not found / gone / forbidden)', 'Remove them from the sitemap, or restore the pages if they should exist.'],
  server_error: ['URLs return 5xx server errors', 'Check the server/logs — Google will slow crawling if errors persist.'],
  error: ['URLs are unreachable (timeout / DNS / SSL)', 'Check that these pages load at all; re-run later to rule out a blip.'],
  noindex: ['URLs are marked noindex', 'A sitemap should only list pages you want indexed. Remove them or drop the noindex.'],
  canonicalised: ['URLs point their canonical to a different URL', 'List the canonical URL in the sitemap instead of this one.'],
  blocked: ['URLs are blocked by robots.txt for Googlebot', 'Unblock them in robots.txt or remove them from the sitemap.'],
};
const REMOVE_SET = ['redirect', 'client_error', 'server_error', 'error', 'noindex', 'canonicalised', 'blocked'];

let mode = 'url';
let parseData = null;
let urls = [];
let currentFilter = 'all';
let shown = 200;
let stopRequested = false;
let checking = false;
let charts = {};

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function safeHref(u) { return /^https?:\/\//i.test(u || '') ? esc(u) : '#'; }

function setMode(m) {
  mode = m;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.mode === m));
  document.getElementById('inputUrlWrap').style.display = m === 'url' ? 'block' : 'none';
  document.getElementById('inputTextWrap').style.display = m === 'url' ? 'none' : 'block';
  const ta = document.getElementById('sitemapText');
  const hint = document.getElementById('textHint');
  if (m === 'xml') {
    ta.placeholder = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n  <url><loc>https://example.com/</loc><lastmod>2026-09-01</lastmod></url>\n</urlset>';
    hint.textContent = 'Paste the full sitemap XML — handy for checking a sitemap before you upload it.';
  } else {
    ta.placeholder = 'https://example.com/page-1\nhttps://example.com/page-2\nhttps://example.com/page-3';
    hint.textContent = 'One URL per line — runs the bulk status + indexability check without a sitemap.';
  }
}

function showError(msg) {
  const box = document.getElementById('errorBox');
  box.textContent = msg; box.style.display = msg ? 'block' : 'none';
}

async function startValidate() {
  if (checking) return;
  const value = mode === 'url' ? document.getElementById('sitemapUrl').value.trim() : document.getElementById('sitemapText').value.trim();
  showError('');
  if (!value) { showError(mode === 'url' ? 'Please enter a sitemap URL.' : 'Please paste something to check.'); return; }

  const btn = document.getElementById('validateBtn');
  const status = document.getElementById('scanStatus');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Validating…';
  status.textContent = mode === 'url' ? 'Fetching the sitemap (large sitemap indexes can take up to a minute)…' : 'Validating…';
  document.getElementById('results').style.display = 'none';
  document.getElementById('progressCard').style.display = 'none';
  document.getElementById('progressText').textContent = 'Checking URLs…';
  document.getElementById('progressFill').style.width = '0';

  try {
    const resp = await fetch('/api/parse', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode, value }) });
    let data;
    try { data = await resp.json(); } catch (e) { throw new Error('The server took too long or returned an error. Please try again.'); }
    if (!resp.ok) { showError(data.error || 'Something went wrong. Please try again.'); status.textContent = ''; return; }

    parseData = data;
    urls = data.urls.map(u => ({ ...u, result: null }));
    currentFilter = 'all'; shown = 200;
    document.getElementById('searchInput').value = '';
    document.getElementById('results').style.display = 'block';
    renderAll();
    const truncNote = data.meta.total_urls > data.returned_urls ? ` (${data.returned_urls.toLocaleString()} loaded for status checks)` : '';
    status.textContent = `Found ${data.meta.total_urls.toLocaleString()} URL(s)${truncNote}.`;

    if (parseInt(document.getElementById('checkCount').value, 10) > 0 && urls.length) {
      await runStatusCheck();
    }
  } catch (err) {
    showError(err.message || 'Network error — please try again in a moment.');
    status.textContent = '';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Validate Sitemap';
  }
}

function selectedSources() {
  if (!parseData || parseData.meta.kind !== 'sitemapindex') return null;
  const set = new Set();
  document.querySelectorAll('.child-check:checked').forEach(cb => set.add(cb.value));
  return set;
}

async function runStatusCheck() {
  if (checking) return;
  let n = parseInt(document.getElementById('checkCount').value, 10);
  if (!n) n = 100;
  const pick = document.getElementById('checkPick').value;
  const sources = selectedSources();
  let pool = urls.filter(u => !sources || sources.has(u.source));
  if (!pool.length) { showError('No URLs to check — select at least one child sitemap with URLs.'); return; }
  showError('');
  if (pick === 'random') {
    pool = pool.slice();
    for (let i = pool.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [pool[i], pool[j]] = [pool[j], pool[i]]; }
  }
  const target = pool.slice(0, n);
  urls.forEach(u => u.result = null);

  checking = true; stopRequested = false;
  const card = document.getElementById('progressCard');
  const fill = document.getElementById('progressFill');
  const text = document.getElementById('progressText');
  const recheck = document.getElementById('recheckBtn');
  recheck.disabled = true;
  document.getElementById('stopBtn').disabled = false;
  card.style.display = 'block';
  renderAll();

  const BATCH = {{ batch }};
  let done = 0;
  try {
    for (let i = 0; i < target.length; i += BATCH) {
      if (stopRequested) break;
      const batch = target.slice(i, i + BATCH);
      text.textContent = `Checking ${Math.min(i + BATCH, target.length)} of ${target.length} URLs…`;
      try {
        const resp = await fetch('/api/check', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ urls: batch.map(u => u.loc) }) });
        const data = await resp.json();
        if (resp.ok) {
          data.results.forEach((r, idx) => { batch[idx].result = r; });
        } else {
          batch.forEach(u => u.result = { verdict: 'error', error: data.error || 'Check failed' });
        }
      } catch (e) {
        batch.forEach(u => u.result = { verdict: 'error', error: 'Request failed' });
      }
      done += batch.length;
      fill.style.width = `${Math.round(done / target.length * 100)}%`;
      renderAll(true);
    }
    text.textContent = stopRequested ? `Stopped after ${done} of ${target.length} URLs.` : `Done — checked ${done} URL(s).`;
  } finally {
    checking = false;
    recheck.disabled = false;
    document.getElementById('stopBtn').disabled = true;
    renderAll();
  }
}

function stopCheck() { stopRequested = true; }

function verdictOf(u) {
  if (!u.result) return u.blocked ? 'blocked' : 'unchecked';
  let v = u.result.verdict;
  if (u.blocked && ['valid', 'noindex', 'canonicalised'].includes(v)) v = 'blocked';
  return v;
}

function computeStats() {
  const counts = {};
  Object.keys(VERDICTS).forEach(k => counts[k] = 0);
  urls.forEach(u => counts[verdictOf(u)]++);
  const checked = urls.filter(u => u.result && u.result.verdict !== 'unverifiable');
  const valid = checked.filter(u => verdictOf(u) === 'valid').length;
  const remove = urls.filter(u => REMOVE_SET.includes(verdictOf(u))).length;
  return { counts, checkedCount: checked.length, valid, remove };
}

function renderAll(light) {
  if (!parseData) return;
  const st = computeStats();
  renderSummary(st);
  if (!light) { renderOverview(); renderChildren(); }
  renderChecklist(st);
  renderCharts(st);
  renderFilters(st);
  renderTable(false);
}

function renderSummary(st) {
  const d = parseData;
  const structure = d.structure_score;
  let score = structure, sub = `Structure ${structure}/100`;
  if (st.checkedCount) {
    const urlScore = st.valid / st.checkedCount * 100;
    score = Math.round(0.4 * structure + 0.6 * urlScore);
    sub += ` · URLs ${Math.round(urlScore)}% valid`;
  }
  document.getElementById('healthScoreText').textContent = score;
  document.getElementById('healthSub').textContent = sub;
  const ring = document.getElementById('healthRing');
  ring.style.strokeDashoffset = 264 - (score / 100) * 264;
  ring.style.stroke = score >= 90 ? '#6af7c8' : score >= 70 ? '#f7a26a' : '#f76a6a';
  document.getElementById('statUrls').textContent = d.meta.total_urls.toLocaleString();
  document.getElementById('statCritical').textContent = d.counts.critical;
  document.getElementById('statWarnings').textContent = d.counts.warning;
  const anyChecked = urls.some(u => u.result);
  document.getElementById('statValid').textContent = anyChecked ? `${st.valid}/${st.checkedCount}` : '–';
  document.getElementById('statRemove').textContent = anyChecked || st.counts.blocked ? st.remove : '–';
}

function fmtBytes(b) {
  if (b == null) return '–';
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
  return (b / 1048576).toFixed(2) + ' MB';
}

function renderOverview() {
  const d = parseData, m = d.meta;
  const kindLabel = { urlset: 'URL sitemap (<urlset>)', sitemapindex: `Sitemap index · ${m.children.length} child sitemaps`, list: 'Pasted URL list' }[m.kind] || 'Unknown / unreadable';
  let html = '<div class="chip-label">File</div><div class="chips">';
  html += `<span class="chip">${esc(kindLabel)}</span>`;
  html += `<span class="chip">${m.total_urls.toLocaleString()} URLs</span>`;
  if (m.bytes != null) html += `<span class="chip ${m.bytes > 52428800 ? 'no' : ''}">${fmtBytes(m.bytes)} uncompressed</span>`;
  if (m.gzip) html += '<span class="chip ok">gzip compressed</span>';
  if (m.final_url && m.sitemap_url && m.final_url !== m.sitemap_url) html += `<span class="chip mid">redirects → ${esc(m.final_url)}</span>`;
  html += '</div>';

  const r = d.robots;
  if (r) {
    html += '<div class="chip-label">robots.txt</div><div class="chips">';
    html += r.found ? `<span class="chip ok">found · ${esc(r.url)}</span>` : `<span class="chip no">not found · ${esc(r.url)}</span>`;
    if (r.declared === true) html += '<span class="chip ok">✓ this sitemap is declared</span>';
    if (r.declared === false) html += '<span class="chip no">✗ this sitemap isn\'t declared</span>';
    if (r.found && r.declared_sitemaps.length) html += `<span class="chip">${r.declared_sitemaps.length} Sitemap: line(s)</span>`;
    const blocked = urls.filter(u => u.blocked).length;
    if (r.found) html += `<span class="chip ${blocked ? 'no' : 'ok'}">${blocked ? blocked + ' URL(s) blocked for Googlebot' : 'No listed URLs blocked for Googlebot'}</span>`;
    html += '</div>';
    if (r.found && r.ai && r.ai.length) {
      html += '<div class="chip-label">AI crawler access (homepage)</div><div class="chips">';
      r.ai.forEach(a => { html += `<span class="chip ${a.allowed ? 'ok' : 'mid'}">${a.allowed ? '✓' : '✗'} ${esc(a.bot)}</span>`; });
      html += '</div>';
    }
  }
  document.getElementById('overview').innerHTML = html;
}

function renderChildren() {
  const m = parseData.meta;
  const card = document.getElementById('childCard');
  if (m.kind !== 'sitemapindex') { card.style.display = 'none'; return; }
  card.style.display = 'block';
  const loaded = new Set(urls.map(u => u.source));
  document.getElementById('childSub').textContent = `${m.children.filter(c => !c.skipped).length} analysed · up to 1,000 URLs per child loaded for status checks`;
  document.getElementById('childBody').innerHTML = m.children.map(c => {
    const ok = !c.error && c.url_count;
    const statusHtml = c.error ? `<span class="badge ${c.skipped ? 'info' : 'critical'}">${esc(c.error)}</span>` : `<span class="badge pass">${c.status} OK${c.gzip ? ' · gz' : ''}</span>`;
    const canCheck = loaded.has(c.loc);
    return `<tr>
      <td><input type="checkbox" class="child-check" value="${esc(c.loc)}" ${canCheck ? 'checked' : 'disabled'}></td>
      <td class="url-cell"><a href="${safeHref(c.loc)}" target="_blank" rel="noopener">${esc(c.loc)}</a></td>
      <td>${c.url_count == null ? '–' : c.url_count.toLocaleString()}</td>
      <td>${esc(c.lastmod || '–')}</td>
      <td>${statusHtml}</td></tr>`;
  }).join('');
}

function toggleAllChildren(on) {
  document.querySelectorAll('.child-check:not(:disabled)').forEach(cb => cb.checked = on);
}

function checkItemHtml(sev, sevLabel, title, count, detail, fix, examples) {
  let h = `<div class="check-item"><span class="badge ${sev}">${sevLabel}</span><div class="check-body">`;
  h += `<div class="check-title">${esc(title)}${count ? `<span class="check-count">× ${count.toLocaleString()}</span>` : ''}</div>`;
  if (detail) h += `<div class="check-detail">${esc(detail)}</div>`;
  if (fix) h += `<div class="check-fix">${esc(fix)}</div>`;
  if (examples && examples.length) h += `<details><summary>Examples</summary><ul>${examples.map(e => `<li>${esc(e)}</li>`).join('')}</ul></details>`;
  return h + '</div></div>';
}

function renderChecklist(st) {
  const d = parseData;
  // URL status findings (from the bulk check)
  let uf = '';
  const anyChecked = urls.some(u => u.result);
  if (anyChecked) {
    const items = REMOVE_SET.filter(k => k !== 'blocked' && st.counts[k] > 0).map(k => {
      const ex = urls.filter(u => verdictOf(u) === k).slice(0, 5).map(u => u.loc + (u.result && u.result.final_url && k === 'redirect' ? '  →  ' + u.result.final_url : '') + (u.result && u.result.canonical && k === 'canonicalised' ? '  →  canonical: ' + u.result.canonical : ''));
      return checkItemHtml(['client_error', 'server_error', 'error'].includes(k) ? 'critical' : 'warning', ['client_error', 'server_error', 'error'].includes(k) ? 'Critical' : 'Warning', FIX_TEXT[k][0], st.counts[k], null, FIX_TEXT[k][1], ex);
    });
    uf = '<div class="group-label">URL status findings</div>' + (items.length ? items.join('') : checkItemHtml('pass', 'Pass', `All ${st.checkedCount} checked URLs return 200, are indexable and self-canonical`, 0, null, '', []));
    uf += '<div class="group-label" style="margin-top:1rem;">Sitemap checks</div>';
  }
  document.getElementById('urlFindings').innerHTML = uf;

  const failing = d.checklist.filter(c => c.severity !== 'pass');
  const passing = d.checklist.filter(c => c.severity === 'pass');
  const labels = { critical: 'Critical', warning: 'Warning', info: 'Info', pass: 'Pass' };
  let html = failing.map(c => checkItemHtml(c.severity, labels[c.severity], c.title, c.count, c.detail, c.fix, c.examples)).join('');
  if (!failing.length) html += checkItemHtml('pass', 'Pass', 'No structural problems found', 0, null, '', []);
  if (passing.length) {
    html += `<details class="pass-toggle"><summary class="check-item" style="border:none;color:var(--accent3);font-family:'DM Mono',monospace;font-size:0.76rem;cursor:pointer;">✓ ${passing.length} checks passed — show</summary>`;
    html += passing.map(c => checkItemHtml('pass', 'Pass', c.title, 0, null, '', [])).join('') + '</details>';
  }
  document.getElementById('checklist').innerHTML = html;
  document.getElementById('checkSub').textContent = `${d.counts.critical} critical · ${d.counts.warning} warnings · ${d.counts.info} info · ${d.counts.pass} passed`;
}

function renderCharts(st) {
  const font = { family: 'Inter', size: 11 };
  const legend = { position: 'bottom', labels: { color: '#c8c8e0', font, padding: 8, boxWidth: 10 } };
  const keys = Object.keys(VERDICTS).filter(k => st.counts[k] > 0);
  const vData = { labels: keys.map(k => VERDICTS[k].label), datasets: [{ data: keys.map(k => st.counts[k]), backgroundColor: keys.map(k => VERDICTS[k].color), borderWidth: 0 }] };
  if (charts.verdict) { charts.verdict.data = vData; charts.verdict.update('none'); }
  else charts.verdict = new Chart(document.getElementById('verdictChart'), { type: 'doughnut', data: vData, options: { maintainAspectRatio: false, cutout: '62%', plugins: { legend } } });

  const codeCounts = {};
  urls.forEach(u => { if (!u.result) return; const k = u.result.status_code != null ? String(u.result.status_code) : (u.result.error || 'Error'); codeCounts[k] = (codeCounts[k] || 0) + 1; });
  const codes = Object.entries(codeCounts).sort((a, b) => b[1] - a[1]).slice(0, 8);
  const colorFor = c => /^2/.test(c) ? '#6af7c8' : /^3/.test(c) ? '#f7a26a' : /^4/.test(c) ? '#f76a6a' : /^5/.test(c) ? '#c94a4a' : '#8888a8';
  const cData = { labels: codes.length ? codes.map(c => c[0]) : ['Not checked yet'], datasets: [{ data: codes.length ? codes.map(c => c[1]) : [0], backgroundColor: codes.map(c => colorFor(c[0])), borderRadius: 4 }] };
  const barOpts = { maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { ticks: { color: '#c8c8e0', font }, grid: { display: false } }, y: { beginAtZero: true, ticks: { color: '#c8c8e0', font, precision: 0 }, grid: { color: 'rgba(255,255,255,0.05)' } } } };
  if (charts.codes) { charts.codes.data = cData; charts.codes.update('none'); }
  else charts.codes = new Chart(document.getElementById('codesChart'), { type: 'bar', data: cData, options: barOpts });

  const b = parseData.lastmod_buckets || {};
  const lLabels = Object.keys(b);
  const lData = { labels: lLabels.length ? lLabels : ['n/a for URL lists'], datasets: [{ data: lLabels.map(k => b[k]), backgroundColor: ['#6af7c8', '#7c6af7', '#f7a26a', '#f76a6a', '#3a3a52'], borderRadius: 4 }] };
  if (charts.lastmod) { charts.lastmod.data = lData; charts.lastmod.update('none'); }
  else charts.lastmod = new Chart(document.getElementById('lastmodChart'), { type: 'bar', data: lData, options: barOpts });
}

function renderFilters(st) {
  const row = document.getElementById('filterRow');
  const keys = ['all', ...(st.remove ? ['remove'] : []), ...Object.keys(VERDICTS).filter(k => st.counts[k] > 0)];
  if (!keys.includes(currentFilter)) currentFilter = 'all';
  row.innerHTML = keys.map(k => {
    const label = k === 'all' ? `All (${urls.length})` : k === 'remove' ? `⚠ Needs fixing (${st.remove})` : `${VERDICTS[k].label} (${st.counts[k]})`;
    return `<button class="filter-btn ${k === currentFilter ? 'active' : ''}" onclick="setFilter('${k}')">${esc(label)}</button>`;
  }).join('');
  const validN = st.counts.valid;
  document.getElementById('exportNote').textContent = urls.some(u => u.result)
    ? `Clean sitemap = the unique checked URL(s) that return 200, are indexable, self-canonical and not blocked.`
    : 'Run the status check to unlock the clean sitemap download.';
  document.getElementById('cleanBtn').disabled = !validN;
}

function setFilter(k) { currentFilter = k; shown = 200; renderAll(true); }
function showMore() { shown += 200; renderTable(false); }

function filteredUrls() {
  const q = document.getElementById('searchInput').value.trim().toLowerCase();
  const match = u => currentFilter === 'all' || (currentFilter === 'remove' ? REMOVE_SET.includes(verdictOf(u)) : verdictOf(u) === currentFilter);
  return urls.filter(u => match(u) && (!q || u.loc.toLowerCase().includes(q)));
}

function flashScroll(id) {
  const el = document.getElementById(id);
  el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash');
}
function jumpToTable(filter) {
  currentFilter = filter; shown = 200;
  document.getElementById('searchInput').value = '';
  renderAll(true);
  flashScroll('tableCard');
}
function jumpToReport() { flashScroll('reportCard'); }

function renderTable(resetPaging) {
  if (resetPaging) shown = 200;
  const list = filteredUrls();
  const multiSource = parseData.meta.kind === 'sitemapindex';
  const rows = list.slice(0, shown).map(u => {
    const v = verdictOf(u); const meta = VERDICTS[v]; const r = u.result;
    const statusTxt = !r ? '–' : (r.status_code != null ? r.status_code : esc(r.error || 'Error'));
    let finalCell = '–';
    if (r && r.redirect_chain && r.redirect_chain.length) {
      finalCell = `<a href="${safeHref(r.final_url)}" target="_blank" rel="noopener">${esc(r.final_url)}</a><div class="small-muted">${r.redirect_chain.map(h => `${h.status_code} → ${esc(h.url)}`).join('<br>')}</div>`;
    } else if (r && r.error) {
      finalCell = `<span class="small-muted">${esc(r.error)}</span>`;
    }
    let idx = [];
    if (u.blocked) idx.push('<span style="color:#f7d46a">Blocked by robots.txt</span>');
    if (r && r.noindex) idx.push(`<span style="color:#b58cff">noindex (${esc(r.noindex)})</span>`);
    if (r && r.canonical) idx.push(r.canonical_match ? '<span style="color:var(--accent3)">Self-canonical</span>' : `<span style="color:#a99cf9">Canonical → ${esc(r.canonical)}</span>`);
    if (r && !r.canonical && r.status_code === 200) idx.push('<span class="small-muted">No canonical tag</span>');
    return `<tr>
      <td class="url-cell"><a href="${safeHref(u.loc)}" target="_blank" rel="noopener">${esc(u.loc)}</a>${multiSource && u.source ? `<div class="small-muted">in ${esc(u.source.split('/').pop())}</div>` : ''}</td>
      <td>${statusTxt}${r && r.response_ms != null ? `<div class="small-muted">${r.response_ms} ms</div>` : ''}</td>
      <td><span class="vbadge" style="background:${meta.color}22;color:${v === 'unchecked' ? '#8888a8' : meta.color};border:1px solid ${meta.color}55">${meta.label}</span></td>
      <td class="url-cell">${finalCell}</td>
      <td class="url-cell">${idx.join('<br>') || '–'}</td>
      <td style="white-space:nowrap">${esc(u.lastmod || '–')}</td></tr>`;
  });
  document.getElementById('resultsBody').innerHTML = rows.join('');
  document.getElementById('emptyState').style.display = list.length ? 'none' : 'block';
  document.getElementById('moreRow').style.display = list.length > shown ? 'block' : 'none';
}

function download(content, name, type) {
  const blob = new Blob([content], { type });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = name;
  document.body.appendChild(link); link.click(); link.remove();
}

function exportCsv() {
  if (!urls.length) return;
  const head = ['URL', 'Source Sitemap', 'Lastmod', 'Status Code', 'Verdict', 'Final URL', 'Redirect Chain', 'Noindex', 'Canonical', 'Self-Canonical', 'Blocked by robots.txt', 'Response ms', 'Error'];
  const rows = [head];
  urls.forEach(u => {
    const r = u.result || {};
    rows.push([u.loc, u.source || '', u.lastmod || '', r.status_code ?? '', VERDICTS[verdictOf(u)].label, r.final_url || '',
      (r.redirect_chain || []).map(h => `${h.status_code} ${h.url}`).join(' -> '), r.noindex || '', r.canonical || '',
      r.canonical == null ? '' : (r.canonical_match ? 'Yes' : 'No'), u.blocked ? 'Yes' : 'No', r.response_ms ?? '', r.error || '']);
  });
  const csv = rows.map(row => row.map(c => `"${String(c).replace(/"/g, '""')}"`).join(',')).join('\n');
  download('﻿' + csv, 'sitemap-validation-report.csv', 'text/csv;charset=utf-8');
}

function xmlEsc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&apos;'); }

function exportCleanSitemap() {
  const seen = new Set();
  const valid = urls.filter(u => verdictOf(u) === 'valid' && !seen.has(u.loc) && seen.add(u.loc));
  if (!valid.length) return;
  const w3c = /^\d{4}(-\d{2}(-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2}))?)?)?$/;
  let xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n';
  valid.forEach(u => {
    xml += `  <url>\n    <loc>${xmlEsc(u.loc)}</loc>\n`;
    if (u.lastmod && w3c.test(u.lastmod)) xml += `    <lastmod>${xmlEsc(u.lastmod)}</lastmod>\n`;
    xml += '  </url>\n';
  });
  xml += '</urlset>\n';
  download(xml, 'clean-sitemap.xml', 'application/xml');
}

setMode('url');
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(debug=True, port=5062)
