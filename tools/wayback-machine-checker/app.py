"""
Wayback Machine Checker  -  Flask app
See the full archive history of any URL or domain from the Internet Archive:

  * First seen / last seen / archived days / years active / content versions
  * Snapshots-per-year chart, split by HTTP status (2xx / 3xx / 4xx / 5xx)
  * Status history: when the page was live, redirected or broken - and where
    archived redirects pointed
  * Red flags for due diligence: long archive gaps (possible expiry / drop),
    redirects to another domain, error periods, thin or stale history
  * Before / after picker with Wayback's "Changes" compare link
  * "Archive this page now" (Save Page Now) shortcut
  * Optional: list old pages the archive has seen on the domain
  * CSV export of snapshots and pages

Uses the free, key-less Wayback CDX API: https://archive.org/help/wayback_api.php

Built by Mahalakshmi Marimuthu - Digital Marketing Strategist & AI-Powered SEO Expert
"""
import os
import re
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from urllib.parse import urljoin, urlparse

import requests
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

WAYBACK = os.environ.get("WAYBACK_BASE", "https://web.archive.org").rstrip("/")
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": BROWSER_UA, "Accept": "application/json,text/plain,*/*"}
TOTAL_BUDGET = 100          # seconds; gunicorn runs with --timeout 120
MAX_SNAPSHOTS = 20000       # one row per archived day -> ~55 years, plenty
MAX_PAGES = 1000
GAP_DAYS = 365              # a gap this long is worth a flag
MIN_PHASE = 3               # shorter status blips are folded into the phase around them
MAX_REDIRECT_LOOKUPS = 4

SESSION = requests.Session()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def clean_target(raw):
    """'https://www.Site.com/page?x=1#top' -> ('www.site.com/page?x=1', 'www.site.com')."""
    s = (raw or "").strip()
    if not s:
        return None, None
    s = s.split("#", 1)[0]
    s = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", s, flags=re.I)
    s = s.lstrip("/")
    host = re.split(r"[/?]", s, 1)[0].lower()
    host = host.split("@")[-1]
    if not host or "." not in host or " " in host or len(host) > 253:
        return None, None
    rest = s[len(re.split(r"[/?]", s, 1)[0]):]
    return host + rest, host


def bare(host):
    host = (host or "").lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def to_date(ts):
    ts = (ts or "").ljust(8, "0")[:8]
    try:
        return datetime.strptime(ts, "%Y%m%d").date()
    except ValueError:
        try:
            return date(int(ts[:4]), 1, 1)
        except ValueError:
            return None


def status_class(code):
    if code and len(code) == 3 and code.isdigit():
        return {"2": "2xx", "3": "3xx", "4": "4xx", "5": "5xx"}.get(code[0], "other")
    return "other"


def cdx(params, deadline):
    """Call the CDX API. Returns (rows as list of dicts, error message)."""
    url = WAYBACK + "/cdx/search/cdx"
    last = "The Wayback Machine didn't respond."
    for attempt in range(2):
        remaining = deadline - time.time()
        if remaining < 5:
            break
        try:
            r = SESSION.get(url, params=params, headers=HEADERS, timeout=(10, max(5, min(60, remaining))))
        except requests.exceptions.Timeout:
            last = "The Wayback Machine is taking too long to answer (very large sites can do this)."
            continue
        except requests.exceptions.RequestException:
            last = "Could not connect to the Wayback Machine."
            time.sleep(1.5)
            continue
        if r.status_code in (429, 502, 503, 504):
            last = f"The Wayback Machine is busy right now (HTTP {r.status_code})."
            time.sleep(2)
            continue
        if r.status_code != 200:
            return None, f"The Wayback Machine returned HTTP {r.status_code}."
        body = r.text.strip()
        if not body:
            return [], None
        try:
            data = r.json()
        except ValueError:
            return None, "The Wayback Machine sent a response this tool couldn't read."
        if not data:
            return [], None
        head = data[0]
        return [dict(zip(head, row)) for row in data[1:]], None
    return None, last


def wayback_target(location):
    """'https://web.archive.org/web/2015id_/http://x.com/' -> ('2015', 'http://x.com/')."""
    p = urlparse(location)
    path = p.path + (("?" + p.query) if p.query else "")
    m = re.match(r"^/web/(\d{1,14})(?:[a-z]{2}_)?/(.+)$", path)
    if m:
        return m.group(1), m.group(2)
    return None, location


def redirect_target(ts, original, deadline):
    """Read where an archived 3xx capture pointed. Best effort - returns URL or None."""
    current = original
    url = f"{WAYBACK}/web/{ts}id_/{original}"
    for _ in range(4):
        if deadline - time.time() < 6:
            return None
        try:
            r = SESSION.get(url, headers=HEADERS, allow_redirects=False, timeout=(6, 10), stream=True)
            r.close()
        except requests.exceptions.RequestException:
            return None
        loc = r.headers.get("Location")
        if r.status_code not in (301, 302, 303, 307, 308) or not loc:
            return None
        loc = urljoin(url, loc)
        new_ts, target = wayback_target(loc)
        if new_ts is None:
            return target            # points outside the archive
        if target.rstrip("/") == current.rstrip("/"):
            url = loc                # Wayback moving us to the nearest exact timestamp
            continue
        if not re.match(r"^https?://", target, re.I):
            target = urljoin(current, target)
        return target
    return None


# --------------------------------------------------------------------------- #
# Analysis (pure - no network)
# --------------------------------------------------------------------------- #
def build_phases(snaps):
    """Group snapshots into periods with the same status class, smoothing tiny blips."""
    runs = []
    prev_cls = None
    for i, s in enumerate(snaps):
        cls = s["cls"]
        if cls == "other":           # revisit / unknown -> same as before
            cls = prev_cls or "other"
        prev_cls = cls
        if runs and runs[-1]["cls"] == cls:
            runs[-1]["end"] = i
        else:
            runs.append({"cls": cls, "start": i, "end": i})

    phases = []
    for idx, r in enumerate(runs):
        length = r["end"] - r["start"] + 1
        is_last = idx == len(runs) - 1
        short = length < MIN_PHASE and not (is_last and length >= 2)
        if phases and short:
            phases[-1]["end"] = r["end"]
            phases[-1]["blips"] += length
            continue
        if phases and phases[-1]["cls"] == r["cls"]:
            phases[-1]["end"] = r["end"]
            continue
        phases.append({"cls": r["cls"], "start": r["start"], "end": r["end"], "blips": 0})

    out = []
    for p in phases:
        chunk = snaps[p["start"]:p["end"] + 1]
        codes = Counter(s["status"] for s in chunk if s["cls"] == p["cls"])
        first = next((s for s in chunk if s["cls"] == p["cls"]), chunk[0])
        out.append({
            "cls": p["cls"], "from": chunk[0]["ts"], "to": chunk[-1]["ts"], "count": len(chunk),
            "code": codes.most_common(1)[0][0] if codes else "",
            "sample_ts": first["ts"], "sample_url": first["url"], "blips": p["blips"],
        })
    return out


def analyze(snaps, host, today=None):
    today = today or date.today()
    dates = [s["d"] for s in snaps]
    years = defaultdict(lambda: Counter())
    for s in snaps:
        years[s["d"].year][s["cls"]] += 1
    y_first, y_last = dates[0].year, dates[-1].year
    by_year = [{"year": y, **{k: years[y].get(k, 0) for k in ("2xx", "3xx", "4xx", "5xx", "other")}}
               for y in range(y_first, y_last + 1)]

    gaps = []
    for a, b in zip(snaps, snaps[1:]):
        days = (b["d"] - a["d"]).days
        if days >= GAP_DAYS:
            gaps.append({"from": a["ts"], "to": b["ts"], "days": days})
    longest = max(((b["d"] - a["d"]).days for a, b in zip(snaps, snaps[1:])), default=0)

    digests = {s["digest"] for s in snaps if s["cls"] == "2xx" and s["digest"]}
    cls_counts = Counter(s["cls"] for s in snaps)
    summary = {
        "first": snaps[0]["ts"], "last": snaps[-1]["ts"], "days": len(snaps),
        "years_active": len({d.year for d in dates}), "span_years": round((dates[-1] - dates[0]).days / 365.25, 1),
        "versions": len(digests), "longest_gap": longest, "status": dict(cls_counts),
        "since_last": (today - dates[-1]).days,
    }
    return by_year, gaps, summary


def build_checks(summary, gaps, phases, host):
    checks = []
    add = lambda sev, title, detail: checks.append({"sev": sev, "title": title, "detail": detail})
    fmt = lambda ts: to_date(ts).strftime("%d %b %Y")

    add("pass", "Archive history found",
        f"First archived on {fmt(summary['first'])} — about {summary['span_years']:g} years of history "
        f"across {summary['days']:,} archived days.")

    # gaps
    if gaps:
        for g in sorted(gaps, key=lambda g: -g["days"])[:3]:
            yrs = g["days"] / 365.25
            add("warn", f"Archive gap of {yrs:.1f} years",
                f"No snapshots between {fmt(g['from'])} and {fmt(g['to'])}. The site may have been offline, "
                "expired and re-registered, or blocking the archive. Check the snapshots on either side.")
        if len(gaps) > 3:
            add("info", f"{len(gaps) - 3} more long gaps", "See the snapshot timeline chart for every quiet year.")
    else:
        add("pass", "Continuous history", "No gap of a year or more between snapshots.")

    # status phases
    last = phases[-1] if phases else None
    for i, p in enumerate(phases):
        is_last = i == len(phases) - 1
        span = f"{fmt(p['from'])} → {fmt(p['to'])}" if p["from"] != p["to"] else fmt(p["from"])
        if p["cls"] == "3xx":
            tgt = p.get("target")
            th = bare(urlparse(tgt).netloc) if tgt else ""
            if tgt and th and th != bare(host):
                add("fail" if is_last else "warn", f"Redirected to another domain: {th}",
                    f"Archived snapshots from {span} redirect to {tgt}. For an expired-domain purchase, "
                    "this often means the domain was sold, merged or used to pass link equity.")
            elif is_last:
                add("info", "Latest snapshots are a redirect",
                    f"Since {fmt(p['from'])} the archive sees a {p['code']} redirect"
                    + (f" to {tgt}." if tgt else ".") + " Check the target page for this content.")
        elif p["cls"] in ("4xx", "5xx"):
            kind = "not found / blocked" if p["cls"] == "4xx" else "server error"
            if is_last:
                add("fail", f"Latest snapshots return {p['code'] or p['cls']}",
                    f"From {fmt(p['from'])} the archive sees a {kind} response. The page may be gone — "
                    "if it still gets links, 301-redirect it to the closest live page.")
            else:
                add("warn", f"{p['code'] or p['cls']} period ({p['count']} snapshots)",
                    f"The page returned a {kind} response from {span} before recovering.")

    if last and last["cls"] == "2xx" and len(phases) > 1:
        add("pass", "Page is live in the latest snapshots", f"Returning 200 OK since {fmt(last['from'])}.")

    # thin / stale
    if summary["days"] < 10:
        add("warn", "Thin archive history",
            f"Only {summary['days']} archived day{'s' if summary['days'] != 1 else ''}. "
            "Few other sites linked here, or it's new — there's little history to judge.")
    if summary["since_last"] > 365:
        add("info", "Not archived recently",
            f"The last snapshot is {summary['since_last'] // 365} year(s) old. Use “Archive this page now” "
            "to save the current version.")
    if summary["versions"] == 1 and summary["days"] >= 10:
        add("info", "Content never changed",
            "Every live snapshot has identical content. That's normal for a static page, "
            "but on a whole website it can mean a parked or placeholder domain.")

    score = 100
    for c in checks:
        score -= {"fail": 25, "warn": 10}.get(c["sev"], 0)
    return checks, max(0, min(100, score))


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/api/check", methods=["POST"])
def api_check():
    data = request.get_json(silent=True) or {}
    target, host = clean_target(data.get("url", ""))
    if not target:
        return jsonify({"ok": False, "error": "Please enter a valid website or page URL, e.g. example.com"}), 400
    want_pages = bool(data.get("pages"))
    yf = str(data.get("from") or "").strip()
    yt = str(data.get("to") or "").strip()
    deadline = time.time() + TOTAL_BUDGET

    params = [("url", target), ("output", "json"),
              ("fl", "timestamp,original,statuscode,mimetype,digest,length"),
              ("collapse", "timestamp:8"), ("limit", str(MAX_SNAPSHOTS))]
    if re.fullmatch(r"(19|20)\d\d", yf):
        params.append(("from", yf))
    if re.fullmatch(r"(19|20)\d\d", yt):
        params.append(("to", yt))

    page_params = [("url", host), ("matchType", "domain"), ("output", "json"),
                   ("fl", "timestamp,original"), ("collapse", "urlkey"),
                   ("filter", "statuscode:200"), ("filter", "mimetype:text/html"),
                   ("limit", str(MAX_PAGES))]

    with ThreadPoolExecutor(max_workers=2) as ex:
        f_main = ex.submit(cdx, params, deadline)
        f_pages = ex.submit(cdx, page_params, deadline) if want_pages else None
        rows, err = f_main.result()
        pages_rows, pages_err = f_pages.result() if f_pages else (None, None)

    if rows is None:
        return jsonify({"ok": False, "error": err + " Please try again in a minute."}), 502

    snaps = []
    for r in rows:
        ts = (r.get("timestamp") or "").strip()
        d = to_date(ts)
        if not d:
            continue
        code = (r.get("statuscode") or "").strip()
        snaps.append({"ts": ts, "d": d, "url": r.get("original") or target, "status": code,
                      "cls": status_class(code), "mime": r.get("mimetype") or "",
                      "digest": r.get("digest") or "", "length": r.get("length") or ""})
    snaps.sort(key=lambda s: s["ts"])
    # "revisit" captures mean "same content as last time" - carry the previous status forward
    prev = None
    for s in snaps:
        if s["cls"] == "other" and s["mime"] == "warc/revisit" and prev:
            s["status"], s["cls"] = prev["status"], prev["cls"]
        prev = s

    pages = []
    if pages_rows:
        seen = set()
        for r in pages_rows:
            u = r.get("original") or ""
            if u and u not in seen:
                seen.add(u)
                pages.append({"url": u, "ts": r.get("timestamp") or ""})
        pages.sort(key=lambda p: p["url"].lower())

    base = {"ok": True, "target": target, "host": host, "wayback": WAYBACK,
            "save_url": f"https://web.archive.org/save/{target}",
            "pages": pages, "pages_requested": want_pages,
            "pages_error": pages_err if want_pages and pages_rows is None else None,
            "pages_truncated": len(pages_rows or []) >= MAX_PAGES}

    if not snaps:
        return jsonify({**base, "empty": True, "snapshots": [], "checks": [], "score": None})

    by_year, gaps, summary = analyze(snaps, host)
    phases = build_phases(snaps)

    # where did the archived redirects point?
    redirect_phases = [p for p in phases if p["cls"] == "3xx"][-MAX_REDIRECT_LOOKUPS:]
    if redirect_phases:
        with ThreadPoolExecutor(max_workers=MAX_REDIRECT_LOOKUPS) as ex:
            results = list(ex.map(lambda p: redirect_target(p["sample_ts"], p["sample_url"], deadline),
                                  redirect_phases))
        for p, t in zip(redirect_phases, results):
            p["target"] = t

    checks, score = build_checks(summary, gaps, phases, host)
    return jsonify({
        **base, "empty": False, "score": score, "checks": checks, "summary": summary,
        "by_year": by_year, "gaps": gaps, "phases": phases,
        "truncated": len(rows) >= MAX_SNAPSHOTS,
        "snapshots": [[s["ts"], s["status"], s["mime"], s["length"], s["url"]] for s in snaps],
    })


@app.route("/")
def home():
    return Response(PAGE, mimetype="text/html")


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Wayback Machine Checker – See Any Website's Archive History</title>
<meta name="description" content="Free Wayback Machine Checker: see when a URL or domain was first archived, every snapshot by year, status and redirect history, long gaps and red flags — plus before/after compare links and CSV export.">
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
  .side-note{font-size:.7rem;color:var(--muted);line-height:1.55;background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:.7rem .8rem}
  .side-note b{color:var(--text);font-weight:600}
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
  input[type=text],input[type=date],select{width:100%;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:.75rem 1rem;font-family:'DM Mono',monospace;font-size:.82rem}
  input[type=date]{color-scheme:dark}
  select{cursor:pointer}
  input:focus,select:focus{outline:none;border-color:var(--accent)}
  .btn{display:inline-flex;align-items:center;gap:.5rem;background:var(--accent);color:#fff;border:none;border-radius:8px;padding:.7rem 1.5rem;font-size:.85rem;font-weight:600;cursor:pointer;font-family:'Inter',sans-serif;white-space:nowrap;text-decoration:none}
  .btn:hover{background:var(--accent-h)}
  .btn:disabled{opacity:.5;cursor:wait}
  .btn.ghost{background:var(--surface);border:1px solid var(--border);color:var(--text);font-weight:500;padding:.55rem 1.1rem;font-size:.8rem}
  .btn.ghost:hover{border-color:var(--accent)}
  .btn.ghost.disabled{opacity:.4;pointer-events:none}
  .row-inline{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center}
  .row-inline input{flex:1;min-width:240px}
  details.adv{margin-top:.9rem}
  details.adv summary{cursor:pointer;font-size:.78rem;color:var(--muted);font-family:'DM Mono',monospace}
  details.adv summary:hover{color:var(--text)}
  .adv-body{margin-top:.8rem;display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:.8rem;align-items:end}
  .check{display:flex;gap:.5rem;align-items:center;font-size:.8rem;color:var(--muted)}
  .check input{accent-color:var(--accent)}
  .hint{font-size:.72rem;color:var(--muted);margin-top:.5rem;font-family:'DM Mono',monospace}
  .err{font-size:.8rem;color:var(--red);margin-top:.6rem}
  .spinner{display:none;margin-top:.8rem;font-size:.78rem;color:var(--muted);font-family:'DM Mono',monospace}
  .spinner.show{display:block}

  .overview{display:grid;grid-template-columns:220px 1fr 1.2fr;gap:1rem;margin:1.4rem 0}
  .chart-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.1rem 1.2rem;min-width:0}
  .chart-card h3{font-family:'DM Mono',monospace;font-size:.66rem;letter-spacing:.12em;font-weight:500;margin-bottom:.8rem;color:var(--muted);text-transform:uppercase}
  .ring-wrap{position:relative;width:160px;height:160px;margin:0 auto}
  .ring-center{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
  .ring-center .score{font-family:'Sora',sans-serif;font-size:1.9rem;font-weight:800}
  .ring-center .sub{font-size:.6rem;font-family:'DM Mono',monospace;color:var(--muted);text-transform:uppercase;letter-spacing:.1em}
  .bar-box{position:relative;height:220px}

  .metrics{display:grid;grid-template-columns:1fr 1fr;gap:.7rem}
  .metric{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:.7rem .9rem;min-width:0}
  .metric .k{font-family:'DM Mono',monospace;font-size:.6rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
  .metric .v{font-family:'Sora',sans-serif;font-size:1.05rem;font-weight:700;margin-top:.15rem;overflow-wrap:anywhere}
  .v.mint{color:var(--mint)} .v.orange{color:var(--orange)} .v.red{color:var(--red)} .v.purple{color:var(--accent)}

  .quick{display:flex;gap:.6rem;flex-wrap:wrap;margin:-.2rem 0 .4rem}

  .badge{display:inline-flex;align-items:center;gap:.3rem;font-size:.66rem;font-family:'DM Mono',monospace;padding:.22rem .6rem;border-radius:100px;white-space:nowrap;border:1px solid}
  .badge.ok{background:rgba(106,247,200,.1);border-color:rgba(106,247,200,.3);color:var(--mint)}
  .badge.warn{background:rgba(247,162,106,.1);border-color:rgba(247,162,106,.35);color:var(--orange)}
  .badge.error{background:rgba(247,106,106,.1);border-color:rgba(247,106,106,.35);color:var(--red)}
  .badge.redir{background:rgba(124,106,247,.12);border-color:rgba(124,106,247,.4);color:var(--accent)}
  .badge.info{background:rgba(136,136,168,.1);border-color:rgba(136,136,168,.3);color:var(--muted)}

  .sec-title{font-family:'Sora',sans-serif;font-size:1rem;font-weight:700;margin:2rem 0 .9rem;display:flex;align-items:center;gap:.6rem;flex-wrap:wrap}
  .sec-title .count{font-family:'DM Mono',monospace;font-size:.7rem;color:var(--muted);font-weight:400}

  .chips{display:flex;gap:.4rem;flex-wrap:wrap}
  .chip{background:var(--surface);border:1px solid var(--border);color:var(--muted);padding:.35rem .8rem;border-radius:100px;font-size:.72rem;cursor:pointer;font-family:'DM Mono',monospace}
  .chip.active{background:rgba(124,106,247,.14);border-color:rgba(124,106,247,.4);color:var(--accent)}
  .issues{display:flex;flex-direction:column;gap:.5rem}
  .issue{border-left:3px solid var(--border);background:var(--surface);border-radius:0 10px 10px 0;padding:.7rem 1rem;font-size:.82rem}
  .issue.error{border-color:var(--red)} .issue.warn{border-color:var(--orange)} .issue.info{border-color:var(--muted)} .issue.ok{border-color:var(--mint)}
  .issue .top{display:flex;gap:.5rem;align-items:flex-start}
  .issue .msg{font-weight:600;overflow-wrap:anywhere}
  .issue .fix{color:var(--muted);font-size:.76rem;margin-top:.3rem;overflow-wrap:anywhere}

  /* status timeline strip */
  .strip{display:flex;height:34px;border-radius:8px;overflow:hidden;border:1px solid var(--border);background:var(--surface2)}
  .strip div{min-width:3px;height:100%}
  .strip-axis{display:flex;justify-content:space-between;font-family:'DM Mono',monospace;font-size:.66rem;color:var(--muted);margin-top:.35rem}
  .phase-list{margin-top:1rem;display:flex;flex-direction:column;gap:.45rem}
  .phase{display:grid;grid-template-columns:110px 1fr auto;gap:.8rem;align-items:start;font-size:.8rem;padding:.55rem .8rem;background:var(--surface2);border:1px solid var(--border);border-radius:8px}
  .phase .when{font-family:'DM Mono',monospace;font-size:.72rem;color:var(--muted)}
  .phase .what{overflow-wrap:anywhere}
  .phase .what .small{font-size:.72rem;color:var(--muted)}

  .compare{display:grid;grid-template-columns:220px 1fr;gap:1rem;align-items:start}
  .cmp-out{display:grid;grid-template-columns:1fr 1fr;gap:.7rem}
  .cmp-box{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:.8rem 1rem;min-width:0}
  .cmp-box .k{font-family:'DM Mono',monospace;font-size:.6rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
  .cmp-box .v{font-family:'Sora',sans-serif;font-weight:700;margin:.2rem 0 .5rem}

  .tbl-tools{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center;margin-bottom:.7rem}
  .tbl-tools input[type=text]{max-width:260px;padding:.5rem .8rem}
  .tbl-tools select{width:auto;padding:.45rem .8rem}
  .tbl-wrap{overflow-x:auto;background:var(--surface);border:1px solid var(--border);border-radius:12px}
  table{width:100%;border-collapse:collapse;font-size:.8rem}
  th{font-family:'DM Mono',monospace;font-size:.6rem;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);text-align:left;padding:.65rem .8rem;border-bottom:1px solid var(--border);white-space:nowrap}
  td{padding:.55rem .8rem;border-bottom:1px solid var(--border);vertical-align:top}
  tr:last-child td{border-bottom:none}
  tbody tr:hover td{background:rgba(124,106,247,.05)}
  td.mono{font-family:'DM Mono',monospace;font-size:.72rem;word-break:break-all}
  .more-row{text-align:center;margin-top:.8rem}
  .empty-box{margin-top:1.4rem;text-align:center;padding:2.2rem 1rem}
  .empty-box h2{font-family:'Sora',sans-serif;font-size:1.1rem;margin-bottom:.5rem}
  .empty-box p{color:var(--muted);font-size:.85rem;margin-bottom:1rem}

  @media(max-width:1100px){.overview{grid-template-columns:220px 1fr}.overview .bars{grid-column:1/-1}}
  @media(max-width:960px){.overview,.compare{grid-template-columns:1fr}.sidebar{display:none}.main{margin-left:0;padding:1.2rem 1rem}}
  @media(max-width:600px){.cmp-out{grid-template-columns:1fr}.phase{grid-template-columns:1fr}}
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
      <a href="#" class="sidebar-link active">🕰️&nbsp; Wayback Machine Checker</a>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-label">Navigate</div>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/tools/index.html" class="sidebar-link">🧰&nbsp; All My Tools</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio" class="sidebar-link">👩‍💻&nbsp; Portfolio</a>
      <a href="https://mahalakshmi26-hub.github.io/my-portfolio/blog/index.html" class="sidebar-link">📝&nbsp; Blog Posts</a>
    </div>
    <div class="side-note">
      <b>Data source:</b> the free Internet Archive Wayback Machine. Big sites can take up to a minute to load. One row = one archived day.
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
      <h1>🕰️ Wayback Machine <span>Checker</span></h1>
    </div>

    <form class="card" id="form">
      <label class="lbl" for="url">Website or page URL</label>
      <div class="row-inline">
        <input type="text" id="url" placeholder="https://www.yourwebsite.com or a full page URL">
        <button class="btn" type="submit" id="runBtn">🔍 Check history</button>
      </div>
      <details class="adv">
        <summary>+ Advanced options</summary>
        <div class="adv-body">
          <div>
            <label class="lbl" for="yFrom">From year (optional)</label>
            <input type="text" id="yFrom" placeholder="e.g. 2015" maxlength="4" inputmode="numeric">
          </div>
          <div>
            <label class="lbl" for="yTo">To year (optional)</label>
            <input type="text" id="yTo" placeholder="e.g. 2024" maxlength="4" inputmode="numeric">
          </div>
          <label class="check" style="padding-bottom:.8rem"><input type="checkbox" id="pages"> Also list old pages found on this domain (slower)</label>
        </div>
      </details>
      <div class="spinner" id="sp">Asking the Wayback Machine… large sites can take up to a minute.</div>
      <div class="err" id="err"></div>
    </form>

    <div id="emptyBox" class="card empty-box" style="display:none">
      <h2>No snapshots found</h2>
      <p id="emptyMsg"></p>
      <a class="btn" id="emptySave" target="_blank">📥 Archive this page now</a>
    </div>

    <div id="results" style="display:none">
      <div class="overview">
        <div class="chart-card">
          <h3>History health</h3>
          <div class="ring-wrap" id="ring"></div>
          <div style="display:flex;justify-content:center;gap:.4rem;margin-top:.8rem;flex-wrap:wrap" id="ringBadges"></div>
        </div>
        <div class="chart-card">
          <h3>At a glance</h3>
          <div class="metrics" id="metrics"></div>
          <div class="hint" style="overflow-wrap:anywhere" id="checked"></div>
        </div>
        <div class="chart-card bars">
          <h3>Snapshots per year, by status</h3>
          <div class="bar-box"><canvas id="chart"></canvas></div>
        </div>
      </div>

      <div class="quick">
        <a class="btn ghost" id="qFirst" target="_blank">⏮ First snapshot</a>
        <a class="btn ghost" id="qLast" target="_blank">⏭ Latest snapshot</a>
        <a class="btn ghost" id="qAll" target="_blank">🗓 Wayback calendar</a>
        <a class="btn ghost" id="qSave" target="_blank">📥 Archive this page now</a>
      </div>

      <div class="sec-title">🚩 Red Flags &amp; Findings <span class="count" id="issueCount"></span></div>
      <div class="issues" id="issues"></div>

      <div class="sec-title">🧭 Status History <span class="count">when the page was live, redirected or broken</span></div>
      <div class="card">
        <div class="strip" id="strip"></div>
        <div class="strip-axis" id="stripAxis"></div>
        <div class="phase-list" id="phases"></div>
      </div>

      <div class="sec-title">↔️ Before / After <span class="count">pick a date — e.g. a traffic drop or site launch</span></div>
      <div class="card compare">
        <div>
          <label class="lbl" for="cmpDate">Date to check</label>
          <input type="date" id="cmpDate">
          <div class="hint">Finds the nearest snapshot on each side.</div>
        </div>
        <div>
          <div class="cmp-out" id="cmpOut"></div>
          <div class="row-inline" style="margin-top:.8rem"><a class="btn ghost disabled" id="cmpDiff" target="_blank">🔀 Compare the two versions on Wayback</a></div>
        </div>
      </div>

      <div class="sec-title">📸 Snapshots <span class="count" id="snapCount"></span></div>
      <div class="tbl-tools">
        <select id="yearSel"></select>
        <div class="chips" id="stChips"></div>
        <button type="button" class="btn ghost" id="csvBtn" style="margin-left:auto">⬇ Download CSV</button>
      </div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>Date</th><th>Status</th><th>Type</th><th>Size</th><th>Captured URL</th><th></th></tr></thead>
          <tbody id="snapBody"></tbody>
        </table>
      </div>
      <div class="more-row"><button type="button" class="btn ghost" id="moreBtn" style="display:none">Show more</button></div>

      <div id="pagesSec" style="display:none">
        <div class="sec-title">🗂 Old Pages Found on This Domain <span class="count" id="pageCount"></span></div>
        <div class="tbl-tools">
          <input type="text" id="pageFilter" placeholder="Filter pages…">
          <button type="button" class="btn ghost" id="pagesCsv" style="margin-left:auto">⬇ Download CSV</button>
        </div>
        <div class="tbl-wrap">
          <table>
            <thead><tr><th>Page URL</th><th>First archived</th><th></th></tr></thead>
            <tbody id="pageBody"></tbody>
          </table>
        </div>
        <div class="more-row"><button type="button" class="btn ghost" id="pageMore" style="display:none">Show more</button></div>
      </div>
    </div>
  </main>
</div>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const WB = 'https://web.archive.org';
const COLORS = { '2xx': '#6af7c8', '3xx': '#7c6af7', '4xx': '#f76a6a', '5xx': '#f7a26a', 'other': '#55557a' };
const NAMES = { '2xx': 'Live (2xx)', '3xx': 'Redirect (3xx)', '4xx': 'Not found (4xx)', '5xx': 'Server error (5xx)', 'other': 'Other' };
const BADGE = { '2xx': 'ok', '3xx': 'redir', '4xx': 'error', '5xx': 'warn', 'other': 'info' };
const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
let DATA = null, chart = null, shown = 100, pShown = 100, stFilter = 'all';

const tsDate = ts => new Date(Date.UTC(+ts.slice(0,4), (+ts.slice(4,6) || 1) - 1, +ts.slice(6,8) || 1));
const fmt = ts => `${+ts.slice(6,8) || 1} ${MON[(+ts.slice(4,6) || 1) - 1]} ${ts.slice(0,4)}`;
const cls = code => /^\d{3}$/.test(code || '') ? ({'2':'2xx','3':'3xx','4':'4xx','5':'5xx'}[code[0]] || 'other') : 'other';
const snapUrl = (ts, url) => `${WB}/web/${ts}/${url}`;
const size = n => { n = +n; if (!n) return '—'; return n >= 1048576 ? (n/1048576).toFixed(1)+' MB' : n >= 1024 ? Math.round(n/1024)+' KB' : n+' B'; };
const plural = (n, w) => `${n.toLocaleString()} ${w}${n === 1 ? '' : 's'}`;

$('form').addEventListener('submit', async e => {
  e.preventDefault();
  const url = $('url').value.trim();
  $('err').textContent = '';
  if (!url) { $('err').textContent = 'Please enter a website or page URL.'; return; }
  $('sp').classList.add('show'); $('runBtn').disabled = true;
  try {
    const r = await fetch('/api/check', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, from: $('yFrom').value.trim(), to: $('yTo').value.trim(), pages: $('pages').checked }) });
    let j;
    try { j = await r.json(); } catch (x) { throw new Error('The server took too long — the Wayback Machine may be slow. Please try again.'); }
    if (!j.ok) throw new Error(j.error || 'Something went wrong.');
    DATA = j; render();
  } catch (err) {
    $('err').textContent = err.message || 'Could not reach the server — please try again.';
  } finally {
    $('sp').classList.remove('show'); $('runBtn').disabled = false;
  }
});

function render() {
  const d = DATA;
  if (d.empty) {
    $('results').style.display = 'none'; $('emptyBox').style.display = '';
    $('emptyMsg').textContent = `The Wayback Machine has no snapshots of ${d.target} for this period. It may be new, rarely linked, or excluded from the archive.`;
    $('emptySave').href = d.save_url;
    return;
  }
  $('emptyBox').style.display = 'none'; $('results').style.display = '';
  shown = 100; pShown = 100; stFilter = 'all';
  const s = d.summary;

  // ring
  const sc = d.score, col = sc >= 80 ? '#6af7c8' : sc >= 50 ? '#f7a26a' : '#f76a6a';
  $('ring').innerHTML = `<svg viewBox="0 0 160 160" width="160" height="160">
      <circle cx="80" cy="80" r="68" fill="none" stroke="#23233a" stroke-width="12"/>
      <circle cx="80" cy="80" r="68" fill="none" stroke="${col}" stroke-width="12" stroke-linecap="${sc > 0 ? 'round' : 'butt'}"
        stroke-dasharray="${(427.26 * sc / 100).toFixed(2)} 427.26" transform="rotate(-90 80 80)"/></svg>
    <div class="ring-center"><div class="score" style="color:${col}">${sc}</div><div class="sub">out of 100</div></div>`;
  const nF = d.checks.filter(c => c.sev === 'fail').length, nW = d.checks.filter(c => c.sev === 'warn').length;
  $('ringBadges').innerHTML = `<span class="badge error">${nF} red flag${nF === 1 ? '' : 's'}</span><span class="badge warn">${nW} warning${nW === 1 ? '' : 's'}</span>`;

  // metrics
  const gapY = s.longest_gap >= 365 ? (s.longest_gap / 365.25).toFixed(1) + ' yrs' : plural(s.longest_gap, 'day');
  $('metrics').innerHTML = [
    ['First seen', fmt(s.first), 'purple'],
    ['Last seen', fmt(s.last), s.since_last > 365 ? 'orange' : 'mint'],
    ['Archived days', s.days.toLocaleString() + (d.truncated ? '+' : ''), ''],
    ['Years active', `${s.years_active} of ${Math.max(1, Math.ceil(s.span_years))}`, ''],
    ['Content versions', s.versions.toLocaleString(), 'purple'],
    ['Longest gap', gapY, s.longest_gap >= 365 ? 'orange' : 'mint'],
  ].map(([k, v, c]) => `<div class="metric"><div class="k">${k}</div><div class="v ${c}">${esc(v)}</div></div>`).join('');
  $('checked').innerHTML = `Checked: <span style="color:var(--text)">${esc(d.target)}</span>` +
    (d.truncated ? ' · showing the first 20,000 archived days' : '');

  // quick links
  const snaps = d.snapshots, first = snaps[0], last = snaps[snaps.length - 1];
  $('qFirst').href = snapUrl(first[0], first[4]);
  $('qLast').href = snapUrl(last[0], last[4]);
  $('qAll').href = `${WB}/web/*/${d.target}`;
  $('qSave').href = d.save_url;

  // chart
  if (typeof Chart !== 'undefined') {
    const keys = ['2xx', '3xx', '4xx', '5xx', 'other'].filter(k => d.by_year.some(y => y[k]));
    const data = { labels: d.by_year.map(y => y.year), datasets: keys.map(k => ({ label: NAMES[k], data: d.by_year.map(y => y[k]),
      backgroundColor: COLORS[k], borderRadius: 3, borderSkipped: false, maxBarThickness: 28 })) };
    const opts = { maintainAspectRatio: false, animation: false,
      scales: { x: { stacked: true, ticks: { color: '#8888a8', font: { family: 'DM Mono', size: 10 }, maxRotation: 0, autoSkip: true }, grid: { display: false } },
                y: { stacked: true, beginAtZero: true, ticks: { color: '#8888a8', font: { family: 'DM Mono', size: 10 }, precision: 0 }, grid: { color: '#1c1c2c' } } },
      plugins: { legend: { position: 'bottom', labels: { color: '#8888a8', font: { family: 'DM Mono', size: 10 }, boxWidth: 10 } },
                 tooltip: { callbacks: { title: it => 'Year ' + it[0].label, label: it => ` ${it.dataset.label}: ${it.raw} days` } } } };
    if (chart) chart.destroy();
    chart = new Chart($('chart'), { type: 'bar', data, options: opts });
  }

  // issues
  const sevMap = { fail: 'error', warn: 'warn', info: 'info', pass: 'ok' };
  const label = { error: '✕ red flag', warn: '! warning', info: 'i note', ok: '✓ good' };
  const order = { fail: 0, warn: 1, info: 2, pass: 3 };
  const list = [...d.checks].sort((x, y) => order[x.sev] - order[y.sev]);
  $('issueCount').textContent = `${nF + nW} to review · ${d.checks.filter(c => c.sev === 'pass').length} good`;
  $('issues').innerHTML = list.map(c => `<div class="issue ${sevMap[c.sev]}"><div class="top">
      <span class="badge ${sevMap[c.sev]}">${label[sevMap[c.sev]]}</span><div class="msg">${esc(c.title)}</div></div>
      <div class="fix">${esc(c.detail)}</div></div>`).join('');

  renderPhases();
  setupCompare();
  setupSnapTable();
  renderPages();
}

function renderPhases() {
  const ph = DATA.phases, t0 = tsDate(ph[0].from).getTime();
  const tEnd = tsDate(ph[ph.length - 1].to).getTime(), span = Math.max(1, tEnd - t0);
  $('strip').innerHTML = ph.map((p, i) => {
    const a = tsDate(p.from).getTime(), b = i < ph.length - 1 ? tsDate(ph[i + 1].from).getTime() : tEnd;
    const w = ph.length === 1 ? 100 : Math.max(0.6, (b - a) / span * 100);
    return `<div style="flex:${w} 0 0;background:${COLORS[p.cls]}" title="${esc(NAMES[p.cls])}: ${fmt(p.from)} → ${fmt(p.to)}"></div>`;
  }).join('');
  $('stripAxis').innerHTML = `<span>${ph[0].from.slice(0,4)}</span><span>${ph[ph.length - 1].to.slice(0,4)}</span>`;
  $('phases').innerHTML = ph.slice().reverse().map(p => {
    let what = `<b>${esc(NAMES[p.cls])}</b>`;
    if (p.cls === '3xx') what += p.target ? ` → <span style="font-family:'DM Mono',monospace;font-size:.75rem">${esc(p.target)}</span>`
                                          : ' <span class="small">(target not available)</span>';
    const extra = p.blips ? ` · ${p.blips} odd snapshot${p.blips === 1 ? '' : 's'} folded in` : '';
    return `<div class="phase"><div class="when">${p.from.slice(0,8) === p.to.slice(0,8) ? fmt(p.from) : fmt(p.from) + '<br>→ ' + fmt(p.to)}</div>
      <div class="what">${what}<div class="small">${plural(p.count, 'archived day')}${p.code ? ' · mostly HTTP ' + esc(p.code) : ''}${extra}</div></div>
      <div><a class="badge ${BADGE[p.cls]}" href="${snapUrl(p.sample_ts, p.sample_url)}" target="_blank">view ↗</a></div></div>`;
  }).join('');
}

function setupCompare() {
  const s = DATA.snapshots;
  const lo = `${s[0][0].slice(0,4)}-${s[0][0].slice(4,6)}-${s[0][0].slice(6,8)}`;
  const l = s[s.length - 1][0], hi = `${l.slice(0,4)}-${l.slice(4,6)}-${l.slice(6,8)}`;
  $('cmpDate').min = lo; $('cmpDate').max = hi; $('cmpDate').value = '';
  $('cmpOut').innerHTML = '<div class="cmp-box" style="grid-column:1/-1;color:var(--muted);font-size:.8rem">Choose a date to see the snapshot just before and just after it.</div>';
  $('cmpDiff').classList.add('disabled'); $('cmpDiff').removeAttribute('href');
}
$('cmpDate').addEventListener('change', () => {
  const v = $('cmpDate').value.replace(/-/g, '');
  if (!v || !DATA) return;
  const s = DATA.snapshots;
  let before = null, after = null;
  for (const r of s) { if (r[0].slice(0, 8) < v) before = r; else { after = r; break; } }
  const box = (k, r) => r ? `<div class="cmp-box"><div class="k">${k}</div><div class="v">${fmt(r[0])}</div>
      <span class="badge ${BADGE[cls(r[1])]}">${esc(r[1] || '—')}</span>
      <a class="btn ghost" style="margin-left:.4rem;padding:.3rem .8rem;font-size:.72rem" href="${snapUrl(r[0], r[4])}" target="_blank">Open ↗</a></div>`
    : `<div class="cmp-box"><div class="k">${k}</div><div class="v" style="color:var(--muted)">None</div></div>`;
  $('cmpOut').innerHTML = box('Before', before) + box('On / after', after);
  if (before && after) {
    $('cmpDiff').href = `${WB}/web/diff/${before[0]}/${after[0]}/${DATA.target}`;
    $('cmpDiff').classList.remove('disabled');
  } else { $('cmpDiff').classList.add('disabled'); $('cmpDiff').removeAttribute('href'); }
});

function setupSnapTable() {
  const years = [...new Set(DATA.snapshots.map(r => r[0].slice(0, 4)))].reverse();
  $('yearSel').innerHTML = '<option value="">All years</option>' + years.map(y => `<option>${y}</option>`).join('');
  const present = new Set(DATA.snapshots.map(r => cls(r[1])));
  const chips = [['all', 'All'], ['2xx', '2xx live'], ['3xx', '3xx redirect'], ['err', '4xx/5xx error'], ['other', 'Other']]
    .filter(([k]) => k === 'all' || (k === 'err' ? present.has('4xx') || present.has('5xx') : present.has(k)));
  $('stChips').innerHTML = chips.map(([k, t]) => `<span class="chip${k === 'all' ? ' active' : ''}" data-k="${k}">${t}</span>`).join('');
  renderSnaps();
}
$('stChips').addEventListener('click', e => {
  const c = e.target.closest('.chip'); if (!c) return;
  stFilter = c.dataset.k; shown = 100;
  document.querySelectorAll('#stChips .chip').forEach(x => x.classList.toggle('active', x === c));
  renderSnaps();
});
$('yearSel').addEventListener('change', () => { shown = 100; renderSnaps(); });
$('moreBtn').addEventListener('click', () => { shown += 200; renderSnaps(); });

function filteredSnaps() {
  const y = $('yearSel').value;
  return DATA.snapshots.filter(r => {
    if (y && r[0].slice(0, 4) !== y) return false;
    const c = cls(r[1]);
    if (stFilter === 'all') return true;
    if (stFilter === 'err') return c === '4xx' || c === '5xx';
    return c === stFilter;
  }).reverse();
}
function renderSnaps() {
  const rows = filteredSnaps();
  $('snapCount').textContent = `${rows.length.toLocaleString()} of ${DATA.snapshots.length.toLocaleString()} archived days · newest first`;
  $('snapBody').innerHTML = rows.length ? rows.slice(0, shown).map(r => `<tr>
      <td style="white-space:nowrap">${fmt(r[0])}</td>
      <td><span class="badge ${BADGE[cls(r[1])]}">${esc(r[1] || '—')}</span></td>
      <td class="mono">${esc(r[2] === 'warc/revisit' ? 'unchanged' : r[2])}</td>
      <td class="mono">${size(r[3])}</td>
      <td class="mono">${esc(r[4])}</td>
      <td><a href="${snapUrl(r[0], r[4])}" target="_blank" class="badge info">view ↗</a></td></tr>`).join('')
    : '<tr><td colspan="6" style="color:var(--muted)">No snapshots match this filter.</td></tr>';
  $('moreBtn').style.display = rows.length > shown ? '' : 'none';
}

function renderPages() {
  const d = DATA;
  if (!d.pages_requested) { $('pagesSec').style.display = 'none'; return; }
  $('pagesSec').style.display = '';
  const q = $('pageFilter').value.trim().toLowerCase();
  const rows = d.pages.filter(p => !q || p.url.toLowerCase().includes(q));
  $('pageCount').textContent = d.pages_error ? '' : `${rows.length.toLocaleString()} page${rows.length === 1 ? '' : 's'}` +
    (d.pages_truncated ? ' · first 1,000 the archive returned' : '');
  if (d.pages_error) {
    $('pageBody').innerHTML = `<tr><td colspan="3" style="color:var(--orange)">${esc(d.pages_error)} Try again, or untick this option for large sites.</td></tr>`;
    $('pageMore').style.display = 'none'; return;
  }
  $('pageBody').innerHTML = rows.length ? rows.slice(0, pShown).map(p => `<tr>
      <td class="mono">${esc(p.url)}</td><td style="white-space:nowrap">${p.ts ? fmt(p.ts) : '—'}</td>
      <td style="white-space:nowrap"><a href="${WB}/web/*/${esc(p.url)}" target="_blank" class="badge info">all snapshots ↗</a></td></tr>`).join('')
    : '<tr><td colspan="3" style="color:var(--muted)">No archived HTML pages found.</td></tr>';
  $('pageMore').style.display = rows.length > pShown ? '' : 'none';
}
$('pageFilter').addEventListener('input', () => { pShown = 100; renderPages(); });
$('pageMore').addEventListener('click', () => { pShown += 200; renderPages(); });

function downloadCsv(name, header, rows) {
  const q = v => '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"';
  const out = [header.map(q).join(',')].concat(rows.map(r => r.map(q).join(',')));
  const blob = new Blob(['﻿' + out.join('\r\n')], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
const hostSlug = () => (DATA.host || 'site').replace(/^www\./, '').replace(/[^a-z0-9.-]/gi, '-');
$('csvBtn').addEventListener('click', () => {
  if (!DATA) return;
  downloadCsv(`wayback-snapshots-${hostSlug()}.csv`, ['Date', 'Timestamp', 'Status', 'Type', 'Size (bytes)', 'Captured URL', 'Snapshot link'],
    filteredSnaps().map(r => [tsDate(r[0]).toISOString().slice(0, 10), r[0], r[1], r[2], r[3], r[4], snapUrl(r[0], r[4])]));
});
$('pagesCsv').addEventListener('click', () => {
  if (!DATA) return;
  const q = $('pageFilter').value.trim().toLowerCase();
  downloadCsv(`wayback-pages-${hostSlug()}.csv`, ['Page URL', 'First archived', 'All snapshots'],
    DATA.pages.filter(p => !q || p.url.toLowerCase().includes(q)).map(p => [p.url, p.ts ? tsDate(p.ts).toISOString().slice(0, 10) : '', `${WB}/web/*/${p.url}`]));
});
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(debug=True, port=5000)
