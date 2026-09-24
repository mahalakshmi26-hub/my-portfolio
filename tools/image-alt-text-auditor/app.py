"""
Image Alt Text Auditor  -  Flask app
Finds every image on a page (including lazy-loaded ones, <picture>, SVG role="img",
image buttons and CSS background images), then grades the QUALITY of each alt text,
not just whether it exists: missing, empty-but-meaningful, too long, generic,
file-name-as-alt, duplicated, keyword-stuffed, image-only links with no name.
Also gives extra image-SEO hints (width/height, file names, lazy loading, formats)
and an optional focus-keyword check.

Built by Mahalakshmi Marimuthu - Digital Marketing Strategist & AI-Powered SEO Expert
"""
import re
import time
from collections import Counter, defaultdict
from urllib.parse import urljoin, urlparse, unquote

import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template_string, request

app = Flask(__name__)

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
REQUEST_TIMEOUT = 12
MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_IMAGES = 400               # rows shown per page
MAX_BG_IMAGES = 30             # CSS background images listed
ALT_MAX_LEN = 125
ALT_MIN_LEN = 5
RETRY_STATUSES = {403, 429, 503}
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

LAZY_ATTRS = ["data-src", "data-lazy-src", "data-original", "data-lazy", "data-url", "data-img",
              "data-echo", "data-hi-res-src", "data-full-src"]
SRCSET_ATTRS = ["srcset", "data-srcset", "data-lazy-srcset"]
IMG_EXT = re.compile(r"\.(jpe?g|png|gif|webp|svg|avif|bmp|tiff?|ico)$", re.I)
LEGACY_EXT = {"jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff"}

GENERIC_ALTS = {
    "image", "img", "images", "picture", "pic", "pics", "photo", "photos", "photograph", "graphic",
    "logo", "icon", "banner", "thumbnail", "thumb", "placeholder", "untitled", "default", "spacer",
    "blank", "null", "none", "alt", "alt text", "alttext", "image alt", "photo alt", "slide", "slider",
    "hero", "hero image", "banner image", "featured image", "main image", "product image", "product",
    "click here", "click", "link", "here", "more", "read more", "button", "arrow", "bullet", "undefined",
    "image description", "description", "img alt", "test", "dummy", "sample", "new", "screenshot",
}
GENERIC_PATTERN = re.compile(r"^(image|img|photo|pic|picture|banner|icon|slide|graphic|logo|thumbnail|screenshot)"
                             r"[\s_-]*\d*$", re.I)
REDUNDANT_PREFIX = re.compile(r"^(an?\s+)?(image|picture|photo|photograph|graphic|pic|icon)\s+(of|showing|shows|with)\b", re.I)
CAMERA_NAME = re.compile(r"^(img|dsc|dscn|dscf|pxl|p\d{3}|mvimg|image|photo|screenshot|screen[\s_-]?shot|"
                         r"whatsapp[\s_-]?image|untitled|capture|snip)[\W_]*\d", re.I)
HASH_NAME = re.compile(r"^[a-f0-9]{16,}$|^[a-z0-9]{24,}$", re.I)
TRACKER_HINTS = ("facebook.com/tr", "doubleclick", "/pixel", "pixel.", "google-analytics", "googletagmanager",
                 "bat.bing", "analytics", "/beacon", "linkedin.com/px", "t.co/i/adsct", "scorecardresearch")
STOPWORDS = set("""a an the and or of for to in on at by with from is are was were be as it its this that
these those your our my their his her you we they i me us them into over under about than then""".split())
CONTENT_HINT = re.compile(r"hero|banner|product|feature|main|cover|article|post|gallery|chart|infographic", re.I)

SEVERITY_ORDER = {"error": 0, "warn": 1, "info": 2}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def normalize_input(raw):
    raw = (raw or "").strip()
    if raw and not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    return raw


def valid_url(u):
    try:
        p = urlparse(u)
        return p.scheme in ("http", "https") and "." in (p.hostname or "")
    except Exception:
        return False


def fetch(url, retries=1):
    headers = {"User-Agent": BROWSER_UA,
               "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
               "Accept-Language": "en-IN,en;q=0.9"}
    res = {"final_url": url, "status": None, "content": b"", "content_type": "", "error": None}
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True, stream=True)
            chunks, total = [], 0
            for chunk in r.iter_content(16384):
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_HTML_BYTES:
                    break
            r.close()
            res.update(final_url=r.url, status=r.status_code, content=b"".join(chunks),
                       content_type=r.headers.get("Content-Type", "").lower(), error=None,
                       encoding=r.encoding)
            if r.status_code in RETRY_STATUSES and attempt < retries:
                time.sleep(1.5)
                continue
            return res
        except requests.exceptions.Timeout:
            res["error"] = "The page took too long to respond (timed out)."
        except requests.exceptions.SSLError:
            res["error"] = "The site's SSL certificate could not be verified."
        except requests.exceptions.ConnectionError:
            res["error"] = "Could not connect to the site. Check the URL and try again."
        except requests.exceptions.RequestException as e:
            res["error"] = f"Request failed ({e.__class__.__name__})."
        if attempt < retries:
            time.sleep(1)
    return res


def first_srcset_url(value):
    if not value:
        return ""
    first = value.strip().split(",")[0].strip()
    return first.split()[0] if first else ""


def resolve_src(tag):
    """Return (src, lazy_detected, raw_src_attr) for an <img>/<input> tag."""
    src = (tag.get("src") or "").strip()
    lazy_src = ""
    for a in LAZY_ATTRS:
        if tag.get(a):
            lazy_src = tag.get(a).strip()
            break
    if not lazy_src:
        for a in SRCSET_ATTRS[1:]:
            if tag.get(a):
                lazy_src = first_srcset_url(tag.get(a))
                break
    placeholder = (not src) or src.startswith("data:") or src in ("#", "about:blank")
    if lazy_src and placeholder:
        return lazy_src, True, src
    if not src and tag.get("srcset"):
        return first_srcset_url(tag.get("srcset")), False, src
    return src, bool(lazy_src), src


def file_name_of(src):
    if not src or src.startswith("data:"):
        return "(inline data image)" if src else "(no src)"
    path = unquote(urlparse(src).path or "")
    name = path.rstrip("/").split("/")[-1]
    return name or path or src


def file_stem(name):
    return IMG_EXT.sub("", name or "").strip()


def ext_of(name):
    m = IMG_EXT.search(name or "")
    return m.group(1).lower() if m else ""


def norm_text(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def norm_key(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def int_attr(v):
    try:
        return int(float(str(v).lower().replace("px", "").strip()))
    except (TypeError, ValueError):
        return None


def has_dimensions(tag):
    if tag.get("width") and tag.get("height"):
        return True
    style = (tag.get("style") or "").lower().replace(" ", "")
    return ("width:" in style and "height:" in style) or "aspect-ratio" in style


def filename_to_words(name):
    stem = file_stem(name)
    stem = re.sub(r"[-_]+", " ", stem)
    stem = re.sub(r"\b\d{2,4}x\d{2,4}\b", "", stem)       # 300x200
    stem = re.sub(r"\b(v\d+|final|copy|new|min|scaled|compressed|edited)\b", "", stem, flags=re.I)
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem


def descriptive_file_name(name):
    stem = file_stem(name)
    if not stem or len(stem) < 4:
        return False
    if CAMERA_NAME.match(stem) or HASH_NAME.match(stem) or re.fullmatch(r"[\d_\-\s]+", stem):
        return False
    words = [w for w in re.split(r"[-_\s]+", stem) if w and not w.isdigit()]
    return len(words) >= 2


def link_parent(tag):
    """If this image is the only meaningful content of a link, return the <a>."""
    a = tag.find_parent("a")
    if not a or not a.get("href"):
        return None
    if a.get("aria-label") or a.get("aria-labelledby") or (a.get("title") or "").strip():
        return None
    clone_text = ""
    for s in a.find_all(string=True):
        if s.parent and s.parent.name in ("script", "style", "noscript"):
            continue
        clone_text += s
    if norm_text(clone_text):
        return None
    others = [i for i in a.find_all(["img", "svg"]) if i is not tag]
    if any((o.get("alt") or "").strip() or o.get("aria-label") for o in others):
        return None
    return a


def looks_like_tracker(src, tag):
    w, h = int_attr(tag.get("width")), int_attr(tag.get("height"))
    if (w is not None and w <= 2) and (h is not None and h <= 2):
        return True
    s = (src or "").lower()
    return any(t in s for t in TRACKER_HINTS)


def keyword_hits(text, kw):
    if not kw:
        return 0
    return len(re.findall(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", text.lower()))


# --------------------------------------------------------------------------- #
# Alt text quality rules
# --------------------------------------------------------------------------- #
def grade_alt(alt, fname, kw):
    """Return list of (severity, code, message, fix) for a NON-EMPTY alt."""
    out = []
    text = norm_text(alt)
    key = norm_key(text)
    stem_key = norm_key(file_stem(fname))

    if key in GENERIC_ALTS or GENERIC_PATTERN.match(text):
        out.append(("warn", "generic", f'Generic alt text: "{text}".',
                    "Describe what the image actually shows, e.g. \"Bar chart comparing monthly sales for 2024 and 2025\"."))
    elif IMG_EXT.search(text) or (stem_key and key == stem_key and (" " not in text)) \
            or (len(text.split()) <= 2 and CAMERA_NAME.match(text)) or (" " not in text and HASH_NAME.match(text)):
        out.append(("warn", "filename", "The alt text is just a file name.",
                    "Replace it with a short sentence that describes the image for someone who can't see it."))
    elif len(text) < ALT_MIN_LEN:
        out.append(("warn", "short", f"Very short alt text ({len(text)} characters) — probably not descriptive.",
                    "Use a few words that describe the image's content or purpose."))

    if len(text) > ALT_MAX_LEN:
        out.append(("warn", "long", f"Alt text is long ({len(text)} characters).",
                    f"Keep it under ~{ALT_MAX_LEN} characters. Move long explanations into the page copy or a caption."))

    if REDUNDANT_PREFIX.match(text):
        out.append(("info", "prefix", 'Starts with "image of" / "picture of".',
                    "Screen readers already announce it's an image — start with what it shows."))

    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in STOPWORDS]
    counts = Counter(words)
    repeated = [w for w, c in counts.items() if c >= 3]
    segments = [s.strip() for s in re.split(r"[,|;/]+", text) if s.strip()]
    kw_count = keyword_hits(text, kw)
    if repeated or (len(segments) >= 4 and all(len(s.split()) <= 3 for s in segments)) or kw_count >= 2:
        why = (f'"{repeated[0]}" repeated {counts[repeated[0]]} times' if repeated
               else (f'focus keyword used {kw_count} times' if kw_count >= 2 else "reads like a keyword list"))
        out.append(("warn", "stuffing", f"Looks keyword-stuffed ({why}).",
                    "Google advises against filling alt text with keywords. Write one natural description."))
    return out


def analyze(raw_url, keyword=""):
    url = normalize_input(raw_url)
    kw = norm_text(keyword).lower()
    view = {"input": raw_url, "fetch_ok": False, "fetch_error": None, "keyword": kw}
    if not valid_url(url):
        view["fetch_error"] = "That doesn't look like a valid URL. Try something like https://www.yourwebsite.com/your-page"
        return view

    res = fetch(url)
    if res["error"]:
        view["fetch_error"] = res["error"]
        return view
    status = res["status"]
    if status in RETRY_STATUSES:
        view["fetch_error"] = (f"The site blocked the request (HTTP {status}). Some sites use bot protection "
                               "(Cloudflare, Akamai) that stops automated checks — try another page or try again later.")
        return view
    if status and status >= 400:
        view["fetch_error"] = f"The page returned HTTP {status}, so there's nothing to audit."
        return view
    if res["content_type"] and "html" not in res["content_type"] and "xml" not in res["content_type"]:
        view["fetch_error"] = f"That URL isn't an HTML page (Content-Type: {res['content_type'].split(';')[0]})."
        return view

    try:
        html = res["content"].decode(res.get("encoding") or "utf-8", errors="replace")
    except LookupError:
        html = res["content"].decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")
    base = res["final_url"]
    base_tag = soup.find("base", href=True)
    if base_tag:
        base = urljoin(base, base_tag["href"])

    title = norm_text(soup.title.string if soup.title and soup.title.string else "")
    images = []
    seen_srcs = set()

    def add(item):
        if len(images) < MAX_IMAGES:
            item["n"] = len(images) + 1
            images.append(item)

    # ---- <img> and <input type=image>  (skip <noscript> copies of images we already have)
    candidates = soup.find_all(["img", "input"])
    main_imgs = [t for t in candidates if not t.find_parent("noscript")]
    ns_imgs = [t for t in candidates if t.find_parent("noscript")]
    main_srcs = set()
    for t in main_imgs:
        if t.name == "img":
            s, _, _ = resolve_src(t)
            main_srcs.add(urljoin(base, s) if s else "")
    ordered = main_imgs + [t for t in ns_imgs if t.name == "img" and
                           urljoin(base, resolve_src(t)[0] or "") not in main_srcs]

    for tag in ordered:
        if tag.name == "input" and (tag.get("type") or "").lower() != "image":
            continue
        kind = "image button" if tag.name == "input" else "img"
        src, lazy, raw_src = resolve_src(tag)
        abs_src = urljoin(base, src) if src and not src.startswith("data:") else src
        in_picture = bool(tag.find_parent("picture"))
        if in_picture:
            kind = "picture"
        if tag.find_parent("noscript"):
            kind = "img (noscript)"
        fname = file_name_of(abs_src)
        item = {"kind": kind, "src": abs_src, "file": fname, "alt": tag.get("alt"),
                "lazy": lazy, "status": "ok", "issues": [], "hints": [],
                "width": tag.get("width") or "", "height": tag.get("height") or "",
                "loading": (tag.get("loading") or "").lower(), "in_link": False,
                "picture_modern": False, "suggest": "", "dims": has_dimensions(tag)}
        if in_picture:
            pic = tag.find_parent("picture")
            types = " ".join((s.get("type") or "") + " " + (s.get("srcset") or "") for s in pic.find_all("source"))
            item["picture_modern"] = bool(re.search(r"webp|avif", types, re.I))

        role = (tag.get("role") or "").lower()
        aria_hidden = (tag.get("aria-hidden") or "").lower() == "true"
        aria_label = norm_text(tag.get("aria-label"))
        title_attr = norm_text(tag.get("title"))
        alt = tag.get("alt")
        link = link_parent(tag)
        item["in_link"] = bool(link)
        issues = item["issues"]

        if not src:
            issues.append(("info", "nosrc", "No image URL in the raw HTML — it's probably loaded by JavaScript.",
                           "Check this one in the browser. If the alt is also set by JavaScript, Google usually "
                           "still sees it after rendering."))

        if looks_like_tracker(abs_src, tag) and alt is None:
            item["status"] = "skip"
            item["kind"] = "tracking pixel"
            issues.append(("info", "tracker", "Tracking pixel with no alt.",
                           'Add alt="" so screen readers skip it. It has no SEO value either way.'))
        elif alt is None:
            if role in ("presentation", "none") or aria_hidden:
                item["status"] = "decorative"
                issues.append(("info", "decor-noalt", "Hidden from screen readers with role/aria-hidden but has no alt.",
                               'Add alt="" as well — it\'s the standard way to mark a decorative image.'))
            else:
                item["status"] = "missing"
                if link:
                    issues.append(("error", "link-noname", "Missing alt on an image that is the only content of a link.",
                                   "The link has no name for Google or screen readers. Describe where the link goes, "
                                   'e.g. alt="Download the free SEO checklist".'))
                else:
                    issues.append(("error", "missing", "Missing alt attribute.",
                                   "Add an alt that describes the image, or alt=\"\" if it's purely decorative."))
                if aria_label:
                    issues.append(("info", "aria-only", f'Has aria-label "{aria_label}" but no alt.',
                                   "Screen readers read aria-label, but Google Images uses alt. Copy it into alt too."))
                elif title_attr:
                    issues.append(("info", "title-only", f'Has a title ("{title_attr}") but no alt.',
                                   "A title tooltip is not a replacement for alt text — add a real alt."))
        elif not alt.strip():
            if alt != "":
                item["status"] = "warn"
                issues.append(("warn", "spaces", "Alt contains only spaces.",
                               'Use alt="" (no space) for decorative images, or describe the image.'))
            elif link:
                item["status"] = "missing"
                issues.append(("error", "link-empty", 'Empty alt ("") on an image that is the only content of a link.',
                               "The link ends up with no name. Describe where the link goes."))
            else:
                item["status"] = "decorative"
                w, h = int_attr(tag.get("width")), int_attr(tag.get("height"))
                hint_txt = " ".join([tag.get("class") and " ".join(tag.get("class")) or "", tag.get("id") or "", fname])
                big = (w and h and w >= 300 and h >= 150)
                if big or CONTENT_HINT.search(hint_txt):
                    item["status"] = "warn"
                    issues.append(("warn", "decor-content", 'Marked decorative (alt="") but it looks like a content image.',
                                   "If it carries information (hero, product, chart, infographic), give it a real "
                                   "description. Keep alt=\"\" only for pure decoration."))
        else:
            found = grade_alt(alt, fname, kw)
            issues.extend(found)
            if any(s == "warn" for s, *_ in found):
                item["status"] = "warn"
            if title_attr and norm_key(title_attr) == norm_key(alt):
                issues.append(("info", "title-dup", "title attribute repeats the alt text.",
                               "Not harmful, but redundant — you can drop the title."))

        if item["status"] in ("missing",) and descriptive_file_name(fname):
            item["suggest"] = filename_to_words(fname)
        add(item)

    # ---- inline SVG with role="img"
    for svg in soup.find_all("svg"):
        if (svg.get("role") or "").lower() != "img":
            continue
        name = norm_text(svg.get("aria-label"))
        if not name and svg.get("aria-labelledby"):
            ids = svg.get("aria-labelledby").split()
            name = norm_text(" ".join((soup.find(id=i).get_text(" ") if soup.find(id=i) else "") for i in ids))
        if not name and svg.find("title"):
            name = norm_text(svg.find("title").get_text(" "))
        item = {"kind": "svg", "src": "", "file": "(inline SVG)", "alt": name if name else None, "lazy": False,
                "status": "ok", "issues": [], "hints": [], "width": svg.get("width") or "",
                "height": svg.get("height") or "", "loading": "", "in_link": False, "picture_modern": True,
                "suggest": ""}
        if not name:
            item["status"] = "missing"
            item["issues"].append(("error", "svg-noname", 'SVG has role="img" but no accessible name.',
                                   "Add aria-label=\"…\" or a <title> inside the SVG."))
        else:
            found = grade_alt(name, "", kw)
            item["issues"].extend(found)
            if any(s == "warn" for s, *_ in found):
                item["status"] = "warn"
        add(item)

    # ---- CSS background images (inline styles only)
    bg_count = 0
    for el in soup.find_all(style=re.compile(r"background(-image)?\s*:[^;]*url\(", re.I)):
        if bg_count >= MAX_BG_IMAGES:
            break
        m = re.search(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", el.get("style", ""), re.I)
        if not m or m.group(1).startswith("data:"):
            continue
        bg_count += 1
        abs_src = urljoin(base, m.group(1).strip())
        label = norm_text(el.get("aria-label"))
        item = {"kind": "css background", "src": abs_src, "file": file_name_of(abs_src),
                "alt": label if label else None, "lazy": False, "status": "skip", "issues": [], "hints": [],
                "width": "", "height": "", "loading": "", "in_link": False, "picture_modern": False, "suggest": ""}
        item["issues"].append(("info", "bg", "CSS background image — it can't have alt text.",
                               "Fine for decoration. If it carries meaning (banner with text, product shot), use an "
                               '<img> with alt, or add role="img" and aria-label to the element.'))
        add(item)

    # ---- duplicate alt texts across different images
    by_alt = defaultdict(set)
    for it in images:
        if it["alt"] and it["alt"].strip() and it["kind"] != "css background":
            by_alt[norm_key(it["alt"])].add(it["src"] or f"#{it['n']}")
    for it in images:
        if it["alt"] and it["alt"].strip() and it["kind"] != "css background":
            k = norm_key(it["alt"])
            if k and len(by_alt[k]) >= 2 and not any(c in ("generic", "filename") for _, c, *_ in it["issues"]):
                it["issues"].append(("warn", "duplicate", f"Same alt text used on {len(by_alt[k])} different images.",
                                     "Each image should describe itself — make the alt specific to what this one shows."))
                if it["status"] == "ok":
                    it["status"] = "warn"

    # ---- extra image SEO hints
    content_imgs = [it for it in images if it["kind"] in ("img", "picture", "img (noscript)", "image button")]
    hint_groups = {
        "dims": {"title": "No width / height set", "icon": "📐",
                 "fix": "Add width and height attributes (or CSS aspect-ratio) so the layout doesn't jump while "
                        "images load. This helps your CLS (Core Web Vitals) score.", "items": []},
        "fname": {"title": "Non-descriptive file names", "icon": "🏷️",
                  "fix": "Google uses file names as a light signal. Rename files like IMG_2034.jpg to "
                         "blue-running-shoes-side-view.jpg before uploading.", "items": []},
        "lcp_lazy": {"title": "First image is lazy-loaded", "icon": "🐢",
                     "fix": 'Remove loading="lazy" from the first / hero image — it is often your LCP element and '
                            "lazy-loading it delays the biggest paint.", "items": []},
        "no_lazy": {"title": "Below-the-fold images not lazy-loaded", "icon": "⏳",
                    "fix": 'Add loading="lazy" to images further down the page so they only load when needed.',
                    "items": []},
        "format": {"title": "Older image formats (JPG / PNG / GIF)", "icon": "🗜️",
                   "fix": "Serve WebP or AVIF (for example with a <picture> element). They're usually 25–50% "
                          "smaller at the same quality.", "items": []},
    }
    for idx, it in enumerate(content_imgs):
        if it["status"] == "skip" or not it["src"] or it["src"].startswith("data:"):
            continue
        n = it["n"]
        if not it.get("dims"):
            hint_groups["dims"]["items"].append(n)
            it["hints"].append("dims")
        if not descriptive_file_name(it["file"]) and ext_of(it["file"]) != "svg":
            stem = file_stem(it["file"])
            if CAMERA_NAME.match(stem) or HASH_NAME.match(stem) or re.fullmatch(r"[\d_\-\s]+", stem or "0") \
                    or len(stem) < 4:
                hint_groups["fname"]["items"].append(n)
                it["hints"].append("fname")
        if idx == 0 and it["loading"] == "lazy":
            hint_groups["lcp_lazy"]["items"].append(n)
            it["hints"].append("lcp_lazy")
        if idx >= 4 and it["loading"] != "lazy" and not it["lazy"]:
            hint_groups["no_lazy"]["items"].append(n)
            it["hints"].append("no_lazy")
        if ext_of(it["file"]) in LEGACY_EXT and not it["picture_modern"]:
            hint_groups["format"]["items"].append(n)
            it["hints"].append("format")
    hints = [dict(key=k, **v) for k, v in hint_groups.items() if v["items"]]

    # ---- sort each image's issues and tally
    tally = Counter()
    for it in images:
        it["issues"].sort(key=lambda x: SEVERITY_ORDER[x[0]])
        for sev, code, *_ in it["issues"]:
            tally[code] += 1

    counts = Counter(it["status"] for it in images)
    scored = counts["ok"] + counts["warn"] + counts["missing"] + counts["decorative"]
    score = round(100 * (counts["ok"] + counts["decorative"] + 0.5 * counts["warn"]) / scored) if scored else 100

    img_tags = [it for it in images if it["kind"] in ("img", "picture", "img (noscript)", "image button")
                and it["status"] != "skip"]
    with_alt = sum(1 for it in img_tags if it["alt"] is not None)
    alt_lengths = [len(norm_text(it["alt"])) for it in images if it["alt"] and it["alt"].strip()
                   and it["kind"] != "css background"]
    no_src = sum(1 for it in images if any(c == "nosrc" for _, c, *_ in it["issues"]))
    lazy_found = sum(1 for it in images if it["lazy"])

    # ---- focus keyword
    kw_view = None
    if kw:
        descriptive = [it for it in images if it["alt"] and it["alt"].strip() and it["kind"] != "css background"]
        hits = [it for it in descriptive if keyword_hits(it["alt"], kw) >= 1]
        stuffed = [it for it in hits if any(c == "stuffing" for _, c, *_ in it["issues"])]
        first_content = next((it for it in images if it["kind"] in ("img", "picture") and it["status"] != "skip"
                              and it["src"] and "logo" not in (it["file"] + (it["alt"] or "")).lower()), None)
        notes = []
        if not descriptive:
            notes.append(("info", "No descriptive alt texts on the page to check the keyword against."))
        elif not hits:
            notes.append(("warn", f'None of the alt texts mention "{kw}". If an image genuinely shows it, '
                                  "say so naturally in that image's alt."))
        else:
            share = len(hits) / len(descriptive)
            if len(hits) >= 4 and share > 0.5:
                notes.append(("warn", f'"{kw}" appears in {len(hits)} of {len(descriptive)} alts ({round(share*100)}%) '
                                      "— this can look like stuffing. Only use it where the image really shows it."))
            else:
                natural = len(hits) - len(stuffed)
                if natural:
                    notes.append(("ok", f'"{kw}" appears naturally in {natural} of {len(descriptive)} alt texts.'))
            if stuffed:
                notes.append(("warn", "Keyword looks stuffed in image " +
                              ", ".join(f"#{it['n']}" for it in stuffed) + " — rewrite as one natural description."))
        if first_content and descriptive and first_content in descriptive and first_content not in hits:
            notes.append(("info", f"The first main image (#{first_content['n']}) doesn't mention the keyword. "
                                  "If it's the hero image for this topic, that's a good place for it."))
        kw_view = {"hits": len(hits), "total": len(descriptive), "notes": notes,
                   "hit_ids": [it["n"] for it in hits]}

    notes = []
    if no_src:
        notes.append(f"{no_src} image(s) have no URL in the raw HTML — they're likely added by JavaScript.")
    if len(img_tags) <= 2 and len(re.findall(r"<script", html, re.I)) > 15:
        notes.append("Very few images were found in the raw HTML but the page runs a lot of JavaScript. "
                     "Some images may be added after the page loads, which this tool can't see.")
    if len(images) >= MAX_IMAGES:
        notes.append(f"Showing the first {MAX_IMAGES} images only.")

    view.update(
        fetch_ok=True, final_url=res["final_url"], status=status, title=title, images=images,
        counts={"total": len(images), "ok": counts["ok"], "warn": counts["warn"], "missing": counts["missing"],
                "decorative": counts["decorative"], "skip": counts["skip"]},
        score=score, img_total=len(img_tags), with_alt=with_alt,
        coverage=round(100 * with_alt / len(img_tags)) if img_tags else 100,
        avg_len=round(sum(alt_lengths) / len(alt_lengths)) if alt_lengths else 0,
        lazy_found=lazy_found, link_noname=tally["link-noname"] + tally["link-empty"],
        hints=hints, notes=notes, keyword_view=kw_view,
        issue_types=issue_type_counts(tally),
    )
    return view


ISSUE_LABELS = [
    ("Missing alt", ["missing", "svg-noname"]),
    ("Image link, no name", ["link-noname", "link-empty"]),
    ("Generic", ["generic"]),
    ("File name as alt", ["filename"]),
    ("Too short", ["short"]),
    ("Too long", ["long"]),
    ("Keyword stuffing", ["stuffing"]),
    ("Duplicate alt", ["duplicate"]),
    ("Decorative but content", ["decor-content"]),
    ("Only spaces", ["spaces"]),
]


def issue_type_counts(tally):
    out = []
    for label, codes in ISSUE_LABELS:
        n = sum(tally[c] for c in codes)
        if n:
            out.append({"label": label, "n": n, "codes": codes})
    return out


STATUS_LABEL = {"ok": "Good", "warn": "Needs work", "missing": "Missing", "decorative": "Decorative",
                "skip": "Not scored"}
HINT_LABEL = {"dims": "No width/height", "fname": "Non-descriptive file name", "lcp_lazy": "First image lazy-loaded",
              "no_lazy": "Not lazy-loaded", "format": "Older format"}


def build_export(view):
    if not view or not view.get("fetch_ok"):
        return []
    rows = [["#", "Type", "Status", "Image URL", "File name", "Alt text", "Alt length", "Issues", "Fixes",
             "Image SEO hints", "Suggested starting point", "Page"]]
    for it in view["images"]:
        alt = it["alt"]
        rows.append([it["n"], it["kind"], STATUS_LABEL[it["status"]], it["src"], it["file"],
                     "(missing)" if alt is None else ('(empty "")' if alt == "" else norm_text(alt)),
                     len(norm_text(alt)) if alt else 0,
                     " | ".join(i[2] for i in it["issues"]), " | ".join(i[3] for i in it["issues"]),
                     ", ".join(HINT_LABEL[h] for h in it["hints"]), it["suggest"], view["final_url"]])
    return rows


# --------------------------------------------------------------------------- #
# Page template
# --------------------------------------------------------------------------- #
PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Image Alt Text Auditor</title>
<meta name="referrer" content="no-referrer">
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
  .kw-row{margin-top:.9rem;max-width:520px}
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
  .chart-box{position:relative;height:200px}

  .metrics{display:grid;grid-template-columns:1fr 1fr;gap:.7rem}
  .metric{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:.7rem .9rem;min-width:0}
  .metric .k{font-family:'DM Mono',monospace;font-size:.6rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
  .metric .v{font-family:'Sora',sans-serif;font-size:1.05rem;font-weight:700;margin-top:.15rem;overflow-wrap:anywhere}
  .v.mint{color:var(--mint)} .v.orange{color:var(--orange)} .v.red{color:var(--red)} .v.purple{color:var(--accent)}

  .badge{display:inline-flex;align-items:center;gap:.3rem;font-size:.66rem;font-family:'DM Mono',monospace;padding:.22rem .6rem;border-radius:100px;white-space:nowrap;border:1px solid}
  .badge.ok{background:rgba(106,247,200,.1);border-color:rgba(106,247,200,.3);color:var(--mint)}
  .badge.warn{background:rgba(247,162,106,.1);border-color:rgba(247,162,106,.35);color:var(--orange)}
  .badge.missing,.badge.error{background:rgba(247,106,106,.1);border-color:rgba(247,106,106,.35);color:var(--red)}
  .badge.decorative{background:rgba(124,106,247,.1);border-color:rgba(124,106,247,.35);color:var(--accent)}
  .badge.skip,.badge.info{background:rgba(136,136,168,.1);border-color:rgba(136,136,168,.3);color:var(--muted)}

  .sec-title{font-family:'Sora',sans-serif;font-size:1rem;font-weight:700;margin:2rem 0 .9rem;display:flex;align-items:center;gap:.6rem;flex-wrap:wrap}
  .sec-title .count{font-family:'DM Mono',monospace;font-size:.7rem;color:var(--muted);font-weight:400}
  .notes{margin-top:.8rem;display:flex;flex-direction:column;gap:.4rem}
  .note{font-size:.76rem;color:var(--orange);background:rgba(247,162,106,.06);border:1px solid rgba(247,162,106,.25);border-radius:8px;padding:.5rem .8rem}

  .chips{display:flex;gap:.4rem;flex-wrap:wrap}
  .chip{background:var(--surface);border:1px solid var(--border);color:var(--muted);padding:.35rem .8rem;border-radius:100px;font-size:.72rem;cursor:pointer;font-family:'DM Mono',monospace}
  .chip.active{background:rgba(124,106,247,.14);border-color:rgba(124,106,247,.4);color:var(--accent)}
  .tbl-tools{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center;margin-bottom:.7rem}
  .tbl-tools input{max-width:260px;padding:.5rem .8rem}
  .tbl-wrap{overflow-x:auto;background:var(--surface);border:1px solid var(--border);border-radius:12px}
  table{width:100%;border-collapse:collapse;font-size:.8rem}
  th{font-family:'DM Mono',monospace;font-size:.6rem;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);text-align:left;padding:.65rem .8rem;border-bottom:1px solid var(--border);white-space:nowrap}
  td{padding:.7rem .8rem;border-bottom:1px solid var(--border);vertical-align:top}
  tr:last-child td{border-bottom:none}
  tbody tr:hover td{background:rgba(124,106,247,.05)}
  td.num{font-family:'DM Mono',monospace;font-size:.72rem;color:var(--muted)}
  .thumb{width:84px;height:64px;border-radius:8px;background:var(--surface2) repeating-conic-gradient(#1d1d2a 0% 25%, transparent 0% 50%) 50%/14px 14px;border:1px solid var(--border);display:flex;align-items:center;justify-content:center;overflow:hidden;font-size:.6rem;color:var(--muted);font-family:'DM Mono',monospace;text-align:center}
  .thumb img{max-width:100%;max-height:100%;object-fit:contain}
  .fname{font-family:'DM Mono',monospace;font-size:.72rem;word-break:break-all;color:var(--text)}
  .fname a{color:var(--text)} .fname a:hover{color:var(--accent)}
  .kind{font-family:'DM Mono',monospace;font-size:.6rem;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-top:.25rem}
  .tag-mini{display:inline-block;font-family:'DM Mono',monospace;font-size:.58rem;padding:.05rem .4rem;border-radius:4px;background:rgba(124,106,247,.12);color:var(--accent);margin-left:.3rem;text-transform:none;letter-spacing:0}
  .alt-text{font-size:.8rem;overflow-wrap:anywhere;max-width:340px}
  .alt-text .q{color:var(--text)}
  .alt-text .none{color:var(--red);font-family:'DM Mono',monospace;font-size:.72rem}
  .alt-text .empty{color:var(--accent);font-family:'DM Mono',monospace;font-size:.72rem}
  .alt-len{font-family:'DM Mono',monospace;font-size:.62rem;color:var(--muted);margin-top:.2rem}
  .alt-len.over{color:var(--orange)}
  .kw-hit{font-family:'DM Mono',monospace;font-size:.6rem;color:var(--mint);margin-top:.2rem}
  .iss{list-style:none;display:flex;flex-direction:column;gap:.45rem;min-width:260px}
  .iss li{font-size:.76rem;line-height:1.45;padding-left:.7rem;border-left:2px solid var(--border)}
  .iss li.error{border-color:var(--red)} .iss li.warn{border-color:var(--orange)} .iss li.info{border-color:#5a5a78}
  .iss .m{font-weight:600}
  .iss .f{color:var(--muted);font-size:.72rem}
  .iss .suggest{color:var(--mint);font-size:.72rem;font-family:'DM Mono',monospace}
  .good-line{font-size:.76rem;color:var(--mint)}
  .hint-tags{margin-top:.4rem;display:flex;gap:.3rem;flex-wrap:wrap}
  .hint-tag{font-family:'DM Mono',monospace;font-size:.58rem;padding:.1rem .45rem;border-radius:4px;background:rgba(136,136,168,.1);color:var(--muted);border:1px solid rgba(136,136,168,.2)}

  .hint-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:.8rem}
  .hint-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1rem 1.1rem}
  .hint-card .t{font-weight:600;font-size:.88rem;display:flex;gap:.5rem;align-items:center}
  .hint-card .t .n{margin-left:auto;white-space:nowrap;font-family:'DM Mono',monospace;font-size:.7rem;color:var(--orange)}
  .hint-card .f{font-size:.76rem;color:var(--muted);margin-top:.4rem}
  .hint-card .ids{font-family:'DM Mono',monospace;font-size:.66rem;color:var(--muted);margin-top:.5rem;overflow-wrap:anywhere}
  .hint-card .ids a{color:var(--accent)}
  .kw-card{margin-top:.2rem}
  .kw-note{font-size:.8rem;padding:.35rem 0 .35rem .7rem;border-left:2px solid var(--border);margin-top:.4rem}
  .kw-note.ok{border-color:var(--mint)} .kw-note.warn{border-color:var(--orange)} .kw-note.info{border-color:#5a5a78}
  .all-good{color:var(--mint);font-size:.85rem;padding:.8rem 0}
  .dl-row{display:flex;gap:.6rem;flex-wrap:wrap;margin-top:1rem}
  .jump{cursor:pointer;transition:border-color .15s,transform .15s}
  .jump:hover{border-color:var(--accent);transform:translateY(-1px)}
  .metric.jump .k::after{content:' ↓';color:var(--accent);opacity:0;transition:opacity .15s}
  .metric.jump:hover .k::after{opacity:1}
  .chip.custom{background:rgba(106,247,200,.1);border-color:rgba(106,247,200,.35);color:var(--mint)}
  tr.empty-row td{text-align:center;color:var(--muted);font-size:.8rem;padding:1.4rem}
  tr.flash td{background:rgba(124,106,247,.14)!important;transition:background .6s}

  @media(max-width:1100px){.overview{grid-template-columns:220px 1fr}.overview .types{grid-column:1/-1}}
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
      <a href="#" class="sidebar-link active">🖼️&nbsp; Image Alt Text Auditor</a>
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
        Missing &amp; empty alts · generic, file-name, too short / long · keyword stuffing · duplicates · image-only links · lazy-loaded images, &lt;picture&gt;, SVG, CSS backgrounds · width/height, file names, formats
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
      <h1>🖼️ Image Alt Text <span>Auditor</span></h1>
    </div>

    <form class="card" method="POST" action="/check" onsubmit="document.getElementById('sp').classList.add('show')">
      <label class="lbl" for="url">Page URL</label>
      <div class="row-inline">
        <input type="text" id="url" name="url" placeholder="https://www.yourwebsite.com/your-page" value="{{ form.url }}">
        <button class="btn" type="submit">🔍 Audit Images</button>
      </div>
      <div class="kw-row">
        <label class="lbl" for="keyword">Focus keyword (optional) — checks it's used naturally, not stuffed</label>
        <input type="text" id="keyword" name="keyword" placeholder="Your main keyword" value="{{ form.keyword }}">
      </div>
      <div class="spinner" id="sp">Fetching the page and checking every image…</div>
      {% if error %}<div class="err">{{ error }}</div>{% endif %}
    </form>

    {% if view %}
      {% if not view.fetch_ok %}
        <div class="card" style="margin-top:1.2rem;border-color:rgba(247,106,106,.4)">
          <div class="err" style="margin-top:0">Couldn't audit this URL: {{ view.fetch_error }}</div>
        </div>
      {% else %}
      {% set sc = view.score %}
      {% set sc_col = '#6af7c8' if sc >= 80 else ('#f7a26a' if sc >= 50 else '#f76a6a') %}
      {% set c = view.counts %}

      <div class="overview">
        <div class="chart-card">
          <h3>Alt text score</h3>
          <div class="ring-wrap">
            <svg viewBox="0 0 160 160" width="160" height="160">
              <circle cx="80" cy="80" r="68" fill="none" stroke="#23233a" stroke-width="12"/>
              <circle cx="80" cy="80" r="68" fill="none" stroke="{{ sc_col }}" stroke-width="12" stroke-linecap="{{ 'round' if sc > 0 else 'butt' }}"
                      stroke-dasharray="{{ (427.26 * sc / 100) | round(2) }} 427.26" transform="rotate(-90 80 80)"/>
            </svg>
            <div class="ring-center"><div class="score" style="color:{{ sc_col }}">{{ sc }}</div><div class="sub">out of 100</div></div>
          </div>
          <div style="display:flex;justify-content:center;gap:.4rem;margin-top:.8rem;flex-wrap:wrap">
            <span class="badge missing jump" onclick="jumpTo({st:'missing',label:'Missing'})" title="Show these images">{{ c.missing }} missing</span>
            <span class="badge warn jump" onclick="jumpTo({st:'warn',label:'Needs work'})" title="Show these images">{{ c.warn }} need work</span>
          </div>
        </div>

        <div class="chart-card">
          <h3>At a glance</h3>
          <div class="metrics">
            <div class="metric jump" onclick="jumpTo({st:'all'})" title="Show all images"><div class="k">Images found</div><div class="v purple">{{ c.total }}</div></div>
            <div class="metric jump" onclick="jumpTo({codes:['missing','link-noname','decor-noalt'],label:'No alt attribute'})" title="Show images with no alt attribute"><div class="k">Alt coverage</div><div class="v {{ 'mint' if view.coverage == 100 else ('orange' if view.coverage >= 80 else 'red') }}">{{ view.coverage }}%</div></div>
            <div class="metric jump" onclick="jumpTo({st:'ok',label:'Good'})" title="Show images with good alt text"><div class="k">Good alts</div><div class="v mint">{{ c.ok }}</div></div>
            <div class="metric jump" onclick="jumpTo({st:'decorative',label:'Decorative'})" title="Show decorative images"><div class="k">Decorative (alt="")</div><div class="v purple">{{ c.decorative }}</div></div>
            <div class="metric jump" onclick="jumpTo({codes:['link-noname','link-empty'],label:'Image links with no name'})" title="Show image-only links with no name"><div class="k">Image links w/o name</div><div class="v {{ 'red' if view.link_noname else 'mint' }}">{{ view.link_noname }}</div></div>
            <div class="metric jump" onclick="jumpTo({codes:['short','long'],label:'Alt too short / too long'})" title="Show alt texts that are too short or too long"><div class="k">Avg alt length</div><div class="v {{ 'orange' if view.avg_len > 125 else '' }}">{{ view.avg_len }} chars</div></div>
          </div>
          <div class="hint" style="overflow-wrap:anywhere">Checked: <span style="color:var(--text)">{{ view.final_url }}</span> · HTTP {{ view.status }}{% if view.lazy_found %} · {{ view.lazy_found }} lazy-loaded image(s) detected{% endif %}</div>
        </div>

        <div class="chart-card types">
          <h3>Status breakdown</h3>
          <div class="chart-box"><canvas id="statusChart"></canvas></div>
        </div>
      </div>

      {% if view.notes %}
      <div class="notes">{% for n in view.notes %}<div class="note">ℹ️ {{ n }}</div>{% endfor %}</div>
      {% endif %}

      {% if view.issue_types %}
      <div class="sec-title">📊 Issues by type <span class="count">{{ view.issue_types | sum(attribute='n') }} total</span></div>
      <div class="chart-card"><div class="chart-box" style="height:{{ 60 + view.issue_types|length * 28 }}px"><canvas id="typeChart"></canvas></div></div>
      {% endif %}

      {% if view.keyword_view %}
      <div class="sec-title">🎯 Focus keyword · "{{ view.keyword }}" <span class="count">in {{ view.keyword_view.hits }} of {{ view.keyword_view.total }} alt texts</span></div>
      <div class="card kw-card">
        {% for sev, msg in view.keyword_view.notes %}<div class="kw-note {{ sev }}">{{ msg }}</div>{% endfor %}
      </div>
      {% endif %}

      <!-- ======================= IMAGES TABLE ======================= -->
      <div class="sec-title" id="imagesSection">🖼️ Every image <span class="count">{{ c.total }} found</span></div>
      {% if view.images %}
      <div class="tbl-tools">
        <div class="chips" id="statusChips">
          <button type="button" class="chip active" data-st="all">All ({{ c.total }})</button>
          {% if c.missing %}<button type="button" class="chip" data-st="missing">Missing ({{ c.missing }})</button>{% endif %}
          {% if c.warn %}<button type="button" class="chip" data-st="warn">Needs work ({{ c.warn }})</button>{% endif %}
          {% if c.ok %}<button type="button" class="chip" data-st="ok">Good ({{ c.ok }})</button>{% endif %}
          {% if c.decorative %}<button type="button" class="chip" data-st="decorative">Decorative ({{ c.decorative }})</button>{% endif %}
          {% if c.skip %}<button type="button" class="chip" data-st="skip">Not scored ({{ c.skip }})</button>{% endif %}
        </div>
        <input type="text" id="rowFilter" placeholder="Search file name or alt…">
      </div>
      <div class="tbl-wrap">
        <table id="imgTable">
          <thead><tr><th>#</th><th>Preview</th><th>Image</th><th>Alt text</th><th>Status</th><th>Issues &amp; how to fix</th></tr></thead>
          <tbody>
          {% for it in view.images %}
            {% set altlen = (it.alt | trim | length) if it.alt else 0 %}
            <tr id="img-{{ it.n }}" data-st="{{ it.status }}" data-codes=" {% for i in it.issues %}{{ i[1] }} {% endfor %}" data-text="{{ (it.file ~ ' ' ~ (it.alt or '') ~ ' ' ~ it.src) | lower }}">
              <td class="num">{{ it.n }}</td>
              <td>
                <div class="thumb">
                  {% if it.src %}<img src="{{ it.src }}" loading="lazy" referrerpolicy="no-referrer" alt="" onerror="this.parentNode.textContent='no preview'">{% else %}{{ 'SVG' if it.kind == 'svg' else 'no src' }}{% endif %}
                </div>
              </td>
              <td style="max-width:260px">
                <div class="fname">{% if it.src and not it.src.startswith('data:') %}<a href="{{ it.src }}" target="_blank" rel="noopener">{{ it.file }}</a>{% else %}{{ it.file }}{% endif %}</div>
                <div class="kind">{{ it.kind }}{% if it.lazy %}<span class="tag-mini">lazy</span>{% endif %}{% if it.in_link %}<span class="tag-mini">in link</span>{% endif %}{% if it.width and it.height %}<span class="tag-mini">{{ it.width }}×{{ it.height }}</span>{% endif %}</div>
              </td>
              <td class="alt-text">
                {% if it.alt is none %}<span class="none">— no alt —</span>
                {% elif it.alt == '' %}<span class="empty">alt="" (decorative)</span>
                {% else %}<span class="q">"{{ it.alt | trim }}"</span>
                  <div class="alt-len {{ 'over' if altlen > 125 }}">{{ altlen }} chars</div>
                  {% if view.keyword_view and it.n in view.keyword_view.hit_ids %}<div class="kw-hit">✓ contains focus keyword</div>{% endif %}
                {% endif %}
              </td>
              <td><span class="badge {{ it.status }}">{{ status_label[it.status] }}</span></td>
              <td>
                {% if it.issues %}
                <ul class="iss">
                  {% for sev, code, msg, fix in it.issues %}
                    <li class="{{ sev }}"><div class="m">{{ msg }}</div><div class="f">{{ fix }}</div></li>
                  {% endfor %}
                  {% if it.suggest %}<li class="info"><div class="suggest">💡 Starting point from the file name: "{{ it.suggest }}" — rewrite it to describe the image.</div></li>{% endif %}
                </ul>
                {% else %}
                  <div class="good-line">{{ '✓ Correctly marked as decorative' if it.status == 'decorative' else '✓ Descriptive and a good length' }}</div>
                {% endif %}
                {% if it.hints %}<div class="hint-tags">{% for h in it.hints %}<span class="hint-tag">{{ hint_label[h] }}</span>{% endfor %}</div>{% endif %}
              </td>
            </tr>
          {% endfor %}
          <tr class="empty-row" id="emptyRow" style="display:none"><td colspan="6">✓ No images match this filter — nothing to fix here.</td></tr>
          </tbody>
        </table>
      </div>
      {% else %}
        <div class="card"><div class="all-good" style="color:var(--muted)">No images were found in this page's HTML. If you can see images in the browser, they're probably loaded by JavaScript.</div></div>
      {% endif %}

      <!-- ======================= EXTRA HINTS ======================= -->
      <div class="sec-title">⚡ Extra image SEO hints <span class="count">not part of the score</span></div>
      {% if view.hints %}
      <div class="hint-grid">
        {% for h in view.hints %}
        <div class="hint-card">
          <div class="t">{{ h.icon }} {{ h.title }} <span class="n">{{ h['items'] | length }} image{{ 's' if h['items']|length != 1 }}</span></div>
          <div class="f">{{ h.fix }}</div>
          <div class="ids">Images: {% for n in h['items'][:40] %}<a href="#img-{{ n }}" onclick="flash({{ n }})">#{{ n }}</a>{{ ', ' if not loop.last }}{% endfor %}{% if h['items']|length > 40 %} …{% endif %}</div>
        </div>
        {% endfor %}
      </div>
      {% else %}
        <div class="all-good">✓ No extra image SEO issues found.</div>
      {% endif %}

      <div class="dl-row">
        <button type="button" class="btn ghost" onclick="downloadCSV()">⬇️ Download full report (CSV)</button>
      </div>

      <script>
        const REPORT = {{ export | tojson }};
        const C = {{ view.counts | tojson }};
        const TYPES = {{ view.issue_types | tojson }};
        const ST_KEY = {'Good': 'ok', 'Needs work': 'warn', 'Missing': 'missing', 'Decorative': 'decorative', 'Not scored': 'skip'};
        const statusData = [
          ['Good', C.ok, '#6af7c8'], ['Needs work', C.warn, '#f7a26a'], ['Missing', C.missing, '#f76a6a'],
          ['Decorative', C.decorative, '#7c6af7'], ['Not scored', C.skip, '#5a5a78']
        ].filter(d => d[1] > 0);
        if (statusData.length) {
          new Chart(document.getElementById('statusChart'), {
            type: 'doughnut',
            data: {labels: statusData.map(d => d[0]),
                   datasets: [{data: statusData.map(d => d[1]), backgroundColor: statusData.map(d => d[2]),
                               borderColor: '#12121a', borderWidth: 2}]},
            options: {maintainAspectRatio: false, cutout: '62%',
                      onHover: (e, els) => { e.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
                      onClick: (e, els) => { if (els.length) { const lbl = statusData[els[0].index][0];
                        jumpTo({st: ST_KEY[lbl], label: lbl}); } },
                      plugins: {legend: {position: 'right', labels: {color: '#e8e8f0', boxWidth: 10, font: {size: 11}}}}}
          });
        }
        if (TYPES.length && document.getElementById('typeChart')) {
          new Chart(document.getElementById('typeChart'), {
            type: 'bar',
            data: {labels: TYPES.map(t => t.label),
                   datasets: [{label: 'Images', data: TYPES.map(t => t.n), borderRadius: 4,
                                backgroundColor: TYPES.map(t => ['Missing alt', 'Image link, no name'].includes(t.label) ? '#f76a6a' : '#f7a26a')}]},
            options: {indexAxis: 'y', maintainAspectRatio: false,
                      onHover: (e, els) => { e.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
                      onClick: (e, els) => { if (els.length) { const t = TYPES[els[0].index];
                        jumpTo({codes: t.codes, label: t.label}); } },
                      scales: {x: {ticks: {color: '#8888a8', precision: 0}, grid: {color: '#23233a'}},
                               y: {ticks: {color: '#e8e8f0', font: {family: 'DM Mono', size: 11}}, grid: {display: false}}},
                      plugins: {legend: {display: false}}}
          });
        }

        let activeSt = 'all', activeCodes = null;
        const rf = document.getElementById('rowFilter');
        function filterRows() {
          const q = rf ? rf.value.toLowerCase() : '';
          let shown = 0;
          document.querySelectorAll('#imgTable tbody tr[id^="img-"]').forEach(tr => {
            const stOk = activeSt === 'all' || tr.dataset.st === activeSt;
            const codeOk = !activeCodes || activeCodes.some(c => tr.dataset.codes.includes(' ' + c + ' '));
            const show = stOk && codeOk && tr.dataset.text.includes(q);
            tr.style.display = show ? '' : 'none';
            if (show) shown++;
          });
          const er = document.getElementById('emptyRow'); if (er) er.style.display = shown ? 'none' : '';
        }
        let customLabel = null;
        function setChips() {
          const chips = document.getElementById('statusChips'); if (!chips) return;
          let custom = chips.querySelector('.chip.custom');
          if (customLabel) {
            if (!custom) { custom = document.createElement('button'); custom.type = 'button'; custom.className = 'chip custom';
                           custom.title = 'Clear filter'; custom.onclick = () => jumpTo({st: 'all'}, false); chips.appendChild(custom); }
            custom.textContent = 'Showing: ' + customLabel + '  ✕';
          } else if (custom) { custom.remove(); }
          chips.querySelectorAll('.chip[data-st]').forEach(c => c.classList.toggle('active', !customLabel && c.dataset.st === activeSt));
        }
        function jumpTo(f, scroll = true) {
          activeSt = f.st || 'all';
          activeCodes = f.codes || null;
          const hasChip = !!document.querySelector('#statusChips .chip[data-st="' + activeSt + '"]');
          customLabel = (activeCodes || !hasChip) ? (f.label || 'filtered') : null;
          if (rf) rf.value = '';
          setChips(); filterRows();
          if (scroll) { const sec = document.getElementById('imagesSection'); if (sec) sec.scrollIntoView({behavior: 'smooth', block: 'start'}); }
        }
        document.querySelectorAll('#statusChips .chip[data-st]').forEach(ch => ch.addEventListener('click', () => jumpTo({st: ch.dataset.st}, false)));
        if (rf) rf.addEventListener('input', filterRows);

        function flash(n) {
          jumpTo({st: 'all'}, false);
          const tr = document.getElementById('img-' + n);
          if (tr) { tr.classList.add('flash'); setTimeout(() => tr.classList.remove('flash'), 1400); }
        }
        function csvCell(v) { v = (v === null || v === undefined) ? '' : String(v); return '"' + v.replace(/"/g, '""') + '"'; }
        function downloadCSV() {
          const csv = '﻿' + REPORT.map(r => r.map(csvCell).join(',')).join('\r\n');
          const blob = new Blob([csv], {type: 'text/csv;charset=utf-8'});
          const a = document.createElement('a');
          a.href = URL.createObjectURL(blob);
          a.download = 'image-alt-text-audit.csv';
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


def render(view=None, error=None, form=None):
    form = form or {"url": "", "keyword": ""}
    return render_template_string(PAGE, view=view, error=error, form=form, export=build_export(view),
                                  status_label=STATUS_LABEL, hint_label=HINT_LABEL)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/", methods=["GET"])
def home():
    url = request.args.get("url", "").strip()
    keyword = request.args.get("keyword", "").strip()
    form = {"url": url, "keyword": keyword}
    return render(analyze(url, keyword) if url else None, form=form)


@app.route("/check", methods=["POST"])
def check():
    url = request.form.get("url", "").strip()
    keyword = request.form.get("keyword", "").strip()
    form = {"url": url, "keyword": keyword}
    if not url:
        return render(error="Please enter a page URL.", form=form)
    return render(analyze(url, keyword), form=form)


@app.route("/healthz")
def healthz():
    return "ok"


if __name__ == "__main__":
    app.run(debug=True, port=8501)
