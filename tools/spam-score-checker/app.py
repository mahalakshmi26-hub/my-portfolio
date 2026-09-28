"""
Spam Score Checker  -  Flask app
Check any website for the on-page warning signs that Google's spam policies target.

  * Spam risk score 0-100 with a plain Low / Medium / High verdict
  * "Should I get a backlink from this site?" answer: Safe / Caution / Avoid
  * ~17 checks: hidden links, spammy words, cloaking, hacked-code signs, thin content,
    keyword stuffing, outbound link spam, redirects, trust pages, HTTPS, domain age
  * Each flag explains why it matters, which Google spam policy it relates to, and how to fix it
  * CSV export

No API key needed. Hosted on Vercel (vercel.json in this folder).
Built by Mahalakshmi Marimuthu - Digital Marketing Strategist & AI-Powered SEO Expert
"""
import re
import time
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

TIMEOUT = 12
MAX_BYTES = 3_000_000
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
GOOGLEBOT_UA = ("Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)")
HEADERS = {"User-Agent": BROWSER_UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
           "Accept-Language": "en-US,en;q=0.9"}

# Severity -> points added to the spam risk score when a check is flagged
POINTS = {"High": 15, "Medium": 8, "Low": 4}

SPAM_TERMS = [
    r"casino", r"slot gacor", r"situs slot", r"judi online", r"togel", r"poker online", r"sportsbook",
    r"online betting", r"viagra", r"cialis", r"levitra", r"porn", r"xxx", r"escort service", r"call girls?",
    r"hookup", r"replica watch(?:es)?", r"buy (?:instagram |tiktok |youtube )?followers", r"cheap essays?",
    r"essay writing service", r"keygen", r"cracked apk", r"mod apk", r"crack download",
]
SPAM_RX = re.compile(r"\b(?:" + "|".join(SPAM_TERMS) + r")\b", re.I)

HIDDEN_RX = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?:px|pt|em)?\s*(?:;|$)|"
    r"text-indent\s*:\s*-\d{3,}|(?:left|top)\s*:\s*-\d{3,}px|opacity\s*:\s*0\s*(?:;|$)", re.I)

OBFUSCATED_RX = re.compile(
    r"eval\s*\(\s*function\s*\(\s*p\s*,\s*a\s*,\s*c\s*,\s*k\s*,\s*e|eval\s*\(\s*unescape|"
    r"document\.write\s*\(\s*unescape|eval\s*\(\s*atob|String\.fromCharCode\((?:\s*\d+\s*,){40,}", re.I)

POPUP_NETWORKS = ["popads.net", "popcash.net", "propellerads", "adsterra", "hilltopads", "clickadu",
                  "exoclick", "juicyads", "popunder", "onclickads", "adcash"]

STOPWORDS = set("""a about above after again all also am an and any are as at be because been before being
below between both but by can could did do does doing down during each few for from further had has have
having he her here hers him his how i if in into is it its itself just let me more most my no nor not now
of off on once only or other our ours out over own same she should so some such than that the their them
then there these they this those through to too under until up very was we were what when where which while
who whom why will with would you your yours get us new one see more click read home page""".split())

SLD_2PART = {"co", "com", "net", "org", "gov", "ac", "edu", "gen", "firm", "ind", "nic", "res"}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def normalise_url(raw):
    s = (raw or "").strip().strip("\"'<>")
    if not s:
        return ""
    if "://" not in s:
        s = "https://" + s
    try:
        p = urlparse(s)
    except ValueError:
        return ""
    if p.scheme not in ("http", "https") or not p.hostname or "." not in p.hostname:
        return ""
    return s


def base_domain(host):
    host = (host or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in SLD_2PART:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def fetch(url, ua=BROWSER_UA, tries=2):
    """GET with a real browser UA; one retry on 403/429/503. Returns (response, error)."""
    h = dict(HEADERS, **{"User-Agent": ua})
    last = None
    for attempt in range(tries):
        try:
            r = requests.get(url, headers=h, timeout=TIMEOUT, allow_redirects=True, stream=True)
            body = r.raw.read(MAX_BYTES, decode_content=True)
            r._content = body
            if r.status_code in (403, 429, 503) and attempt < tries - 1:
                last = r
                time.sleep(1.5)
                continue
            return r, None
        except requests.RequestException as e:
            last = e
            if attempt < tries - 1:
                time.sleep(1)
    if isinstance(last, requests.Response):
        return last, None
    return None, str(last)


def visible_text(soup):
    s = BeautifulSoup(str(soup), "html.parser")
    for t in s(["script", "style", "noscript", "template", "svg", "iframe"]):
        t.decompose()
    return re.sub(r"\s+", " ", s.get_text(" ")).strip()


def words_of(text):
    return re.findall(r"[a-zA-Z][a-zA-Z'-]{1,}", text)


def domain_age(domain):
    """Registration date via RDAP (free). Returns (days, iso_date) or (None, None)."""
    try:
        r = requests.get(f"https://rdap.org/domain/{domain}", timeout=10,
                         headers={"Accept": "application/rdap+json", "User-Agent": BROWSER_UA})
        if r.status_code != 200:
            return None, None
        for ev in r.json().get("events") or []:
            if ev.get("eventAction") == "registration" and ev.get("eventDate"):
                d = datetime.fromisoformat(ev["eventDate"].replace("Z", "+00:00"))
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
                return (datetime.now(timezone.utc) - d).days, d.strftime("%d %b %Y")
    except Exception:
        pass
    return None, None


def check(cid, name, category, severity, policy, status, detail, why, fix):
    return {"id": cid, "name": name, "category": category, "severity": severity, "policy": policy,
            "status": status, "detail": detail, "why": why, "fix": fix,
            "points": POINTS[severity] if status == "flag" else 0}


# --------------------------------------------------------------------------- #
# The checks
# --------------------------------------------------------------------------- #
def analyse(url):
    r, err = fetch(url)
    if r is None:
        raise RuntimeError("Couldn't reach that website. Check the address and try again.")
    if r.status_code in (401, 403, 429, 503):
        raise RuntimeError(f"This website blocked the automated check (HTTP {r.status_code}). "
                           "Many large sites use bot protection — that's not a spam signal.")
    if r.status_code >= 400:
        raise RuntimeError(f"The page returned an error (HTTP {r.status_code}). Try the homepage instead.")
    ctype = r.headers.get("Content-Type", "")
    if "html" not in ctype.lower() and ctype:
        raise RuntimeError("That address doesn't return a web page (HTML). Try the site's homepage.")

    html = r.text or ""
    final_url = r.url
    start_host = urlparse(url).hostname or ""
    final_host = urlparse(final_url).hostname or ""
    domain = base_domain(final_host)
    soup = BeautifulSoup(html, "html.parser")

    # Start the slower lookups in the background so the whole check stays fast
    pool = ThreadPoolExecutor(max_workers=2)
    g_future = pool.submit(fetch, final_url, GOOGLEBOT_UA, 1)
    age_future = pool.submit(domain_age, domain)

    text = visible_text(soup)
    words = words_of(text)
    wc = len(words)

    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absu = urljoin(final_url, href)
        h = urlparse(absu).hostname or ""
        links.append({"url": absu, "host": h, "text": a.get_text(" ", strip=True),
                      "external": bool(h) and base_domain(h) != domain, "tag": a})
    ext = [l for l in links if l["external"]]

    out = []

    # ---------------- Content ----------------
    hits = SPAM_RX.findall(text)
    spam_ct = Counter(h.lower() for h in hits)
    top = ", ".join(f"{k} ({v})" for k, v in spam_ct.most_common(4))
    out.append(check("spam_words", "Spammy words in the content", "Content", "High",
        "Hacked content · Spammy auto-generated content",
        "flag" if len(hits) >= 3 else "pass",
        f"Found {len(hits)} spam-type terms: {top}" if hits else "No gambling, pharma, adult or 'crack' terms found",
        "Gambling, pharma, adult or pirated-software words on an unrelated site are a classic sign of a hacked site or a link farm.",
        "If these words shouldn't be there, check for injected pages or comments, clean them, update your CMS/plugins and change passwords."))

    hidden_ext, hidden_txt = 0, 0
    for el in soup.find_all(style=HIDDEN_RX):
        for a in el.find_all("a", href=True):
            h = urlparse(urljoin(final_url, a["href"])).hostname or ""
            if h and base_domain(h) != domain:
                hidden_ext += 1
        if len(words_of(el.get_text(" "))) >= 40:
            hidden_txt += 1
    out.append(check("hidden_links", "Hidden links to other websites", "Content", "High",
        "Hidden text and links",
        "flag" if hidden_ext >= 1 else "pass",
        f"{hidden_ext} external link(s) inside elements hidden with CSS" if hidden_ext else "No hidden outbound links found",
        "Links hidden from visitors (display:none, off-screen, zero font size) but visible to Google are a direct spam-policy violation and a common sign of paid or injected links.",
        "Remove the hidden links. If you didn't add them, your site may be hacked — scan your theme files and database."))

    out.append(check("hidden_text", "Large blocks of hidden text", "Content", "Medium",
        "Hidden text and links",
        "flag" if hidden_txt >= 2 else "pass",
        f"{hidden_txt} hidden block(s) with 40+ words" if hidden_txt else "No large hidden text blocks found",
        "Stuffing text into hidden elements to rank for keywords is against Google's spam policies. (Tabs and accordions are fine — this looks for bigger hidden blocks.)",
        "Make important text visible, or remove it. Content inside tabs/accordions is OK if users can open it."))

    out.append(check("thin", "Thin content", "Content", "Medium",
        "Thin / low-value content · Scaled content abuse",
        "flag" if wc < 250 else "pass",
        f"{wc:,} words of visible text on the page",
        "Pages with very little real content look low-value, and a site full of them can look like scaled or doorway content.",
        "Add genuinely useful content for visitors. (If the site builds its text with JavaScript, this can be a false alarm.)"))

    content_words = [w.lower() for w in words if len(w) >= 3 and w.lower() not in STOPWORDS]
    top_word, dens = "", 0.0
    if content_words and wc >= 150:
        top_word, n = Counter(content_words).most_common(1)[0]
        dens = n / wc * 100
    out.append(check("stuffing", "Keyword stuffing", "Content", "Medium",
        "Keyword stuffing",
        "flag" if dens > 5 else ("unverifiable" if wc < 150 else "pass"),
        (f"Most-repeated word: '{top_word}' at {dens:.1f}% of all words" if top_word else "Not enough text to judge"),
        "Repeating the same keyword unnaturally is one of the oldest spam tactics and can get a page demoted.",
        "Write naturally for readers — use synonyms and related terms instead of repeating one keyword."))

    # ---------------- Links ----------------
    spam_links = [l for l in ext if SPAM_RX.search(l["url"]) or SPAM_RX.search(l["text"])]
    out.append(check("spam_links", "Links to spammy websites", "Links", "High",
        "Link spam",
        "flag" if len(spam_links) >= 2 else "pass",
        (f"{len(spam_links)} outbound link(s) to gambling/pharma/adult-type pages, e.g. {spam_links[0]['host']}"
         if spam_links else "No outbound links to spam-type pages"),
        "Linking out to casino, pharma or adult sites is a strong sign of selling links or a hacked site.",
        "Remove these links, or mark genuine paid links with rel=\"sponsored\"."))

    n_ext = len(ext)
    ratio = n_ext / len(links) if links else 0
    too_many = n_ext > 100 or (n_ext > 50 and ratio > 0.6)
    out.append(check("outbound", "Too many outbound links", "Links", "Medium",
        "Link spam (link schemes)",
        "flag" if too_many else "pass",
        f"{n_ext} outbound links out of {len(links)} total ({ratio*100:.0f}% external)",
        "A page that's mostly links to other websites looks like a link directory or link farm.",
        "Cut outbound links down to ones that genuinely help readers, and add rel=\"sponsored\"/\"nofollow\" where needed."))

    unmarked = [l for l in ext if not ({"nofollow", "sponsored", "ugc"} & set(
        (l["tag"].get("rel") or []) if isinstance(l["tag"].get("rel"), list) else str(l["tag"].get("rel") or "").split()))]
    out.append(check("rel", "Outbound links without nofollow/sponsored", "Links", "Low",
        "Link spam (unqualified paid links)",
        "flag" if n_ext >= 30 and len(unmarked) / max(n_ext, 1) > 0.9 else "pass",
        f"{len(unmarked)} of {n_ext} outbound links pass full link value (no rel attribute)",
        "Lots of 'followed' outbound links on one page can look like links being sold. Normal editorial links are fine.",
        "Add rel=\"sponsored\" to paid links and rel=\"ugc\" to user-generated ones."))

    # ---------------- Technical / security ----------------
    scripts = " ".join(s.get_text() for s in soup.find_all("script") if not s.get("src"))
    obf = OBFUSCATED_RX.search(scripts)
    out.append(check("obfuscated", "Obfuscated (hidden) JavaScript", "Security", "High",
        "Hacked content · Malware",
        "flag" if obf else "pass",
        "Found packed/encoded script code (eval/unescape/fromCharCode)" if obf else "No obfuscated inline scripts found",
        "Hackers often inject scrambled JavaScript to redirect visitors or show spam. Google warns users about sites that do this.",
        "Find where the script comes from (theme, plugin, database) and remove it. Run a malware scan and check Google Search Console > Security issues."))

    src_all = " ".join((s.get("src") or "") for s in soup.find_all("script")) + " " + scripts
    pops = [p for p in POPUP_NETWORKS if p in src_all.lower()]
    out.append(check("popups", "Aggressive pop-up / pop-under ad networks", "Security", "Medium",
        "Malicious & unwanted behaviour",
        "flag" if pops else "pass",
        f"Found: {', '.join(pops)}" if pops else "No pop-under ad networks detected",
        "Pop-under and redirect ad networks are common on low-quality and spam sites and hurt user trust.",
        "Use reputable ad networks and avoid pop-unders and forced redirects."))

    meta_ref = soup.find("meta", attrs={"http-equiv": re.compile("^refresh$", re.I)})
    ref_to_other = False
    if meta_ref and "url=" in (meta_ref.get("content") or "").lower():
        target = urljoin(final_url, meta_ref["content"].split("=", 1)[1].strip(" '\""))
        ref_to_other = base_domain(urlparse(target).hostname or "") != domain
    out.append(check("meta_refresh", "Sneaky redirect (meta refresh)", "Technical", "Medium",
        "Sneaky redirects",
        "flag" if ref_to_other else "pass",
        "Page uses a meta refresh to send visitors to another website" if ref_to_other else "No meta refresh redirect to another site",
        "Automatically sending visitors somewhere different from what Google indexed is a spam-policy violation.",
        "Use a proper 301 redirect for moved pages, and don't redirect visitors to other sites."))

    cross = base_domain(start_host) != base_domain(final_host)
    out.append(check("cross_redirect", "Redirects to a different domain", "Technical", "Medium",
        "Sneaky redirects · Expired domain abuse",
        "flag" if cross else "pass",
        f"{start_host} redirects to {final_host}" if cross else "Stays on the same domain",
        "A domain that forwards to a different website may have been bought or repurposed. Fine for rebrands; risky for backlinks.",
        "If this is a rebrand, that's OK. If you're vetting a link, check the new site instead."))

    out.append(check("https", "No HTTPS", "Technical", "Medium",
        "Trust & security",
        "flag" if not final_url.lower().startswith("https://") else "pass",
        "Loads over secure HTTPS" if final_url.lower().startswith("https://") else "Page loads over plain HTTP",
        "Browsers mark HTTP sites as 'Not secure'. Almost every legitimate site uses HTTPS today.",
        "Install a free SSL certificate (e.g. Let's Encrypt) and redirect HTTP to HTTPS."))

    title = (soup.title.get_text(strip=True) if soup.title else "")
    md = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    mdesc = (md.get("content") or "").strip() if md else ""
    out.append(check("meta", "Missing title or meta description", "Technical", "Low",
        "Basic quality signal",
        "flag" if not title or not mdesc else "pass",
        ("Title and meta description both present" if title and mdesc else
         "Missing: " + ", ".join(x for x, v in (("title", title), ("meta description", mdesc)) if not v)),
        "Abandoned or auto-generated spam pages often skip basic metadata.",
        "Add a unique, descriptive title and meta description."))

    # ---------------- Trust ----------------
    blob = " ".join((l["url"] + " " + l["text"]).lower() for l in links)
    missing = [n for n, rx in (("About", r"about"), ("Contact", r"contact"), ("Privacy policy", r"privacy"))
               if not re.search(rx, blob)]
    out.append(check("trust_pages", "Missing trust pages", "Trust", "Low" if len(missing) < 2 else "Medium",
        "Who's behind the site (E-E-A-T)",
        "flag" if missing else "pass",
        ("Links to About, Contact and Privacy pages found" if not missing else "No link found to: " + ", ".join(missing)),
        "Real businesses tell visitors who they are and how to reach them. Spam sites usually hide this.",
        "Add clear About, Contact and Privacy Policy pages and link them from your footer."))

    # ---------------- Cloaking ----------------
    g, _ = g_future.result()
    if g is None or g.status_code >= 400:
        out.append(check("cloaking", "Cloaking (different page for Google)", "Content", "High",
            "Cloaking", "unverifiable",
            "Couldn't load the page as Googlebot (the site may block unverified bots — that's normal)",
            "Cloaking means showing Google different content than visitors see — a serious violation.",
            "Nothing to fix here; this check just couldn't run."))
    else:
        gw = len(words_of(visible_text(BeautifulSoup(g.text or "", "html.parser"))))
        gdom = base_domain(urlparse(g.url).hostname or "")
        diff = abs(gw - wc) / max(gw, wc, 1)
        cloak = gdom != domain or (max(gw, wc) >= 100 and diff > 0.5)
        out.append(check("cloaking", "Cloaking (different page for Google)", "Content", "High",
            "Cloaking",
            "flag" if cloak else "pass",
            (f"Visitors see {wc:,} words, Googlebot sees {gw:,} words" +
             (f" and lands on {gdom}" if gdom != domain else "")),
            "Cloaking means showing Google different content than visitors see — a serious spam-policy violation.",
            "Serve the same content to Googlebot and visitors. (Big differences can also come from bot protection or A/B tests.)"))

    # ---------------- Domain ----------------
    days, reg = age_future.result()
    pool.shutdown(wait=False)
    if days is None:
        out.append(check("age", "Very new domain", "Domain", "Medium", "Expired domain abuse · Trust",
            "unverifiable", "Registration date not available for this domain",
            "Brand-new domains have no track record; spam networks often use fresh or recently re-registered domains.",
            "Nothing to fix — the registry didn't share the date."))
    else:
        yrs = days / 365.25
        age_txt = f"Registered {reg} ({yrs:.1f} years ago)" if yrs >= 1 else f"Registered {reg} ({days} days ago)"
        out.append(check("age", "Very new domain", "Domain", "Medium" if days < 180 else "Low",
            "Expired domain abuse · Trust",
            "flag" if days < 365 else "pass", age_txt,
            "Brand-new domains have no track record; spam networks often use fresh or recently re-registered domains.",
            "Nothing to fix on a new site — just build trust over time. When vetting links, be extra careful with young domains."))

    score = min(100, sum(c["points"] for c in out))
    if score <= 20:
        level, verdict = ["Low risk", "ok"], ["Safe for backlinks", "ok"]
    elif score <= 45:
        level, verdict = ["Medium risk", "warn"], ["Use caution", "warn"]
    else:
        level, verdict = ["High risk", "error"], ["Avoid links from this site", "error"]

    return {
        "url": url, "final_url": final_url, "domain": domain, "score": score,
        "level": level, "verdict": verdict, "checks": out,
        "stats": {"words": wc, "links": len(links), "external": n_ext,
                  "age": out[-1]["detail"], "flags": sum(1 for c in out if c["status"] == "flag"),
                  "passed": sum(1 for c in out if c["status"] == "pass"),
                  "unverifiable": sum(1 for c in out if c["status"] == "unverifiable")},
        "checked_at": datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC"),
    }


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/api/check", methods=["POST"])
def api_check():
    body = request.get_json(silent=True) or {}
    url = normalise_url(body.get("url"))
    if not url:
        return jsonify({"ok": False, "error": "Please enter a valid website, e.g. yourwebsite.com"}), 400
    try:
        return jsonify({"ok": True, **analyse(url)})
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    except Exception:
        return jsonify({"ok": False, "error": "Something went wrong while checking that site. Please try again."}), 500


@app.route("/")
def home():
    return Response(PAGE, mimetype="text/html")


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Spam Score Checker – Free Website Spam Risk Checker</title>
<meta name="description" content="Free Spam Score Checker: check any website for hidden links, spammy words, cloaking, hacked code, thin content and other warning signs Google's spam policies target. Get a 0-100 spam risk score and a backlink verdict.">
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
  .side-note{font-size:.7rem;color:var(--muted);line-height:1.5;background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:.7rem .8rem}
  .side-note b{color:var(--text)}
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
  input[type=text],textarea{width:100%;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:.75rem 1rem;font-family:'DM Mono',monospace;font-size:.82rem}
  textarea{resize:vertical}
  input:focus,textarea:focus{outline:none;border-color:var(--accent)}
  .btn{display:inline-flex;align-items:center;gap:.5rem;background:var(--accent);color:#fff;border:none;border-radius:8px;padding:.7rem 1.5rem;font-size:.85rem;font-weight:600;cursor:pointer;font-family:'Inter',sans-serif;white-space:nowrap}
  .btn:hover{background:var(--accent-h)}
  .btn:disabled{opacity:.5;cursor:wait}
  .btn.ghost{background:var(--surface);border:1px solid var(--border);color:var(--text);font-weight:500;padding:.55rem 1.1rem;font-size:.8rem}
  .btn.ghost:hover{border-color:var(--accent)}
  .row-inline{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center}
  .row-inline input{flex:1;min-width:240px}
  details.adv{margin-top:.9rem}
  details.adv summary{cursor:pointer;font-size:.78rem;color:var(--muted);font-family:'DM Mono',monospace}
  details.adv summary:hover{color:var(--text)}
  .adv-body{margin-top:.8rem}
  .hint{font-size:.72rem;color:var(--muted);margin-top:.5rem;font-family:'DM Mono',monospace}
  .err{font-size:.8rem;color:var(--red);margin-top:.6rem}
  .spinner{display:none;margin-top:.8rem;font-size:.78rem;color:var(--muted);font-family:'DM Mono',monospace}
  .spinner.show{display:block}

  .overview{display:grid;grid-template-columns:220px 1fr 1.2fr;gap:1rem;margin:1.4rem 0}
  .chart-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.1rem 1.2rem;min-width:0}
  .chart-card h3{font-family:'DM Mono',monospace;font-size:.66rem;letter-spacing:.12em;font-weight:500;margin-bottom:.8rem;color:var(--muted);text-transform:uppercase}
  .ring-wrap{position:relative;width:160px;height:160px;margin:0 auto}
  .ring-center{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
  .ring-center .score{font-family:'Sora',sans-serif;font-size:2.1rem;font-weight:800}
  .ring-center .sub{font-size:.6rem;font-family:'DM Mono',monospace;color:var(--muted);text-transform:uppercase;letter-spacing:.1em}
  .ring-dom{text-align:center;font-family:'DM Mono',monospace;font-size:.72rem;margin-top:.6rem;overflow-wrap:anywhere}
  .chart-box{position:relative;height:210px}

  .metrics{display:grid;grid-template-columns:1fr 1fr;gap:.7rem}
  .metric{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:.7rem .9rem;min-width:0}
  .metric .k{font-family:'DM Mono',monospace;font-size:.6rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
  .metric .v{font-family:'Sora',sans-serif;font-size:1.05rem;font-weight:700;margin-top:.15rem;overflow-wrap:anywhere}
  .mint{color:var(--mint)} .orange{color:var(--orange)} .red{color:var(--red)} .purple{color:var(--accent)} .muted{color:var(--muted)}

  .badge{display:inline-flex;align-items:center;gap:.3rem;font-size:.66rem;font-family:'DM Mono',monospace;padding:.22rem .6rem;border-radius:100px;white-space:nowrap;border:1px solid}
  .badge.ok{background:rgba(106,247,200,.1);border-color:rgba(106,247,200,.3);color:var(--mint)}
  .badge.purple{background:rgba(124,106,247,.12);border-color:rgba(124,106,247,.4);color:var(--accent)}
  .badge.warn{background:rgba(247,162,106,.1);border-color:rgba(247,162,106,.35);color:var(--orange)}
  .badge.error{background:rgba(247,106,106,.1);border-color:rgba(247,106,106,.35);color:var(--red)}
  .badge.none{background:rgba(136,136,168,.1);border-color:rgba(136,136,168,.3);color:var(--muted)}

  .sec-title{font-family:'Sora',sans-serif;font-size:1rem;font-weight:700;margin:2rem 0 .9rem;display:flex;align-items:center;gap:.6rem;flex-wrap:wrap}
  .sec-title .count{font-family:'DM Mono',monospace;font-size:.7rem;color:var(--muted);font-weight:400}
  .sec-title .btn{margin-left:auto}

  .tbl-wrap{overflow-x:auto;background:var(--surface);border:1px solid var(--border);border-radius:12px}
  table{width:100%;border-collapse:collapse;font-size:.8rem}
  th{font-family:'DM Mono',monospace;font-size:.6rem;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);text-align:left;padding:.65rem .8rem;border-bottom:1px solid var(--border);white-space:nowrap}
  td{padding:.6rem .8rem;border-bottom:1px solid var(--border);vertical-align:middle}
  tr:last-child td{border-bottom:none}
  tbody tr:hover td{background:rgba(124,106,247,.05)}
  tr.pick{cursor:pointer}
  tr.pick.sel td{background:rgba(124,106,247,.09)}
  td.mono{font-family:'DM Mono',monospace;font-size:.74rem;word-break:break-all}
  .sc{display:flex;align-items:center;gap:.6rem;min-width:130px}
  .sc b{font-family:'Sora',sans-serif;width:28px}
  .sc-bar{flex:1;height:6px;background:var(--surface2);border-radius:100px;overflow:hidden}
  .sc-bar i{display:block;height:100%;border-radius:100px}

  .about{margin-top:2rem;font-size:.8rem;color:var(--muted);line-height:1.65}
  .about b{color:var(--text)}
  .about ul{margin:.5rem 0 0 1.1rem}

  @media(max-width:1100px){.overview{grid-template-columns:220px 1fr}.overview .wide{grid-column:1/-1}}
  @media(max-width:960px){.overview{grid-template-columns:1fr}.sidebar{display:none}.main{margin-left:0;padding:1.2rem 1rem}}
  .verdict-box{border-radius:10px;padding:.8rem 1rem;margin-bottom:.8rem;font-family:'Sora',sans-serif;font-weight:700;font-size:.95rem;border:1px solid}
  .verdict-box small{display:block;font-family:'DM Mono',monospace;font-weight:400;font-size:.62rem;letter-spacing:.1em;text-transform:uppercase;opacity:.8;margin-bottom:.15rem}
  .verdict-box.ok{background:rgba(106,247,200,.08);border-color:rgba(106,247,200,.35);color:var(--mint)}
  .verdict-box.warn{background:rgba(247,162,106,.08);border-color:rgba(247,162,106,.35);color:var(--orange)}
  .verdict-box.error{background:rgba(247,106,106,.08);border-color:rgba(247,106,106,.35);color:var(--red)}
  .filters{display:flex;gap:.4rem;flex-wrap:wrap;margin-bottom:.8rem}
  .chip{background:var(--surface);border:1px solid var(--border);color:var(--muted);border-radius:100px;padding:.3rem .85rem;font-size:.74rem;cursor:pointer;font-family:'Inter',sans-serif}
  .chip.on{background:rgba(124,106,247,.14);border-color:rgba(124,106,247,.4);color:var(--accent);font-weight:600}
  td .nm{font-weight:600}
  td .why{font-size:.74rem;color:var(--muted);margin-top:.3rem;line-height:1.5}
  td .why b{color:var(--text);font-weight:600}
  td .pol{font-family:'DM Mono',monospace;font-size:.66rem;color:var(--accent)}
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
      <a href="#" class="sidebar-link active">🛡️&nbsp; Spam Score Checker</a>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-label">Navigate</div>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="sidebar-link">🧰&nbsp; All My Tools</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio" class="sidebar-link">👩‍💻&nbsp; Portfolio</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/blog/index.html" class="sidebar-link">📝&nbsp; Blog Posts</a>
    </div>
    <div class="side-note"><b>How it works:</b> 17 checks based on Google's spam policies, run on the page you enter. Lower score = safer site.</div>
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
      <h1>🛡️ Spam Score <span>Checker</span></h1>
    </div>

    <form class="card" id="form">
      <label class="lbl" for="url">Website or page URL</label>
      <div class="row-inline">
        <input type="text" id="url" placeholder="yourwebsite.com" autocomplete="off">
        <button class="btn" type="submit" id="runBtn">🔍 Check Spam Score</button>
      </div>
      <div class="hint">Enter a homepage or any page. https:// is added automatically.</div>
      <div class="spinner" id="sp">Scanning the page for spam signals… this can take up to 30 seconds.</div>
      <div class="err" id="err"></div>
    </form>

    <div id="results" style="display:none">
      <div class="overview">
        <div class="chart-card">
          <h3>Spam risk score</h3>
          <div class="ring-wrap" id="ring"></div>
          <div class="ring-dom" id="ringDom"></div>
        </div>
        <div class="chart-card">
          <h3>Verdict</h3>
          <div class="verdict-box" id="verdict"></div>
          <div class="metrics" id="metrics"></div>
        </div>
        <div class="chart-card wide">
          <h3>Risk points by category</h3>
          <div class="chart-box"><canvas id="chart"></canvas></div>
        </div>
      </div>

      <div class="sec-title">🚩 Spam signals <span class="count" id="count"></span>
        <button type="button" class="btn ghost" id="csvBtn">⬇ Download CSV</button></div>
      <div class="filters" id="filters">
        <button type="button" class="chip on" data-f="all">All</button>
        <button type="button" class="chip" data-f="flag">🚩 Flagged</button>
        <button type="button" class="chip" data-f="pass">✅ Passed</button>
        <button type="button" class="chip" data-f="unverifiable">❔ Couldn't check</button>
      </div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>Status</th><th>Check</th><th>Category</th><th>Severity</th><th>What we found</th></tr></thead>
          <tbody id="body"></tbody>
        </table>
      </div>

      <div class="about">
        <b>About this score.</b> This is my own <b>spam risk score</b>, built from warning signs listed in
        <a href="https://developers.google.com/search/docs/essentials/spam-policies" target="_blank">Google's spam policies</a> —
        it is not Moz's Spam Score, and Google doesn't publish a spam score of its own. It checks only the page you enter (not backlinks),
        so treat it as a quick health check, not a penalty prediction.
        <ul>
          <li>Points per flag: <b>High</b> 15 · <b>Medium</b> 8 · <b>Low</b> 4 (capped at 100)</li>
          <li><b>0–20</b> Low risk (safe for backlinks) · <b>21–45</b> Medium risk (use caution) · <b>46–100</b> High risk (avoid)</li>
          <li>"Couldn't check" results add no points — often the site simply blocks automated bots.</li>
        </ul>
      </div>
    </div>
  </main>
</div>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const colOf = s => s <= 20 ? '#6af7c8' : s <= 45 ? '#f7a26a' : '#f76a6a';
const SEV = { High: 'error', Medium: 'warn', Low: 'purple' };
const ST = { flag: ['🚩 Flagged', 'error'], pass: ['✅ Passed', 'ok'], unverifiable: ['❔ Couldn\'t check', 'none'] };
let D = null, FILTER = 'all', CH = null;

$('form').addEventListener('submit', async e => {
  e.preventDefault();
  const url = $('url').value.trim();
  $('err').textContent = '';
  if (!url) { $('err').textContent = 'Please enter a website or page URL.'; return; }
  $('sp').classList.add('show'); $('runBtn').disabled = true;
  try {
    const r = await fetch('/api/check', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }) });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'Something went wrong.');
    D = j; FILTER = 'all';
    document.querySelectorAll('.chip').forEach(c => c.classList.toggle('on', c.dataset.f === 'all'));
    render();
    $('results').style.display = '';
  } catch (err) {
    $('err').textContent = err.message || 'Could not reach the server — please try again.';
  } finally {
    $('sp').classList.remove('show'); $('runBtn').disabled = false;
  }
});

function ring(sc) {
  const col = colOf(sc);
  return `<svg viewBox="0 0 160 160" width="160" height="160">
      <circle cx="80" cy="80" r="68" fill="none" stroke="#23233a" stroke-width="12"/>
      <circle cx="80" cy="80" r="68" fill="none" stroke="${col}" stroke-width="12" stroke-linecap="${sc > 0 ? 'round' : 'butt'}"
        stroke-dasharray="${(427.26 * sc / 100).toFixed(2)} 427.26" transform="rotate(-90 80 80)"/></svg>
    <div class="ring-center"><div class="score" style="color:${col}">${sc}</div><div class="sub">risk / 100</div></div>`;
}

function render() {
  $('ring').innerHTML = ring(D.score);
  $('ringDom').innerHTML = esc(D.domain);
  $('verdict').className = 'verdict-box ' + D.verdict[1];
  $('verdict').innerHTML = `<small>${esc(D.level[0])} · Backlink verdict</small>${esc(D.verdict[0])}`;
  const s = D.stats;
  $('metrics').innerHTML = [
    ['Flags', s.flags, s.flags ? 'red' : 'mint'],
    ['Checks passed', s.passed + ' / ' + D.checks.length, 'mint'],
    ['Words on page', s.words.toLocaleString('en-US'), ''],
    ['Outbound links', s.external + ' of ' + s.links, ''],
  ].map(([k, v, cl]) => `<div class="metric"><div class="k">${k}</div><div class="v ${cl}">${esc(v)}</div></div>`).join('');
  $('count').textContent = `${D.checks.length} checks · checked ${D.checked_at}`;
  table(); drawChart();
}

function table() {
  const order = { flag: 0, unverifiable: 1, pass: 2 }, sevO = { High: 0, Medium: 1, Low: 2 };
  const rows = D.checks.filter(c => FILTER === 'all' || c.status === FILTER)
    .sort((a, b) => order[a.status] - order[b.status] || sevO[a.severity] - sevO[b.severity]);
  $('body').innerHTML = rows.length ? rows.map(c => `<tr>
      <td><span class="badge ${ST[c.status][1]}">${ST[c.status][0]}</span></td>
      <td><div class="nm">${esc(c.name)}</div><div class="pol">${esc(c.policy)}</div>
        ${c.status === 'flag' ? `<div class="why"><b>Why it matters:</b> ${esc(c.why)}<br><b>How to fix:</b> ${esc(c.fix)}</div>` : ''}</td>
      <td>${esc(c.category)}</td>
      <td><span class="badge ${SEV[c.severity]}">${esc(c.severity)}${c.points ? ' · +' + c.points : ''}</span></td>
      <td style="font-size:.78rem">${esc(c.detail)}</td></tr>`).join('')
    : '<tr><td colspan="5" class="muted" style="text-align:center;padding:1.2rem">Nothing here 🎉</td></tr>';
}

document.querySelectorAll('.chip').forEach(ch => ch.addEventListener('click', () => {
  FILTER = ch.dataset.f;
  document.querySelectorAll('.chip').forEach(c => c.classList.toggle('on', c === ch));
  table();
}));

function drawChart() {
  if (typeof Chart === 'undefined') return;
  if (CH) CH.destroy();
  const cats = ['Content', 'Links', 'Security', 'Technical', 'Trust', 'Domain'];
  const pts = cats.map(c => D.checks.filter(x => x.category === c).reduce((a, x) => a + x.points, 0));
  const grid = { color: '#23233a' }, ticks = { color: '#8888a8', font: { family: 'DM Mono', size: 10 } };
  CH = new Chart($('chart'), {
    type: 'bar',
    data: { labels: cats, datasets: [{ data: pts, backgroundColor: pts.map(p => p >= 15 ? '#f76a6a' : p > 0 ? '#f7a26a' : '#6af7c8'),
      borderRadius: 5, maxBarThickness: 22, minBarLength: 3 }] },
    options: { indexAxis: 'y', maintainAspectRatio: false, plugins: { legend: { display: false },
        tooltip: { callbacks: { label: c => ' ' + c.parsed.x + ' risk points' } } },
      scales: { x: { min: 0, suggestedMax: 30, grid, ticks }, y: { grid: { display: false }, ticks } } }
  });
}

$('csvBtn').addEventListener('click', () => {
  if (!D) return;
  const q = v => '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"';
  const out = [['Website', D.final_url].map(q).join(','), ['Spam risk score', D.score + ' / 100 (' + D.level[0] + ')'].map(q).join(','),
    ['Backlink verdict', D.verdict[0]].map(q).join(','), '',
    ['Status', 'Check', 'Category', 'Severity', 'Points', 'Google policy', 'What we found', 'Why it matters', 'How to fix'].map(q).join(',')];
  D.checks.forEach(c => out.push([c.status, c.name, c.category, c.severity, c.points, c.policy, c.detail, c.why, c.fix].map(q).join(',')));
  const blob = new Blob(['﻿' + out.join('\r\n')], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
  a.download = `spam-score-${D.domain}.csv`; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
});
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(debug=True, port=5000)
