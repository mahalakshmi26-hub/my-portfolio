"""Domain age lookup engine: RDAP (primary) -> WHOIS (fallback) + Wayback first-seen.

All sources are free and need no API key.
"""
import calendar
import re
import socket
import threading
import time
from datetime import datetime, timezone

import requests
import tldextract

UA = "Mozilla/5.0 (compatible; DomainAgeChecker/1.0; +https://mahalakshmi26-hub.github.io/my-portfolio)"
_extract = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)  # bundled suffix list, no network

# ----------------------------------------------------------------------------
# Input normalisation
# ----------------------------------------------------------------------------

def normalize(raw):
    """Return (registrable_domain, note) or (None, error)."""
    s = (raw or "").strip().lower()
    if not s:
        return None, "empty"
    s = re.sub(r"^[a-z][a-z0-9+.-]*://", "", s)
    s = re.split(r"[/?#]", s, 1)[0]
    s = s.split("@")[-1]
    s = re.sub(r":\d+$", "", s).strip(".")
    ext = _extract(s)
    if not ext.domain or not ext.suffix:
        return None, "not a valid domain"
    reg = ext.registered_domain
    try:
        reg = reg.encode("idna").decode()
    except UnicodeError:
        return None, "invalid characters in domain"
    note = None
    if ext.subdomain and ext.subdomain != "www":
        note = f"Subdomain '{ext.subdomain}' ignored: age belongs to the registered domain."
    return reg, note


# ----------------------------------------------------------------------------
# Date helpers
# ----------------------------------------------------------------------------
_FORMATS = [
    "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d-%b-%Y %H:%M:%S", "%d-%b-%Y", "%d-%B-%Y", "%Y.%m.%d",
    "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d", "%b %d %Y", "%d %b %Y", "%a %b %d %Y",
]


def parse_dt(value):
    if not value:
        return None
    s = str(value).strip()
    s = re.sub(r"\s*\(.*?\)\s*$", "", s)          # "(UTC)" style suffixes
    s = re.sub(r"\s+(UTC|GMT)$", "", s, flags=re.I)
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        d = None
        for f in _FORMATS:
            try:
                d = datetime.strptime(s, f)
                break
            except ValueError:
                continue
        if d is None:
            m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
            if not m:
                return None
            d = datetime(int(m[1]), int(m[2]), int(m[3]))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def age_parts(start, now):
    y = now.year - start.year
    m = now.month - start.month
    d = now.day - start.day
    if d < 0:
        m -= 1
        pm = now.month - 1 or 12
        py = now.year if now.month > 1 else now.year - 1
        d += calendar.monthrange(py, pm)[1]
    if m < 0:
        y -= 1
        m += 12
    return y, m, d


def age_text(y, m, d):
    bits = []
    if y:
        bits.append(f"{y} year{'s' if y != 1 else ''}")
    if m:
        bits.append(f"{m} month{'s' if m != 1 else ''}")
    if d or not bits:
        bits.append(f"{d} day{'s' if d != 1 else ''}")
    return ", ".join(bits)


def trust_label(days):
    if days < 365:
        return "New (under 1 year)"
    if days < 365 * 3:
        return "Young (1-3 years)"
    if days < 365 * 10:
        return "Established (3-10 years)"
    return "Aged (10+ years)"


# ----------------------------------------------------------------------------
# RDAP
# ----------------------------------------------------------------------------
_bootstrap = {"ts": 0, "map": {}}
_bs_lock = threading.Lock()


def _rdap_base(tld):
    with _bs_lock:
        if time.time() - _bootstrap["ts"] > 86400 or not _bootstrap["map"]:
            try:
                r = requests.get("https://data.iana.org/rdap/dns.json", timeout=10, headers={"User-Agent": UA})
                r.raise_for_status()
                mp = {}
                for tlds, urls in r.json().get("services", []):
                    for t in tlds:
                        mp[t.lower()] = urls
                _bootstrap.update(ts=time.time(), map=mp)
            except Exception:
                pass
        urls = _bootstrap["map"].get(tld.lower())
    if not urls:
        return None
    urls = sorted(urls, key=lambda u: not u.startswith("https"))
    return urls[0]


def _vcard_name(entity):
    try:
        for item in entity.get("vcardArray", [None, []])[1]:
            if item[0] == "fn":
                return item[3]
    except Exception:
        pass
    return None


def _find_registrar(entities):
    for e in entities or []:
        if "registrar" in [r.lower() for r in e.get("roles", [])]:
            name = _vcard_name(e)
            if name:
                return name
            for pid in e.get("publicIds", []) or []:
                if pid.get("identifier"):
                    return f"IANA ID {pid['identifier']}"
        sub = _find_registrar(e.get("entities"))
        if sub:
            return sub
    return None


class LookupError_(Exception):
    pass


def rdap_lookup(domain):
    tld = domain.rsplit(".", 1)[-1]
    base = _rdap_base(tld)
    urls = []
    if base:
        urls.append(base.rstrip("/") + "/domain/" + domain)
    urls.append("https://rdap.org/domain/" + domain)  # generic redirector as second chance
    last_err = None
    for url in urls:
        for attempt in range(2):
            try:
                r = requests.get(url, timeout=12, headers={"Accept": "application/rdap+json", "User-Agent": UA},
                                 allow_redirects=True)
            except requests.RequestException as e:
                last_err = f"RDAP request failed ({type(e).__name__})"
                break
            if r.status_code == 404:
                raise LookupError_("not_found")
            if r.status_code == 429:
                last_err = "RDAP server rate-limited the request"
                time.sleep(1.5)
                continue
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError:
                    last_err = "RDAP returned invalid JSON"
                    break
            last_err = f"RDAP returned HTTP {r.status_code}"
            break
    raise LookupError_(last_err or "RDAP unavailable")


def parse_rdap(data):
    out = {"created": None, "expires": None, "updated": None}
    for ev in data.get("events", []):
        act = (ev.get("eventAction") or "").lower()
        dt = parse_dt(ev.get("eventDate"))
        if not dt:
            continue
        if act == "registration" and not out["created"]:
            out["created"] = dt
        elif act == "expiration":
            out["expires"] = dt
        elif act == "last changed":
            out["updated"] = dt
    out["registrar"] = _find_registrar(data.get("entities"))
    out["nameservers"] = sorted({(ns.get("ldhName") or "").lower() for ns in data.get("nameservers", []) if ns.get("ldhName")})
    out["status"] = data.get("status", [])
    return out


# ----------------------------------------------------------------------------
# WHOIS fallback (port 43) for TLDs with no working RDAP
# ----------------------------------------------------------------------------

def _whois_raw(server, query, timeout=10):
    with socket.create_connection((server, 43), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall((query + "\r\n").encode())
        chunks = []
        while True:
            try:
                b = s.recv(4096)
            except socket.timeout:
                break
            if not b:
                break
            chunks.append(b)
            if sum(map(len, chunks)) > 60000:
                break
    return b"".join(chunks).decode("utf-8", "ignore")


def whois_lookup(domain):
    tld = domain.rsplit(".", 1)[-1]
    iana = _whois_raw("whois.iana.org", tld)
    m = re.search(r"(?im)^whois:\s*(\S+)", iana)
    if not m:
        raise LookupError_("no WHOIS server for this TLD")
    raw = _whois_raw(m.group(1), domain)
    if re.search(r"(?i)(no match|not found|no data found|no entries found|status:\s*free|available)", raw[:600]):
        raise LookupError_("not_found")

    def grab(keys):
        mm = re.search(r"(?im)^\s*(?:%s)\s*:\s*(.+)$" % "|".join(keys), raw)
        return mm.group(1).strip() if mm else None

    created = parse_dt(grab(["creation date", "created", "created on", "registered on", "registration time",
                             "domain registration date", "registered", "domain name commencement date"]))
    expires = parse_dt(grab(["registry expiry date", "registrar registration expiration date", "expiry date",
                             "expires on", "expiration date", "paid-till", "renewal date"]))
    updated = parse_dt(grab(["updated date", "last updated", "last modified", "changed"]))
    registrar = grab(["registrar", "sponsoring registrar"])
    ns = sorted({x.split()[0].lower().rstrip(".") for x in re.findall(r"(?im)^\s*name ?servers?\s*:\s*(.+)$", raw)})
    status = [x.split()[0] for x in re.findall(r"(?im)^\s*(?:domain )?status\s*:\s*(.+)$", raw)]
    return {"created": created, "expires": expires, "updated": updated, "registrar": registrar,
            "nameservers": ns, "status": status}


# ----------------------------------------------------------------------------
# Wayback Machine first capture
# ----------------------------------------------------------------------------

def wayback_first(domain):
    """Return (first_capture_datetime | None, reason | None)."""
    headers = {"User-Agent": UA}
    reason = None
    for attempt in range(2):
        try:
            r = requests.get("https://web.archive.org/cdx/search/cdx",
                             params={"url": domain, "limit": 1, "output": "json", "fl": "timestamp"},
                             timeout=25, headers=headers)
            if r.status_code == 200:
                rows = r.json()
                if len(rows) >= 2:
                    return datetime.strptime(rows[1][0][:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc), None
                reason = "no captures found"
                break
            reason = f"Wayback returned HTTP {r.status_code}"
        except requests.Timeout:
            reason = "Wayback timed out"
        except Exception as e:
            reason = f"Wayback unreachable ({type(e).__name__})"
        time.sleep(1)
    if reason != "no captures found":
        try:  # second route: availability API, closest snapshot to 1996 = effectively the earliest
            r = requests.get("https://archive.org/wayback/available",
                             params={"url": domain, "timestamp": "19960101"}, timeout=15, headers=headers)
            snap = (r.json().get("archived_snapshots") or {}).get("closest")
            if snap and snap.get("timestamp"):
                return (datetime.strptime(snap["timestamp"][:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc),
                        "taken from the Wayback availability API (may be slightly later than the very first capture)")
        except Exception:
            pass
    return None, reason


# ----------------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------------
_cache = {}
_cache_lock = threading.Lock()
CACHE_TTL = 3600


def _fmt(dt):
    return dt.strftime("%Y-%m-%d") if dt else None


def check_domain(domain, original=None, note=None):
    with _cache_lock:
        hit = _cache.get(domain)
        if hit and time.time() - hit[0] < CACHE_TTL:
            res = dict(hit[1])
            res["input"] = original or domain
            res["cached"] = True
            return res

    now = datetime.now(timezone.utc)
    res = {"input": original or domain, "domain": domain, "ok": False, "error": None, "notes": [], "source": None,
           "created": None, "expires": None, "updated": None, "age_days": None, "age_text": None, "trust": None,
           "expires_in_days": None, "registrar": None, "nameservers": [], "status": [],
           "wayback_first": None, "cached": False}
    if note:
        res["notes"].append(note)

    info = None
    try:
        info = parse_rdap(rdap_lookup(domain))
        res["source"] = "RDAP (registry)"
    except LookupError_ as e:
        if str(e) == "not_found":
            res["error"] = "Domain not found in the registry. It may be unregistered, or the TLD does not expose this lookup."
        else:
            try:
                info = whois_lookup(domain)
                res["source"] = "WHOIS (fallback)"
                res["notes"].append("RDAP was unavailable for this TLD, so WHOIS was used.")
            except LookupError_ as e2:
                if str(e2) == "not_found":
                    res["error"] = "Domain not found (looks unregistered)."
                else:
                    res["error"] = f"{e}; WHOIS fallback also failed ({e2})."
            except Exception as e2:
                res["error"] = f"{e}; WHOIS fallback failed ({type(e2).__name__})."
    except Exception as e:
        res["error"] = f"Lookup failed ({type(e).__name__})."

    wb, wb_note = wayback_first(domain)
    res["wayback_first"] = _fmt(wb)
    if wb_note:
        res["notes"].append("Wayback: " + wb_note + ".")

    if info:
        res["ok"] = True
        c = info["created"]
        res.update(created=_fmt(c), expires=_fmt(info["expires"]), updated=_fmt(info["updated"]),
                   registrar=info["registrar"], nameservers=info["nameservers"], status=info["status"])
        if c:
            y, m, d = age_parts(c, now)
            days = (now - c).days
            res.update(age_days=days, age_text=age_text(y, m, d), trust=trust_label(days))
            if wb and (c - wb).days > 30:
                res["notes"].append("Wayback Machine captured this domain before the current registration date. It was "
                                    "probably dropped and re-registered, so the site's real history is older than the registry age.")
        else:
            res["notes"].append("The registry does not publish a creation date for this domain/TLD.")
            if wb:
                res["notes"].append("Wayback first-seen date is the best available age signal.")
        if info["expires"]:
            left = (info["expires"] - now).days
            res["expires_in_days"] = left
            if left < 0:
                res["notes"].append("Registration has passed its expiry date (may be in grace/redemption).")
            elif left <= 60:
                res["notes"].append(f"Expires in {left} days.")
    elif wb:
        y, m, d = age_parts(wb, now)
        res["notes"].append("Registry lookup failed; only the Wayback first-seen date is available (a minimum age, not the registration date).")

    if res["ok"] and (wb or wb_note == "no captures found"):
        with _cache_lock:
            _cache[domain] = (time.time(), dict(res))
    return res
