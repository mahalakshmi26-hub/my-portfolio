"""
Meta Tag Generator  -  Flask app
Give it a page URL + a primary keyword (+ optional secondary keyword) and it returns
SEO-ready meta title, meta description and meta keywords that are checked for
character length, pixel width and basic grammar / style.

Generation is template based (no paid API): every sentence comes from a fixed,
grammar-checked pattern, so the wording is always clean. The page at the URL is
fetched to detect the brand name, the page type and to compare against the
current title / description.

Built by Mahalakshmi Marimuthu - Digital Marketing Strategist & AI-Powered SEO Expert
"""
import datetime
import ipaddress
import json
import os
import re
import socket
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

# --------------------------------------------------------------------------- #
# Limits (Google truncates by pixel width, so both characters and pixels matter)
# --------------------------------------------------------------------------- #
TITLE_MIN, TITLE_MAX, TITLE_PX_LIMIT, TITLE_FONT = 30, 60, 580, 20
DESC_MIN, DESC_MAX, DESC_PX_LIMIT, DESC_FONT = 120, 155, 920, 14

# Arial / Helvetica glyph widths (1000 units per em). Google's SERP uses Arial.
WIDTHS = {
    " ": 278, "!": 278, '"': 355, "#": 556, "$": 556, "%": 889, "&": 667, "'": 191,
    "(": 333, ")": 333, "*": 389, "+": 584, ",": 278, "-": 333, ".": 278, "/": 278,
    ":": 278, ";": 278, "<": 584, "=": 584, ">": 584, "?": 556, "@": 1015,
    "[": 278, "\\": 278, "]": 278, "^": 469, "_": 556, "`": 333, "{": 334, "|": 260,
    "}": 334, "~": 584, "–": 556, "—": 1000, "’": 222, "‘": 222,
    "“": 333, "”": 333, "₹": 556, "•": 350, "·": 278,
    "…": 1000,
}
for _c, _w in zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                  [667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833,
                   722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611]):
    WIDTHS[_c] = _w
for _c, _w in zip("abcdefghijklmnopqrstuvwxyz",
                  [556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833,
                   556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500]):
    WIDTHS[_c] = _w
for _c in "0123456789":
    WIDTHS[_c] = 556


def text_px(text, size):
    return round(sum(WIDTHS.get(ch, 556) for ch in text) * size / 1000)


def measure(kind, text):
    if kind == "title":
        mn, mx, lim, size = TITLE_MIN, TITLE_MAX, TITLE_PX_LIMIT, TITLE_FONT
    else:
        mn, mx, lim, size = DESC_MIN, DESC_MAX, DESC_PX_LIMIT, DESC_FONT
    chars, px = len(text), text_px(text, size)
    if chars > mx or px > lim:
        status, label = "long", "Too long - may be cut off in Google"
    elif chars < mn:
        status, label = "short", "A bit short - room for more detail"
    else:
        status, label = "good", "Good length"
    return {"chars": chars, "px": px, "limit_px": lim, "min": mn, "max": mx,
            "status": status, "label": label, "pct": min(100, round(px / lim * 100))}


# --------------------------------------------------------------------------- #
# Keyword formatting (keeps acronyms / bank names correct)
# --------------------------------------------------------------------------- #
ACRONYMS = {"emi", "sip", "cibil", "kyc", "pan", "gst", "itr", "nri", "hdfc", "icici", "sbi",
            "pnb", "idfc", "rbi", "upi", "fd", "rd", "ppf", "nps", "epf", "lic", "tds", "ifsc",
            "neft", "rtgs", "imps", "ipo", "nav", "amc", "etf", "atm", "emis", "ai", "seo",
            "cc", "roi", "apr", "msme", "itr", "uk", "usa", "uae"}
BANK_ACRONYMS = {"hdfc", "icici", "sbi", "pnb", "idfc", "lic"}
PROPER = {"bankbazaar": "BankBazaar", "paisabazaar": "Paisabazaar", "indusind": "IndusInd",
          "kotak": "Kotak", "axis": "Axis", "bajaj": "Bajaj", "finserv": "Finserv",
          "tata": "Tata", "indian": "Indian", "india": "India", "aditya": "Aditya",
          "birla": "Birla", "fullerton": "Fullerton", "muthoot": "Muthoot", "piramal": "Piramal",
          "moneyview": "MoneyView", "navi": "Navi", "fibe": "Fibe", "groww": "Groww",
          "canara": "Canara", "baroda": "Baroda", "union": "Union", "federal": "Federal",
          "idbi": "IDBI", "yes": "Yes", "hsbc": "HSBC", "citi": "Citi", "standard": "Standard",
          "chartered": "Chartered", "karnataka": "Karnataka", "bandhan": "Bandhan"}
# words that are only proper when they follow a proper noun ("Yes Bank", "Union Bank")
CONTEXT_ONLY = {"yes", "union", "standard", "chartered", "federal", "indian", "india",
                "karnataka", "canara", "baroda", "tata", "aditya", "birla", "navi"}
SUFFIX_WORDS = {"bank", "finance", "finserv", "capital", "housing", "fincorp", "securities", "of"}
SMALL_WORDS = {"a", "an", "the", "and", "or", "of", "in", "to", "for", "on", "at", "by", "vs",
               "with", "from", "as", "per", "via"}
SITE_BRANDS = {"bankbazaar": "BankBazaar", "paisabazaar": "Paisabazaar", "policybazaar": "PolicyBazaar",
               "groww": "Groww", "bajajfinserv": "Bajaj Finserv", "hdfcbank": "HDFC Bank",
               "icicibank": "ICICI Bank", "sbi": "SBI", "axisbank": "Axis Bank"}


def _clean_kw(kw):
    kw = re.sub(r"\s+", " ", (kw or "").strip()).rstrip("?").strip()
    if kw.isupper() and " " in kw:
        kw = kw.lower()
    return kw


def fmt_kw(kw, mode="sentence", cap_first=False):
    """mode='sentence' -> lower-case words, mode='title' -> Title Case. Acronyms/brands fixed."""
    tokens = _clean_kw(kw).split(" ")
    out, prev_proper = [], False
    for i, t in enumerate(tokens):
        low = re.sub(r"[^\w&\-]", "", t.lower())
        kind, val = "word", t.lower()
        if any(ch.isupper() for ch in t[1:]) or (t.isupper() and len(t) > 1):
            kind, val = "fixed", t
        elif low in ACRONYMS:
            kind, val = "fixed", t.upper()
        elif low in PROPER and (low not in CONTEXT_ONLY or prev_proper or
                                (i + 1 < len(tokens) and tokens[i + 1].lower() in SUFFIX_WORDS)):
            kind, val = "fixed", PROPER[low]
        elif prev_proper and low in SUFFIX_WORDS and low != "of":
            kind, val = "fixed", low.capitalize()
        prev_proper = kind == "fixed" and (low in PROPER or low in BANK_ACRONYMS or
                                           (any(ch.isupper() for ch in t[1:])))
        if kind == "word" and mode == "title":
            if i > 0 and low in SMALL_WORDS:
                val = low
            else:
                val = "-".join(p.capitalize() for p in val.split("-"))
        out.append(val)
    s = " ".join(out)
    if cap_first and s:
        s = s[0].upper() + s[1:]
    return s


def _stem(w):
    w = w.lower()
    for suf in ("ations", "ation", "ings", "ing", "ions", "ion", "ed", "es", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[:-len(suf)]
    return w


def same_intent(a, b):
    """True when two keywords are just word-form variants of each other
    (e.g. 'personal loan rejection' vs 'personal loan rejected')."""
    ta = {_stem(t) for t in re.sub(r"[^a-z0-9 ]", " ", a.lower()).split()}
    tb = {_stem(t) for t in re.sub(r"[^a-z0-9 ]", " ", b.lower()).split()}
    return bool(ta) and ta == tb


def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()


def has_kw(text, kw):
    return bool(kw) and norm(kw) in norm(text)


# --------------------------------------------------------------------------- #
# Keyword -> content type
# --------------------------------------------------------------------------- #
def classify(kw, hint=""):
    k = kw.lower()
    if re.match(r"^(what|why|how|when|which|who|can|is|are|does|do|should|will)\b", k):
        return "question"
    if re.search(r"\b(vs|versus|compare|comparison)\b", k) or re.match(r"^(best|top|cheapest|lowest)\b", k):
        return "compare"
    if "calculator" in k:
        return "calculator"
    if re.search(r"\beligibility\b|\bcriteria\b|\bqualif", k):
        return "eligibility"
    if re.search(r"\b(interest rates?|rates?|charges|fees|roi)\b", k):
        return "rates"
    if re.search(r"\b(documents?|documentation|checklist)\b", k):
        return "documents"
    if re.search(r"\b(reject\w*|declin\w*|denied|denial|reasons?|mistakes?|problems?|issues?|fraud|scam|"
                 r"complaints?|defaults?|delay\w*|cancel\w*|foreclos\w*|prepay\w*|penalty|penalties|"
                 r"harassment|recovery|settlement|late)\b", k):
        return "issue"
    if re.search(r"\b(score|report|status|login|customer care|number|app|apps)\b", k):
        return hint if hint else "generic"
    if re.search(r"\b(guide|guidelines?|rules|regulations?|norms|faqs?|difference|definition|tips|meaning|benefits|features|types|process|steps)\b", k):
        return "guide"
    # a product page only when the keyword itself ENDS in a product noun ("personal loan", "credit card")
    last = re.sub(r"\b(online|in india|india)$", "", k).strip().split(" ")[-1] if k.strip() else ""
    if re.search(r"\bloans? (against|on) \w+$", k):  # "loan against property", "loan on car"
        return "product"
    if last in {"loan", "loans", "card", "cards", "insurance", "account", "accounts", "deposit", "deposits",
                "scheme", "schemes", "plan", "plans", "policy", "policies", "mortgage", "finance", "fund", "funds"}:
        return "product"
    return hint if hint else "generic"


def page_hint(url, title, h1):
    path = urlparse(url or "").path.lower()
    s = " ".join([path, title or "", h1 or ""]).lower()
    if "calculator" in s:
        return "calculator"
    if "eligib" in path:  # only trust the URL for this, page text mentions it too often
        return "eligibility"
    if re.search(r"interest[- ]rate", s):
        return "rates"
    if "document" in path:
        return "documents"
    if re.search(r"/blog|guide|how-to|what-is", s):
        return "guide"
    return ""


# --------------------------------------------------------------------------- #
# Template libraries  (all sentences are fixed, grammar-checked patterns)
#   {K} keyword (lower case)   {K2} secondary   {KCAP}/{KQ} capitalised keyword
#   {the_K} "the" + keyword    {with_B} " with Brand" (or empty)   {Y} year
# --------------------------------------------------------------------------- #
DESC_LIB = {
    "product": {
        "open": ["Compare {K} offers, interest rates and eligibility criteria before you apply.",
                 "Explore {K} features, eligibility and required documents in one place.",
                 "Find the right {K} option by comparing rates, fees and eligibility."],
        "open2": ["Compare {K} offers and learn about {K2} before you apply.",
                  "Explore {K} details, including {K2}, to make an informed choice."],
        "mid": ["Understand charges and repayment terms before you decide.",
                "Get clear answers on approval factors and documentation."],
        "mid2": ["Includes clear details on {K2}."],
        "cta": ["Apply online{with_B}.", "Check your eligibility and apply online{with_B}.",
                "Start your application{with_B} today."]},
    "rates": {
        "open": ["See the latest {K} and compare lenders side by side.",
                 "Check current {K} and the key details before you apply.",
                 "Compare {K} across lenders to find an option that suits your needs."],
        "open2": ["Check the latest {K} and learn about {K2} in one place."],
        "mid": ["Understand what affects your offer and how lenders decide.",
                "Get clear details on processing fees and other charges."],
        "mid2": ["Includes clear details on {K2}."],
        "cta": ["Compare and apply online{with_B}.", "Explore offers{with_B} today."]},
    "calculator": {
        "open": ["Use the {K} to plan your finances quickly and accurately.",
                 "Get quick, accurate results with the {K}.",
                 "Plan smarter with the {K} and see your numbers in seconds."],
        "open2": ["Use the {K} and explore {K2} to plan your finances better."],
        "mid": ["Enter a few details to see your results instantly.",
                "Adjust the inputs to compare different scenarios."],
        "mid2": ["Also learn about {K2}."],
        "cta": ["Try it online{with_B}.", "Use it{with_B} today."]},
    "eligibility": {
        "open": ["Understand {K} and check whether you qualify before you apply.",
                 "Learn about {K}, requirements and the factors that affect your approval.",
                 "Get clear details on {K}, required documents and how lenders assess applications."],
        "open2": ["Understand {K} and {K2} before you apply."],
        "mid": ["See the factors that affect your approval chances.",
                "Know the documents lenders usually ask for."],
        "mid2": ["Includes clear details on {K2}."],
        "cta": ["Check your details and apply online{with_B}.", "Get started{with_B} today."]},
    "documents": {
        "open": ["See {K} in one clear checklist so your application goes smoothly.",
                 "Know exactly what to keep ready: {K}, explained clearly."],
        "open2": ["See {K} and {K2} in one clear checklist so your application goes smoothly."],
        "mid": ["Avoid delays by preparing everything in advance.",
                "Learn what lenders usually verify."],
        "mid2": ["Also covers {K2}."],
        "cta": ["Apply online{with_B}.", "Start your application{with_B} today."]},
    "guide": {
        "open": ["Get a clear explanation of {K}, with key points and practical tips.",
                 "Learn all about {K} with simple explanations and practical tips.",
                 "Everything you need to know about {K}, explained in simple terms."],
        "open2": ["Learn all about {K} and {K2} with simple explanations and practical tips."],
        "mid": ["Understand the key points and what to watch out for.",
                "Make informed decisions with clear, practical information."],
        "mid2": ["Also covers {K2}."],
        "cta": ["Explore more guides{with_B}.", "Learn more{with_B} today."]},
    "question": {
        "open": ["{KQ}? Get a clear, simple explanation with key points and practical tips.",
                 "{KQ}? Find a simple explanation along with the key factors to know."],
        "open2": ["{KQ}? Get a clear explanation, plus key details on {K2}."],
        "mid": ["Understand the essentials before you make a decision.",
                "Avoid common mistakes with practical, easy-to-follow advice."],
        "mid2": ["Also covers {K2}."],
        "cta": ["Read more{with_B}.", "Explore more{with_B} today."]},
    "howto": {
        "open": ["{KQ}? Follow this clear, step-by-step guide with practical tips.",
                 "{KQ}? Learn each step, the documents needed and common mistakes to avoid."],
        "open2": ["{KQ}? Follow this step-by-step guide, including details on {K2}."],
        "mid": ["Understand the essentials before you make a decision.",
                "Avoid common mistakes with practical, easy-to-follow advice."],
        "mid2": ["Also covers {K2}."],
        "cta": ["Read more{with_B}.", "Explore more{with_B} today."]},
    "compare": {
        "open": ["Compare {the_K} by features, fees and eligibility to choose with confidence.",
                 "Explore {the_K} with a clear look at rates, fees and eligibility."],
        "open2": ["Compare {the_K} and learn about {K2} to choose with confidence."],
        "mid": ["Review the pros and cons before you decide.",
                "Get clear, side-by-side details to help you choose."],
        "mid2": ["Includes clear details on {K2}."],
        "cta": ["Compare and apply online{with_B}.", "Find your best fit{with_B} today."]},
    "compare_vs": {
        "open": ["{KCAP}: see a side-by-side comparison of rates, fees and eligibility.",
                 "{KCAP}: compare features, charges and eligibility in one place."],
        "open2": ["{KCAP}: compare rates, fees and eligibility, including {K2}."],
        "mid": ["Review the pros and cons before you decide.",
                "Get clear, side-by-side details to help you choose."],
        "mid2": ["Includes clear details on {K2}."],
        "cta": ["Compare and apply online{with_B}.", "Find your best fit{with_B} today."]},
    "issue": {
        "open": ["{KCAP}: understand the common causes, practical fixes and what to do next.",
                 "{KCAP}: learn the main reasons, practical solutions and the next steps to take."],
        "open2": ["{KCAP}: understand the common causes and next steps, including {K2}."],
        "mid": ["Get clear, practical guidance you can act on.",
                "Avoid common mistakes with simple, step-by-step advice."],
        "mid2": ["Also covers {K2}."],
        "cta": ["Explore more guides{with_B}.", "Read more{with_B} today."]},
    "generic": {
        "open": ["{KCAP}: clear explanations, key points and practical tips in one place.",
                 "Get clear, practical information on {K}, explained in simple terms.",
                 "Learn what you need to know about {K}, with simple explanations and practical tips."],
        "open2": ["Learn about {K} and {K2} with clear explanations and practical tips."],
        "mid": ["Understand the essentials before you make a decision.",
                "Find accurate, easy-to-follow information in one place."],
        "mid2": ["Also covers {K2}."],
        "cta": ["Read more{with_B}.", "Learn more{with_B} today."]},
}

TITLE_DESC = {
    "product": ["Apply Online", "Check Eligibility & Apply", "Compare Offers & Apply Online",
                "Features, Eligibility & Apply", "Rates, Eligibility & How to Apply"],
    "rates": ["Compare Lenders {year}", "Compare & Apply Online", "Fees, Charges & Eligibility",
              "Compare Top Lenders", "Check {year} Details"],
    "calculator": ["Quick & Easy Online Tool", "Get Results in Seconds", "Plan Your Finances Online",
                   "Simple, Fast & Accurate"],
    "eligibility": ["Criteria & How to Check", "Check Online", "Requirements & Documents", "Who Can Apply"],
    "documents": ["Complete Checklist", "Full List & Requirements", "Check What You Need",
                  "Complete Guide {year}"],
    "guide": ["Complete Guide {year}", "Step-by-Step Guide", "Explained Simply",
              "Everything You Need to Know", "Key Points & Tips"],
    "question": ["Complete Guide", "Simple Explanation", "Everything You Need to Know",
                 "Step-by-Step Guide", "Explained {year}"],
    "compare": ["Compare & Choose {year}", "Features, Fees & Comparison", "Detailed Comparison",
                "Full Comparison {year}"],
    "issue": ["Reasons, Fixes & Next Steps", "Common Causes & Solutions", "What to Do Next",
              "Causes, Fixes & Tips"],
    "generic": ["Complete Guide {year}", "Everything You Need to Know", "Key Details & Tips",
                "Explained Simply"],
}

KEYWORD_MODS = {
    "product": ["{k} online", "{k} apply online", "{k} eligibility", "{k} interest rate",
                "{k} documents required", "best {k}"],
    "rates": ["{k} {year}", "latest {k}", "compare {k}", "{k} comparison"],
    "calculator": ["{k} online", "online {k}", "{k} tool"],
    "eligibility": ["{k} criteria", "{k} check online", "{k} documents"],
    "documents": ["{k} checklist", "{k} list", "{k} required"],
    "guide": ["{k} guide", "{k} explained", "{k} tips"],
    "question": ["{k} explained", "{k} guide"],
    "compare": ["{k} comparison", "{k} {year}"],
    "issue": ["{k} reasons", "{k} solutions", "{k} guide"],
    "generic": ["{k} guide", "{k} online", "{k} explained"],
}


# --------------------------------------------------------------------------- #
# Candidate builders + ranking
# --------------------------------------------------------------------------- #
def clean(s):
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    return s


TITLE_FRAMES = {
    "kf": "{K} \u2013 {d}",
    "kc": "{K}: {d}",
    "ek": "Everything to Know About {K}",
    "gt": "Guide to {K}: {d}",
    "ex": "{K} Explained: {d}",
    "yr": "{K} ({Y}): {d}",
    "un": "Understanding {K}: {d}",
    "wk": "What to Know About {K}: {d}",
    "la": "Latest {K}: {d}",
    "on": "{K} Online \u2013 {d}",
    "us": "Use the {K}: {d}",
    "q": "{K}? {d}",
    "qg": "{d}: {K}?",
    "qy": "{K}? ({Y} Guide)",
}
_INFO = ["kf", "kc", "ek", "gt", "ex", "yr", "un", "wk"]
FRAMES_BY_CLASS = {
    "guide": _INFO, "generic": _INFO, "issue": _INFO,
    "eligibility": ["kf", "kc", "ek", "gt", "ex", "wk", "yr"],
    "documents": ["kf", "kc", "ek", "gt", "ex", "wk", "yr"],
    "rates": ["kf", "kc", "la", "ek", "yr", "wk"],
    "product": ["kf", "kc", "yr", "on"],
    "calculator": ["kf", "kc", "us", "ex"],
    "compare": ["kf", "kc", "yr"],
    "question": ["q", "qg", "qy"],
}


def _frame_words(tpl):
    """Fixed (non-placeholder) words of a frame, used to avoid repeating them in the descriptor."""
    return {w.lower() for w in re.findall(r"[A-Za-z]{4,}", re.sub(r"\{[^}]*\}", " ", tpl))}


def build_titles(kw, kw2, brand, cls, year):
    K = fmt_kw(kw, "title")
    K2 = fmt_kw(kw2, "title") if kw2 else ""
    klow = K.lower()
    descs = [d.format(year=year) for d in TITLE_DESC[cls]]
    rows = []  # (score, text, measure, frame)

    def consider(text, frame, has_d):
        c = clean(text)
        m = measure("title", c)
        fits = m["chars"] <= TITLE_MAX and m["px"] <= TITLE_PX_LIMIT
        s = 0
        if fits and m["chars"] >= TITLE_MIN:
            s += 100
        elif fits:
            s += 40
        else:
            s -= max(m["chars"] - TITLE_MAX, (m["px"] - TITLE_PX_LIMIT) / 9) * 3
        s += 6 if brand and brand in c else 0
        s += 12 if K2 and has_kw(c, kw2) else 0
        s += 6 if has_d else 0
        s -= abs(m["chars"] - 56) * 0.5
        rows.append((s, c, m, frame))

    for fid in FRAMES_BY_CLASS[cls]:
        tpl = TITLE_FRAMES[fid]
        fixed = _frame_words(tpl)
        if any(w in klow for w in fixed):  # e.g. keyword already contains "guide"
            continue
        uses_d = "{d}" in tpl
        for d in (descs if uses_d else [""]):
            dl = d.lower()
            if uses_d and (any(w in dl for w in fixed) or ("{Y}" in tpl and str(year) in d)):
                continue
            base = tpl.format(K=K, d=d, Y=year)
            consider(base, fid, uses_d)
            if brand:
                consider(f"{base} | {brand}", fid, uses_d)
            if K2:
                consider(f"{base} | {K2}", fid, uses_d)
    # plain keyword fallbacks (low priority, only used when nothing else fits)
    Kq = K + "?" if cls == "question" else K
    for t in (Kq, f"{Kq} | {brand}" if brand else ""):
        if t:
            m = measure("title", t)
            rows.append((-30 - abs(m["chars"] - 56) * 0.5, t, m, "plain"))

    rows.sort(key=lambda r: -r[0])
    picked, used_frames, seen = [], set(), set()
    for s, c, m, f in rows:  # best option of each different structure first
        if f == "plain" or f in used_frames or c.lower() in seen:
            continue
        used_frames.add(f)
        seen.add(c.lower())
        picked.append({"text": c, **m})
        if len(picked) == 5:
            break
    for s, c, m, f in rows:  # top up if fewer than 3 structures were possible
        if len(picked) >= 3:
            break
        if c.lower() not in seen:
            seen.add(c.lower())
            picked.append({"text": c, **m})
    return picked


def build_descriptions(kw, kw2, brand, cls, year):
    sub = cls
    kl = kw.lower()
    if cls == "compare" and re.search(r"\b(vs|versus)\b", kl):
        sub = "compare_vs"
    if cls == "question" and kl.startswith("how to"):
        sub = "howto"
    lib = DESC_LIB[sub]
    K = fmt_kw(kw, "sentence")
    V = {"K": K, "K2": fmt_kw(kw2, "sentence") if kw2 else "",
         "KCAP": fmt_kw(kw, "sentence", cap_first=True),
         "KQ": fmt_kw(kw, "sentence", cap_first=True),
         "the_K": K if K.lower().startswith("the ") else "the " + K,
         "B": brand, "with_B": f" with {brand}" if brand else "", "Y": year}
    opens = (lib["open2"] if kw2 else []) + lib["open"]
    mids = [""] + (lib["mid2"] if kw2 else []) + lib["mid"]
    ctas = [""] + lib["cta"]
    rows, seen = [], set()
    n_open2 = len(lib["open2"]) if kw2 else 0
    for oi, o in enumerate(opens):
        for m in mids:
            if oi < n_open2 and m in lib["mid2"]:
                continue  # secondary keyword is already in the opener - don't repeat it
            for c in ctas:
                text = clean(" ".join(p for p in (o, m, c) if p).format_map(V))
                if text.lower() in seen:
                    continue
                seen.add(text.lower())
                me = measure("description", text)
                fits = me["chars"] <= DESC_MAX and me["px"] <= DESC_PX_LIMIT
                s = 0
                if fits and me["chars"] >= DESC_MIN:
                    s += 100
                elif fits:
                    s += 40 - (DESC_MIN - me["chars"]) * 0.5
                else:
                    s -= max(me["chars"] - DESC_MAX, (me["px"] - DESC_PX_LIMIT) / 6) * 2
                s += 12 if c else 0
                s += 25 if kw2 and has_kw(text, kw2) else 0
                s += 4 if brand and brand in text else 0
                idx = norm(text).find(norm(kw))
                s += 8 if 0 <= idx < 70 else 0
                s -= abs(me["chars"] - 142) * 0.3
                rows.append((s, oi, text, me))
    rows.sort(key=lambda r: -r[0])
    picked, used = [], set()
    for s, oi, text, me in rows:
        if oi in used:
            continue
        used.add(oi)
        picked.append({"text": text, **me})
        if len(picked) == 4:
            break
    return picked


def build_keywords(kw, kw2, brand, cls, page, year):
    k = norm(kw)
    out = []

    def add(x):
        x = norm(x)
        if x and x not in out:
            out.append(x)

    add(kw)
    if kw2:
        add(kw2)
    ktoks = set(k.split())
    for mod in KEYWORD_MODS.get(cls, KEYWORD_MODS["generic"]):
        extra = set(norm(mod.replace("{k}", "").replace("{year}", "")).split())
        if extra and extra <= ktoks:
            continue
        add(mod.format(k=k, year=year))
    if brand:
        add(f"{brand} {kw}")
    stop = {"the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "with", "your", "you", "how", "what"}
    sig = {t for t in ktoks if t not in stop}
    for src in [page.get("h1", "")] + page.get("h2s", [])[:3]:
        n = norm(src)
        if n and 2 <= len(n.split()) <= 6 and sig & set(n.split()) and n != k:
            add(n)
    res, total = [], 0
    for x in out:
        if len(res) >= 10 or total + len(x) > 250:
            break
        res.append(x)
        total += len(x) + 2
    return res


# --------------------------------------------------------------------------- #
# Analysis / checks
# --------------------------------------------------------------------------- #
VOWEL_EXC = {"user", "users", "unique", "uniform", "union", "usual", "utility", "use", "used", "one", "euro", "useful"}
AN_EXC = {"hour", "honest", "honour", "heir"}


def grammar_issues(text, is_title=False):
    issues = []
    if not text:
        return issues
    if re.search(r"\s{2,}", text):
        issues.append("double spaces")
    m = re.search(r"\b([A-Za-z]+)\s+\1\b", text, re.I)
    if m:
        issues.append(f'repeated word "{m.group(1)}"')
    if re.search(r"[,;:!?.]{2,}", text.replace("...", "")):
        issues.append("doubled punctuation")
    if re.search(r"\s[,.;:!?]", text):
        issues.append("space before punctuation")
    if re.search(r"[,;:!?][A-Za-z]", text):
        issues.append("missing space after punctuation")
    if text[0].isalpha() and text[0].islower():
        issues.append("does not start with a capital letter")
    for a, w in re.findall(r"\b(a|an)\s+([A-Za-z]+)", text, re.I):
        wl = w.lower()
        vowel = wl[0] in "aeiou" and wl not in VOWEL_EXC
        if a.lower() == "a" and vowel:
            issues.append(f'"a {w}" should be "an {w}"')
        if a.lower() == "an" and (not vowel) and wl not in AN_EXC:
            issues.append(f'"an {w}" should be "a {w}"')
    if text.count("(") != text.count(")") or text.count('"') % 2:
        issues.append("unbalanced brackets or quotes")
    return issues


def run_checks(title, desc, kw, kw2):
    t, d = measure("title", title), measure("description", desc)
    checks = []

    def add(label, ok, detail=""):
        checks.append({"label": label, "ok": bool(ok), "detail": detail})

    add("Title is 30-60 characters", TITLE_MIN <= t["chars"] <= TITLE_MAX, f'{t["chars"]} characters')
    add("Title fits within ~580 px", t["px"] <= TITLE_PX_LIMIT, f'{t["px"]} px')
    add("Primary keyword is in the title", has_kw(title, kw), "")
    pos = norm(title).find(norm(kw)) if has_kw(title, kw) else -1
    add("Primary keyword is in the first 30 characters of the title", 0 <= pos <= 30, "")
    add("Description is 120-155 characters", DESC_MIN <= d["chars"] <= DESC_MAX, f'{d["chars"]} characters')
    add("Description fits within ~920 px", d["px"] <= DESC_PX_LIMIT, f'{d["px"]} px')
    add("Primary keyword is in the description", has_kw(desc, kw), "")
    if kw2:
        add("Secondary keyword is used", has_kw(title, kw2) or has_kw(desc, kw2), "")
    add("Description ends as a complete sentence", bool(desc) and desc.rstrip()[-1] in ".!?", "")
    nk, nk2 = norm(kw), norm(kw2)

    def own_count(text):  # occurrences of the primary keyword, ignoring those inside the secondary keyword
        n = norm(text).count(nk)
        if nk2 and nk in nk2:
            n -= norm(text).count(nk2)
        return n
    stuffed = own_count(desc) > 2 or own_count(title) > 1
    add("No keyword stuffing", not stuffed, "")
    gi = grammar_issues(title, True) + grammar_issues(desc)
    add("Grammar & style (spacing, punctuation, capitals, a/an)", not gi, "; ".join(gi))
    ok = sum(1 for c in checks if c["ok"])
    return {"title": t, "description": d, "checks": checks, "score": round(100 * ok / len(checks))}


# --------------------------------------------------------------------------- #
# Page fetching (server-side, SSRF-safe)
# --------------------------------------------------------------------------- #
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
           "Accept-Language": "en-IN,en;q=0.9"}
ALLOW_PRIVATE = os.environ.get("ALLOW_PRIVATE_URLS") == "1"  # only for local testing


def is_public_host(host):
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved \
                or ip.is_multicast or ip.is_unspecified:
            return False
    return True


def normalise_url(u):
    u = (u or "").strip()
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    return u


def domain_label(host):
    parts = (host or "").lower().replace("www.", "", 1).split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net", "gov", "ac"}:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else parts[0]


def fetch_page(url):
    out = {"ok": False, "final_url": url, "status": None, "title": "", "h1": "", "h2s": [],
           "meta_description": "", "og_site": "", "body_text": "", "error": ""}
    try:
        cur = url
        for _ in range(6):
            p = urlparse(cur)
            if p.scheme not in ("http", "https") or not p.hostname:
                raise ValueError("Only http and https URLs are supported.")
            if not ALLOW_PRIVATE and not is_public_host(p.hostname):
                raise ValueError("That address can't be fetched (it is private or does not resolve).")
            r = requests.get(cur, headers=HEADERS, timeout=(6, 14), allow_redirects=False, stream=True)
            if r.is_redirect and r.headers.get("Location"):
                cur = urljoin(cur, r.headers["Location"])
                r.close()
                continue
            break
        else:
            raise ValueError("The page redirected too many times.")
        out["final_url"], out["status"] = cur, r.status_code
        content = b""
        for chunk in r.iter_content(65536):
            content += chunk
            if len(content) > 2_000_000:
                break
        r.close()
        if r.status_code >= 400:
            raise ValueError(f"The page returned HTTP {r.status_code} (the site may block automated requests).")
        soup = BeautifulSoup(content, "html.parser")
        out["title"] = clean(soup.title.get_text()) if soup.title else ""
        h1 = soup.find("h1")
        out["h1"] = clean(h1.get_text(" ")) if h1 else ""
        out["h2s"] = [clean(h.get_text(" ")) for h in soup.find_all("h2")][:8]
        md = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
        out["meta_description"] = clean(md.get("content", "")) if md else ""
        og = soup.find("meta", attrs={"property": "og:site_name"})
        out["og_site"] = clean(og.get("content", "")) if og else ""
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        out["body_text"] = clean(soup.get_text(" "))[:60000]
        out["ok"] = True
    except requests.exceptions.RequestException as e:
        out["error"] = "Couldn't reach the page (" + type(e).__name__ + ")."
    except ValueError as e:
        out["error"] = str(e)
    except Exception as e:  # never let a parsing quirk break the tool
        out["error"] = "Couldn't read the page (" + type(e).__name__ + ")."
    return out


def detect_brand(url, page, user_brand):
    if user_brand:
        return clean(user_brand)
    host = urlparse(url).hostname if url else ""
    label = domain_label(host) if host else ""
    if re.match(r"^[\d.]+$", host or "") or (host and ":" in host):  # IP address, no brand to detect
        label = ""
    if label in SITE_BRANDS:
        return SITE_BRANDS[label]
    if page.get("og_site") and len(page["og_site"]) <= 30:
        return page["og_site"]
    m = re.split(r"\s[|\-–—]\s", page.get("title", ""))
    if len(m) > 1 and 2 <= len(m[-1]) <= 25:
        return m[-1].strip()
    return label.capitalize() if label else ""


# --------------------------------------------------------------------------- #
# AI mode (Google Gemini API, free tier) - optional. Needs the GEMINI_API_KEY env var.
# Every AI suggestion is re-checked here (length, pixel width, keyword, grammar, banned claims)
# because language models are bad at counting characters.
# --------------------------------------------------------------------------- #
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
GEMINI_BASE = os.environ.get("GEMINI_BASE", "https://generativelanguage.googleapis.com/v1beta")
GEMINI_FALLBACKS = [m.strip() for m in os.environ.get(
    "GEMINI_FALLBACK_MODELS", "gemini-3.5-flash,gemini-3.1-flash-lite").split(",") if m.strip()]
AI_LIMIT_PER_HOUR = int(os.environ.get("AI_LIMIT_PER_HOUR", "10"))
_AI_HITS = {}
BANNED = re.compile(r"\b(guarantee[ds]?|instant(ly)? approv\w*|lowest|cheapest|100%|no documents?|"
                    r"assured|zero documentation|risk[- ]free)\b", re.I)


class AIError(Exception):
    pass


def ai_configured():
    return bool(os.environ.get("GEMINI_API_KEY"))


def ai_rate_ok(ip):
    now = datetime.datetime.now().timestamp()
    hits = [t for t in _AI_HITS.get(ip, []) if now - t < 3600]
    if len(hits) >= AI_LIMIT_PER_HOUR:
        _AI_HITS[ip] = hits
        return False
    hits.append(now)
    _AI_HITS[ip] = hits
    return True


AI_SYSTEM = (
    "You are an expert SEO copywriter. You write meta titles, meta descriptions and meta keywords for web "
    "pages (often Indian personal-finance pages) in clear, natural, grammatically perfect English.\n"
    "Rules:\n"
    "1. Write 5 meta titles. Each title must be at most 55 characters and must contain the EXACT primary "
    "keyword phrase. Every title must use a DIFFERENT structure (for example: keyword first, a question, "
    "benefit-led, year or number, colon list). Do not just put the keyword first and add a different ending "
    "each time. The brand name may appear in at most two titles, at the end after ' | '.\n"
    "2. Write 4 meta descriptions. Each must be between 120 and 145 characters, one or two complete "
    "sentences, contain the EXACT primary keyword phrase once, use the secondary keyword naturally if one is "
    "given, and end with a soft call to action. No keyword stuffing.\n"
    "3. Match the search intent behind the keyword and the page (informational, comparison or "
    "transactional).\n"
    "4. Use only facts found in the page context. Never invent interest rates, numbers, approval times, "
    "awards or offers. Never use the words: guaranteed, instant approval, lowest, cheapest, 100%, no "
    "documents, assured, risk-free.\n"
    "5. Write 8 meta keywords: lower-case, relevant, no duplicates, the primary keyword first.\n"
    "6. Reply with JSON only, in the requested schema."
)
AI_SCHEMA = {"type": "object", "properties": {
    "titles": {"type": "array", "items": {"type": "string"}},
    "descriptions": {"type": "array", "items": {"type": "string"}},
    "keywords": {"type": "array", "items": {"type": "string"}}},
    "required": ["titles", "descriptions", "keywords"]}


def _api_message(r):
    try:
        return str(r.json().get("error", {}).get("message", ""))
    except ValueError:
        return ""


def _parse_gemini(r):
    try:
        parts = r.json()["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
        return json.loads(text)
    except (KeyError, IndexError, ValueError, TypeError):
        raise AIError("Gemini's answer could not be read. Please try again.")


def call_gemini(user_text, temperature=0.8):
    """Returns (parsed_json, model_used). Retries once on 5xx and falls back to other free models."""
    key = os.environ.get("GEMINI_API_KEY", "")
    body = {"systemInstruction": {"parts": [{"text": AI_SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": user_text}]}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": 4096,
                                 "responseMimeType": "application/json", "responseSchema": AI_SCHEMA}}
    deadline = time.monotonic() + 45
    last, last_code, tried = "no response", 0, []
    for model in [GEMINI_MODEL] + [m for m in GEMINI_FALLBACKS if m != GEMINI_MODEL]:
        url = f"{GEMINI_BASE}/models/{model}:generateContent"
        tried.append(model)
        for attempt in range(2):
            left = deadline - time.monotonic()
            if left < 5:
                break
            try:
                r = requests.post(url, headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                                  json=body, timeout=min(25, left))
            except requests.exceptions.Timeout:
                last, last_code = "timed out", 0
                break
            except requests.exceptions.RequestException:
                last, last_code = "network error", 0
                break
            code = r.status_code
            if code == 200:
                return _parse_gemini(r), model
            msg = _api_message(r)
            last, last_code = f"HTTP {code} {msg[:120]}".strip(), code
            if code in (500, 502, 503, 504):
                time.sleep(1.5)
                continue  # one more try on the same model
            if code in (429, 404):
                break  # try the next model
            raise AIError(f"Gemini rejected the request (HTTP {code}). {msg[:160]} "
                          "Check that GEMINI_API_KEY is valid.")
    if last_code == 429:
        raise AIError("Gemini's free usage limit was reached. Wait a minute and try again.")
    raise AIError(f"Gemini is not answering right now ({last}). Tried: {', '.join(tried)}. "
                  "Please try again in a minute.")


def _tidy(x):
    return re.sub(r"\s+", " ", str(x)).strip().strip("\"'`“”")


def validate_ai(items, kind, kw, kw2, seen):
    out, notes = [], []
    lo = TITLE_MIN - 5 if kind == "title" else 110
    for raw in items or []:
        t = _tidy(raw)
        if not t or t.lower() in seen:
            continue
        m = measure(kind, t)
        why = ""
        if not has_kw(t, kw):
            why = "does not contain the exact primary keyword"
        elif m["chars"] > (TITLE_MAX if kind == "title" else DESC_MAX) or \
                m["px"] > (TITLE_PX_LIMIT if kind == "title" else DESC_PX_LIMIT):
            why = f'too long ({m["chars"]} characters)'
        elif m["chars"] < lo:
            why = f'too short ({m["chars"]} characters)'
        elif BANNED.search(t):
            why = "uses a banned claim word"
        elif kind == "description" and t[-1] not in ".!?":
            why = "does not end as a complete sentence"
        else:
            gi = grammar_issues(t)
            if gi:
                why = "; ".join(gi)
        if why:
            notes.append((t, why))
            continue
        seen.add(t.lower())
        out.append({"text": t, **m, "src": "ai"})
    return out, notes


def ai_generate(kw, kw2, brand, cls, page, year):
    ctx = {"page_url": page.get("final_url", ""), "primary_keyword": kw, "secondary_keyword": kw2,
           "brand": brand, "page_type_guess": cls, "year": year,
           "current_title": page.get("title", ""), "current_h1": page.get("h1", ""),
           "current_meta_description": page.get("meta_description", ""),
           "subheadings": page.get("h2s", [])[:6], "page_excerpt": page.get("body_text", "")[:1500]}
    base = "Write the meta tags for this page.\n" + json.dumps(ctx, ensure_ascii=False)
    titles, descs, keywords, seen_t, seen_d, feedback = [], [], [], set(), set(), ""
    model_used = GEMINI_MODEL
    for attempt in range(2):
        res, model_used = call_gemini(base + feedback, temperature=0.8 if attempt == 0 else 0.6)
        t_ok, t_bad = validate_ai(res.get("titles"), "title", kw, kw2, seen_t)
        d_ok, d_bad = validate_ai(res.get("descriptions"), "description", kw, kw2, seen_d)
        titles += t_ok
        descs += d_ok
        if not keywords:
            keywords = res.get("keywords") or []
        if len(titles) >= 3 and len(descs) >= 2:
            break
        problems = [f"- \"{t}\": {w}" for t, w in (t_bad + d_bad)[:8]]
        feedback = ("\n\nYour previous answer had problems:\n" + "\n".join(problems) +
                    "\nReturn a complete new JSON answer. Count characters carefully: every title at most 55 "
                    "characters, every description 120 to 145 characters, each containing the exact primary "
                    "keyword phrase.")
    ks, seen_k = [], set()
    for k in [kw] + list(keywords):
        k = norm(k)
        if k and k not in seen_k and len(k) <= 60:
            seen_k.add(k)
            ks.append(k)
    return {"titles": titles[:5], "descriptions": descs[:4], "keywords": ks[:10], "model": model_used}


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/api/generate", methods=["POST"])
def api_generate():
    data = request.get_json(silent=True) or {}
    kw = _clean_kw(data.get("primary"))
    kw2 = _clean_kw(data.get("secondary"))
    if not kw:
        return jsonify({"error": "Please enter a primary keyword."}), 400
    if len(kw) > 90 or len(kw2) > 90:
        return jsonify({"error": "Keywords are limited to 90 characters."}), 400
    url = normalise_url(data.get("url"))
    page = {"ok": False, "error": "", "h1": "", "h2s": [], "title": "", "meta_description": "",
            "body_text": "", "final_url": url}
    warnings = []
    if url:
        page = fetch_page(url)
        if not page["ok"]:
            warnings.append(page["error"] + " Suggestions are based on your keywords and the URL only.")
    else:
        warnings.append("No URL given, so suggestions are based on your keywords only.")
    brand = detect_brand(url, page, data.get("brand"))
    hint = page_hint(url, page.get("title", ""), page.get("h1", ""))
    override = data.get("page_type") or ""
    cls = override if override in TITLE_DESC else classify(kw, hint)
    year = datetime.date.today().year

    kw2_use = kw2
    if kw2 and same_intent(kw, kw2):
        kw2_use = ""
        warnings.append("Your secondary keyword is just another form of the primary one, so it is only added "
                        "to the meta keywords and not repeated in the title or description.")
    titles = build_titles(kw, kw2_use, brand, cls, year)
    descs = build_descriptions(kw, kw2_use, brand, cls, year)
    keywords = build_keywords(kw, kw2, brand, cls, page, year)
    for row in titles + descs:
        row["src"] = "template"
    ai_info = {"requested": bool(data.get("ai")), "used": False, "model": GEMINI_MODEL}
    if data.get("ai"):
        ip = (request.headers.get("X-Forwarded-For", request.remote_addr or "") or "").split(",")[0].strip()
        if not ai_configured():
            warnings.append("AI mode isn't switched on for this server yet (the GEMINI_API_KEY setting is "
                            "missing). Showing template options.")
        elif not ai_rate_ok(ip):
            warnings.append(f"AI limit reached for now ({AI_LIMIT_PER_HOUR} requests per hour). "
                            "Showing template options.")
        else:
            try:
                ai = ai_generate(kw, kw2_use, brand, cls, page, year)
                if ai["titles"] or ai["descriptions"]:
                    ai_info["used"] = True
                    ai_info["model"] = ai.get("model", GEMINI_MODEL)
                    titles = ai["titles"] + titles
                    descs = ai["descriptions"] + descs
                    merged = list(ai["keywords"])
                    for k in keywords:
                        if k not in merged:
                            merged.append(k)
                    keywords = merged[:10]
                    if not (ai["titles"] and ai["descriptions"]):
                        warnings.append("Gemini's answer was only partly usable, so template options are "
                                        "mixed in.")
                else:
                    warnings.append("Gemini's suggestions did not pass the length and grammar checks, so "
                                    "template options are shown. Try again for a fresh set.")
            except AIError as e:
                warnings.append(str(e) + " Showing template options.")
            except Exception:
                warnings.append("The AI step failed unexpectedly. Showing template options.")
    if titles and titles[0]["status"] == "long":
        warnings.append("Your keyword is long, so the title may exceed the ideal length. Try a shorter keyword.")
    if descs and descs[0]["status"] == "long":
        warnings.append("Your keyword is long, so the description may exceed the ideal length.")

    signals = []
    if page.get("ok"):
        body = page.get("body_text", "")
        signals = [
            {"label": "Keyword in current title", "ok": has_kw(page["title"], kw)},
            {"label": "Keyword in H1", "ok": has_kw(page["h1"], kw)},
            {"label": "Keyword in page content", "ok": has_kw(body, kw)},
            {"label": "Current meta description present", "ok": bool(page["meta_description"])},
        ]
    page_out = {
        "ok": page.get("ok", False), "final_url": page.get("final_url", url), "title": page.get("title", ""),
        "title_m": measure("title", page["title"]) if page.get("title") else None,
        "h1": page.get("h1", ""), "meta_description": page.get("meta_description", ""),
        "desc_m": measure("description", page["meta_description"]) if page.get("meta_description") else None,
        "signals": signals}
    best_t, best_d = titles[0]["text"], descs[0]["text"]
    return jsonify({"brand": brand, "page_type": cls, "titles": titles, "descriptions": descs,
                    "keywords": keywords, "page": page_out, "warnings": warnings,
                    "analysis": run_checks(best_t, best_d, kw, kw2_use), "secondary_effective": kw2_use,
                    "ai": ai_info,
                    "year": year})


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    data = request.get_json(silent=True) or {}
    title = clean(str(data.get("title", "")))[:300]
    desc = clean(str(data.get("description", "")))[:600]
    return jsonify(run_checks(title, desc, _clean_kw(data.get("primary")), _clean_kw(data.get("secondary"))))


@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.route("/api/config")
def api_config():
    return jsonify({"ai": ai_configured(), "model": GEMINI_MODEL})


@app.route("/favicon.ico")
def favicon():
    return Response(status=204)


@app.route("/healthz")
def healthz():
    return "ok"


# --------------------------------------------------------------------------- #
# Front end
# --------------------------------------------------------------------------- #
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Meta Tag Generator | Mahalakshmi Marimuthu</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@500;600;700&family=Inter:wght@400;500;600&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0a0f;--surface:#12121a;--surface2:#14141c;--border:#23233a;--accent:#7c6af7;--accent-h:#6a58e8;
--accent2:#f7a26a;--accent3:#6af7c8;--danger:#f76a6a;--text:#e8e8f0;--muted:#8888a8;--good:#6af7c8;--warn:#f7a26a;--bad:#f76a6a}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:Inter,sans-serif;font-size:15px;line-height:1.55;display:flex;min-height:100vh}
a{color:inherit;text-decoration:none}
.sidebar{width:250px;flex-shrink:0;background:var(--surface);border-right:1px solid var(--border);padding:20px 16px;display:flex;flex-direction:column;position:sticky;top:0;height:100vh;overflow-y:auto}
.brand{display:flex;align-items:center;gap:10px;margin-bottom:26px}
.brand-mark{width:34px;height:34px;border-radius:9px;background:linear-gradient(135deg,var(--accent),var(--accent2));display:grid;place-items:center;font-family:Sora;font-weight:700}
.brand-text{font-family:Sora;font-weight:600;font-size:16px}
.sidebar-section{margin-bottom:20px}
.sidebar-label{font-family:'DM Mono',monospace;font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:8px;padding-left:10px}
.sidebar-link{display:block;padding:9px 12px;border-radius:9px;font-size:14px;color:var(--muted);border:1px solid transparent;margin-bottom:3px}
.sidebar-link:hover{color:var(--text);background:rgba(255,255,255,.04)}
.sidebar-link.active{background:rgba(124,106,247,.14);border:1px solid rgba(124,106,247,.4);color:var(--accent);font-weight:700}
.info-box{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:12px;font-size:12.5px;color:var(--muted)}
.info-box b{color:var(--text)}
.sidebar-footer{margin-top:auto;padding-top:18px;font-size:12px;color:var(--muted);border-top:1px solid var(--border)}
.credit-name{color:var(--accent3);font-weight:700}
.sidebar-social{display:flex;gap:8px;margin-top:10px}
.sidebar-social a{width:28px;height:28px;border-radius:7px;display:grid;place-items:center;font-family:'DM Mono',monospace;font-size:12px;background:rgba(106,247,200,.08);border:1px solid rgba(106,247,200,.35);color:var(--accent3)}
.sidebar-social a:hover{background:rgba(106,247,200,.2)}
main{flex:1;min-width:0;padding:34px 40px 60px;max-width:1100px}
h1{font-family:Sora;font-size:28px;font-weight:700;margin-bottom:6px}
.sub{color:var(--muted);max-width:720px;margin-bottom:24px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:20px;margin-bottom:18px}
.card h2{font-family:Sora;font-size:16px;font-weight:600;margin-bottom:12px;display:flex;align-items:center;justify-content:space-between;gap:10px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.grid .full{grid-column:1/-1}
label{display:block;font-size:12.5px;color:var(--muted);margin-bottom:6px;font-family:'DM Mono',monospace;text-transform:uppercase;letter-spacing:.05em}
label .optional{text-transform:none;letter-spacing:0;opacity:.7}
input[type=text],select,textarea{width:100%;background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:11px 13px;color:var(--text);font:inherit;outline:none}
input:focus,select:focus,textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(124,106,247,.18)}
textarea{resize:vertical;min-height:84px}
.btn{background:var(--accent);color:#fff;border:0;border-radius:10px;padding:12px 22px;font:600 14.5px Inter;cursor:pointer}
.btn:hover{background:var(--accent-h)}
.btn:disabled{opacity:.6;cursor:wait}
.btn-sm{background:transparent;border:1px solid var(--border);color:var(--muted);border-radius:8px;padding:5px 11px;font:500 12px 'DM Mono',monospace;cursor:pointer}
.btn-sm:hover{color:var(--text);border-color:var(--accent)}
.btn-row{display:flex;gap:12px;flex-wrap:wrap;margin-top:18px}
.btn-ai{background:rgba(247,162,106,.12);border:1px solid rgba(247,162,106,.5);color:var(--accent2)}
.btn-ai:hover{background:rgba(247,162,106,.22)}
.tag{font:600 10.5px 'DM Mono',monospace;font-style:normal;background:rgba(247,162,106,.15);color:var(--accent2);border:1px solid rgba(247,162,106,.4);border-radius:5px;padding:1px 6px;margin-left:6px;vertical-align:1px}
.info{background:rgba(106,247,200,.07);border:1px solid rgba(106,247,200,.3);color:#a8f0d6;border-radius:10px;padding:10px 14px;margin-bottom:12px;font-size:13.5px}
.adv-toggle{background:none;border:0;color:var(--accent);font:500 13px Inter;cursor:pointer;padding:0;margin-top:2px}
.adv{display:none;margin-top:14px}
.adv.open{display:grid}
.err{background:rgba(247,106,106,.1);border:1px solid rgba(247,106,106,.4);color:#ffb0b0;border-radius:10px;padding:11px 14px;margin-bottom:16px;display:none}
.warn{background:rgba(247,162,106,.09);border:1px solid rgba(247,162,106,.35);color:#f7c9a3;border-radius:10px;padding:10px 14px;margin-bottom:12px;font-size:13.5px}
#results{display:none}
.top-row{display:grid;grid-template-columns:200px 1fr;gap:18px;margin-bottom:18px}
.score-card{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center}
.ring{position:relative;width:120px;height:120px}
.ring svg{transform:rotate(-90deg)}
.ring .num{position:absolute;inset:0;display:grid;place-items:center;font:700 30px Sora}
.score-label{font-size:12.5px;color:var(--muted);margin-top:8px}
.serp-wrap{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:18px 20px}
.serp-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.serp-head span{font:500 12px 'DM Mono',monospace;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.seg button{background:transparent;border:1px solid var(--border);color:var(--muted);padding:4px 11px;font:500 12px Inter;cursor:pointer}
.seg button:first-child{border-radius:7px 0 0 7px}.seg button:last-child{border-radius:0 7px 7px 0;border-left:0}
.seg button.on{background:rgba(124,106,247,.16);color:var(--accent);border-color:rgba(124,106,247,.5)}
.serp{background:#fff;border-radius:10px;padding:16px 18px;font-family:Arial,Helvetica,sans-serif;max-width:660px}
.serp.mobile{max-width:380px}
.serp .site{display:flex;align-items:center;gap:10px;margin-bottom:6px}
.serp .fav{width:26px;height:26px;border-radius:50%;background:#f1f3f4;display:grid;place-items:center;font-size:13px;color:#5f6368;font-weight:700}
.serp .sn{font-size:14px;color:#202124;line-height:1.2}
.serp .su{font-size:12px;color:#4d5156;line-height:1.2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:520px}
.serp .st{font-size:20px;line-height:1.3;color:#1a0dab;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:600px;margin-bottom:3px}
.serp.mobile .st{white-space:normal;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.serp .sd{font-size:14px;line-height:1.58;color:#4d5156;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.serp.mobile .sd{-webkit-line-clamp:3}
.meter{margin-top:9px}
.bar{position:relative;height:8px;border-radius:5px;background:#1d1d2b;overflow:hidden}
.bar .fill{position:absolute;left:0;top:0;bottom:0;border-radius:5px;transition:width .25s}
.fill.good{background:var(--good)}.fill.short{background:var(--warn)}.fill.long{background:var(--bad)}
.meter-meta{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:7px;font:500 12px 'DM Mono',monospace;color:var(--muted)}
.st-good{color:var(--good)}.st-short{color:var(--warn)}.st-long{color:var(--bad)}
.opts{margin-top:14px;display:grid;gap:7px}
.opts-label{font:500 11px 'DM Mono',monospace;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-top:16px}
.opt{display:flex;justify-content:space-between;gap:12px;align-items:center;text-align:left;background:var(--bg);border:1px solid var(--border);border-radius:9px;padding:9px 12px;color:var(--text);font:14px Inter;cursor:pointer;width:100%}
.opt:hover{border-color:var(--accent)}
.opt.on{border-color:rgba(124,106,247,.7);background:rgba(124,106,247,.09)}
.opt small{font:500 11.5px 'DM Mono',monospace;color:var(--muted);white-space:nowrap}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{background:rgba(106,247,200,.08);border:1px solid rgba(106,247,200,.3);color:var(--accent3);border-radius:20px;padding:4px 6px 4px 12px;font-size:13px;display:inline-flex;align-items:center;gap:6px}
.chip button{background:none;border:0;color:inherit;cursor:pointer;font-size:15px;line-height:1;opacity:.7}
.note{font-size:12.5px;color:var(--muted);margin-top:12px}
.checks{display:grid;gap:8px}
.check{display:flex;gap:10px;align-items:flex-start;font-size:14px}
.dot{flex-shrink:0;width:20px;height:20px;border-radius:50%;display:grid;place-items:center;font-size:12px;font-weight:700;margin-top:1px}
.dot.ok{background:rgba(106,247,200,.15);color:var(--good)}
.dot.no{background:rgba(247,106,106,.15);color:var(--bad)}
.check small{display:block;color:var(--muted);font-size:12.5px}
.snap{display:grid;gap:10px;font-size:14px}
.snap .row{display:grid;grid-template-columns:150px 1fr;gap:12px}
.snap .k{font:500 12px 'DM Mono',monospace;color:var(--muted);text-transform:uppercase}
.badge{display:inline-block;font:500 12px Inter;padding:3px 10px;border-radius:14px;margin:0 6px 6px 0}
.badge.ok{background:rgba(106,247,200,.1);color:var(--good);border:1px solid rgba(106,247,200,.3)}
.badge.no{background:rgba(247,106,106,.1);color:var(--bad);border:1px solid rgba(247,106,106,.3)}
.about h3{font-family:Sora;font-size:15px;margin:16px 0 8px}
.about ol{padding-left:20px;color:var(--muted)}
.about p{color:var(--muted)}
.feedback{margin-top:18px;padding:14px 16px;border:1px dashed var(--border);border-radius:12px;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;color:var(--muted);font-size:14px}
.feedback a{background:var(--accent);color:#fff;padding:8px 16px;border-radius:9px;font-weight:600;font-size:13px}
@media(max-width:900px){body{flex-direction:column}.sidebar{width:100%;height:auto;position:static;flex-direction:column}main{padding:22px 16px 50px}.grid,.top-row{grid-template-columns:1fr}.snap .row{grid-template-columns:1fr;gap:2px}}
</style>
</head>
<body>
<aside class="sidebar">
  <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="brand">
    <span class="brand-mark">M</span><span class="brand-text">SEO Tools</span>
  </a>
  <div class="sidebar-section">
    <div class="sidebar-label">Tool</div>
    <a href="#" class="sidebar-link active">✍️ Meta Tag Generator</a>
  </div>
  <div class="sidebar-section">
    <div class="sidebar-label">Navigate</div>
    <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="sidebar-link">🧰 All My Tools</a>
    <a href="https://mahalakshmi26-hub.github.io/my-portfolio" class="sidebar-link">👩‍💻 Portfolio</a>
    <a href="https://mahalakshmi26-hub.github.io/my-portfolio/blog/index.html" class="sidebar-link">📝 Blog Posts</a>
  </div>
  <div class="sidebar-section">
    <div class="info-box"><b>Length targets</b><br>Title: 30–60 chars, under ~580 px<br>Description: 120–155 chars, under ~920 px<br>Widths are estimated using Arial, the font Google uses in results.</div>
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

<main>
  <h1>Meta Tag Generator</h1>
  <p class="sub">Enter a page URL and a keyword. Get a meta title, meta description and meta keywords that fit Google's character and pixel limits and read cleanly, with no grammar slips.</p>

  <form id="gen-form" class="card" autocomplete="off">
    <div class="grid">
      <div class="full"><label for="url">Page URL</label><input type="text" id="url" placeholder="https://www.example.com/personal-loan.html"></div>
      <div><label for="primary">Primary keyword</label><input type="text" id="primary" placeholder="e.g. personal loan interest rates" maxlength="90"></div>
      <div><label for="secondary">Secondary keyword <span class="optional">(optional)</span></label><input type="text" id="secondary" placeholder="e.g. personal loan eligibility" maxlength="90"></div>
    </div>
    <button type="button" class="adv-toggle" id="adv-toggle">+ Advanced options</button>
    <div class="grid adv" id="adv">
      <div><label for="brand">Brand name <span class="optional">(auto-detected if blank)</span></label><input type="text" id="brand" placeholder="e.g. BankBazaar"></div>
      <div><label for="ptype">Page type</label>
        <select id="ptype"><option value="">Auto-detect</option><option value="product">Product / service page</option><option value="rates">Rates &amp; fees</option><option value="calculator">Calculator / tool</option><option value="eligibility">Eligibility</option><option value="documents">Documents</option><option value="guide">Guide / blog</option><option value="question">Question / how-to</option><option value="issue">Reasons / problems</option><option value="compare">Comparison / best-of</option><option value="generic">Other</option></select></div>
    </div>
    <div class="btn-row"><button class="btn" id="go" type="submit">Generate Meta Tags</button><button class="btn btn-ai" id="go-ai" type="button" disabled>✨ Improve with AI</button></div>
    <p class="note" id="ai-note">AI mode sends the page title, headings, a short text excerpt and your keywords to Google's Gemini API. On Google's free tier this content may be used to improve its products, so avoid confidential pages.</p>
  </form>

  <div class="err" id="err"></div>

  <div id="results">
    <div id="warns"></div>
    <div class="top-row">
      <div class="card score-card">
        <div class="ring"><svg width="120" height="120" viewBox="0 0 100 100"><circle cx="50" cy="50" r="42" fill="none" stroke="#1d1d2b" stroke-width="9"/><circle id="ring-c" cx="50" cy="50" r="42" fill="none" stroke="#6af7c8" stroke-width="9" stroke-linecap="round" stroke-dasharray="263.9" stroke-dashoffset="263.9" style="transition:stroke-dashoffset .5s"/></svg><div class="num" id="ring-n">0</div></div>
        <div class="score-label">Meta score</div>
      </div>
      <div class="serp-wrap">
        <div class="serp-head"><span>Google preview</span><div class="seg"><button class="on" data-v="d">Desktop</button><button data-v="m">Mobile</button></div></div>
        <div class="serp" id="serp"><div class="site"><div class="fav" id="s-fav">G</div><div><div class="sn" id="s-name"></div><div class="su" id="s-url"></div></div></div><div class="st" id="s-title"></div><div class="sd" id="s-desc"></div></div>
      </div>
    </div>

    <div class="card">
      <h2>Meta title <button class="btn-sm" data-copy="title">Copy</button></h2>
      <input type="text" id="title-in" maxlength="120">
      <div class="meter" id="title-meter"></div>
      <div class="opts-label">Title options</div>
      <div class="opts" id="title-opts"></div>
    </div>

    <div class="card">
      <h2>Meta description <button class="btn-sm" data-copy="desc">Copy</button></h2>
      <textarea id="desc-in" maxlength="300"></textarea>
      <div class="meter" id="desc-meter"></div>
      <div class="opts-label">Description options</div>
      <div class="opts" id="desc-opts"></div>
    </div>

    <div class="card">
      <h2>Meta keywords <button class="btn-sm" data-copy="kw">Copy</button></h2>
      <div class="chips" id="chips"></div>
      <p class="note">Google ignores the meta keywords tag for ranking. It is included here for other search engines and internal use. Click × to remove any keyword.</p>
    </div>

    <div class="card"><h2>Quality checks <button class="btn-sm" id="copy-all">Copy all</button></h2><div class="checks" id="checks"></div></div>
    <div class="card" id="snap-card"><h2>What was read from the page</h2><div class="snap" id="snap"></div></div>
  </div>

  <div class="card about" style="margin-top:26px">
    <h2>About Meta Tag Generator</h2>
    <p>This tool writes a meta title, meta description and meta keywords for a page from its URL and your target keywords. It reads the page to detect the brand and page type, then builds wording from grammar-checked patterns so the copy always reads naturally. The optional Improve with AI button asks Google's Gemini to write fresh options, which are then re-checked for length, keyword use and grammar. Every option is measured in characters and in pixels, because Google cuts titles and descriptions by width, not by character count.</p>
    <h3>How to use it</h3>
    <ol>
      <li>Paste the URL of the page you want to write meta tags for.</li>
      <li>Type your primary keyword, and a secondary keyword if you have one.</li>
      <li>Click Generate Meta Tags and review the Google preview.</li>
      <li>Pick another option or edit the text. The meters and checks update as you type.</li>
      <li>Copy the title, description and keywords into your CMS.</li>
    </ol>
    <div class="feedback"><span>Have a suggestion or found a bug?</span><a href="mailto:mahalakshmi.digitalpro@gmail.com?subject=Feedback%3A%20Meta%20Tag%20Generator">Send feedback</a></div>
  </div>
</main>

<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let S={data:null,payload:null,title:'',desc:'',keywords:[],timer:null,view:'d'};

$('#adv-toggle').onclick=()=>{const a=$('#adv');a.classList.toggle('open');$('#adv-toggle').textContent=(a.classList.contains('open')?'– Hide':'+')+' Advanced options'};
function showErr(m){const e=$('#err');e.textContent=m;e.style.display=m?'block':'none'}

let AI_OK=false;
fetch('/api/config').then(r=>r.json()).then(c=>{AI_OK=!!c.ai;$('#go-ai').disabled=!AI_OK;
  if(!AI_OK)$('#ai-note').textContent='AI mode is off on this server. Set the GEMINI_API_KEY setting to switch it on. The template generator above still works.'}).catch(()=>{});
async function run(ai){
  showErr('');
  const payload={url:$('#url').value.trim(),primary:$('#primary').value.trim(),secondary:$('#secondary').value.trim(),brand:$('#brand').value.trim(),page_type:$('#ptype').value,ai:!!ai};
  if(!payload.primary){showErr('Please enter a primary keyword.');return}
  const btn=ai?$('#go-ai'):$('#go');
  $('#go').disabled=true;$('#go-ai').disabled=true;
  btn.textContent=ai?'Asking Gemini… (5–20 sec)':(payload.url?'Reading the page…':'Generating…');
  try{
    const r=await fetch('/api/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    const d=await r.json();
    if(!r.ok||d.error)throw new Error(d.error||'Something went wrong. Please try again.');
    S.data=d;S.payload=payload;S.title=d.titles[0].text;S.desc=d.descriptions[0].text;S.keywords=d.keywords.slice();
    render();
  }catch(e){showErr(e.message)}
  finally{$('#go').disabled=false;$('#go').textContent='Generate Meta Tags';$('#go-ai').disabled=!AI_OK;$('#go-ai').textContent='✨ Improve with AI'}
}
$('#gen-form').addEventListener('submit',ev=>{ev.preventDefault();run(false)});
$('#go-ai').addEventListener('click',()=>run(true));

function meterHTML(m){
  return '<div class="bar"><div class="fill '+m.status+'" style="width:'+m.pct+'%"></div></div>'+
  '<div class="meta-line meter-meta"><span>'+m.chars+' characters</span><span>'+m.px+' / '+m.limit_px+' px</span><span class="st-'+m.status+'">'+esc(m.label)+'</span></div>';
}
function render(){
  const d=S.data;$('#results').style.display='block';
  $('#warns').innerHTML=(d.ai&&d.ai.used?'<div class="info">The options tagged AI were written by Gemini, then re-checked here for length, keyword use and grammar.</div>':'')+(d.warnings||[]).map(w=>'<div class="warn">'+esc(w)+'</div>').join('');
  $('#title-in').value=S.title;$('#desc-in').value=S.desc;
  const mk=(list,cur,kind)=>list.map((o,i)=>'<button type="button" class="opt'+(o.text===cur?' on':'')+'" data-k="'+kind+'" data-i="'+i+'"><span>'+esc(o.text)+(o.src==='ai'?' <em class="tag">AI</em>':'')+'</span><small>'+o.chars+' ch · '+o.px+' px</small></button>').join('');
  $('#title-opts').innerHTML=mk(d.titles,S.title,'t');
  $('#desc-opts').innerHTML=mk(d.descriptions,S.desc,'d');
  paintChips();paintSnap();paintAnalysis(d.analysis);
  $('#results').scrollIntoView({behavior:'smooth',block:'start'});
}
function paintChips(){
  $('#chips').innerHTML=S.keywords.map((k,i)=>'<span class="chip">'+esc(k)+'<button type="button" data-rm="'+i+'" title="Remove">×</button></span>').join('')||'<span class="note">No keywords</span>';
}
function paintSnap(){
  const p=S.data.page,rows=[];
  rows.push(['Brand detected',esc(S.data.brand||'Not detected')]);
  rows.push(['Content type',esc(S.data.page_type)]);
  if(p.ok){
    rows.push(['Current title',p.title?esc(p.title)+' <span class="st-'+p.title_m.status+'">('+p.title_m.chars+' chars, '+p.title_m.px+' px)</span>':'<span class="st-long">Missing</span>']);
    rows.push(['H1',p.h1?esc(p.h1):'<span class="st-long">Missing</span>']);
    rows.push(['Current description',p.meta_description?esc(p.meta_description)+' <span class="st-'+p.desc_m.status+'">('+p.desc_m.chars+' chars, '+p.desc_m.px+' px)</span>':'<span class="st-long">Missing</span>']);
    rows.push(['Keyword signals',p.signals.map(s=>'<span class="badge '+(s.ok?'ok':'no')+'">'+(s.ok?'✓ ':'✗ ')+esc(s.label)+'</span>').join('')]);
  }else{rows.push(['Page read','<span class="st-short">Not available. Suggestions are based on your keywords and URL.</span>'])}
  $('#snap').innerHTML=rows.map(r=>'<div class="row"><div class="k">'+r[0]+'</div><div>'+r[1]+'</div></div>').join('');
}
function paintAnalysis(a){
  $('#title-meter').innerHTML=meterHTML(a.title);$('#desc-meter').innerHTML=meterHTML(a.description);
  $('#checks').innerHTML=a.checks.map(c=>'<div class="check"><span class="dot '+(c.ok?'ok':'no')+'">'+(c.ok?'✓':'!')+'</span><div>'+esc(c.label)+(c.detail?'<small>'+esc(c.detail)+'</small>':'')+'</div></div>').join('');
  const s=a.score,col=s>=85?'#6af7c8':(s>=60?'#f7a26a':'#f76a6a');
  const c=$('#ring-c');c.style.stroke=col;c.style.strokeDashoffset=263.9*(1-s/100);$('#ring-n').textContent=s;$('#ring-n').style.color=col;
  paintSerp();
}
function paintSerp(){
  const raw=(S.data.page&&S.data.page.final_url)||S.payload.url||'';
  let host='',path='';
  try{const u=new URL(/^https?:/i.test(raw)?raw:'https://'+raw);host=u.hostname.replace(/^www\./,'');path=u.pathname.split('/').filter(Boolean).join(' › ')}catch(e){host=raw}
  $('#s-name').textContent=S.data.brand||host||'Your site';$('#s-fav').textContent=(S.data.brand||host||'G').charAt(0).toUpperCase();
  $('#s-url').textContent=host?('https://'+host+(path?' › '+path:'')):'https://www.example.com';
  $('#s-title').textContent=S.title;$('#s-desc').textContent=S.desc;
}
async function analyse(){
  try{
    const r=await fetch('/api/analyze',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:S.title,description:S.desc,primary:S.payload.primary,secondary:S.data.secondary_effective||''})});
    paintAnalysis(await r.json());
  }catch(e){}
}
function queue(){paintSerp();clearTimeout(S.timer);S.timer=setTimeout(analyse,220)}

$('#title-in').addEventListener('input',e=>{S.title=e.target.value;queue()});
$('#desc-in').addEventListener('input',e=>{S.desc=e.target.value;queue()});
document.addEventListener('click',e=>{
  const o=e.target.closest('.opt');
  if(o){const list=o.dataset.k==='t'?S.data.titles:S.data.descriptions,t=list[+o.dataset.i].text;
    if(o.dataset.k==='t'){S.title=t;$('#title-in').value=t}else{S.desc=t;$('#desc-in').value=t}
    o.parentElement.querySelectorAll('.opt').forEach(x=>x.classList.toggle('on',x===o));queue();return}
  const rm=e.target.closest('[data-rm]');
  if(rm){S.keywords.splice(+rm.dataset.rm,1);paintChips();return}
  const cp=e.target.closest('[data-copy]');
  if(cp){const v={title:S.title,desc:S.desc,kw:S.keywords.join(', ')}[cp.dataset.copy];copy(v,cp);return}
  if(e.target.id==='copy-all'){copy('Meta title: '+S.title+'\nMeta description: '+S.desc+'\nMeta keywords: '+S.keywords.join(', '),e.target)}
  const sg=e.target.closest('.seg button');
  if(sg){document.querySelectorAll('.seg button').forEach(b=>b.classList.toggle('on',b===sg));$('#serp').classList.toggle('mobile',sg.dataset.v==='m')}
});
function copy(text,btn){
  const done=()=>{const t=btn.textContent;btn.textContent='Copied ✓';setTimeout(()=>btn.textContent=t,1200)};
  if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(text).then(done,()=>fb(text,done))}else fb(text,done);
}
function fb(text,done){const t=document.createElement('textarea');t.value=text;document.body.appendChild(t);t.select();try{document.execCommand('copy')}catch(e){}t.remove();done()}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False)
