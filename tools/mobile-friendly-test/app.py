"""
Mobile Friendly Test — Dashboard Edition
Checks whether a page works well on phones: viewport, content width, font
sizes, tap targets, pop-ups, plugins, responsive images, mobile speed and
mobile-vs-desktop content parity (mobile-first indexing).
Uses Google's free PageSpeed Insights API (Lighthouse, mobile) + its own
HTML checks.
Flask app | by Mahalakshmi Marimuthu
"""

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

PSI_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
PSI_API_KEY = os.environ.get("PSI_API_KEY", "").strip()
PSI_TIMEOUT = 70
FETCH_TIMEOUT = 15
MAX_STYLESHEETS = 5
MAX_CSS_BYTES = 600_000

MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# Results are cached per URL for 10 minutes, so re-testing is instant and the
# quick + full requests share one fetch instead of doing the work twice.
CACHE_TTL = 600
ON_VERCEL = bool(os.environ.get("VERCEL"))
EXEC = ThreadPoolExecutor(max_workers=8)
_CACHE, _LOCK = {}, threading.Lock()


def cached(kind: str, url: str, fn):
    """Returns a Future for fn(url), reusing a fresh or in-flight one."""
    key = (kind, url)
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            fut = hit[1]
            if not (fut.done() and fut.exception()):
                return fut
        fut = EXEC.submit(fn, url)
        _CACHE[key] = (now, fut)
        if len(_CACHE) > 60:
            for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:20]:
                _CACHE.pop(k, None)
        return fut

# Scripts that commonly show pop-ups / overlays (engagement, lead-gen, push prompts)
POPUP_SCRIPTS = {
    "webengage": "WebEngage", "moengage": "MoEngage", "clevertap": "CleverTap",
    "wzrk": "CleverTap", "onesignal": "OneSignal", "optinmonster": "OptinMonster",
    "sumo.com": "Sumo", "hellobar": "Hello Bar", "poptin": "Poptin",
    "privy": "Privy", "wisepops": "Wisepops", "sleeknote": "Sleeknote",
    "izooto": "iZooto", "pushowl": "PushOwl", "getsitecontrol": "GetSiteControl",
}
POPUP_WORDS = re.compile(
    r"(popup|pop-up|interstitial|newsletter|subscribe|exit-intent|lead-?form|"
    r"app-?banner|download-?app|smartbanner|offer-?modal)", re.I
)


class PSIError(Exception):
    pass


# ---------------------------------------------------------------- helpers
def clean_url(raw: str) -> str:
    u = (raw or "").strip()
    if u and not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u


def strip_md(text: str) -> str:
    return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text or "")


def strip_media_blocks(css: str) -> str:
    """Removes comments and @media/@supports/@container blocks (brace-matched)."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out, i = [], 0
    pat = re.compile(r"@(media|supports|container)\b", re.I)
    while True:
        m = pat.search(css, i)
        if not m:
            out.append(css[i:])
            break
        out.append(css[i:m.start()])
        brace = css.find("{", m.end())
        if brace == -1:
            break
        depth, j = 1, brace + 1
        while j < len(css) and depth:
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
            j += 1
        i = j
    return "".join(out)


def rate(value, good, poor):
    if value is None:
        return None
    if value <= good:
        return "pass"
    if value <= poor:
        return "warn"
    return "fail"


# ---------------------------------------------------------------- fetching
def fetch_html(url: str, ua: str) -> dict:
    try:
        r = requests.get(
            url, timeout=FETCH_TIMEOUT, allow_redirects=True,
            headers={"User-Agent": ua, "Accept": "text/html,application/xhtml+xml",
                     "Accept-Language": "en-IN,en;q=0.9"},
        )
    except requests.exceptions.RequestException as e:
        return {"ok": False, "error": f"Couldn't fetch the page ({type(e).__name__})."}
    if r.status_code >= 400:
        return {"ok": False, "error": f"The site returned HTTP {r.status_code} (it may block automated checks)."}
    return {"ok": True, "html": r.text, "final_url": r.url, "status": r.status_code}


def fetch_css(page_url: str, soup: BeautifulSoup) -> str:
    parts = [s.get_text() for s in soup.find_all("style")]
    hrefs = []
    for link in soup.find_all("link", href=True):
        rel = " ".join(link.get("rel") or []).lower()
        media = (link.get("media") or "").lower()
        if "stylesheet" in rel and "print" not in media:
            hrefs.append(urljoin(page_url, link["href"]))
    hrefs = hrefs[:MAX_STYLESHEETS]

    def get(u):
        try:
            r = requests.get(u, timeout=8, headers={"User-Agent": MOBILE_UA}, stream=True)
            if r.status_code != 200:
                return ""
            data = r.raw.read(MAX_CSS_BYTES, decode_content=True)
            return data.decode(r.encoding or "utf-8", errors="ignore")
        except Exception:
            return ""

    if hrefs:
        with ThreadPoolExecutor(max_workers=5) as pool:
            parts.extend(pool.map(get, hrefs))
    return "\n".join(parts)


def gather_page(url: str) -> dict:
    """Mobile + desktop HTML fetched in parallel, then the page's CSS."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_mob = pool.submit(fetch_html, url, MOBILE_UA)
        f_desk = pool.submit(fetch_html, url, DESKTOP_UA)
        mob, desk = f_mob.result(), f_desk.result()
    css = ""
    if mob.get("ok"):
        soup = BeautifulSoup(mob["html"], "html.parser")
        css = strip_media_blocks(fetch_css(mob["final_url"], soup))
    return {"mob": mob, "desk": desk, "css": css}


def fetch_psi(url: str) -> dict:
    if not PSI_API_KEY:
        raise PSIError("No PSI_API_KEY set — screenshot, tap-target and speed checks were skipped.")
    params = [("url", url), ("key", PSI_API_KEY), ("strategy", "mobile")]
    for c in ("performance", "accessibility", "seo"):
        params.append(("category", c))
    try:
        resp = requests.get(PSI_ENDPOINT, params=params, timeout=PSI_TIMEOUT)
    except requests.exceptions.Timeout:
        raise PSIError("Google PageSpeed Insights took too long to respond.")
    except requests.exceptions.RequestException as e:
        raise PSIError(f"Couldn't reach PageSpeed Insights ({type(e).__name__}).")
    try:
        data = resp.json()
    except ValueError:
        raise PSIError(f"PageSpeed Insights returned an unreadable response (HTTP {resp.status_code}).")
    if resp.status_code != 200:
        raise PSIError((data.get("error") or {}).get("message") or f"HTTP {resp.status_code}")
    lh = data.get("lighthouseResult") or {}
    rt = lh.get("runtimeError")
    if rt and rt.get("code"):
        raise PSIError(rt.get("message") or rt.get("code"))
    return data


# ---------------------------------------------------------------- analysis
def page_facts(html: str, final_url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.get_text(strip=True) if soup.title else "")
    h1 = soup.find("h1")
    h1 = h1.get_text(" ", strip=True) if h1 else ""
    links = len(soup.find_all("a", href=True))
    imgs = len(soup.find_all("img"))
    ld = len(soup.find_all("script", type="application/ld+json"))
    body = BeautifulSoup(html, "html.parser")
    for t in body(["script", "style", "noscript", "template", "svg"]):
        t.decompose()
    words = len(re.findall(r"\w+", body.get_text(" ")))
    return {"soup": soup, "title": title, "h1": h1, "links": links, "images": imgs,
            "schema": ld, "words": words, "final_url": final_url}


def psi_audit(audits: dict, aid: str):
    a = audits.get(aid)
    if not a:
        return None
    mode = a.get("scoreDisplayMode")
    if mode in ("notApplicable",):
        return {"score": 1, "title": a.get("title", ""), "display": "", "items": 0}
    if mode in ("manual", "informative", "error") or a.get("score") is None:
        return None
    items = ((a.get("details") or {}).get("items")) or []
    return {"score": a["score"], "title": a.get("title", ""),
            "display": a.get("displayValue", ""), "items": len(items)}


def mk(key, name, weight, status, found, why, fix, snippet="", critical=False):
    return {"key": key, "name": name, "weight": weight, "status": status, "found": found,
            "why": why, "fix": fix, "snippet": snippet, "critical": critical}


def run_checks(url: str, page: dict, psi: dict, psi_error: str, pending: bool = False) -> dict:
    mob, desk = page["mob"], page["desk"]
    audits = ((psi or {}).get("lighthouseResult") or {}).get("audits") or {}
    facts = page_facts(mob["html"], mob["final_url"]) if mob.get("ok") else None
    dfacts = page_facts(desk["html"], desk["final_url"]) if desk.get("ok") else None
    soup = facts["soup"] if facts else None
    css = page["css"]
    checks = []

    # 1. Viewport
    vp_tag = soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)}) if soup else None
    vp = (vp_tag.get("content") or "").lower().replace(" ", "") if vp_tag else None
    why = "Without a viewport tag, phones render the page at desktop width (~980px) and shrink it — text becomes tiny and users must pinch-zoom."
    fix = "Add this tag inside <head>:"
    snip = '<meta name="viewport" content="width=device-width, initial-scale=1">'
    if soup is not None:
        if vp is None:
            checks.append(mk("viewport", "Viewport is set for mobile", 15, "fail", "No viewport meta tag found.", why, fix, snip, True))
        elif "width=device-width" not in vp:
            checks.append(mk("viewport", "Viewport is set for mobile", 15, "fail", f"Viewport is '{vp}' — it doesn't use width=device-width.", why, "Replace it with:", snip, True))
        else:
            checks.append(mk("viewport", "Viewport is set for mobile", 15, "pass", f"Found: {vp}", why, "Nothing to fix.", "", True))
    else:
        a = psi_audit(audits, "viewport")
        if a:
            st = "pass" if a["score"] >= 0.9 else "fail"
            checks.append(mk("viewport", "Viewport is set for mobile", 15, st, a["title"], why, fix if st == "fail" else "Nothing to fix.", snip if st == "fail" else "", True))

    # 2. Zoom allowed
    why = "Blocking pinch-zoom (user-scalable=no or maximum-scale below 2) makes the page hard to read for users with low vision — Lighthouse flags it as an accessibility issue."
    if vp is not None:
        blocked = bool(re.search(r"user-scalable=(no|0)", vp))
        m = re.search(r"maximum-scale=([\d.]+)", vp)
        if m:
            try:
                blocked = blocked or float(m.group(1)) < 2
            except ValueError:
                pass
        checks.append(mk("zoom", "Users can pinch-to-zoom", 6, "warn" if blocked else "pass",
                         "Zoom is disabled in the viewport tag." if blocked else "Zoom is allowed.",
                         why, "Remove user-scalable=no and maximum-scale from the viewport tag." if blocked else "Nothing to fix.",
                         snip if blocked else ""))
    else:
        a = psi_audit(audits, "meta-viewport")
        if a:
            st = "pass" if a["score"] >= 0.9 else "warn"
            checks.append(mk("zoom", "Users can pinch-to-zoom", 6, st, a["title"], why,
                             "Remove user-scalable=no and maximum-scale from the viewport tag." if st != "pass" else "Nothing to fix."))

    # 3. Content fits screen
    why = "If content is wider than the screen, users have to scroll sideways — one of the most common mobile usability failures."
    fix = "Replace fixed pixel widths with fluid ones, and make media shrink to fit:"
    snip3 = "img, video, iframe, table { max-width: 100%; height: auto; }\n.container { width: 100%; max-width: 1200px; }"
    a = psi_audit(audits, "content-width")
    wide = []
    if soup is not None:
        for w in re.findall(r"(?<![-\w])width\s*:\s*(\d{3,5})(?:\.\d+)?px", css):
            if int(w) > 480:
                wide.append(f"width:{w}px")
        for el in soup.find_all(style=True):
            for w in re.findall(r"(?<![-\w])width\s*:\s*(\d{3,5})(?:\.\d+)?px", el["style"]):
                if int(w) > 480:
                    wide.append(f"<{el.name}> width:{w}px")
        for el in soup.find_all(["table", "iframe"], width=True):
            if str(el["width"]).isdigit() and int(el["width"]) > 480:
                wide.append(f"<{el.name} width=\"{el['width']}\">")
    if a:
        st = "pass" if a["score"] >= 0.9 else "fail"
        found = "Google's mobile render shows content fits the screen." if st == "pass" else "Google's mobile render found content wider than the screen."
        checks.append(mk("width", "Content fits the screen", 15, st, found, why, fix if st == "fail" else "Nothing to fix.", snip3 if st == "fail" else "", True))
    elif soup is not None:
        if wide:
            ex = ", ".join(sorted(set(wide))[:4])
            checks.append(mk("width", "Content fits the screen", 15, "warn",
                             f"{len(wide)} fixed width(s) wider than a phone outside media queries (e.g. {ex}). Check the screenshot for sideways scrolling.",
                             why, fix, snip3, True))
        else:
            checks.append(mk("width", "Content fits the screen", 15, "pass", "No fixed widths wider than a phone screen found.", why, "Nothing to fix.", "", True))

    # 4. Font size
    why = "Text below ~12px is hard to read on a phone. Google recommends a base font size of at least 16px for body text."
    fix = "Use a readable base size and relative units:"
    snip4 = "body { font-size: 16px; line-height: 1.5; }\nsmall, .note { font-size: 0.875rem; }"
    a = psi_audit(audits, "font-size")
    if a:
        st = "pass" if a["score"] >= 0.9 else "fail"
        checks.append(mk("font", "Text is readable without zooming", 10, st, a["display"] or a["title"], why,
                         fix if st != "pass" else "Nothing to fix.", snip4 if st != "pass" else ""))
    elif soup is not None:
        small = [float(s) for s in re.findall(r"font-size\s*:\s*(\d+(?:\.\d+)?)px", css) if float(s) < 12]
        for el in soup.find_all(style=True):
            small += [float(s) for s in re.findall(r"font-size\s*:\s*(\d+(?:\.\d+)?)px", el["style"]) if float(s) < 12]
        if len(small) >= 3:
            checks.append(mk("font", "Text is readable without zooming", 10, "warn",
                             f"{len(small)} CSS rule(s) set text smaller than 12px (smallest: {min(small):g}px).", why, fix, snip4))
        else:
            checks.append(mk("font", "Text is readable without zooming", 10, "pass", "No widespread tiny font sizes found.", why, "Nothing to fix."))

    # 5. Tap targets
    why = "Buttons and links that are too small or too close together cause mis-taps. Aim for at least 48×48px touch areas with 8px spacing."
    fix = "Give links and buttons more padding and space:"
    snip5 = "a, button { min-height: 48px; min-width: 48px; padding: 12px 16px; }\nnav a + a { margin-left: 8px; }"
    a = psi_audit(audits, "target-size") or psi_audit(audits, "tap-targets")
    if a:
        st = "pass" if a["score"] >= 0.9 else "warn"
        found = "Tap targets are large enough and well spaced." if st == "pass" else f"{a['items'] or 'Some'} tap target(s) are too small or too close together."
        checks.append(mk("tap", "Buttons & links are easy to tap", 10, st, found, why, fix if st != "pass" else "Nothing to fix.", snip5 if st != "pass" else ""))
    elif pending:
        checks.append(mk("tap", "Buttons & links are easy to tap", 10, "pending", "Waiting for Google's mobile render…", why, ""))
    else:
        checks.append(mk("tap", "Buttons & links are easy to tap", 10, "skip", psi_error or "Google didn't return tap-target data for this page.", why, ""))

    # 6. Plugins
    why = "Flash, Java applets and similar plugins don't run on phones — any content inside them is invisible to mobile users."
    if soup is not None:
        bad = soup.find_all("applet")
        for el in soup.find_all(["object", "embed"]):
            t = (el.get("type") or "") + (el.get("data") or "") + (el.get("src") or "")
            if re.search(r"(flash|\.swf|java|silverlight)", t, re.I):
                bad.append(el)
        checks.append(mk("plugins", "No unsupported plugins (Flash etc.)", 8, "fail" if bad else "pass",
                         f"{len(bad)} plugin element(s) found." if bad else "No Flash/Java/Silverlight content.",
                         why, "Replace plugin content with HTML5 video, images or plain HTML." if bad else "Nothing to fix.", "", True))

    # 7. Responsive images
    why = "Phones on mobile data shouldn't download desktop-sized images. srcset/sizes (or <picture>) lets the browser pick the right size."
    snip7 = '<img src="card-800.webp"\n     srcset="card-400.webp 400w, card-800.webp 800w"\n     sizes="(max-width: 600px) 100vw, 800px"\n     width="800" height="450" alt="..." loading="lazy">'
    if soup is not None:
        imgs = soup.find_all("img")
        if len(imgs) < 3:
            checks.append(mk("images", "Images are sized for mobile", 6, "pass", f"{len(imgs)} image(s) on the page — nothing significant to optimise.", why, "Nothing to fix."))
        else:
            resp = [i for i in imgs if i.get("srcset") or i.get("data-srcset") or (i.parent and i.parent.name == "picture")]
            share = round(100 * len(resp) / len(imgs))
            st = "pass" if share >= 50 else "warn"
            checks.append(mk("images", "Images are sized for mobile", 6, st, f"{len(resp)} of {len(imgs)} images ({share}%) use srcset/<picture>.",
                             why, "Add srcset and sizes to large content images:" if st == "warn" else "Nothing to fix.", snip7 if st == "warn" else ""))

    # 8. Intrusive pop-ups
    why = "Google can rank pages lower when a pop-up covers the main content right after a user lands from search on mobile. Cookie/age notices and small banners are fine."
    if soup is not None:
        tools = set()
        for s in soup.find_all("script"):
            blob = (s.get("src") or "") + " " + (s.string or "")[:3000]
            for k, v in POPUP_SCRIPTS.items():
                if k in blob.lower():
                    tools.add(v)
        fixed = []
        for el in soup.find_all(True, attrs={"class": True}):
            cls = " ".join(el.get("class")) + " " + (el.get("id") or "")
            st_attr = (el.get("style") or "").replace(" ", "").lower()
            if POPUP_WORDS.search(cls) and "position:fixed" in st_attr and "display:none" not in st_attr:
                fixed.append(cls.strip()[:40])
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            b = body.replace(" ", "").lower()
            if POPUP_WORDS.search(sel) and "position:fixed" in b and "display:none" not in b and "cookie" not in sel.lower():
                fixed.append(sel.strip()[:40])
        if fixed or tools:
            bits = []
            if fixed:
                bits.append(f"{len(set(fixed))} fixed-position pop-up style element(s) (e.g. {', '.join(sorted(set(fixed))[:2])})")
            if tools:
                bits.append("pop-up/engagement scripts: " + ", ".join(sorted(tools)))
            checks.append(mk("popups", "No intrusive pop-ups on landing", 10, "warn", "Found " + "; ".join(bits) + ". Check the screenshot to confirm nothing covers the content.",
                             why, "Delay pop-ups until the user scrolls or engages, keep them small (e.g. a bottom banner), and never cover the main content on first load."))
        else:
            checks.append(mk("popups", "No intrusive pop-ups on landing", 10, "pass", "No obvious pop-up/overlay patterns found. Check the screenshot to confirm.", why, "Nothing to fix."))

    # 9. Mobile vs desktop parity
    why = "Google indexes the mobile version of your page (mobile-first indexing). Content, headings or structured data missing on mobile won't count for ranking."
    parity = None
    if facts and dfacts:
        parity = [
            ("Title", facts["title"] or "—", dfacts["title"] or "—", "pass" if facts["title"] == dfacts["title"] else "warn"),
            ("H1", facts["h1"] or "—", dfacts["h1"] or "—", "pass" if facts["h1"] == dfacts["h1"] else "warn"),
            ("Word count", facts["words"], dfacts["words"], "pass" if facts["words"] >= 0.8 * dfacts["words"] else "fail"),
            ("Links", facts["links"], dfacts["links"], "pass" if facts["links"] >= 0.7 * dfacts["links"] else "warn"),
            ("Images", facts["images"], dfacts["images"], "pass" if facts["images"] >= 0.7 * dfacts["images"] else "warn"),
            ("Structured data blocks", facts["schema"], dfacts["schema"], "pass" if facts["schema"] >= dfacts["schema"] else "fail"),
        ]
        mh, dh = urlparse(facts["final_url"]).netloc, urlparse(dfacts["final_url"]).netloc
        if mh != dh:
            parity.append(("Final URL host", mh, dh, "warn"))
        fails = [p[0] for p in parity if p[3] == "fail"]
        warns = [p[0] for p in parity if p[3] == "warn"]
        st = "fail" if fails else ("warn" if warns else "pass")
        found = "Mobile and desktop versions match." if st == "pass" else "Differences in: " + ", ".join(fails + warns) + "."
        checks.append(mk("parity", "Same content on mobile & desktop", 12, st, found, why,
                         "Serve the same main content, headings, links and structured data to phones — hide with CSS only if it's still in the HTML." if st != "pass" else "Nothing to fix.", "", True))
    else:
        checks.append(mk("parity", "Same content on mobile & desktop", 12, "skip", "Couldn't fetch both versions of the page to compare.", why, ""))

    # 10. Mobile speed (Core Web Vitals)
    vitals = None
    why = "Page experience on mobile, measured by Core Web Vitals, is part of how Google judges a page — slow phones on slow networks feel it most."
    if audits:
        le = (psi.get("loadingExperience") or {}).get("metrics") or {}

        def fp(k):
            v = le.get(k)
            return v.get("percentile") if v else None

        lcp = fp("LARGEST_CONTENTFUL_PAINT_MS")
        lcp_src = "real users"
        if lcp is None:
            lcp, lcp_src = (audits.get("largest-contentful-paint") or {}).get("numericValue"), "lab"
        cls_v = fp("CUMULATIVE_LAYOUT_SHIFT_SCORE")
        cls_src = "real users"
        if cls_v is not None:
            cls_v = cls_v / 100
        else:
            cls_v, cls_src = (audits.get("cumulative-layout-shift") or {}).get("numericValue"), "lab"
        inp = fp("INTERACTION_TO_NEXT_PAINT")
        inp_name, inp_src, inp_good, inp_poor = "INP", "real users", 200, 500
        if inp is None:
            inp, inp_name, inp_src, inp_good, inp_poor = (audits.get("total-blocking-time") or {}).get("numericValue"), "TBT", "lab", 200, 600
        perf = (((psi.get("lighthouseResult") or {}).get("categories") or {}).get("performance") or {}).get("score")
        vitals = {
            "perf": round(perf * 100) if perf is not None else None,
            "rows": [
                {"name": "LCP", "help": "Main content load", "display": f"{lcp/1000:.2f}s" if lcp is not None else "—", "rating": rate(lcp, 2500, 4000), "src": lcp_src},
                {"name": "CLS", "help": "Layout stability", "display": f"{cls_v:.3f}" if cls_v is not None else "—", "rating": rate(cls_v, 0.1, 0.25), "src": cls_src},
                {"name": inp_name, "help": "Tap responsiveness", "display": f"{round(inp)}ms" if inp is not None else "—", "rating": rate(inp, inp_good, inp_poor), "src": inp_src},
            ],
        }
        ratings = [r["rating"] for r in vitals["rows"] if r["rating"]]
        st = "fail" if "fail" in ratings else ("warn" if "warn" in ratings else "pass")
        checks.append(mk("speed", "Loads fast on a phone (Core Web Vitals)", 10, st,
                         " · ".join(f"{r['name']} {r['display']}" for r in vitals["rows"]) + f"  (mobile performance score {vitals['perf']}/100)",
                         why, "Compress and lazy-load images, serve WebP/AVIF, defer non-critical JavaScript, and set width/height on images and ads to stop layout shifts." if st != "pass" else "Nothing to fix."))
    elif pending:
        checks.append(mk("speed", "Loads fast on a phone (Core Web Vitals)", 10, "pending", "Waiting for Google's mobile render…", why, ""))
    else:
        checks.append(mk("speed", "Loads fast on a phone (Core Web Vitals)", 10, "skip", psi_error or "Speed data unavailable.", why, ""))

    # ---- score & verdict
    scored = [c for c in checks if c["status"] not in ("skip", "pending")]
    got = sum(c["weight"] * {"pass": 1, "warn": 0.5, "fail": 0}[c["status"]] for c in scored)
    total = sum(c["weight"] for c in scored) or 1
    score = round(100 * got / total)
    critical_fail = any(c["critical"] and c["status"] == "fail" for c in scored)
    if pending:
        verdict, vclass = "Quick result — full score in a few seconds", "warn"
    elif len(scored) < 4 and not critical_fail:
        verdict, vclass = "Partial check — the site blocked a full test", "warn"
    elif critical_fail or score < 50:
        verdict, vclass = "Not mobile-friendly", "fail"
    elif score >= 85 and not any(c["status"] == "fail" for c in scored):
        verdict, vclass = "Mobile-friendly", "pass"
    else:
        verdict, vclass = "Mostly mobile-friendly — a few fixes needed", "warn"

    order = {"fail": 0, "warn": 1, "pass": 2, "pending": 3, "skip": 4}
    checks.sort(key=lambda c: (order[c["status"]], -c["weight"]))
    counts = {k: sum(1 for c in checks if c["status"] == k) for k in ("pass", "warn", "fail", "skip", "pending")}

    shot = ((audits.get("final-screenshot") or {}).get("details") or {}).get("data")
    notes = []
    if not mob.get("ok"):
        notes.append("Couldn't read the page HTML directly: " + mob.get("error", "") + " Results use Google's render only.")
    if psi_error:
        notes.append(psi_error)

    return {
        "url": url, "score": score, "verdict": verdict, "vclass": vclass, "checks": checks,
        "counts": counts, "parity": parity, "vitals": vitals, "screenshot": shot, "notes": notes,
        "pending": pending,
    }


def analyze_quick(url: str):
    """Instant checks from the page HTML while Google's render runs in the background."""
    if not ON_VERCEL:
        cached("psi", url, fetch_psi)      # start Google's render right away (shared server only)
    page = cached("page", url, gather_page).result()
    if not page["mob"].get("ok"):
        return None
    return run_checks(url, page, None, "", pending=True)


def analyze_full(url: str) -> dict:
    f_psi = cached("psi", url, fetch_psi)
    page = cached("page", url, gather_page).result()
    psi, psi_error = None, ""
    try:
        psi = f_psi.result()
    except PSIError as e:
        psi_error = str(e)
    if not page["mob"].get("ok") and not psi:
        raise PSIError(page["mob"].get("error", "") + (" " + psi_error if psi_error else ""))
    return run_checks(url, page, psi, psi_error)


def export_rows(view: dict) -> list:
    label = {"pass": "Pass", "warn": "Warning", "fail": "Fail", "skip": "Skipped"}
    rows = [{"Check": "Overall", "Status": view["verdict"], "Finding": f"Score {view['score']}/100", "How to fix": ""}]
    for c in view["checks"]:
        rows.append({"Check": c["name"], "Status": label[c["status"]], "Finding": c["found"],
                     "How to fix": (c["fix"] + (" " + c["snippet"] if c["snippet"] else "")).strip()})
    for p in view["parity"] or []:
        rows.append({"Check": f"Parity — {p[0]}", "Status": label[p[3]], "Finding": f"Mobile: {p[1]} | Desktop: {p[2]}", "How to fix": ""})
    return rows


# ---------------------------------------------------------------- template
PAGE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Mobile Friendly Test</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%237c6af7'/%3E%3Ctext x='50%25' y='50%25' dominant-baseline='central' text-anchor='middle' font-family='Georgia%2C serif' font-size='18' font-weight='900' fill='white'%3EM%3C/text%3E%3C/svg%3E">
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@600;700;800&family=Inter:wght@400;500;600&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root{--bg:#0a0a0f;--surface:#12121a;--surface2:#171722;--border:#23233a;--accent:#7c6af7;--accent-h:#6a58e8;--mint:#6af7c8;--orange:#f7a26a;--red:#f76a7c;--text:#e8e8f0;--muted:#8888a8}
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);font-size:15px;line-height:1.6}
  a{color:var(--accent);text-decoration:none}
  .shell{display:flex;min-height:100vh}
  .sidebar{width:230px;background:var(--surface);border-right:1px solid var(--border);padding:1.4rem 1rem;position:fixed;top:0;bottom:0;display:flex;flex-direction:column;overflow-y:auto}
  .main{flex:1;margin-left:230px;padding:1.6rem 2rem 3rem;max-width:1300px}
  .brand{display:flex;align-items:center;gap:.6rem;font-family:'Sora',sans-serif;font-weight:800;font-size:.95rem;margin-bottom:2rem;color:var(--text)}
  .brand:hover .brand-text{color:var(--accent)}
  .brand-mark{width:30px;height:30px;border-radius:8px;background:var(--accent);display:flex;align-items:center;justify-content:center;color:#fff;font-size:1rem}
  .sidebar-section{margin-bottom:1.4rem}
  .sidebar-label{font-family:'DM Mono',monospace;font-size:.62rem;letter-spacing:.16em;text-transform:uppercase;color:var(--muted);margin-bottom:.5rem;padding-left:.3rem}
  .sidebar-link{display:flex;align-items:center;gap:.6rem;padding:.55rem .8rem;border-radius:8px;color:var(--muted);font-size:.85rem;font-weight:500;margin-bottom:.2rem;border:1px solid transparent}
  .sidebar-link.active{background:rgba(124,106,247,.14);border:1px solid rgba(124,106,247,.4);color:var(--accent);font-weight:700}
  .sidebar-link:hover{color:var(--text)} .sidebar-link.active:hover{color:var(--accent)}
  .sidebar-footer{margin-top:auto;font-size:.68rem;color:var(--muted);line-height:1.5}
  .credit-name{color:var(--mint);font-weight:700}
  .credit-name:hover{text-decoration:underline}
  .sidebar-social{display:flex;gap:.5rem;margin-top:.7rem}
  .sidebar-social a{width:28px;height:28px;border-radius:6px;background:rgba(106,247,200,.08);border:1px solid rgba(106,247,200,.35);color:var(--mint);display:flex;align-items:center;justify-content:center;font-family:'DM Mono',monospace;font-size:.7rem;transition:background .2s}
  .sidebar-social a:hover{background:rgba(106,247,200,.18)}

  .topbar{margin-bottom:1.4rem}
  .topbar h1{font-family:'Sora',sans-serif;font-size:1.25rem;font-weight:700}
  .topbar h1 span{color:var(--accent)}
  .crumb{font-family:'DM Mono',monospace;font-size:.68rem;color:var(--muted);letter-spacing:.12em;text-transform:uppercase}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.2rem 1.4rem}
  .form-row{display:flex;gap:.8rem;flex-wrap:wrap;align-items:flex-end}
  .form-field{flex:1;min-width:240px}
  .form-field label{font-size:.8rem;color:var(--muted);display:block;margin-bottom:.4rem}
  input[type=text]{width:100%;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:.7rem .9rem;font-family:'DM Mono',monospace;font-size:.82rem}
  input[type=text]:focus{outline:none;border-color:var(--accent)}
  .btn{display:inline-flex;align-items:center;gap:.5rem;background:var(--accent);color:#fff;border:none;border-radius:8px;padding:.7rem 1.5rem;font-size:.85rem;font-weight:600;cursor:pointer;font-family:'Inter',sans-serif}
  .btn:hover{background:var(--accent-h)}
  .hint{font-size:.72rem;color:var(--muted);margin-top:.6rem;font-family:'DM Mono',monospace}
  .spinner{display:none;margin-top:.8rem;font-size:.78rem;color:var(--muted);font-family:'DM Mono',monospace}
  .spinner.show{display:block}
  .btn:disabled{opacity:.6;cursor:wait}
  .pulse{animation:pulse 1.4s ease-in-out infinite}
  @keyframes pulse{50%{opacity:.4}}
  .note{margin-top:1rem;font-size:.78rem;color:var(--orange);background:rgba(247,162,106,.06);border:1px solid rgba(247,162,106,.25);border-radius:10px;padding:.6rem .9rem}

  .summary{display:grid;grid-template-columns:1.1fr .9fr 1fr;gap:1rem;margin:1.4rem 0}
  .panel{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.2rem}
  .panel h3{font-family:'DM Mono',monospace;font-size:.66rem;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin-bottom:.9rem;font-weight:500}
  .ring{position:relative;width:150px;height:150px;margin:0 auto}
  .ring svg{transform:rotate(-90deg)}
  .ring-center{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
  .ring-center .num{font-family:'Sora',sans-serif;font-size:2rem;font-weight:800}
  .ring-center .of{font-size:.62rem;font-family:'DM Mono',monospace;color:var(--muted)}
  .verdict{text-align:center;margin-top:1rem;font-family:'Sora',sans-serif;font-weight:700;font-size:.95rem}
  .verdict.pass{color:var(--mint)} .verdict.warn{color:var(--orange)} .verdict.fail{color:var(--red)}
  .counts{display:flex;justify-content:center;gap:.5rem;margin-top:.8rem;flex-wrap:wrap}
  .phone{width:190px;margin:0 auto;border:8px solid #2a2a3d;border-radius:26px;overflow:hidden;background:#000;max-height:360px;overflow-y:auto}
  .phone img{display:block;width:100%}
  .phone-empty{height:300px;display:flex;align-items:center;justify-content:center;text-align:center;font-size:.72rem;color:var(--muted);padding:1rem}
  .vital{display:flex;align-items:center;justify-content:space-between;padding:.65rem 0;border-top:1px solid var(--border)}
  .vital:first-of-type{border-top:none}
  .vital .n{font-weight:700;font-size:.9rem}
  .vital .h{font-size:.7rem;color:var(--muted)}
  .perf{font-family:'DM Mono',monospace;font-size:.72rem;color:var(--muted);margin-top:.8rem}

  .badge{display:inline-block;flex-shrink:0;font-size:.64rem;font-family:'DM Mono',monospace;padding:.2rem .6rem;border-radius:100px;white-space:nowrap;border:1px solid}
  .badge.pass{background:rgba(106,247,200,.1);border-color:rgba(106,247,200,.3);color:var(--mint)}
  .badge.warn{background:rgba(247,162,106,.1);border-color:rgba(247,162,106,.35);color:var(--orange)}
  .badge.fail{background:rgba(247,106,124,.1);border-color:rgba(247,106,124,.35);color:var(--red)}
  .badge.skip{background:rgba(136,136,168,.08);border-color:rgba(136,136,168,.3);color:var(--muted)}

  .sec-head{display:flex;align-items:center;justify-content:space-between;gap:.8rem;margin:1.8rem 0 .9rem;flex-wrap:wrap}
  .sec-title{font-family:'Sora',sans-serif;font-size:1rem;font-weight:700}
  .dl{display:inline-flex;align-items:center;gap:.5rem;background:var(--surface);border:1px solid var(--border);color:var(--text);padding:.5rem 1.1rem;border-radius:8px;font-size:.8rem}
  .dl:hover{border-color:var(--accent)}
  details{background:var(--surface);border:1px solid var(--border);border-radius:12px;margin-bottom:.55rem}
  summary{cursor:pointer;padding:.85rem 1.1rem;display:flex;align-items:center;gap:.8rem;list-style:none}
  summary::-webkit-details-marker{display:none}
  summary .t{font-weight:600;font-size:.88rem}
  summary .f{font-size:.76rem;color:var(--muted);margin-left:auto;text-align:right;max-width:55%}
  .body{padding:0 1.1rem 1rem 1.1rem;font-size:.82rem}
  .body p{margin-top:.5rem}.body p:first-child{margin-top:0} .body b{color:var(--text)}
  .body .muted{color:var(--muted)}
  pre{margin-top:.6rem;background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:.7rem .9rem;font-family:'DM Mono',monospace;font-size:.75rem;color:var(--mint);white-space:pre-wrap;word-break:break-word}
  table{width:100%;border-collapse:collapse;background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden;font-size:.82rem}
  th,td{text-align:left;padding:.65rem 1rem;border-top:1px solid var(--border);vertical-align:top}
  th{background:var(--surface2);font-family:'DM Mono',monospace;font-size:.66rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);font-weight:500;border-top:none}
  td.v{max-width:320px;word-break:break-word}

  @media(max-width:1020px){.summary{grid-template-columns:1fr}.sidebar{display:none}.main{margin-left:0;padding:1.2rem}summary{flex-wrap:wrap}summary .f{margin-left:0;max-width:100%;text-align:left}}
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
      <a href="#" class="sidebar-link active">📱&nbsp; Mobile Friendly Test</a>
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
      <div class="crumb">// seo tool · free · mobile-first indexing</div>
      <h1>📱 Mobile <span>Friendly Test</span></h1>
    </div>

    <form class="card" id="f">
      <div class="form-row">
        <div class="form-field">
          <label>Page URL</label>
          <input type="text" name="url" id="u" placeholder="https://example.com/page" required>
        </div>
        <button class="btn" type="submit" id="go">📱 Test on Mobile</button>
      </div>
      <div class="spinner" id="sp"></div>
      <div class="hint" id="err" style="color:var(--red);display:none"></div>
      <div class="hint">Checks viewport, content width, text size, tap targets, pop-ups, speed &amp; mobile-vs-desktop content.</div>
    </form>

    <div id="results"></div>
  </main>
</div>
<script>
function dlCsv(){
  const el=document.getElementById('csvrows'); if(!el) return false;
  const rows=JSON.parse(el.textContent), cols=['Check','Status','Finding','How to fix'];
  const esc=v=>'"'+String(v==null?'':v).replace(/"/g,'""')+'"';
  const csv='\ufeff'+[cols.map(esc).join(',')].concat(rows.map(r=>cols.map(c=>esc(r[c])).join(','))).join('\r\n');
  const a=document.createElement('a'); a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));
  a.download='mobile_friendly_report.csv'; document.body.appendChild(a); a.click(); a.remove();
  return false;
}
(function(){
  const f=document.getElementById('f'), u=document.getElementById('u'), go=document.getElementById('go'),
        sp=document.getElementById('sp'), err=document.getElementById('err'), out=document.getElementById('results');
  let run=0;
  f.addEventListener('submit', async function(e){
    e.preventDefault();
    const url=u.value.trim(); if(!url) return;
    const me=++run, t0=Date.now(); let quickShown=false, done=false;
    err.style.display='none'; out.innerHTML=''; go.disabled=true; sp.classList.add('show');
    const tick=()=>{ if(done||me!==run) return;
      const s=Math.round((Date.now()-t0)/1000);
      sp.textContent=(quickShown?'✅ Quick checks ready · ':'')+'⏳ Google is testing the page on a phone… '+s+'s (usually 15-25s)';
      setTimeout(tick,1000); };
    tick();
    const body=new URLSearchParams({url:url});
    fetch('/api/check?mode=quick',{method:'POST',body:body}).then(r=>r.json()).then(d=>{
      if(me===run && !done && d.html){ out.innerHTML=d.html; quickShown=true; }
    }).catch(()=>{});
    try{
      const d=await (await fetch('/api/check?mode=full',{method:'POST',body:body})).json();
      if(me!==run) return;
      if(d.error){ if(!quickShown){ err.textContent=d.error; err.style.display='block'; } }
      else { out.innerHTML=d.html; }
    }catch(x){ if(me===run){ err.textContent='Something went wrong — please try again.'; err.style.display='block'; } }
    finally{ if(me===run){ done=true; sp.classList.remove('show'); go.disabled=false; } }
  });
})();
</script>
</body>
</html>
"""


RESULTS = r"""
    {% for n in view.notes %}<div class="note">⚠ {{ n }}</div>{% endfor %}

    {% set col = '#8888a8' if view.pending else {'pass':'#6af7c8','warn':'#f7a26a','fail':'#f76a7c'}[view.vclass] %}
    <div class="summary">
      <div class="panel">
        <h3>Mobile-friendly score</h3>
        <div class="ring">
          <svg width="150" height="150" viewBox="0 0 150 150">
            <circle cx="75" cy="75" r="64" fill="none" stroke="#23233a" stroke-width="12"/>
            <circle cx="75" cy="75" r="64" fill="none" stroke="{{ col }}" stroke-width="12" stroke-linecap="round"
              stroke-dasharray="{{ 402.1 * view.score / 100 }} 402.1"/>
          </svg>
          <div class="ring-center"><div class="num" style="color:{{ col }}">{{ view.score }}</div><div class="of">{{ 'provisional' if view.pending else '/ 100' }}</div></div>
        </div>
        {% if view.pending %}<div class="verdict" style="color:var(--muted)">⏳ {{ view.verdict }}</div>
        {% else %}<div class="verdict {{ view.vclass }}">{{ '✅' if view.vclass=='pass' else '⚠️' if view.vclass=='warn' else '❌' }} {{ view.verdict }}</div>{% endif %}
        <div class="counts">
          <span class="badge pass">{{ view.counts.pass }} passed</span>
          <span class="badge warn">{{ view.counts.warn }} warnings</span>
          <span class="badge fail">{{ view.counts.fail }} failed</span>
        </div>
      </div>

      <div class="panel">
        <h3>How Google sees it on a phone</h3>
        <div class="phone">
          {% if view.screenshot %}<img src="{{ view.screenshot }}" alt="Mobile screenshot of the page">
          {% elif view.pending %}<div class="phone-empty"><span class="pulse">⏳ Google is rendering the page on a phone…</span></div>
          {% else %}<div class="phone-empty">Screenshot unavailable{% if not psi_ok %} — needs the PageSpeed Insights API key{% endif %}.</div>{% endif %}
        </div>
      </div>

      <div class="panel">
        <h3>Mobile Core Web Vitals</h3>
        {% if view.vitals %}
          {% for r in view.vitals.rows %}
          <div class="vital">
            <div><div class="n">{{ r.name }}</div><div class="h">{{ r.help }} · {{ r.src }}</div></div>
            <span class="badge {{ r.rating or 'skip' }}">{{ r.display }}</span>
          </div>
          {% endfor %}
          {% if view.vitals.perf is not none %}<div class="perf">Mobile performance score: {{ view.vitals.perf }}/100</div>{% endif %}
        {% elif view.pending %}
          <div class="phone-empty" style="height:auto;padding:2rem 0"><span class="pulse">⏳ Measuring speed on mobile…</span></div>
        {% else %}
          <div class="phone-empty" style="height:auto;padding:2rem 0">Speed data unavailable.</div>
        {% endif %}
      </div>
    </div>

    <div class="sec-head">
      <div class="sec-title">🔍 Checks &amp; how to fix them</div>
      {% if rows %}<a class="dl" href="#" onclick="return dlCsv()">⬇️ Download report (CSV)</a>
      <script type="application/json" id="csvrows">{{ rows|tojson }}</script>{% endif %}
    </div>
    {% for c in view.checks %}
    <details {% if c.status == 'fail' %}open{% endif %}>
      <summary>
        <span class="badge {{ c.status }}">{{ {'pass':'PASS','warn':'WARN','fail':'FAIL','skip':'SKIPPED','pending':'LOADING'}[c.status] }}</span>
        <span class="t">{{ c.name }}</span>
        <span class="f">{{ c.found }}</span>
      </summary>
      <div class="body">
        <p class="muted"><b>Why it matters:</b> {{ c.why }}</p>
        {% if c.fix %}<p><b>How to fix:</b> {{ c.fix }}</p>{% endif %}
        {% if c.snippet %}<pre>{{ c.snippet }}</pre>{% endif %}
      </div>
    </details>
    {% endfor %}

    {% if view.parity %}
    <div class="sec-head"><div class="sec-title">🔁 Mobile vs Desktop content</div></div>
    <table>
      <tr><th>Element</th><th>📱 Mobile</th><th>🖥️ Desktop</th><th>Status</th></tr>
      {% for p in view.parity %}
      <tr><td>{{ p[0] }}</td><td class="v">{{ p[1] }}</td><td class="v">{{ p[2] }}</td>
        <td><span class="badge {{ p[3] }}">{{ {'pass':'MATCH','warn':'DIFFERS','fail':'MISSING ON MOBILE'}[p[3]] }}</span></td></tr>
      {% endfor %}
    </table>
    {% endif %}
"""


# ---------------------------------------------------------------- routes
@app.route("/", methods=["GET"])
def home():
    return render_template_string(PAGE)


@app.route("/api/check", methods=["POST"])
def api_check():
    url = clean_url(request.form.get("url", ""))
    if not url:
        return jsonify({"error": "Please enter a URL."})
    if request.args.get("mode") == "quick":
        view = analyze_quick(url)
        if view is None:
            return jsonify({"html": None})
        return jsonify({"html": render_template_string(RESULTS, view=view, rows=None, psi_ok=bool(PSI_API_KEY))})
    try:
        view = analyze_full(url)
    except PSIError as e:
        return jsonify({"error": str(e)})
    return jsonify({"html": render_template_string(RESULTS, view=view, rows=export_rows(view), psi_ok=bool(PSI_API_KEY))})


if __name__ == "__main__":
    app.run(debug=True, port=8501, threaded=True)
