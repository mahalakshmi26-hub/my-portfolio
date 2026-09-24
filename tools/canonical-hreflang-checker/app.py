"""
Canonical & Hreflang Checker  -  Flask app
Checks a page's canonical setup (HTML tag + HTTP Link header), validates every
hreflang annotation (HTML, HTTP header and optionally the XML sitemap), fetches
each language version to confirm return links / status / canonical, draws a
return-link matrix, flags canonical-vs-hreflang conflicts, and writes out a
corrected set of tags you can copy straight into the page <head>.

Built by Mahalakshmi Marimuthu - Digital Marketing Strategist & AI-Powered SEO Expert
"""
import gzip
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template_string, request

app = Flask(__name__)

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
REQUEST_TIMEOUT = 10
MAX_ALTERNATES = 30            # language versions fetched per check
MAX_WORKERS = 6                # parallel fetches (kept low so WAFs don't block us)
MAX_HTML_BYTES = 4 * 1024 * 1024
MAX_SITEMAP_BYTES = 15 * 1024 * 1024
MAX_CHILD_SITEMAPS = 10        # when a sitemap index is given
RETRY_STATUSES = {403, 429, 503}
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
UNVERIFIABLE_DOMAINS = {"facebook.com", "instagram.com", "x.com", "twitter.com", "linkedin.com",
                        "tiktok.com", "threads.net", "bit.ly", "tinyurl.com", "t.co"}

# --------------------------------------------------------------------------- #
# Language / region codes (ISO 639-1 and ISO 3166-1 alpha-2 - what Google accepts)
# --------------------------------------------------------------------------- #
ISO_639_1 = set("""aa ab ae af ak am an ar as av ay az ba be bg bh bi bm bn bo br bs ca ce ch co cr cs cu
cv cy da de dv dz ee el en eo es et eu fa ff fi fj fo fr fy ga gd gl gn gu gv ha he hi ho hr ht hu hy hz ia
id ie ig ii ik io is it iu ja jv ka kg ki kj kk kl km kn ko kr ks ku kv kw ky la lb lg li ln lo lt lu lv mg
mh mi mk ml mn mr ms mt my na nb nd ne ng nl nn no nr nv ny oc oj om or os pa pi pl ps pt qu rm rn ro ru rw
sa sc sd se sg si sk sl sm sn so sq sr ss st su sv sw ta te tg th ti tk tl tn to tr ts tt tw ty ug uk ur uz
ve vi vo wa wo xh yi yo za zh zu""".split())

ISO_3166 = set("""AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR
BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER
ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL
IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD
ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF
PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX
SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA
ZM ZW""".split())

LANG_NAMES = {
    "en": "English", "hi": "Hindi", "ta": "Tamil", "te": "Telugu", "kn": "Kannada", "ml": "Malayalam",
    "mr": "Marathi", "bn": "Bengali", "gu": "Gujarati", "pa": "Punjabi", "or": "Odia", "ur": "Urdu",
    "as": "Assamese", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian", "pt": "Portuguese",
    "nl": "Dutch", "ru": "Russian", "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "ar": "Arabic",
    "tr": "Turkish", "pl": "Polish", "sv": "Swedish", "da": "Danish", "no": "Norwegian",
    "nb": "Norwegian Bokmål", "fi": "Finnish", "el": "Greek", "cs": "Czech", "hu": "Hungarian",
    "ro": "Romanian", "uk": "Ukrainian", "he": "Hebrew", "id": "Indonesian", "ms": "Malay", "th": "Thai",
    "vi": "Vietnamese", "fa": "Persian", "tl": "Tagalog", "sk": "Slovak", "bg": "Bulgarian",
    "hr": "Croatian", "sr": "Serbian", "sl": "Slovenian", "lt": "Lithuanian", "lv": "Latvian",
    "et": "Estonian", "ne": "Nepali", "si": "Sinhala", "sw": "Swahili", "af": "Afrikaans", "ca": "Catalan",
    "yi": "Yiddish", "ka": "Georgian", "jv": "Javanese", "kr": "Kanuri", "se": "Northern Sami", "ee": "Ewe",
}
REGION_NAMES = {
    "IN": "India", "US": "United States", "GB": "United Kingdom", "CA": "Canada", "AU": "Australia",
    "NZ": "New Zealand", "IE": "Ireland", "SG": "Singapore", "AE": "UAE", "SA": "Saudi Arabia",
    "DE": "Germany", "FR": "France", "ES": "Spain", "MX": "Mexico", "AR": "Argentina", "BR": "Brazil",
    "PT": "Portugal", "IT": "Italy", "NL": "Netherlands", "BE": "Belgium", "CH": "Switzerland",
    "AT": "Austria", "JP": "Japan", "CN": "China", "HK": "Hong Kong", "TW": "Taiwan", "KR": "South Korea",
    "RU": "Russia", "ZA": "South Africa", "NG": "Nigeria", "KE": "Kenya", "PK": "Pakistan",
    "BD": "Bangladesh", "LK": "Sri Lanka", "NP": "Nepal", "MY": "Malaysia", "ID": "Indonesia",
    "PH": "Philippines", "TH": "Thailand", "VN": "Vietnam", "SE": "Sweden", "NO": "Norway",
    "DK": "Denmark", "FI": "Finland", "PL": "Poland", "TR": "Turkey", "IL": "Israel", "EG": "Egypt",
    "QA": "Qatar", "KW": "Kuwait", "CL": "Chile", "CO": "Colombia", "PE": "Peru", "LA": "Laos",
}
# Codes that are NOT valid languages but are common mistakes -> what was probably meant
LANG_INVALID = {"jp": "ja", "cn": "zh", "dk": "da", "gr": "el", "cz": "cs", "ua": "uk", "iw": "he",
                "ji": "yi", "in": "id", "sp": "es", "ge": "ka", "vn": "vi",
                "ir": "fa", "jw": "jv", "mo": "ro", "sh": "sr"}
# Codes that ARE valid languages but are almost always a typo for another one
LANG_SUSPECT = {"kr": "ko", "se": "sv", "ee": "et"}


def lang_name(code):
    return LANG_NAMES.get(code, code)


def validate_hreflang(raw):
    """Return a dict describing whether an hreflang value is valid for Google."""
    code = (raw or "").strip()
    out = {"raw": code, "severity": None, "problems": [], "fix": "", "label": "", "lang": "",
           "region": "", "suggested": None}

    def problem(sev, msg, fix=""):
        out["problems"].append(msg)
        if fix and not out["fix"]:
            out["fix"] = fix
        if sev == "error" or out["severity"] is None:
            out["severity"] = sev

    if not code:
        problem("error", "Empty hreflang value.", "Give every alternate link a code such as en or en-IN.")
        return out
    if code.lower() == "x-default":
        out.update(label="Default / fallback", lang="x-default", suggested="x-default")
        return out
    if "_" in code:
        fixed = validate_hreflang(code.replace("_", "-"))
        problem("error", f'"{code}" uses an underscore - hreflang needs a hyphen.',
                f'Use "{fixed["suggested"] or code.replace("_", "-")}".')
        out.update(lang=fixed["lang"], region=fixed["region"], label=fixed["label"], suggested=fixed["suggested"])
        return out

    parts = code.split("-")
    lang = parts[0].lower()
    rest = parts[1:]
    script = ""
    if rest and len(rest[0]) == 4 and rest[0].isalpha():
        script = rest[0].title()
        rest = rest[1:]
    region = rest[0].upper() if rest else ""
    extra = rest[1:] if rest else []
    out["lang"], out["region"] = lang, region
    s_lang, s_region = lang, region

    # ---- language part
    if lang.upper() in ISO_3166 and lang not in ISO_639_1 and not region and (lang not in LANG_INVALID or lang == "in"):
        problem("error", f'"{code}" is a country code on its own - hreflang must start with a language.',
                f'Put the language first, e.g. "en-{lang.upper()}".')
        s_lang, s_region = None, None
    elif lang in LANG_INVALID:
        good = LANG_INVALID[lang]
        if lang == "in":
            problem("error", '"in" is not a language code (IN is the country code for India).',
                    'For India put the language first: "en-IN", "hi-IN", "ta-IN" …')
            s_lang = None
        else:
            problem("error", f'"{lang}" is not a valid ISO 639-1 language code.',
                    f'Use "{good}" ({lang_name(good)}).')
            s_lang = good
    elif lang not in ISO_639_1:
        problem("error", f'"{lang}" is not a valid ISO 639-1 language code.',
                "Use a two-letter language code such as en, hi, ta, es.")
        s_lang = None
    elif lang in LANG_SUSPECT:
        good = LANG_SUSPECT[lang]
        problem("warn", f'"{lang}" is {lang_name(lang)} - did you mean "{good}" ({lang_name(good)})?',
                f'If you meant {lang_name(good)}, use "{good}".')
        s_lang = good

    # ---- region part
    if region:
        if region == "UK":
            problem("error", '"UK" is not an ISO 3166-1 country code.', 'Use "GB" for the United Kingdom, e.g. "en-GB".')
            s_region = "GB"
        elif region == "EU" or region.isdigit():
            problem("error", f'Google does not support the region "{region}" - only ISO 3166-1 alpha-2 country codes.',
                    "Target a specific country (e.g. es-ES, es-MX) or use the language on its own (e.g. es).")
            s_region = ""
        elif region not in ISO_3166:
            problem("error", f'"{region}" is not a valid ISO 3166-1 country code.',
                    "Use a two-letter country code such as IN, US, GB, AE.")
            s_region = None
        elif region == "LA" and lang == "es":
            problem("warn", '"LA" is the code for Laos, not Latin America.',
                    "Google has no Latin America region - list each country (es-MX, es-AR …) or use plain \"es\".")
    if extra:
        problem("error", f'"{code}" has too many parts.', "Use language or language-REGION only, e.g. en or en-IN.")
        s_region = None if s_region is None else s_region

    if s_lang is not None and s_region is not None:
        out["suggested"] = s_lang + (f"-{script}" if script else "") + (f"-{s_region}" if s_region else "")
    out["label"] = lang_name(lang) + (f" ({script})" if script else "") + (
        f" — {REGION_NAMES.get(region, region)}" if region else "")
    return out


# --------------------------------------------------------------------------- #
# URL helpers
# --------------------------------------------------------------------------- #
def normalize_input(raw):
    raw = (raw or "").strip()
    if raw and not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    return raw


def norm_url(u):
    """Comparable form of a URL: lowercase scheme/host, no default port, no fragment."""
    if not u:
        return ""
    try:
        p = urlparse(u.strip())
    except ValueError:
        return u.strip()
    scheme, netloc = p.scheme.lower(), p.netloc.lower()
    if (scheme == "http" and netloc.endswith(":80")) or (scheme == "https" and netloc.endswith(":443")):
        netloc = netloc.rsplit(":", 1)[0]
    return urlunparse((scheme, netloc, p.path or "/", p.params, p.query, ""))


def is_absolute(href):
    return bool(re.match(r"^https?://", href or "", re.I))


def host_of(u):
    try:
        return urlparse(u).netloc.lower().split(":")[0]
    except ValueError:
        return ""


def why_different(a, b):
    """Plain-English reason two URLs differ (a = page, b = canonical)."""
    pa, pb = urlparse(a), urlparse(b)
    reasons = []
    if pa.scheme.lower() != pb.scheme.lower():
        reasons.append(f"protocol ({pa.scheme} vs {pb.scheme})")
    ha, hb = pa.netloc.lower(), pb.netloc.lower()
    if ha != hb:
        if ha.replace("www.", "", 1) == hb.replace("www.", "", 1):
            reasons.append("www vs non-www")
        else:
            reasons.append("a different domain")
    if pa.path != pb.path:
        if pa.path.rstrip("/") == pb.path.rstrip("/"):
            reasons.append("trailing slash")
        elif pa.path.lower() == pb.path.lower():
            reasons.append("upper/lower case in the path")
        else:
            reasons.append("a different path")
    if pa.query != pb.query:
        reasons.append("query string")
    return reasons


def unverifiable(u):
    h = host_of(u).replace("www.", "", 1)
    return any(h == d or h.endswith("." + d) for d in UNVERIFIABLE_DOMAINS)


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def fetch(url, max_bytes=MAX_HTML_BYTES, retries=1):
    headers = {"User-Agent": BROWSER_UA,
               "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
               "Accept-Language": "en-US,en;q=0.9"}
    base = {"url": url, "final_url": url, "status": None, "content": b"", "content_type": "",
            "link_header": "", "x_robots": "", "chain": [], "error": None, "blocked": False}
    for attempt in range(retries + 1):
        res = dict(base)
        try:
            r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True, stream=True)
            chunks, total = [], 0
            for chunk in r.iter_content(16384):
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    break
            r.close()
            res.update(final_url=r.url, status=r.status_code, content=b"".join(chunks),
                       content_type=r.headers.get("Content-Type", "").lower(),
                       link_header=r.headers.get("Link", ""), x_robots=r.headers.get("X-Robots-Tag", ""),
                       chain=[{"url": h.url, "status": h.status_code} for h in r.history])
            if r.status_code in RETRY_STATUSES and attempt < retries:
                time.sleep(1.5)
                continue
            res["blocked"] = r.status_code in RETRY_STATUSES
            return res
        except requests.exceptions.Timeout:
            res["error"] = "Timed out"
        except requests.exceptions.SSLError:
            res["error"] = "SSL certificate error"
        except requests.exceptions.ConnectionError:
            res["error"] = "Could not connect"
        except requests.exceptions.RequestException as e:
            res["error"] = f"Request failed ({e.__class__.__name__})"
        if attempt >= retries:
            return res
        time.sleep(1)
    return res


def parse_link_header(value, base):
    out = []
    for part in re.split(r",\s*(?=<)", value or ""):
        m = re.match(r"\s*<([^>]*)>\s*(.*)", part, re.S)
        if not m:
            continue
        raw_href, params = m.group(1).strip(), {}
        for pm in re.finditer(r';\s*([A-Za-z\-]+)\s*=\s*(?:"([^"]*)"|([^;,\s]*))', m.group(2)):
            params[pm.group(1).lower()] = (pm.group(2) if pm.group(2) is not None else pm.group(3) or "").strip()
        out.append({"raw": raw_href, "href": urljoin(base, raw_href), "rel": params.get("rel", "").lower().split(),
                    "hreflang": params.get("hreflang")})
    return out


def parse_page(res):
    """Pull canonical / hreflang / robots / lang signals out of one fetched response."""
    final = res["final_url"]
    info = {"canon_html": [], "canon_header": [], "alts_html": [], "alts_header": [], "noindex": False,
            "robots": "", "html_lang": "", "title": "", "is_html": "html" in res["content_type"] or not res["content_type"]}

    for l in parse_link_header(res["link_header"], final):
        if "canonical" in l["rel"]:
            info["canon_header"].append({"raw": l["raw"], "href": l["href"], "relative": not is_absolute(l["raw"]),
                                         "where": "HTTP header"})
        if "alternate" in l["rel"] and l["hreflang"] is not None:
            info["alts_header"].append({"code": l["hreflang"], "raw": l["raw"], "href": l["href"],
                                        "relative": not is_absolute(l["raw"]), "where": "HTTP header",
                                        "source": "HTTP header"})
    robots_bits = [res["x_robots"]] if res["x_robots"] else []

    if info["is_html"] and res["content"]:
        soup = BeautifulSoup(res["content"], "html.parser")
        base_url = final
        base_tag = soup.find("base", href=True)
        if base_tag:
            base_url = urljoin(final, base_tag["href"].strip())
        head, body = soup.head, soup.body

        def where(tag):
            if head is not None and head in tag.parents:
                return "head"
            if body is not None and body in tag.parents:
                return "body"
            return "head"

        for link in soup.find_all("link"):
            rels = link.get("rel") or []
            rels = [r.lower() for r in (rels if isinstance(rels, list) else rels.split())]
            href = (link.get("href") or "").strip()
            if "canonical" in rels:
                info["canon_html"].append({"raw": href, "href": urljoin(base_url, href) if href else "",
                                           "relative": bool(href) and not is_absolute(href), "where": where(link)})
            if "alternate" in rels and link.get("hreflang") is not None:
                info["alts_html"].append({"code": link.get("hreflang").strip(), "raw": href,
                                          "href": urljoin(base_url, href) if href else "",
                                          "relative": bool(href) and not is_absolute(href),
                                          "where": where(link), "source": "HTML"})
        for m in soup.find_all("meta", attrs={"name": re.compile(r"^(robots|googlebot)$", re.I)}):
            if m.get("content"):
                robots_bits.append(m["content"])
        if soup.html and soup.html.get("lang"):
            info["html_lang"] = soup.html["lang"].strip()
        if soup.title and soup.title.string:
            info["title"] = soup.title.string.strip()[:140]

    info["robots"] = " | ".join(robots_bits)
    info["noindex"] = bool(re.search(r"\b(noindex|none)\b", info["robots"], re.I))
    return info


def sitemap_alternates(sitemap_url, page_urls):
    """Find the page's <url> entry in a sitemap (or sitemap index) and read its xhtml:link hreflang."""
    out = {"url": sitemap_url, "ok": False, "found_page": False, "error": None, "alternates": [], "scanned": 0}
    targets = {norm_url(u) for u in page_urls if u}
    queue = [sitemap_url]
    while queue and out["scanned"] <= MAX_CHILD_SITEMAPS:
        sm = queue.pop(0)
        res = fetch(sm, max_bytes=MAX_SITEMAP_BYTES)
        out["scanned"] += 1
        if res["error"] or (res["status"] or 0) >= 400:
            if out["scanned"] == 1:
                out["error"] = f"Sitemap could not be fetched ({res['error'] or 'HTTP ' + str(res['status'])})."
                return out
            continue
        data = res["content"]
        if data[:2] == b"\x1f\x8b":
            try:
                data = gzip.decompress(data)
            except OSError:
                continue
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            if out["scanned"] == 1:
                out["error"] = "The sitemap is not valid XML."
                return out
            continue
        out["ok"] = True
        if root.tag.split("}")[-1] == "sitemapindex":
            for el in root.iter():
                if el.tag.split("}")[-1] == "loc" and el.text:
                    queue.append(el.text.strip())
            continue
        for url_el in root:
            if url_el.tag.split("}")[-1] != "url":
                continue
            loc = next((c.text.strip() for c in url_el if c.tag.split("}")[-1] == "loc" and c.text), "")
            if norm_url(loc) in targets:
                out["found_page"] = True
                for c in url_el:
                    if c.tag.split("}")[-1] == "link" and (c.get("rel") or "").lower() == "alternate" \
                            and c.get("hreflang") is not None:
                        href = (c.get("href") or "").strip()
                        out["alternates"].append({"code": c.get("hreflang").strip(), "raw": href, "href": href,
                                                  "relative": bool(href) and not is_absolute(href),
                                                  "where": "sitemap", "source": "Sitemap"})
                return out
    return out


def inspect_url(url):
    """Fetch another URL in the cluster (alternate or canonical target) and summarise it."""
    info = {"url": url, "final_url": url, "status": None, "error": None, "blocked": False, "fetched": False,
            "redirected": False, "noindex": False, "canonical": "", "hreflang": [], "html_lang": "",
            "unverifiable": False}
    if unverifiable(url):
        info["unverifiable"] = True
        info["error"] = "Site blocks automated checks"
        return info
    res = fetch(url)
    info.update(final_url=res["final_url"], status=res["status"], error=res["error"], blocked=res["blocked"])
    info["redirected"] = bool(res["chain"]) and norm_url(res["final_url"]) != norm_url(url)
    if res["blocked"] or (res["error"] and res["status"] is None and res["error"] == "Timed out"):
        info["unverifiable"] = True
    if res["status"] and res["status"] < 400:
        info["fetched"] = True
        p = parse_page(res)
        info["noindex"] = p["noindex"]
        canon = next((c for c in p["canon_html"] if c["where"] == "head" and c["href"]), None) \
            or next((c for c in p["canon_header"] if c["href"]), None)
        info["canonical"] = canon["href"] if canon else ""
        info["hreflang"] = [{"code": a["code"], "href": a["href"]} for a in p["alts_html"] + p["alts_header"]]
        info["html_lang"] = p["html_lang"]
    return info


# --------------------------------------------------------------------------- #
# Main analysis
# --------------------------------------------------------------------------- #
def analyze(raw_url, sitemap_raw="", check_alts=True):
    url = normalize_input(raw_url)
    issues = []

    def issue(area, severity, message, fix=""):
        issues.append({"area": area, "severity": severity, "message": message, "fix": fix})

    view = {"input_url": raw_url, "url": url, "fetch_ok": False, "fetch_error": None}
    res = fetch(url)
    view.update(final_url=res["final_url"], status=res["status"], chain=res["chain"])
    if res["error"] or (res["status"] or 0) >= 400:
        view["fetch_error"] = res["error"] or f"The page returned HTTP {res['status']}."
        if res["blocked"]:
            view["fetch_error"] += " The site's bot protection blocked the request - try again in a minute."
        return view
    view["fetch_ok"] = True
    final = res["final_url"]
    self_n = norm_url(final)
    page = parse_page(res)
    view.update(title=page["title"], html_lang=page["html_lang"], robots=page["robots"], noindex=page["noindex"],
                is_html=page["is_html"])

    if res["chain"] and norm_url(url) != self_n:
        issue("Canonical", "info", f"The URL you entered redirected to {final}. Everything below is for that final URL.",
              "Use the final URL in internal links, canonicals, hreflang and sitemaps.")

    # ------------------------------------------------------------- canonical
    canon_all = page["canon_html"] + page["canon_header"]
    canon_rows = [dict(c) for c in canon_all]
    head_canon = [c for c in page["canon_html"] if c["where"] == "head"]
    body_canon = [c for c in page["canon_html"] if c["where"] == "body"]
    effective = None
    if head_canon:
        effective = head_canon[0]["href"]
    elif page["canon_header"]:
        effective = page["canon_header"][0]["href"]

    if not canon_all:
        issue("Canonical", "warn", "No canonical tag found (not in the HTML and not in the HTTP header).",
              f'Add <link rel="canonical" href="{final}"> inside <head>. A self-referencing canonical stops '
              "tracking parameters, http/www variants and sort orders from being treated as duplicates.")
    for c in canon_all:
        if not c["href"]:
            issue("Canonical", "error", "A canonical tag has an empty href.", "Give it the full URL of the preferred page.")
    if body_canon:
        issue("Canonical", "error", "A canonical tag sits in the <body>. Google ignores canonicals outside <head>.",
              "Move the canonical link into the <head> section.")
    distinct_html = {norm_url(c["href"]) for c in head_canon if c["href"]}
    if len(head_canon) > 1:
        if len(distinct_html) > 1:
            issue("Canonical", "error", f"{len(head_canon)} canonical tags point to different URLs. "
                  "When they disagree, Google may ignore all of them.",
                  "Keep exactly one canonical tag. Check your CMS/SEO plugin and theme for a duplicate.")
        else:
            issue("Canonical", "warn", f"The canonical tag appears {len(head_canon)} times (same URL).",
                  "Remove the duplicates so only one canonical tag is output.")
    if head_canon and page["canon_header"]:
        if norm_url(head_canon[0]["href"]) != norm_url(page["canon_header"][0]["href"]):
            issue("Canonical", "error", "The HTML canonical and the HTTP Link header canonical point to different URLs.",
                  "Make both point to the same URL, or remove one of them.")
    for c in canon_all:
        if c["relative"]:
            issue("Canonical", "warn", f'The canonical uses a relative URL ("{c["raw"]}").',
                  f'Use the full absolute URL, e.g. "{c["href"]}". Relative canonicals break easily on staging or http copies.')
            break

    canon_target = None
    view["canonical"] = effective or ""
    view["canonical_self"] = bool(effective) and norm_url(effective) == self_n
    if effective and not view["canonical_self"]:
        reasons = why_different(final, effective)
        if urlparse(final).scheme == "https" and urlparse(effective).scheme == "http":
            issue("Canonical", "error", "The canonical points to the insecure http:// version of the page.",
                  f'Change it to "{effective.replace("http://", "https://", 1)}".')
        elif reasons and set(reasons) <= {"www vs non-www", "trailing slash", "upper/lower case in the path"}:
            issue("Canonical", "warn", f"The canonical differs from this URL only by {', '.join(reasons)} - usually a mistake.",
                  "Make the canonical match the exact live URL (and redirect the other variant to it).")
        else:
            issue("Canonical", "info", f"This page is canonicalised to another URL ({', '.join(reasons) or 'different URL'}). "
                  "Google will usually index that URL instead of this one.",
                  "That's correct for duplicates/parameter pages. If this page should rank on its own, make the canonical self-referencing.")
        canon_target = inspect_url(effective)
        t = canon_target
        if t["unverifiable"]:
            issue("Canonical", "info", "Couldn't verify the canonical target - the site blocked the check.", "Open it in a browser to confirm it loads.")
        elif t["status"] is None:
            issue("Canonical", "error", f"The canonical target could not be reached ({t['error']}).", "Point the canonical at a live page.")
        elif t["status"] >= 400:
            issue("Canonical", "error", f"The canonical target returns HTTP {t['status']}.", "Point the canonical at a live, 200-status page.")
        else:
            if t["redirected"]:
                issue("Canonical", "error", f"The canonical target redirects to {t['final_url']}.",
                      "Point the canonical straight at the final URL - never at a redirect.")
            if t["noindex"]:
                issue("Canonical", "error", "The canonical target is set to noindex - mixed signals.",
                      "Either remove noindex from the target or point the canonical elsewhere.")
            if t["canonical"] and norm_url(t["canonical"]) != norm_url(t["final_url"]):
                issue("Canonical", "warn", f"Canonical chain: the target's own canonical points to {t['canonical']}.",
                      "Point this page's canonical directly at the final preferred URL.")
    view["canon_target"] = canon_target
    view["canon_rows"] = canon_rows

    if page["noindex"]:
        if effective and not view["canonical_self"]:
            issue("Canonical", "warn", "The page is noindex AND canonicalised to another URL - Google gets mixed signals.",
                  "Pick one: use the canonical for duplicates, or noindex for pages that shouldn't be in search at all.")
        else:
            issue("Canonical", "info", f"The page is set to noindex ({page['robots']}).", "Fine if intentional - noindex pages won't appear in Google.")

    # ------------------------------------------------------------- hreflang
    sitemap_info = None
    sitemap_url = normalize_input(sitemap_raw) if sitemap_raw.strip() else ""
    if sitemap_url:
        sitemap_info = sitemap_alternates(sitemap_url, [final, url, effective])
        if sitemap_info["error"]:
            issue("Sources", "warn", sitemap_info["error"], "Check the sitemap URL opens in a browser.")
        elif not sitemap_info["found_page"]:
            issue("Sources", "info", f"This page wasn't found in the sitemap (scanned {sitemap_info['scanned']} file(s)).",
                  "Add the page to your XML sitemap. If the sitemap is split, enter the child sitemap that contains it.")
    view["sitemap"] = sitemap_info

    by_source = {"HTML": page["alts_html"], "HTTP header": page["alts_header"],
                 "Sitemap": (sitemap_info or {}).get("alternates", [])}
    declared = []          # merged, de-duplicated (code, url) pairs
    index = {}
    for src, items in by_source.items():
        for a in items:
            key = (a["code"].lower(), norm_url(a["href"]))
            if key not in index:
                index[key] = {"code": a["code"], "href": a["href"], "raw": a["raw"], "sources": [], "where": set(),
                              "relative": False}
                declared.append(index[key])
            index[key]["sources"].append(src)
            index[key]["where"].add(a["where"])
            index[key]["relative"] = index[key]["relative"] or a["relative"]
    view["hreflang_count"] = len(declared)
    has_hreflang = bool(declared)

    # compare sources
    used_sources = [s for s, items in by_source.items() if items]
    if len(used_sources) > 1:
        sets = {s: {(a["code"].lower(), norm_url(a["href"])) for a in by_source[s]} for s in used_sources}
        first = used_sources[0]
        for s in used_sources[1:]:
            if sets[s] != sets[first]:
                only_a, only_b = len(sets[first] - sets[s]), len(sets[s] - sets[first])
                issue("Sources", "warn", f"{first} and {s} hreflang don't match ({only_a} only in {first}, {only_b} only in {s}).",
                      "Using more than one method is allowed, but they must list exactly the same set - or keep just one method.")
    view["used_sources"] = used_sources

    if not has_hreflang:
        issue("Hreflang", "info", "No hreflang annotations found.",
              "That's fine for a single-language site. Add hreflang only when you have translated or country-specific versions of this page.")

    if has_hreflang:
        for d in declared:
            d["validation"] = validate_hreflang(d["code"])
            v = d["validation"]
            if v["severity"]:
                issue("Hreflang", v["severity"], f'hreflang="{d["code"]}": ' + " ".join(v["problems"]), v["fix"])
            if not d["href"]:
                issue("Hreflang", "error", f'hreflang="{d["code"]}" has an empty href.', "Give it the full URL of that language version.")
            elif d["relative"]:
                issue("Hreflang", "error", f'hreflang="{d["code"]}" uses a relative URL ("{d["raw"]}"). Google requires fully-qualified URLs.',
                      f'Use "{d["href"]}".')
            if "body" in d["where"]:
                issue("Hreflang", "error", f'hreflang="{d["code"]}" is in the <body> - Google only reads it in <head>.',
                      "Move the hreflang link tags into <head>.")

        # same code -> different URLs
        by_code = {}
        for d in declared:
            by_code.setdefault(d["code"].lower(), set()).add(norm_url(d["href"]))
        for code, urls in by_code.items():
            if len(urls) > 1:
                issue("Hreflang", "error", f'hreflang="{code}" is used for {len(urls)} different URLs.',
                      "Each language/region code must point to exactly one URL.")
        # self reference / x-default
        view["self_ref"] = any(norm_url(d["href"]) == self_n for d in declared)
        view["has_x_default"] = "x-default" in by_code
        if not view["self_ref"]:
            issue("Hreflang", "error", "The page doesn't list itself in its own hreflang set (missing self-reference).",
                  f'Add <link rel="alternate" hreflang="{guess_code(page["html_lang"])}" href="{final}">. '
                  "Google says every version must list itself as well as the others.")
        if not view["has_x_default"]:
            issue("Hreflang", "warn", 'No hreflang="x-default" found.',
                  "Add an x-default pointing to your fallback page (usually the English or language-selector page) for users who match no listed language.")
        langs_with_region = {c.split("-")[0] for c in by_code if "-" in c and c != "x-default"}
        langs_plain = {c for c in by_code if "-" not in c}
        missing_plain = sorted(l for l in langs_with_region - langs_plain)
        if missing_plain:
            issue("Hreflang", "info", "Only country-specific codes exist for: " + ", ".join(missing_plain) +
                  " (no plain-language version).",
                  f'Consider also adding e.g. hreflang="{missing_plain[0]}" so speakers in other countries get a match.')
        if effective and not view["canonical_self"]:
            issue("Hreflang", "error", "This page has hreflang tags but canonicalises to a different URL. "
                  "Google ignores hreflang on non-canonical pages.",
                  "Put hreflang only on canonical URLs - make this page self-canonical or move the tags to the canonical page.")
        if page["noindex"]:
            issue("Hreflang", "error", "This page is noindex but is part of an hreflang cluster.",
                  "Only indexable pages should be in hreflang sets - remove noindex or remove the page from the cluster.")
    else:
        view["self_ref"] = False
        view["has_x_default"] = False

    # ------------------------------------------------------------- alternates
    targets = []
    seen = {self_n}
    for d in declared:
        n = norm_url(d["href"])
        if d["href"] and is_absolute(d["href"]) and n not in seen:
            seen.add(n)
            targets.append(d["href"])
    skipped = 0
    if len(targets) > MAX_ALTERNATES:
        skipped = len(targets) - MAX_ALTERNATES
        targets = targets[:MAX_ALTERNATES]
        issue("Return links", "info", f"Checked the first {MAX_ALTERNATES} language versions; {skipped} more were not fetched.", "")
    alt_info = {}
    if check_alts and targets:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            for info in pool.map(inspect_url, targets):
                alt_info[norm_url(info["url"])] = info

    # the source page's accepted identities for return links
    source_ids = {self_n, norm_url(url)}
    if effective:
        source_ids.add(norm_url(effective))
    self_codes = {d["code"].lower() for d in declared if norm_url(d["href"]) == self_n}

    rows = []
    for d in declared:
        n = norm_url(d["href"])
        v = d.get("validation") or validate_hreflang(d["code"])
        row = {"code": d["code"], "label": v["label"], "code_sev": v["severity"] or "ok",
               "code_note": " ".join(v["problems"]), "url": d["href"], "sources": d["sources"],
               "is_self": n == self_n, "status": "", "status_sev": "ok", "links_back": "—",
               "canonical_ok": "—", "html_lang": "", "lang_match": "—", "notes": []}
        if row["is_self"]:
            row.update(status=f"{res['status']} (this page)", links_back="self",
                       canonical_ok="yes" if (not effective or view["canonical_self"]) else "no",
                       html_lang=page["html_lang"])
        elif not check_alts:
            row.update(status="not checked", status_sev="info")
        elif n not in alt_info:
            row.update(status="not checked", status_sev="info")
        else:
            a = alt_info[n]
            if a["unverifiable"]:
                row.update(status="unverifiable", status_sev="info", links_back="?", canonical_ok="?")
                row["notes"].append("Site blocked the automated check")
                issue("Return links", "info", f'Couldn\'t verify {d["href"]} ({a["error"] or "HTTP " + str(a["status"])}).',
                      "Its bot protection blocked the request - open it in a browser to confirm.")
            elif a["status"] is None:
                row.update(status=a["error"] or "failed", status_sev="error", links_back="?", canonical_ok="?")
                issue("Return links", "error", f'hreflang="{d["code"]}" URL can\'t be reached ({a["error"]}).',
                      "Fix the URL or remove it from the hreflang set.")
            elif a["status"] >= 400:
                row.update(status=str(a["status"]), status_sev="error", links_back="?", canonical_ok="?")
                issue("Return links", "error", f'hreflang="{d["code"]}" points to a page returning HTTP {a["status"]}.',
                      "Every hreflang URL must return 200 - fix the page or remove the annotation.")
            else:
                row["status"] = str(a["status"])
                if a["redirected"]:
                    row["status"] = f"{a['status']} via redirect"
                    row["status_sev"] = "error"
                    row["notes"].append(f"Redirects to {a['final_url']}")
                    issue("Return links", "error", f'hreflang="{d["code"]}" points to a redirect ({d["href"]} → {a["final_url"]}).',
                          "Use the final destination URL in hreflang.")
                if a["noindex"]:
                    row["status_sev"] = "error"
                    row["notes"].append("noindex")
                    issue("Return links", "error", f'hreflang="{d["code"]}" points to a noindex page.',
                          "Only indexable pages belong in an hreflang set.")
                alt_ids = {norm_url(a["final_url"]), n}
                if a["canonical"] and norm_url(a["canonical"]) not in alt_ids:
                    row["canonical_ok"] = "no"
                    row["notes"].append(f"Canonical → {a['canonical']}")
                    issue("Return links", "error", f'hreflang="{d["code"]}" URL canonicalises to a different page ({a["canonical"]}).',
                          "hreflang must point to canonical URLs - use the canonical URL here, or make that page self-canonical.")
                else:
                    row["canonical_ok"] = "yes" if a["canonical"] else "none"
                back = [h for h in a["hreflang"] if norm_url(h["href"]) in source_ids]
                if back:
                    row["links_back"] = "yes"
                    back_codes = {h["code"].lower() for h in back}
                    if self_codes and not (back_codes & self_codes):
                        row["notes"].append(f"Refers back to this page as {', '.join(sorted(back_codes))}")
                        issue("Return links", "warn", f'{d["href"]} links back, but labels this page "{", ".join(sorted(back_codes))}" '
                              f'while this page calls itself "{", ".join(sorted(self_codes))}".',
                              "Use the same code for a page everywhere it's referenced.")
                else:
                    row["links_back"] = "no"
                    issue("Return links", "error", f'No return link: the hreflang="{d["code"]}" page doesn\'t link back to this page.',
                          "hreflang must be reciprocal - add the full set of hreflang tags (including this page) to that page too. "
                          "Without it Google ignores the pair.")
                row["html_lang"] = a["html_lang"]
        # html lang cross-check (informational - Google ignores html lang, but users and Bing don't)
        if row["html_lang"] and v["lang"] not in ("", "x-default"):
            hl = row["html_lang"].split("-")[0].split("_")[0].lower()
            row["lang_match"] = "yes" if hl == v["lang"] else "no"
            if hl != v["lang"]:
                row["notes"].append(f"<html lang=\"{row['html_lang']}\">")
                issue("Hreflang", "info", f'hreflang="{d["code"]}" page declares <html lang="{row["html_lang"]}">.',
                      "Check the page really is in the language its hreflang says - or fix the html lang attribute.")
        rows.append(row)
    view["rows"] = rows

    # ------------------------------------------------------------- matrix
    nodes = []
    if has_hreflang:
        code_for = {}
        for d in declared:
            code_for.setdefault(norm_url(d["href"]), d["code"])
        node_urls = [final] + [t for t in targets]
        for u in node_urls:
            nu = norm_url(u)
            if nu == self_n:
                links = {norm_url(d["href"]) for d in declared}
                known = True
            else:
                a = alt_info.get(nu)
                known = bool(a and a["fetched"])
                links = {norm_url(h["href"]) for h in (a["hreflang"] if a else [])}
            nodes.append({"url": u, "n": nu, "code": code_for.get(nu, "?"), "links": links, "known": known})
        matrix = []
        for i, a in enumerate(nodes):
            cells = []
            for j, b in enumerate(nodes):
                if i == j:
                    cells.append("self-ok" if a["n"] in a["links"] else ("self-miss" if a["known"] else "unk"))
                elif not a["known"]:
                    cells.append("unk")
                else:
                    ids = {b["n"]} | (source_ids if j == 0 else set())
                    cells.append("yes" if a["links"] & ids else "no")
            matrix.append({"code": a["code"], "url": a["url"], "cells": cells})
        view["matrix"] = matrix
        view["matrix_heads"] = [n["code"] for n in nodes]
    else:
        view["matrix"] = []
        view["matrix_heads"] = []

    # ------------------------------------------------------------- suggested tags
    view["suggested"] = suggest_tags(final, page, declared, alt_info, effective, view)

    # ------------------------------------------------------------- score
    sev_rank = {"error": 0, "warn": 1, "info": 2}
    issues.sort(key=lambda i: (sev_rank[i["severity"]], ["Canonical", "Hreflang", "Return links", "Sources"].index(i["area"])))
    errors = sum(1 for i in issues if i["severity"] == "error")
    warns = sum(1 for i in issues if i["severity"] == "warn")
    view["score"] = max(0, 100 - errors * 12 - warns * 4)
    view["issues"] = issues
    view["counts"] = {"error": errors, "warn": warns, "info": sum(1 for i in issues if i["severity"] == "info")}
    areas = ["Canonical", "Hreflang", "Return links", "Sources"]
    view["area_counts"] = {a: {s: sum(1 for i in issues if i["area"] == a and i["severity"] == s)
                               for s in ("error", "warn", "info")} for a in areas}
    rl_total = sum(1 for r in rows if not r["is_self"] and r["links_back"] in ("yes", "no"))
    rl_ok = sum(1 for r in rows if not r["is_self"] and r["links_back"] == "yes")
    view["return_links"] = f"{rl_ok}/{rl_total}" if rl_total else "—"
    return view


def guess_code(html_lang):
    v = validate_hreflang((html_lang or "").replace("_", "-"))
    return v["suggested"] if html_lang and v["suggested"] else "LANGUAGE-CODE"


def suggest_tags(final, page, declared, alt_info, effective, view):
    """Write out a corrected <head> snippet based on what the page declares."""
    lines = []
    if not declared:
        lines.append(f'<link rel="canonical" href="{final}" />')
        return "\n".join(lines)
    lines.append(f'<link rel="canonical" href="{final}" />')
    used, entries = set(), []
    self_n = norm_url(final)
    has_self = False
    for d in declared:
        v = d.get("validation") or validate_hreflang(d["code"])
        code = v["suggested"] or d["code"]
        if code.lower() in used or not d["href"]:
            continue
        href = d["href"]
        a = alt_info.get(norm_url(href))
        note = ""
        if a and a["fetched"] and a["redirected"]:
            href = a["final_url"]
        if a and not a["unverifiable"]:
            if a["status"] is None or a["status"] >= 400:
                note = f"returns {a['status'] or a['error']} - fix or remove"
            elif a["noindex"]:
                note = "page is noindex - remove or make indexable"
            elif a["canonical"] and norm_url(a["canonical"]) not in {norm_url(href), norm_url(a["final_url"])}:
                note = "page canonicalises elsewhere - make it self-canonical"
            elif not any(norm_url(h["href"]) in {self_n, norm_url(effective or final)} for h in a["hreflang"]):
                note = "add this same tag set to that page too (no return link)"
        if norm_url(href) == self_n:
            has_self = True
        used.add(code.lower())
        entries.append((code, href, note))
    if not has_self:
        entries.insert(0, (guess_code(page["html_lang"]), final, "added: self-reference"))
    if "x-default" not in used:
        fallback = next((h for c, h, _ in entries if c.lower() in ("en", "en-us", "en-gb", "en-in")), entries[0][1])
        entries.append(("x-default", fallback, "added: pick your fallback page"))
    for code, href, note in entries:
        lines.append(f'<link rel="alternate" hreflang="{code}" href="{href}" />' + (f"  <!-- {note} -->" if note else ""))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Template
# --------------------------------------------------------------------------- #
PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Canonical & Hreflang Checker</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%237c6af7'/%3E%3Ctext x='50%25' y='50%25' dominant-baseline='central' text-anchor='middle' font-family='Georgia%2C serif' font-size='18' font-weight='900' fill='white'%3EM%3C/text%3E%3C/svg%3E">
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@600;700;800&family=Inter:wght@400;500;600&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
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
      <a href="#" class="sidebar-link active">🌐&nbsp; Canonical &amp; Hreflang</a>
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
        Canonical tag &amp; header · canonical target health · hreflang codes · self-reference &amp; x-default · return links for up to {{ max_alts }} language versions · HTML vs header vs sitemap
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
      <div class="crumb">// seo tool · free</div>
      <h1>🌐 Canonical &amp; Hreflang <span>Checker</span></h1>
    </div>

    <form class="card" method="POST" action="/check" onsubmit="document.getElementById('sp').classList.add('show')">
      <label class="lbl" for="url">Page URL</label>
      <div class="row-inline">
        <input type="text" id="url" name="url" placeholder="https://www.yourwebsite.com/your-page" value="{{ form.url }}">
        <button class="btn" type="submit">🔍 Check Page</button>
      </div>
      <details class="adv" {% if form.sitemap or not form.check_alts %}open{% endif %}>
        <summary>+ Advanced options</summary>
        <div class="adv-body">
          <div>
            <label class="lbl" for="sitemap">XML sitemap URL (optional) — also checks hreflang declared in the sitemap</label>
            <input type="text" id="sitemap" name="sitemap" placeholder="https://www.yourwebsite.com/sitemap.xml" value="{{ form.sitemap }}">
          </div>
          <label class="check"><input type="checkbox" name="check_alts" value="1" {% if form.check_alts %}checked{% endif %}> Fetch every language version to check return links, status and canonicals</label>
        </div>
      </details>
      <div class="spinner" id="sp">Fetching the page and every language version… this can take up to a minute on big hreflang sets.</div>
      {% if error %}<div class="err">{{ error }}</div>{% endif %}
    </form>

    {% if view %}
      {% if not view.fetch_ok %}
        <div class="card" style="margin-top:1.2rem;border-color:rgba(247,106,106,.4)">
          <div class="err" style="margin-top:0">Couldn't check this URL: {{ view.fetch_error }}</div>
        </div>
      {% else %}
      {% set sc = view.score %}
      {% set sc_col = '#6af7c8' if sc >= 80 else ('#f7a26a' if sc >= 50 else '#f76a6a') %}

      <div class="overview">
        <div class="chart-card">
          <h3>Health score</h3>
          <div class="ring-wrap">
            <svg viewBox="0 0 160 160" width="160" height="160">
              <circle cx="80" cy="80" r="68" fill="none" stroke="#23233a" stroke-width="12"/>
              <circle cx="80" cy="80" r="68" fill="none" stroke="{{ sc_col }}" stroke-width="12" stroke-linecap="{{ 'round' if sc > 0 else 'butt' }}"
                      stroke-dasharray="{{ (427.26 * sc / 100) | round(2) }} 427.26" transform="rotate(-90 80 80)"/>
            </svg>
            <div class="ring-center"><div class="score" style="color:{{ sc_col }}">{{ sc }}</div><div class="sub">out of 100</div></div>
          </div>
          <div style="display:flex;justify-content:center;gap:.4rem;margin-top:.8rem;flex-wrap:wrap">
            <span class="badge error">{{ view.counts.error }} errors</span>
            <span class="badge warn">{{ view.counts.warn }} warnings</span>
          </div>
        </div>

        <div class="chart-card">
          <h3>At a glance</h3>
          <div class="metrics">
            <div class="metric"><div class="k">Canonical</div>
              <div class="v {{ 'mint' if view.canonical_self else ('orange' if view.canonical else 'red') }}">
                {{ 'Self-referencing' if view.canonical_self else ('Points elsewhere' if view.canonical else 'Missing') }}</div></div>
            <div class="metric"><div class="k">Hreflang tags</div><div class="v purple">{{ view.hreflang_count }}</div></div>
            <div class="metric"><div class="k">Return links OK</div><div class="v {{ 'mint' if view.return_links != '—' and view.return_links.split('/')[0] == view.return_links.split('/')[1] else ('orange' if view.return_links == '—' else 'red') }}">{{ view.return_links }}</div></div>
            <div class="metric"><div class="k">x-default</div><div class="v {{ 'mint' if view.has_x_default else ('orange' if view.hreflang_count else '') }}">{{ 'Yes' if view.has_x_default else ('Missing' if view.hreflang_count else '—') }}</div></div>
            <div class="metric"><div class="k">Self-reference</div><div class="v {{ 'mint' if view.self_ref else ('red' if view.hreflang_count else '') }}">{{ 'Yes' if view.self_ref else ('Missing' if view.hreflang_count else '—') }}</div></div>
            <div class="metric"><div class="k">Indexable</div><div class="v {{ 'orange' if view.noindex else 'mint' }}">{{ 'noindex' if view.noindex else 'Yes' }}</div></div>
          </div>
          <div class="hint" style="overflow-wrap:anywhere">Checked: <span style="color:var(--text)">{{ view.final_url }}</span> · HTTP {{ view.status }}{% if view.used_sources %} · hreflang found in: {{ view.used_sources | join(', ') }}{% endif %}</div>
        </div>

        <div class="chart-card bars">
          <h3>Issues by area</h3>
          <div class="bar-box"><canvas id="areaChart"></canvas></div>
        </div>
      </div>

      <!-- ======================= ISSUES ======================= -->
      <div class="sec-title">🛠 Issues &amp; Fixes <span class="count">{{ view.issues | length }} found</span></div>
      {% if view.issues %}
      <div class="chips" id="areaChips">
        <button type="button" class="chip active" data-area="all">All</button>
        {% for a in ['Canonical','Hreflang','Return links','Sources'] %}
          {% set n = view.area_counts[a].error + view.area_counts[a].warn + view.area_counts[a].info %}
          {% if n %}<button type="button" class="chip" data-area="{{ a }}">{{ a }} ({{ n }})</button>{% endif %}
        {% endfor %}
      </div>
      <div class="issues">
        {% for i in view.issues %}
        <div class="issue {{ i.severity }}" data-area="{{ i.area }}">
          <div class="top">
            <span class="badge {{ i.severity }}">{{ {'error':'✕ error','warn':'! warning','info':'i info'}[i.severity] }}</span>
            <div class="msg">{{ i.message }}</div>
            <span class="area-tag">{{ i.area }}</span>
          </div>
          {% if i.fix %}<div class="fix"><b>Fix:</b> {{ i.fix }}</div>{% endif %}
        </div>
        {% endfor %}
      </div>
      {% else %}
      <div class="all-good">✓ No issues found — canonical and hreflang are set up cleanly.</div>
      {% endif %}

      <!-- ======================= CANONICAL ======================= -->
      <div class="sec-title">🔗 Canonical</div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>Where</th><th>Value as written</th><th>Resolves to</th><th>Matches this page?</th></tr></thead>
          <tbody>
          {% for c in view.canon_rows %}
            <tr>
              <td><span class="badge {{ 'error' if c.where == 'body' else 'info' }}">{{ c.where }}</span></td>
              <td class="mono">{{ c.raw or '(empty)' }}</td>
              <td class="mono">{{ c.href or '—' }}</td>
              <td>{% if c.href and c.href | normurl == view.final_url | normurl %}<span class="badge ok">✓ self</span>{% else %}<span class="badge warn">other URL</span>{% endif %}</td>
            </tr>
          {% else %}
            <tr><td colspan="4">No canonical found in the HTML &lt;head&gt; or the HTTP Link header.</td></tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% if view.canon_target %}
      {% set t = view.canon_target %}
      <div class="tbl-wrap" style="margin-top:.8rem">
        <table>
          <thead><tr><th colspan="2">Canonical target check</th></tr></thead>
          <tbody>
            <tr><td>URL</td><td class="mono">{{ t.url }}</td></tr>
            <tr><td>Status</td><td>{% if t.unverifiable %}<span class="badge info">unverifiable</span>{% elif t.status and t.status < 400 %}<span class="badge {{ 'error' if t.redirected else 'ok' }}">{{ t.status }}{{ ' via redirect' if t.redirected else '' }}</span>{% else %}<span class="badge error">{{ t.status or t.error }}</span>{% endif %}</td></tr>
            {% if t.redirected %}<tr><td>Redirects to</td><td class="mono">{{ t.final_url }}</td></tr>{% endif %}
            <tr><td>Indexable</td><td>{% if t.fetched %}{{ '✕ noindex' if t.noindex else '✓ yes' }}{% else %}—{% endif %}</td></tr>
            <tr><td>Its own canonical</td><td class="mono">{{ t.canonical or '—' }}</td></tr>
          </tbody>
        </table>
      </div>
      {% endif %}

      <!-- ======================= HREFLANG TABLE ======================= -->
      <div class="sec-title">🌍 Hreflang Set <span class="count">{{ view.rows | length }} entries</span></div>
      {% if view.rows %}
      <div class="tbl-tools">
        <input type="text" id="rowFilter" placeholder="Filter by code or URL…">
        <label class="check"><input type="checkbox" id="problemsOnly"> Show problems only</label>
      </div>
      <div class="tbl-wrap">
        <table id="hrefTable">
          <thead><tr><th>Code</th><th>URL</th><th>Found in</th><th>Code valid</th><th>Status</th><th>Links back</th><th>Canonical OK</th><th>html lang</th></tr></thead>
          <tbody>
          {% for r in view.rows %}
            {% set bad = r.code_sev != 'ok' or r.status_sev == 'error' or r.links_back == 'no' or r.canonical_ok == 'no' %}
            <tr data-bad="{{ 1 if bad else 0 }}" data-text="{{ (r.code ~ ' ' ~ r.url) | lower }}">
              <td><div class="code-pill">{{ r.code }}</div><div class="small">{{ r.label }}</div></td>
              <td class="mono">{{ r.url or '(empty)' }}{% if r.is_self %} <span class="badge self">this page</span>{% endif %}
                {% for n in r.notes %}<div class="small">↳ {{ n }}</div>{% endfor %}</td>
              <td>{% for s in r.sources %}<span class="badge info" style="margin:0 .2rem .2rem 0">{{ s }}</span>{% endfor %}</td>
              <td>{% if r.code_sev == 'ok' %}<span class="badge ok">✓ valid</span>{% else %}<span class="badge {{ r.code_sev }}" title="{{ r.code_note }}">{{ '✕ invalid' if r.code_sev == 'error' else '! check' }}</span>{% endif %}</td>
              <td><span class="badge {{ r.status_sev }}">{{ r.status }}</span></td>
              <td><span class="badge {{ {'yes':'yes','no':'no','self':'self'}.get(r.links_back, 'unk') }}">{{ {'yes':'✓ yes','no':'✕ no','self':'self'}.get(r.links_back, r.links_back) }}</span></td>
              <td><span class="badge {{ {'yes':'yes','no':'no','none':'none'}.get(r.canonical_ok, 'unk') }}">{{ {'yes':'✓ yes','no':'✕ no','none':'no tag'}.get(r.canonical_ok, r.canonical_ok) }}</span></td>
              <td class="mono">{{ r.html_lang or '—' }}{% if r.lang_match == 'no' %} <span class="badge warn">mismatch</span>{% endif %}</td>
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      {% else %}
      <div class="card" style="font-size:.85rem;color:var(--muted)">No hreflang annotations found in the HTML, HTTP header{{ ' or sitemap' if view.sitemap }}. That's fine for a single-language page.</div>
      {% endif %}

      <!-- ======================= MATRIX ======================= -->
      {% if view.matrix and view.matrix | length > 1 %}
      <div class="sec-title">🔁 Return-Link Matrix</div>
      <div class="tbl-wrap">
        <table class="matrix">
          <thead><tr><th class="rowh">This page ↓ links to →</th>{% for h in view.matrix_heads %}<th>{{ h }}</th>{% endfor %}</tr></thead>
          <tbody>
          {% for m in view.matrix %}
            <tr>
              <td class="rowh"><div class="code-pill">{{ m.code }}</div><div class="small" style="word-break:break-all">{{ m.url }}</div></td>
              {% for c in m.cells %}
                <td><span class="cell {{ c }}" title="{{ m.code }} → {{ view.matrix_heads[loop.index0] }}">{{ {'yes':'✓','self-ok':'✓','no':'✕','self-miss':'✕','unk':'?'}[c] }}</span></td>
              {% endfor %}
            </tr>
          {% endfor %}
          </tbody>
        </table>
      </div>
      <div class="legend">
        <span><span class="cell yes">✓</span> links to it</span>
        <span><span class="cell no">✕</span> missing link (Google ignores the pair)</span>
        <span><span class="cell unk">?</span> couldn't fetch that page</span>
      </div>
      <p class="hint">Every row should be all ✓ — each language version must list every other version and itself.</p>
      {% endif %}

      <!-- ======================= SITEMAP ======================= -->
      {% if view.sitemap %}
      <div class="sec-title">🗺 Sitemap Check</div>
      <div class="tbl-wrap">
        <table><tbody>
          <tr><td>Sitemap</td><td class="mono">{{ view.sitemap.url }}</td></tr>
          <tr><td>Files scanned</td><td>{{ view.sitemap.scanned }}</td></tr>
          <tr><td>Page found</td><td>{% if view.sitemap.error %}<span class="badge error">{{ view.sitemap.error }}</span>{% elif view.sitemap.found_page %}<span class="badge ok">✓ yes</span>{% else %}<span class="badge warn">not found</span>{% endif %}</td></tr>
          <tr><td>hreflang entries in sitemap</td><td>{{ view.sitemap.alternates | length }}</td></tr>
        </tbody></table>
      </div>
      {% endif %}

      <!-- ======================= SUGGESTED TAGS ======================= -->
      <div class="sec-title">✅ Suggested Tags</div>
      <p style="font-size:.8rem;color:var(--muted);margin-bottom:.7rem">
        A cleaned-up version of this page's tags — codes corrected, redirects swapped for final URLs, self-reference and x-default added.
        Review the x-default target and any LANGUAGE-CODE placeholder before pasting it into &lt;head&gt;.
        {% if view.canonical and not view.canonical_self %}<br><b style="color:var(--orange)">Note:</b> this assumes the page should be self-canonical. If it's a duplicate on purpose, keep your current canonical and leave hreflang off this page.{% endif %}
      </p>
      <pre class="code" id="suggested">{{ view.suggested }}</pre>
      <div class="dl-row">
        <button type="button" class="btn ghost" onclick="copyTags(this)">📋 Copy tags</button>
        <button type="button" class="btn ghost" onclick="downloadCSV('issues')">⬇️ Issues CSV</button>
        {% if view.rows %}<button type="button" class="btn ghost" onclick="downloadCSV('hreflang')">⬇️ Hreflang table CSV</button>{% endif %}
      </div>

      <script>
        const REPORT = {{ export | tojson }};
        const AREA = {{ view.area_counts | tojson }};
        new Chart(document.getElementById('areaChart'), {
          type: 'bar',
          data: {
            labels: Object.keys(AREA),
            datasets: [
              {label: 'Errors', data: Object.values(AREA).map(a => a.error), backgroundColor: '#f76a6a', borderRadius: 4},
              {label: 'Warnings', data: Object.values(AREA).map(a => a.warn), backgroundColor: '#f7a26a', borderRadius: 4},
              {label: 'Info', data: Object.values(AREA).map(a => a.info), backgroundColor: '#5a5a78', borderRadius: 4}
            ]
          },
          options: {
            indexAxis: 'y', maintainAspectRatio: false,
            scales: {
              x: {stacked: true, ticks: {color: '#8888a8', precision: 0}, grid: {color: '#23233a'}},
              y: {stacked: true, ticks: {color: '#e8e8f0', font: {family: 'DM Mono', size: 11}}, grid: {display: false}}
            },
            plugins: {legend: {labels: {color: '#8888a8', boxWidth: 10, font: {size: 11}}}}
          }
        });

        document.querySelectorAll('#areaChips .chip').forEach(ch => ch.addEventListener('click', () => {
          document.querySelectorAll('#areaChips .chip').forEach(c => c.classList.remove('active'));
          ch.classList.add('active');
          const a = ch.dataset.area;
          document.querySelectorAll('.issue').forEach(i => i.style.display = (a === 'all' || i.dataset.area === a) ? '' : 'none');
        }));

        function filterRows() {
          const q = (document.getElementById('rowFilter') || {value: ''}).value.toLowerCase();
          const po = (document.getElementById('problemsOnly') || {checked: false}).checked;
          document.querySelectorAll('#hrefTable tbody tr').forEach(tr => {
            const show = tr.dataset.text.includes(q) && (!po || tr.dataset.bad === '1');
            tr.style.display = show ? '' : 'none';
          });
        }
        const rf = document.getElementById('rowFilter'); if (rf) rf.addEventListener('input', filterRows);
        const pc = document.getElementById('problemsOnly'); if (pc) pc.addEventListener('change', filterRows);

        function copyTags(btn) {
          const txt = document.getElementById('suggested').innerText;
          const done = () => { btn.textContent = '✓ Copied'; setTimeout(() => btn.textContent = '📋 Copy tags', 1500); };
          if (navigator.clipboard) { navigator.clipboard.writeText(txt).then(done, () => fallbackCopy(txt, done)); }
          else { fallbackCopy(txt, done); }
        }
        function fallbackCopy(txt, done) {
          const ta = document.createElement('textarea'); ta.value = txt; document.body.appendChild(ta);
          ta.select(); try { document.execCommand('copy'); done(); } catch (e) {} ta.remove();
        }
        function csvCell(v) { v = (v === null || v === undefined) ? '' : String(v); return '"' + v.replace(/"/g, '""') + '"'; }
        function downloadCSV(kind) {
          const rows = REPORT[kind];
          const csv = '﻿' + rows.map(r => r.map(csvCell).join(',')).join('\r\n');
          const blob = new Blob([csv], {type: 'text/csv;charset=utf-8'});
          const a = document.createElement('a');
          a.href = URL.createObjectURL(blob);
          a.download = 'canonical-hreflang-' + kind + '.csv';
          document.body.appendChild(a); a.click(); a.remove();
        }
      </script>
      {% endif %}
    {% endif %}
  </main>
</div>
</body>
</html>
"""


@app.template_filter("normurl")
def _normurl_filter(u):
    return norm_url(u)


def build_export(view):
    if not view or not view.get("fetch_ok"):
        return {}
    issues = [["Severity", "Area", "Issue", "Fix", "Page"]] + [
        [i["severity"], i["area"], i["message"], i["fix"], view["final_url"]] for i in view["issues"]]
    hreflang = [["hreflang", "Language / Region", "URL", "Found In", "Code Valid", "Code Note", "Status",
                 "Links Back", "Canonical OK", "html lang", "Notes"]] + [
        [r["code"], r["label"], r["url"], "; ".join(r["sources"]), r["code_sev"], r["code_note"], r["status"],
         r["links_back"], r["canonical_ok"], r["html_lang"], "; ".join(r["notes"])] for r in view.get("rows", [])]
    return {"issues": issues, "hreflang": hreflang}


def render(view=None, error=None, form=None):
    form = form or {"url": "", "sitemap": "", "check_alts": True}
    return render_template_string(PAGE, view=view, error=error, form=form, max_alts=MAX_ALTERNATES,
                                  export=build_export(view))


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/", methods=["GET"])
def home():
    url = request.args.get("url", "").strip()
    sitemap = request.args.get("sitemap", "").strip()
    form = {"url": url, "sitemap": sitemap, "check_alts": True}
    return render(analyze(url, sitemap, True) if url else None, form=form)


@app.route("/check", methods=["POST"])
def check():
    url = request.form.get("url", "").strip()
    sitemap = request.form.get("sitemap", "").strip()
    check_alts = request.form.get("check_alts") == "1"
    form = {"url": url, "sitemap": sitemap, "check_alts": check_alts}
    if not url:
        return render(error="Please enter a page URL.", form=form)
    return render(analyze(url, sitemap, check_alts), form=form)


@app.route("/healthz")
def healthz():
    return "ok"


if __name__ == "__main__":
    app.run(debug=True, port=8501)
