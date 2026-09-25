"""
DNS Lookup & WHOIS  -  Flask app
Everything about a domain in one screen: DNS records, registration (RDAP / WHOIS)
and email trust (SPF, DMARC, DKIM) - explained in plain English.

  * DNS records via DNS-over-HTTPS (Google, Cloudflare fallback): A, AAAA, CNAME,
    MX, NS, TXT, CAA, SOA + DNSSEC
  * Registration via RDAP (the official WHOIS replacement), classic WHOIS fallback
  * Email trust: SPF (with 10-lookup count), DMARC policy, DKIM auto-detect
  * Hosting / CDN, DNS provider and email provider detection
  * Search Console / Bing / Meta and other verification records detected
  * Health score + issues & fixes, CSV export

Built by Mahalakshmi Marimuthu - Digital Marketing Strategist & AI-Powered SEO Expert
"""
import ipaddress
import re
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
import tldextract
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

TIMEOUT = 8
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
DOH = [
    ("https://dns.google/resolve", {}),
    ("https://cloudflare-dns.com/dns-query", {"Accept": "application/dns-json"}),
]
TYPE_NUM = {1: "A", 2: "NS", 5: "CNAME", 6: "SOA", 12: "PTR", 15: "MX", 16: "TXT",
            28: "AAAA", 43: "DS", 257: "CAA"}
DKIM_SELECTORS = ["google", "selector1", "selector2", "default", "k1", "s1", "s2", "dkim", "mail", "smtp"]

# offline public-suffix list (bundled snapshot - no network needed)
EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)

MEANING = {
    "A": "IPv4 address the domain points to (where the website is served from).",
    "AAAA": "IPv6 address the domain points to.",
    "CNAME": "Alias - this name points to another hostname.",
    "MX": "Mail server that receives email for this domain (lower number = tried first).",
    "NS": "Nameserver that hosts this domain's DNS.",
    "TXT": "Text record - used for SPF, DMARC, site verification and more.",
    "CAA": "Lists which certificate authorities may issue SSL certificates.",
    "SOA": "Start of authority - primary nameserver, admin contact and zone serial.",
    "DS": "DNSSEC key fingerprint published at the registry.",
}

EPP = {
    "clienttransferprohibited": ("ok", "Transfer lock is on - the domain can't be moved to another registrar without you."),
    "servertransferprohibited": ("ok", "Registry-level transfer lock."),
    "clientdeleteprohibited": ("ok", "Delete lock - the domain can't be deleted by mistake."),
    "serverdeleteprohibited": ("ok", "Registry-level delete lock."),
    "clientupdateprohibited": ("ok", "Update lock - details can't be changed without unlocking."),
    "serverupdateprohibited": ("ok", "Registry-level update lock."),
    "clientrenewprohibited": ("warn", "The registrar has blocked renewals."),
    "serverrenewprohibited": ("warn", "The registry has blocked renewals."),
    "clienthold": ("error", "On hold - the domain is NOT published in DNS, so the site and email are down."),
    "serverhold": ("error", "Registry hold - the domain is NOT published in DNS."),
    "active": ("ok", "Active and working normally."),
    "ok": ("ok", "Active with no pending actions."),
    "redemptionperiod": ("error", "Expired and in redemption - it can still be recovered, at a fee."),
    "pendingdelete": ("error", "About to be deleted and released to the public."),
    "pendingtransfer": ("warn", "A transfer to another registrar is in progress."),
    "autorenewperiod": ("info", "Recently auto-renewed."),
    "addperiod": ("info", "Newly registered (grace period)."),
    "renewperiod": ("info", "Recently renewed."),
    "transferperiod": ("info", "Recently transferred."),
    "inactive": ("warn", "Registered but has no nameservers."),
}

CNAME_HOSTS = [
    ("cloudfront.net", "Amazon CloudFront"), ("akamaiedge.net", "Akamai"), ("edgekey.net", "Akamai"),
    ("edgesuite.net", "Akamai"), ("akamaized.net", "Akamai"), ("fastly.net", "Fastly"),
    ("fastlylb.net", "Fastly"), ("cdn.cloudflare.net", "Cloudflare"), ("azureedge.net", "Azure CDN"),
    ("azurefd.net", "Azure Front Door"), ("azurewebsites.net", "Azure App Service"),
    ("vercel-dns.com", "Vercel"), ("vercel.app", "Vercel"), ("netlify", "Netlify"),
    ("github.io", "GitHub Pages"), ("herokudns.com", "Heroku"), ("herokuapp.com", "Heroku"),
    ("myshopify.com", "Shopify"), ("shops.myshopify.com", "Shopify"), ("wpengine.com", "WP Engine"),
    ("squarespace.com", "Squarespace"), ("wixdns.net", "Wix"), ("ghs.googlehosted.com", "Google"),
    ("incapdns.net", "Imperva"), ("sucuri.net", "Sucuri"), ("onrender.com", "Render"),
    ("elb.amazonaws.com", "AWS Load Balancer"), ("b-cdn.net", "Bunny CDN"), ("hubspot", "HubSpot"),
    ("webflow.io", "Webflow"), ("pages.dev", "Cloudflare Pages"), ("firebaseapp.com", "Firebase"),
]
NS_PROVIDERS = [
    ("cloudflare.com", "Cloudflare"), ("awsdns", "Amazon Route 53"), ("azure-dns", "Azure DNS"),
    ("googledomains.com", "Google"), ("ns-cloud", "Google Cloud DNS"), ("domaincontrol.com", "GoDaddy"),
    ("akam.net", "Akamai"), ("ultradns", "UltraDNS"), ("dnsmadeeasy", "DNS Made Easy"),
    ("nsone.net", "NS1"), ("registrar-servers.com", "Namecheap"), ("dns-parking.com", "Hostinger"),
    ("hostinger", "Hostinger"), ("digitalocean.com", "DigitalOcean"), ("vercel-dns.com", "Vercel"),
    ("netlify", "Netlify"), ("wixdns.net", "Wix"), ("squarespacedns", "Squarespace"),
    ("bluehost.com", "Bluehost"), ("hostgator", "HostGator"), ("zoho.com", "Zoho"),
    ("dynect.net", "Oracle Dyn"), ("name.com", "Name.com"), ("ovh.net", "OVH"),
    ("hetzner", "Hetzner"), ("linode.com", "Linode"), ("bigrock", "BigRock"), ("dnsimple", "DNSimple"),
]
MX_PROVIDERS = [
    ("google.com", "Google Workspace"), ("googlemail.com", "Google Workspace"),
    ("outlook.com", "Microsoft 365"), ("protection.outlook", "Microsoft 365"),
    ("zoho", "Zoho Mail"), ("pphosted.com", "Proofpoint"), ("ppe-hosted.com", "Proofpoint"),
    ("mimecast", "Mimecast"), ("secureserver.net", "GoDaddy"), ("amazonaws.com", "Amazon SES / WorkMail"),
    ("mailgun.org", "Mailgun"), ("sendgrid.net", "SendGrid"), ("yandex", "Yandex Mail"),
    ("icloud.com", "iCloud Mail"), ("protonmail", "Proton Mail"), ("titan.email", "Titan Mail"),
    ("messagelabs.com", "Broadcom Email Security"), ("barracuda", "Barracuda"), ("iphmx.com", "Cisco Secure Email"),
    ("fireeyecloud", "Trellix"), ("hostinger", "Hostinger Mail"), ("rediffmail", "Rediffmail"),
]
IP_OWNERS = [
    ("CLOUDFLARE", "Cloudflare"), ("AMAZON", "Amazon Web Services"), ("AWS", "Amazon Web Services"),
    ("GOOGLE", "Google Cloud"), ("MICROSOFT", "Microsoft Azure"), ("MSFT", "Microsoft Azure"),
    ("AKAMAI", "Akamai"), ("LINODE", "Akamai (Linode)"), ("FASTLY", "Fastly"),
    ("DIGITALOCEAN", "DigitalOcean"), ("HETZNER", "Hetzner"), ("OVH", "OVHcloud"),
    ("GODADDY", "GoDaddy"), ("HOSTINGER", "Hostinger"), ("CONTABO", "Contabo"),
    ("VERCEL", "Vercel"), ("GITHUB", "GitHub"), ("SHOPIFY", "Shopify"), ("AUTOMATTIC", "WordPress.com"),
    ("ORACLE", "Oracle Cloud"), ("ALIBABA", "Alibaba Cloud"), ("INCAPSULA", "Imperva"),
    ("UNIFIEDLAYER", "Bluehost / Newfold"), ("NETLIFY", "Netlify"), ("TENCENT", "Tencent Cloud"),
]
VERIFY = [
    ("google-site-verification=", "Google Search Console"),
    ("msvalidate.01=", "Bing Webmaster Tools"),
    ("ms=", "Microsoft 365"),
    ("facebook-domain-verification=", "Meta (Facebook) Business"),
    ("apple-domain-verification=", "Apple"),
    ("yandex-verification", "Yandex Webmaster"),
    ("pinterest-site-verification=", "Pinterest"),
    ("atlassian-domain-verification=", "Atlassian"),
    ("docusign=", "DocuSign"),
    ("adobe-idp-site-verification=", "Adobe"),
    ("adobe-sign-verification=", "Adobe Sign"),
    ("zoom_verify_", "Zoom"),
    ("stripe-verification=", "Stripe"),
    ("globalsign-domain-verification=", "GlobalSign SSL"),
    ("openai-domain-verification=", "OpenAI"),
    ("canva-site-verification=", "Canva"),
    ("hubspot-developer-verification=", "HubSpot"),
    ("slack-domain-verification=", "Slack"),
    ("dropbox-domain-verification=", "Dropbox"),
    ("zoho-verification=", "Zoho"),
    ("cisco-ci-domain-verification=", "Cisco Webex"),
    ("have-i-been-pwned-verification=", "Have I Been Pwned"),
    ("miro-verification=", "Miro"),
    ("notion-domain-verification=", "Notion"),
    ("postman-domain-verification=", "Postman"),
    ("mailru-verification", "Mail.ru"),
    ("brevo-code:", "Brevo"),
]
SPF_SERVICES = [
    ("_spf.google.com", "Google Workspace"), ("spf.protection.outlook.com", "Microsoft 365"),
    ("sendgrid.net", "SendGrid"), ("mailgun.org", "Mailgun"), ("amazonses.com", "Amazon SES"),
    ("zoho", "Zoho"), ("servers.mcsv.net", "Mailchimp"), ("mandrillapp.com", "Mailchimp Transactional"),
    ("_spf.salesforce.com", "Salesforce"), ("hubspotemail.net", "HubSpot"), ("zendesk.com", "Zendesk"),
    ("freshdesk", "Freshdesk"), ("sparkpostmail.com", "SparkPost"), ("mailjet", "Mailjet"),
    ("netcorecloud", "Netcore"), ("pepipost", "Netcore Pepipost"), ("sendinblue", "Brevo"),
    ("brevo", "Brevo"), ("secureserver.net", "GoDaddy"), ("pphosted.com", "Proofpoint"),
    ("mimecast", "Mimecast"), ("mktomail.com", "Marketo"), ("exacttarget.com", "Salesforce Marketing Cloud"),
    ("postmarkapp.com", "Postmark"), ("_spf.atlassian.net", "Atlassian"), ("helpscoutemail.com", "Help Scout"),
]


# --------------------------------------------------------------------------- #
# DNS over HTTPS
# --------------------------------------------------------------------------- #
def doh(name, rtype):
    """Return {'ok', 'status', 'ad', 'answers':[{name,type,ttl,data}]}"""
    for url, headers in DOH:
        try:
            r = requests.get(url, params={"name": name, "type": rtype},
                             headers={"User-Agent": UA, **headers}, timeout=TIMEOUT)
            if r.status_code != 200:
                continue
            j = r.json()
            ans = []
            for a in j.get("Answer") or []:
                t = TYPE_NUM.get(a.get("type"))
                if not t:
                    continue
                ans.append({"name": str(a.get("name", "")).rstrip("."), "type": t,
                            "ttl": a.get("TTL"), "data": clean_data(t, str(a.get("data", "")))})
            return {"ok": True, "status": j.get("Status"), "ad": bool(j.get("AD")), "answers": ans}
        except (requests.exceptions.RequestException, ValueError):
            continue
    return {"ok": False, "status": None, "ad": False, "answers": []}


def txt_join(data):
    parts = re.findall(r'"((?:[^"\\]|\\.)*)"', data)
    s = "".join(parts) if parts else data
    return s.replace('\\"', '"').replace("\\\\", "\\")


def caa_decode(data):
    """Cloudflare may return CAA as RFC 3597 hex: '\\# 19 00 05 69 73 73 75 65 ...'"""
    m = re.match(r"^\\#\s+\d+\s+([0-9a-fA-F\s]+)$", data.strip())
    if not m:
        return data
    try:
        b = bytes.fromhex(m.group(1).replace(" ", ""))
        flags, tlen = b[0], b[1]
        tag = b[2:2 + tlen].decode()
        val = b[2 + tlen:].decode(errors="replace")
        return f'{flags} {tag} "{val}"'
    except (ValueError, IndexError):
        return data


def clean_data(t, data):
    if t == "TXT":
        return txt_join(data)
    if t == "CAA":
        return caa_decode(data)
    if t in ("CNAME", "NS", "PTR"):
        return data.rstrip(".").lower()
    if t == "MX":
        p = data.split()
        if len(p) == 2:
            return f"{p[0]} {p[1].rstrip('.').lower()}"
    if t == "SOA":
        return " ".join(x.rstrip(".") if i < 2 else x for i, x in enumerate(data.split()))
    return data


# --------------------------------------------------------------------------- #
# Input
# --------------------------------------------------------------------------- #
def parse_input(raw):
    raw = (raw or "").strip().lower()
    if not raw:
        return None, "Please enter a domain name."
    if "://" not in raw:
        raw = "http://" + raw
    host = (urlparse(raw).hostname or "").strip(".")
    if not host:
        return None, "That doesn't look like a domain name."
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None, "That domain name has characters that aren't allowed."
    try:
        ipaddress.ip_address(host)
        return None, "Please enter a domain name (like example.com), not an IP address."
    except ValueError:
        pass
    if not re.match(r"^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?)+$", host):
        return None, "That doesn't look like a valid domain name."
    ex = EXTRACT(host)
    if not ex.suffix or not ex.domain:
        return None, "Please enter a full domain with its extension, like example.com."
    apex = f"{ex.domain}.{ex.suffix}"
    return {"host": host, "apex": apex, "tld": ex.suffix.split(".")[-1]}, None


# --------------------------------------------------------------------------- #
# RDAP / WHOIS
# --------------------------------------------------------------------------- #
_BOOT = {"data": None}
_BOOT_LOCK = threading.Lock()


def rdap_base(tld):
    with _BOOT_LOCK:
        if _BOOT["data"] is None:
            try:
                j = requests.get("https://data.iana.org/rdap/dns.json", timeout=TIMEOUT,
                                 headers={"User-Agent": UA}).json()
                m = {}
                for tlds, urls in j.get("services", []):
                    u = next((x for x in urls if x.startswith("https")), urls[0] if urls else None)
                    for t in tlds:
                        m[t.lower()] = u
                _BOOT["data"] = m
            except (requests.exceptions.RequestException, ValueError):
                return None
    return _BOOT["data"].get(tld)


def rdap_get(url):
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept": "application/rdap+json, application/json"},
                         timeout=TIMEOUT, allow_redirects=True)
        if r.status_code == 404:
            return 404
        if r.status_code != 200:
            return None
        return r.json()
    except (requests.exceptions.RequestException, ValueError):
        return None


def vcard(entity):
    out = {}
    try:
        for item in entity.get("vcardArray", [None, []])[1]:
            k, _, _, v = item[0], item[1], item[2], item[3]
            if k in ("fn", "org", "email", "tel") and k not in out:
                out[k] = v if isinstance(v, str) else (v[0] if isinstance(v, list) and v else "")
            if k == "adr" and "country" not in out:
                lab = item[1].get("label") if isinstance(item[1], dict) else None
                if isinstance(v, list) and len(v) >= 7 and v[6]:
                    out["country"] = v[6]
                elif lab:
                    out["country"] = str(lab).splitlines()[-1]
    except (TypeError, IndexError, AttributeError):
        pass
    return out


def is_redacted(s):
    return bool(s) and bool(re.search(r"redact|privacy|private|protected|withheld|gdpr|not disclosed|data protected",
                                      str(s), re.I))


def parse_rdap(j):
    info = {"source": "RDAP", "domain": (j.get("ldhName") or "").lower(), "nameservers": [],
            "status": [], "registrar": "", "registrar_id": "", "registrar_url": "", "abuse_email": "",
            "abuse_phone": "", "created": "", "updated": "", "expires": "", "registrant": "",
            "registrant_country": "", "dnssec": None, "related": ""}
    for e in j.get("events", []):
        a, d = e.get("eventAction", ""), e.get("eventDate", "")
        if a == "registration":
            info["created"] = d
        elif a == "expiration":
            info["expires"] = d
        elif a == "last changed":
            info["updated"] = d
    info["status"] = [s for s in j.get("status", [])]
    info["nameservers"] = sorted({(n.get("ldhName") or "").lower().rstrip(".") for n in j.get("nameservers", [])} - {""})
    sd = j.get("secureDNS") or {}
    if "delegationSigned" in sd:
        info["dnssec"] = bool(sd["delegationSigned"])
    for ent in j.get("entities", []):
        roles = ent.get("roles", [])
        vc = vcard(ent)
        if "registrar" in roles:
            info["registrar"] = vc.get("fn") or vc.get("org") or ""
            for p in ent.get("publicIds", []):
                if "iana" in str(p.get("type", "")).lower():
                    info["registrar_id"] = p.get("identifier", "")
            for l in ent.get("links", []):
                if l.get("href", "").startswith("http") and "rdap" not in l.get("href", ""):
                    info["registrar_url"] = l["href"]
            if ent.get("url"):
                info["registrar_url"] = ent["url"]
            for sub in ent.get("entities", []):
                if "abuse" in sub.get("roles", []):
                    sv = vcard(sub)
                    info["abuse_email"] = sv.get("email", "")
                    info["abuse_phone"] = sv.get("tel", "").replace("tel:", "")
        if "registrant" in roles:
            info["registrant"] = vc.get("org") or vc.get("fn") or ""
            info["registrant_country"] = vc.get("country", "")
    for l in j.get("links", []):
        if l.get("rel") == "related" and "rdap" in str(l.get("type", "")) + str(l.get("href", "")):
            info["related"] = l.get("href", "")
    return info


def whois43(server, query):
    try:
        with socket.create_connection((server, 43), timeout=TIMEOUT) as s:
            s.sendall((query + "\r\n").encode())
            chunks = []
            while True:
                c = s.recv(4096)
                if not c:
                    break
                chunks.append(c)
                if sum(len(x) for x in chunks) > 200_000:
                    break
        return b"".join(chunks).decode("utf-8", errors="replace")
    except OSError:
        return ""


def whois_lookup(apex, tld):
    iana = whois43("whois.iana.org", tld)
    m = re.search(r"^\s*whois:\s*(\S+)", iana, re.M | re.I)
    if not m:
        return None
    text = whois43(m.group(1), apex)
    if not text or re.search(r"no match|not found|no data found|no entries found|status:\s*free|is available",
                             text, re.I):
        return 404 if text else None

    def grab(*pats):
        for p in pats:
            mm = re.search(r"^\s*" + p + r"\s*:\s*(.+)$", text, re.M | re.I)
            if mm and mm.group(1).strip():
                return mm.group(1).strip()
        return ""
    info = {"source": "WHOIS", "domain": apex, "registrar": grab("Registrar", "Sponsoring Registrar", "registrar name"),
            "registrar_id": grab("Registrar IANA ID"), "registrar_url": grab("Registrar URL"),
            "abuse_email": grab("Registrar Abuse Contact Email"),
            "abuse_phone": grab("Registrar Abuse Contact Phone"),
            "created": grab("Creation Date", "Created", "created on", "Registered on", "Registration Time", "registered"),
            "updated": grab("Updated Date", "Last Modified", "last-update", "changed", "Last updated"),
            "expires": grab("Registry Expiry Date", "Expiry Date", "Expiration Date", "Expires", "paid-till",
                            "expire", "Registrar Registration Expiration Date"),
            "registrant": grab("Registrant Organization", "Registrant Name", "org", "registrant"),
            "registrant_country": grab("Registrant Country", "Registrant State/Province"),
            "nameservers": sorted({x.strip().lower().rstrip(".").split()[0] for x in
                                   re.findall(r"^\s*(?:Name Server|nserver|Nameservers?)\s*:\s*(\S.*)$", text, re.M | re.I)
                                   if x.strip()}),
            "status": [x.split()[0] for x in re.findall(r"^\s*(?:Domain )?Status\s*:\s*(\S+)", text, re.M | re.I)],
            "dnssec": None, "related": ""}
    ds = grab("DNSSEC")
    if ds:
        info["dnssec"] = not ds.lower().startswith("unsigned") and ds.lower() not in ("no", "inactive")
    return info


def registration(apex, tld):
    base = rdap_base(tld)
    data = None
    if base:
        data = rdap_get(base.rstrip("/") + "/domain/" + apex)
    if data is None:
        data = rdap_get("https://rdap.org/domain/" + apex)
    if isinstance(data, dict):
        info = parse_rdap(data)
        # thin registries (.com/.net): the registrar's RDAP server may hold registrant details
        if info["related"] and (not info["registrant"] or not info["abuse_email"]):
            rj = rdap_get(info["related"])
            if isinstance(rj, dict):
                ri = parse_rdap(rj)
                for k in ("registrant", "registrant_country", "abuse_email", "abuse_phone", "registrar_url"):
                    if not info[k] and ri.get(k):
                        info[k] = ri[k]
        return info
    w = whois_lookup(apex, tld)
    if isinstance(w, dict):
        return w
    if data == 404 or w == 404:
        return {"source": "RDAP", "not_found": True}
    return None


def ip_owner(ip):
    j = rdap_get("https://rdap.org/ip/" + ip)
    if not isinstance(j, dict):
        return {}
    name = j.get("name", "") or ""
    org = ""
    for ent in j.get("entities", []):
        if "registrant" in ent.get("roles", []) or "administrative" in ent.get("roles", []):
            v = vcard(ent)
            org = v.get("org") or v.get("fn") or ""
            if org:
                break
    return {"network": name, "org": org, "country": j.get("country", "")}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def parse_date(s):
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%Y.%m.%d", "%d.%m.%Y", "%Y/%m/%d"):
        try:
            d = datetime.strptime(s[:len(s)], fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return None


def age_text(d, now):
    days = (now - d).days
    y, rem = divmod(days, 365)
    mo = rem // 30
    if y:
        return f"{y} yr{'s' if y != 1 else ''} {mo} mo"
    if mo:
        return f"{mo} month{'s' if mo != 1 else ''}"
    return f"{days} day{'s' if days != 1 else ''}"


def match_first(value, table):
    v = (value or "").lower()
    for needle, label in table:
        if needle.lower() in v:
            return label
    return ""


def spf_analyse(record, apex):
    """Count DNS lookups (limit 10) by walking include/redirect recursively."""
    seen, count, services, errors = set(), [0], [], []

    def walk(rec, depth):
        if depth > 6:
            return
        for term in rec.split()[1:]:
            t = term.lstrip("+-~?").lower()
            key = t.split(":", 1)[0].split("=", 1)[0].split("/", 1)[0]
            if key in ("include", "a", "mx", "ptr", "exists", "redirect"):
                count[0] += 1
            if key in ("include", "redirect"):
                target = t.split(":", 1)[1] if ":" in t else t.split("=", 1)[1] if "=" in t else ""
                svc = match_first(target, SPF_SERVICES)
                if svc and svc not in services:
                    services.append(svc)
                if target and target not in seen and len(seen) < 25:
                    seen.add(target)
                    res = doh(target, "TXT")
                    subs = [a["data"] for a in res["answers"] if a["type"] == "TXT" and a["data"].lower().startswith("v=spf1")]
                    if subs:
                        walk(subs[0], depth + 1)
                    elif res["ok"]:
                        errors.append(target)
    walk(record, 0)
    m = re.search(r"([+\-~?]?)all\b", record.lower())
    qual = m.group(1) or "+" if m else None
    return {"lookups": count[0], "services": services, "broken_includes": errors, "all": qual}


# --------------------------------------------------------------------------- #
# Main lookup
# --------------------------------------------------------------------------- #
def lookup(target, selector=""):
    host, apex, tld = target["host"], target["apex"], target["tld"]
    www = "www." + apex
    jobs = [(apex, "A"), (apex, "AAAA"), (apex, "MX"), (apex, "NS"), (apex, "TXT"),
            (apex, "CAA"), (apex, "SOA"), (apex, "DS"), ("_dmarc." + apex, "TXT"),
            ("default._bimi." + apex, "TXT"), (www, "A"), (www, "AAAA")]
    if host not in (apex, www):
        jobs += [(host, "A"), (host, "AAAA")]
    sels = [selector] if selector else []
    sels += [s for s in DKIM_SELECTORS if s != selector]
    jobs += [(f"{s}._domainkey.{apex}", "TXT") for s in sels]

    with ThreadPoolExecutor(max_workers=16) as ex:
        reg_future = ex.submit(registration, apex, tld)
        results = dict(zip(jobs, ex.map(lambda j: doh(*j), jobs)))
        reg = reg_future.result()

    if not any(r["ok"] for r in results.values()):
        return {"ok": False, "error": "Couldn't reach the DNS servers right now - please try again in a minute."}

    soa_res = results[(apex, "SOA")]
    ns_res = results[(apex, "NS")]
    nxdomain = soa_res["status"] == 3 and ns_res["status"] == 3
    if nxdomain and (reg is None or reg.get("not_found")):
        return {"ok": False, "error": f"{apex} doesn't exist in DNS and has no registration record - "
                                      f"it may not be registered (it could even be available to buy)."}

    # ---- records table (deduplicated, CNAME chains kept)
    records, seen = [], set()
    main_jobs = [j for j in jobs if "._domainkey." not in j[0] and not j[0].startswith("default._bimi")]
    for j in main_jobs:
        for a in results[j]["answers"]:
            key = (a["name"], a["type"], a["data"])
            if key in seen:
                continue
            seen.add(key)
            records.append({**a, "meaning": MEANING.get(a["type"], "")})
    order = ["A", "AAAA", "CNAME", "MX", "NS", "TXT", "CAA", "SOA", "DS"]
    records.sort(key=lambda r: (order.index(r["type"]) if r["type"] in order else 99,
                                0 if r["name"] == apex else 1, r["name"], r["data"]))

    def data_of(name, t):
        return [a["data"] for a in results.get((name, t), {"answers": []})["answers"] if a["type"] == t and a["name"] == name]

    def ips_for(name):
        ans = results.get((name, "A"), {"answers": []})["answers"] + results.get((name, "AAAA"), {"answers": []})["answers"]
        return [a["data"] for a in ans if a["type"] in ("A", "AAAA")]

    def cnames_for(name):
        ans = results.get((name, "A"), {"answers": []})["answers"]
        return [a["data"] for a in ans if a["type"] == "CNAME"]

    apex_ips, www_ips = ips_for(apex), ips_for(www)
    host_ips = ips_for(host) if host not in (apex, www) else (apex_ips if host == apex else www_ips)
    mx = sorted(data_of(apex, "MX"), key=lambda x: int(x.split()[0]) if x.split()[0].isdigit() else 99)
    ns = sorted(data_of(apex, "NS"))
    txt = data_of(apex, "TXT")
    caa = data_of(apex, "CAA")
    ds = data_of(apex, "DS")

    # ---- providers
    chain = cnames_for(host)
    cdn = next((match_first(c, CNAME_HOSTS) for c in chain if match_first(c, CNAME_HOSTS)), "")
    first_ip = next((ip for ip in host_ips + apex_ips + www_ips if ":" not in ip), None) or \
        next(iter(host_ips + apex_ips + www_ips), None)
    owner = ip_owner(first_ip) if first_ip else {}
    owner_label = match_first(owner.get("network", "") + " " + owner.get("org", ""), IP_OWNERS)
    hosting = cdn or owner_label or owner.get("org") or owner.get("network") or ""
    dns_provider = next((match_first(n, NS_PROVIDERS) for n in ns if match_first(n, NS_PROVIDERS)), "")
    email_provider = next((match_first(m, MX_PROVIDERS) for m in mx if match_first(m, MX_PROVIDERS)), "")
    if not email_provider and mx:
        email_provider = "Custom / self-hosted"

    # ---- email trust
    spf_recs = [t for t in txt if t.lower().startswith("v=spf1")]
    dmarc_recs = [t for t in data_of("_dmarc." + apex, "TXT") if t.lower().startswith("v=dmarc1")]
    bimi = [t for t in data_of("default._bimi." + apex, "TXT") if t.lower().startswith("v=bimi1")]
    dkim_found = []
    for s in sels:
        for t in data_of(f"{s}._domainkey.{apex}", "TXT"):
            if "p=" in t.lower() or t.lower().startswith("v=dkim1"):
                dkim_found.append({"selector": s, "record": t})
                break
    spf = spf_analyse(spf_recs[0], apex) if len(spf_recs) == 1 else None
    dmarc = {}
    if dmarc_recs:
        tags = dict(x.strip().split("=", 1) for x in dmarc_recs[0].split(";") if "=" in x)
        tags = {k.strip().lower(): v.strip() for k, v in tags.items()}
        dmarc = {"p": tags.get("p", "").lower(), "sp": tags.get("sp", "").lower(), "rua": tags.get("rua", ""),
                 "pct": tags.get("pct", "100")}

    # ---- verifications & services
    verifications = []
    for t in txt:
        lab = next((l for n, l in VERIFY if t.lower().startswith(n)), "")
        if lab and lab not in [v["name"] for v in verifications]:
            verifications.append({"name": lab, "record": t})

    # ---- registration summary
    now = datetime.now(timezone.utc)
    reg_out = None
    if reg and not reg.get("not_found"):
        created, expires, updated = parse_date(reg.get("created")), parse_date(reg.get("expires")), parse_date(reg.get("updated"))
        registrant = reg.get("registrant", "")
        reg_out = {**reg,
                   "created_fmt": created.strftime("%d %b %Y") if created else "",
                   "expires_fmt": expires.strftime("%d %b %Y") if expires else "",
                   "updated_fmt": updated.strftime("%d %b %Y") if updated else "",
                   "age": age_text(created, now) if created else "",
                   "age_days": (now - created).days if created else None,
                   "days_left": (expires - now).days if expires else None,
                   "registrant": "Redacted for privacy" if (not registrant or is_redacted(registrant)) else registrant,
                   "status_info": [{"code": s, "sev": EPP.get(re.sub(r"[^a-z]", "", s.lower()), ("info", ""))[0],
                                    "meaning": EPP.get(re.sub(r"[^a-z]", "", s.lower()), ("info", "Registry status code."))[1]}
                                   for s in reg.get("status", [])]}
    dnssec = bool(ds) or bool(reg_out and reg_out.get("dnssec"))

    # ---- checks
    checks = []
    add = lambda sev, area, title, detail: checks.append({"sev": sev, "area": area, "title": title, "detail": detail})

    if nxdomain:
        add("fail", "DNS", "Domain doesn't resolve",
            f"{apex} returns NXDOMAIN - it has no DNS at all, so the website and email can't work.")
    if not apex_ips and not www_ips:
        if not nxdomain:
            add("fail", "DNS", "No website address (A / AAAA record)",
                f"Neither {apex} nor {www} points to a server, so the website can't load.")
    else:
        if not apex_ips:
            add("warn", "DNS", f"{apex} (without www) doesn't resolve",
                f"Visitors typing {apex} get an error. Add an A record (or ALIAS / CNAME flattening) and redirect it to {www}.")
        elif not www_ips:
            add("warn", "DNS", f"{www} doesn't resolve",
                f"Many people type www. Add a CNAME for www pointing to {apex} and 301-redirect it to your main version.")
        else:
            add("pass", "DNS", "Both www and non-www resolve", "Make sure one 301-redirects to the other so Google sees one version.")
    if ns:
        if len(ns) < 2:
            add("warn", "DNS", "Only one nameserver", "Use at least two nameservers so DNS keeps working if one fails.")
        else:
            add("pass", "DNS", f"{len(ns)} nameservers", ", ".join(ns))
    if dnssec:
        add("pass", "Security", "DNSSEC is on", "DNS answers are signed, which protects visitors from spoofed DNS.")
    elif not nxdomain:
        add("info", "Security", "DNSSEC is off",
            "Turning on DNSSEC at your DNS provider and registrar stops attackers from faking your DNS answers.")
    if caa:
        add("pass", "Security", "CAA record set", "Only the listed certificate authorities can issue SSL for this domain.")
    elif not nxdomain:
        add("info", "Security", "No CAA record",
            "Add a CAA record (e.g. 0 issue \"letsencrypt.org\") so only your chosen SSL provider can issue certificates.")

    # email
    if not mx:
        add("info", "Email", "No MX record", "This domain can't receive email. That's fine if it's intentional.")
    if len(spf_recs) > 1:
        add("fail", "Email", "More than one SPF record",
            "Only one v=spf1 record is allowed - with two, SPF fails for every email. Merge them into one.")
    elif not spf_recs:
        add("fail" if mx else "warn", "Email", "No SPF record",
            "Anyone can send email pretending to be you. Add a TXT record starting with v=spf1 listing your mail senders"
            + ("." if mx else " - or \"v=spf1 -all\" if the domain never sends email."))
    elif spf:
        if spf["all"] == "+":
            add("fail", "Email", "SPF allows everyone (+all)", "“+all” lets any server send as you. Change it to ~all or -all.")
        elif spf["all"] == "?":
            add("warn", "Email", "SPF is neutral (?all)", "“?all” gives no protection. Use ~all or -all.")
        elif spf["all"] is None:
            add("warn", "Email", "SPF has no “all” ending", "End the record with ~all or -all so unlisted senders are rejected.")
        if spf["lookups"] > 10:
            add("fail", "Email", f"SPF needs {spf['lookups']} DNS lookups (limit 10)",
                "Receivers stop at 10 lookups and treat SPF as failed. Remove unused includes or flatten the record.")
        for b in spf["broken_includes"]:
            add("warn", "Email", "SPF include has no SPF record", f"include:{b} returns no v=spf1 record - remove or fix it.")
        if spf["all"] in ("-", "~") and spf["lookups"] <= 10:
            add("pass", "Email", "SPF record is valid", f"{spf['lookups']} of 10 DNS lookups used, ends with {spf['all']}all.")
    if not dmarc_recs:
        add("fail" if mx else "warn", "Email", "No DMARC record",
            f"Add a TXT record at _dmarc.{apex}, e.g. v=DMARC1; p=quarantine; rua=mailto:dmarc@{apex}. "
            "Gmail and Yahoo require DMARC for bulk senders.")
    elif len(dmarc_recs) > 1:
        add("fail", "Email", "More than one DMARC record", "Receivers ignore DMARC when there are two. Keep one.")
    else:
        if dmarc["p"] == "none":
            add("warn", "Email", "DMARC is monitoring only (p=none)",
                "It reports spoofing but doesn't stop it. Move to p=quarantine, then p=reject, once reports look clean.")
        elif dmarc["p"] in ("quarantine", "reject"):
            add("pass", "Email", f"DMARC enforced (p={dmarc['p']})", "Spoofed emails are sent to spam or rejected.")
        else:
            add("fail", "Email", "DMARC policy is missing or invalid", "The record needs p=none, p=quarantine or p=reject.")
        if not dmarc["rua"]:
            add("info", "Email", "DMARC has no report address", "Add rua=mailto:… to receive daily reports on who sends as you.")
    if mx or spf_recs:
        if dkim_found:
            add("pass", "Email", "DKIM found", "Selector: " + ", ".join(d["selector"] for d in dkim_found))
        elif selector:
            add("warn", "Email", f"No DKIM key at selector “{selector}”",
                f"Nothing found at {selector}._domainkey.{apex}. Check the selector name in your email provider.")
        else:
            add("info", "Email", "DKIM not found on common selectors",
                "DKIM may still be set up under a custom name. Enter your selector in Advanced options to check it.")

    # registration
    if reg_out:
        dl = reg_out["days_left"]
        if dl is not None:
            if dl < 0:
                add("fail", "Registration", "Domain has expired", f"It expired on {reg_out['expires_fmt']}. Renew it now before someone else registers it.")
            elif dl < 30:
                add("fail", "Registration", f"Expires in {dl} days", f"Renew before {reg_out['expires_fmt']} - an expired domain takes the site and email down.")
            elif dl < 60:
                add("warn", "Registration", f"Expires in {dl} days", "Renew soon, or switch on auto-renew at your registrar.")
            else:
                add("pass", "Registration", "Not expiring soon", f"Valid until {reg_out['expires_fmt']} ({dl} days left).")
        codes = [re.sub(r"[^a-z]", "", s.lower()) for s in reg_out.get("status", [])]
        for c, s in zip(codes, reg_out.get("status", [])):
            if EPP.get(c, ("",))[0] == "error":
                add("fail", "Registration", f"Status: {s}", EPP[c][1])
        if codes and reg_out["source"] == "RDAP" and "clienttransferprohibited" not in codes \
                and "servertransferprohibited" not in codes:
            add("warn", "Registration", "Transfer lock is off",
                "Turn on the registrar lock (clientTransferProhibited) so nobody can move your domain away.")
    elif reg is None:
        add("info", "Registration", "Registration data unavailable",
            "The registry didn't answer (some country domains don't publish RDAP / WHOIS). DNS results are still accurate.")

    score = 100
    for c in checks:
        score -= {"fail": 18, "warn": 6}.get(c["sev"], 0)
    score = max(0, min(100, score))

    counts = {t: sum(1 for r in records if r["type"] == t) for t in order}
    return {
        "ok": True, "domain": apex, "host": host, "checked_at": now.strftime("%d %b %Y, %H:%M UTC"),
        "score": score, "checks": checks, "records": records, "counts": counts,
        "ips": {"apex": apex_ips, "www": www_ips, "host": host_ips},
        "providers": {"hosting": hosting, "cdn": cdn, "ip": first_ip or "", "ip_owner": owner,
                      "dns": dns_provider, "email": email_provider},
        "email": {"mx": mx, "spf": spf_recs, "spf_info": spf, "dmarc": dmarc_recs, "dmarc_info": dmarc,
                  "dkim": dkim_found, "bimi": bimi, "selector": selector},
        "verifications": verifications,
        "registration": reg_out, "dnssec": dnssec,
    }


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/api/lookup", methods=["POST"])
def api_lookup():
    data = request.get_json(silent=True) or {}
    target, err = parse_input(data.get("domain", ""))
    if err:
        return jsonify({"ok": False, "error": err}), 400
    selector = re.sub(r"[^a-z0-9\-_.]", "", str(data.get("selector", "")).strip().lower())[:63]
    out = lookup(target, selector)
    return jsonify(out), (200 if out.get("ok") else 400)


@app.route("/")
def home():
    return Response(PAGE, mimetype="text/html")


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DNS Lookup &amp; WHOIS – Check DNS Records, Domain Registration &amp; Email Security</title>
<meta name="description" content="Free DNS Lookup and WHOIS tool: see every DNS record, who registered a domain and when it expires, plus SPF, DMARC and DKIM email checks, all explained in plain English.">
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
      <a href="#" class="sidebar-link active">🌐&nbsp; DNS Lookup &amp; WHOIS</a>
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
      <h1>🌐 DNS Lookup &amp; <span>WHOIS</span></h1>
    </div>

    <form class="card" id="form">
      <label class="lbl" for="domain">Domain name</label>
      <div class="row-inline">
        <input type="text" id="domain" placeholder="yourwebsite.com" autocomplete="off" spellcheck="false">
        <button class="btn" type="submit" id="runBtn">🔍 Look up</button>
      </div>
      <details class="adv">
        <summary>+ Advanced options</summary>
        <div class="adv-body">
          <div>
            <label class="lbl" for="selector">DKIM selector (optional). I already check common ones like google, selector1 and default.</label>
            <input type="text" id="selector" placeholder="e.g. s1024 or mandrill" autocomplete="off" spellcheck="false">
          </div>
        </div>
      </details>
      <div class="spinner" id="sp">Reading DNS records and registration data…</div>
      <div class="err" id="err"></div>
    </form>

    <div id="results" style="display:none">
      <div class="overview">
        <div class="chart-card">
          <h3>Domain health</h3>
          <div class="ring-wrap" id="ring"></div>
          <div style="display:flex;justify-content:center;gap:.4rem;margin-top:.8rem;flex-wrap:wrap" id="ringBadges"></div>
        </div>
        <div class="chart-card">
          <h3>At a glance</h3>
          <div class="metrics" id="metrics"></div>
          <div class="hint" style="overflow-wrap:anywhere" id="checked"></div>
        </div>
        <div class="chart-card bars">
          <h3>DNS records found</h3>
          <div class="bar-box"><canvas id="chart"></canvas></div>
        </div>
      </div>

      <div class="sec-title">🛠 Issues &amp; Fixes <span class="count" id="issueCount"></span></div>
      <div class="issues" id="issues"></div>

      <div class="sec-title">📇 Registration (WHOIS) <span class="count" id="regSrc"></span></div>
      <div class="tbl-wrap"><table><tbody id="regBody"></tbody></table></div>

      <div class="sec-title">📋 DNS Records <span class="count" id="recCount"></span></div>
      <div class="chips" id="chips"></div>
      <div class="tbl-tools">
        <input type="text" id="filter" placeholder="Filter by name or value…">
        <button type="button" class="btn ghost" id="csvBtn" style="margin-left:auto">⬇ Download CSV</button>
      </div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>Type</th><th>Name</th><th>Value</th><th>TTL</th></tr></thead>
          <tbody id="recBody"></tbody>
        </table>
      </div>

      <div class="sec-title">✉️ Email Trust <span class="count">can someone send email pretending to be you?</span></div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>Check</th><th>Status</th><th>Record</th></tr></thead>
          <tbody id="mailBody"></tbody>
        </table>
      </div>

      <div id="verWrap">
        <div class="sec-title">✅ Verified Services <span class="count">found in TXT records</span></div>
        <div class="tbl-wrap">
          <table>
            <thead><tr><th>Service</th><th>Record</th></tr></thead>
            <tbody id="verBody"></tbody>
          </table>
        </div>
      </div>
    </div>
  </main>
</div>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let DATA = null, chart = null, TYPE = 'ALL';
const ORDER = ['A','AAAA','CNAME','MX','NS','TXT','CAA','SOA','DS'];
const MEAN = {
  A: 'IPv4 address the site is served from', AAAA: 'IPv6 address the site is served from',
  CNAME: 'Alias to another hostname', MX: 'Mail server for this domain', NS: 'Nameserver hosting the DNS',
  TXT: 'Text record (SPF, verification…)', CAA: 'Allowed SSL certificate issuers', SOA: 'Zone authority details',
  DS: 'DNSSEC key fingerprint'
};

$('form').addEventListener('submit', async e => {
  e.preventDefault();
  const domain = $('domain').value.trim();
  $('err').textContent = '';
  if (!domain) { $('err').textContent = 'Please enter a domain name.'; return; }
  $('sp').classList.add('show'); $('runBtn').disabled = true;
  try {
    const r = await fetch('/api/lookup', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ domain, selector: $('selector').value.trim() }) });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'Something went wrong.');
    DATA = j; TYPE = 'ALL'; render();
    $('results').style.display = '';
  } catch (err) {
    $('err').textContent = err.message || 'Could not reach the server. Please try again.';
  } finally {
    $('sp').classList.remove('show'); $('runBtn').disabled = false;
  }
});

function render() {
  const d = DATA, reg = d.registration, p = d.providers;
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
  let exp = '—', expCls = '';
  if (reg && reg.days_left != null) {
    exp = reg.days_left < 0 ? 'Expired' : `${reg.days_left} days`;
    expCls = reg.days_left < 30 ? 'red' : reg.days_left < 60 ? 'orange' : 'mint';
  }
  $('metrics').innerHTML = [
    ['Registrar', reg && reg.registrar ? reg.registrar : '—', 'purple'],
    ['Domain age', reg && reg.age ? reg.age : '—', ''],
    ['Expires in', exp, expCls],
    ['Hosted on', p.hosting || '—', ''],
    ['Email provider', p.email || 'None', p.email ? '' : 'orange'],
    ['DNSSEC', d.dnssec ? 'On' : 'Off', d.dnssec ? 'mint' : 'orange'],
  ].map(([k, v, c]) => `<div class="metric"><div class="k">${k}</div><div class="v ${c}">${esc(v)}</div></div>`).join('');
  $('checked').innerHTML = `Checked: <span style="color:var(--text)">${esc(d.host)}</span> · ${esc(d.checked_at)}`;

  // chart
  const types = ORDER.filter(t => d.counts[t] > 0);
  if (typeof Chart !== 'undefined') {
    const data = { labels: types, datasets: [{ data: types.map(t => d.counts[t]), backgroundColor: '#7c6af7', borderRadius: 4, maxBarThickness: 22 }] };
    const opts = { indexAxis: 'y', maintainAspectRatio: false, plugins: { legend: { display: false } },
      scales: { x: { beginAtZero: true, ticks: { color: '#8888a8', precision: 0, font: { family: 'DM Mono', size: 10 } }, grid: { color: '#1d1d2c' } },
                y: { ticks: { color: '#e8e8f0', font: { family: 'DM Mono', size: 11 } }, grid: { display: false } } } };
    if (chart) { chart.data = data; chart.update(); } else chart = new Chart($('chart'), { type: 'bar', data, options: opts });
  }

  // issues
  const sevMap = { fail: 'error', warn: 'warn', info: 'info' };
  const label = { error: '✕ error', warn: '! warning', info: 'i info' };
  const order = { fail: 0, warn: 1, info: 2 };
  const issues = d.checks.filter(c => c.sev !== 'pass').sort((x, y) => order[x.sev] - order[y.sev]);
  $('issueCount').textContent = `${issues.length} found · ${d.checks.length - issues.length} passed`;
  $('issues').innerHTML = issues.length ? issues.map(c => `<div class="issue ${sevMap[c.sev]}"><div class="top">
      <span class="badge ${sevMap[c.sev]}">${label[sevMap[c.sev]]}</span><div class="msg">${esc(c.title)}</div>
      <span class="area-tag">${esc(c.area)}</span></div>
      <div class="fix">${esc(c.detail)}</div></div>`).join('')
    : '<div class="all-good">✓ No issues found. This domain is set up well.</div>';

  renderReg(); renderChips(); renderRecords(); renderMail();

  // verifications
  $('verWrap').style.display = d.verifications.length ? '' : 'none';
  $('verBody').innerHTML = d.verifications.map(v => `<tr><td><div class="code-pill">${esc(v.name)}</div></td>
      <td class="mono">${esc(v.record)}</td></tr>`).join('');
}

function kv(k, v, extra) {
  return `<tr><td style="width:190px;color:var(--muted);font-size:.78rem">${k}</td><td>${v || '<span style="color:var(--muted)">—</span>'}${extra ? `<div class="small">${extra}</div>` : ''}</td></tr>`;
}
function renderReg() {
  const r = DATA.registration, p = DATA.providers;
  if (!r) {
    $('regSrc').textContent = '';
    $('regBody').innerHTML = `<tr><td style="color:var(--muted)">The registry for this extension didn't share registration data. DNS results below are still accurate.</td></tr>`;
    return;
  }
  $('regSrc').textContent = `source: ${r.source}`;
  const st = (r.status_info || []).map(s => `<span class="badge ${s.sev === 'ok' ? 'ok' : s.sev === 'error' ? 'error' : s.sev}" title="${esc(s.meaning)}" style="margin:0 .3rem .3rem 0">${esc(s.code)}</span>`).join('');
  const stMean = (r.status_info || []).filter(s => s.meaning).map(s => `<b>${esc(s.code)}</b>: ${esc(s.meaning)}`).join('<br>');
  const own = p.ip_owner || {};
  $('regBody').innerHTML = [
    kv('Domain', `<span class="code-pill">${esc(r.domain || DATA.domain)}</span>`),
    kv('Registrar', esc(r.registrar), [r.registrar_id ? 'IANA ID ' + esc(r.registrar_id) : '', r.registrar_url ? `<a href="${esc(r.registrar_url)}" target="_blank" rel="noopener">${esc(r.registrar_url)}</a>` : ''].filter(Boolean).join(' · ')),
    kv('Registered on', esc(r.created_fmt), r.age ? esc(r.age) + ' old' : ''),
    kv('Expires on', esc(r.expires_fmt), r.days_left != null ? (r.days_left < 0 ? 'already expired' : esc(r.days_left) + ' days left') : ''),
    kv('Last updated', esc(r.updated_fmt)),
    kv('Registrant', esc(r.registrant) + (r.registrant_country ? ' · ' + esc(r.registrant_country) : ''),
       r.registrant === 'Redacted for privacy' ? 'Owner details are hidden under GDPR / privacy protection (normal for most domains).' : ''),
    kv('Status', st, stMean),
    kv('Nameservers', (r.nameservers || []).map(esc).join('<br>'), p.dns ? 'DNS provider: ' + esc(p.dns) : ''),
    kv('Hosting', esc(p.hosting), p.ip ? `IP ${esc(p.ip)}${own.network ? ' · network ' + esc(own.network) : ''}${own.country ? ' · ' + esc(own.country) : ''}` : ''),
    kv('DNSSEC', DATA.dnssec ? '<span class="badge ok">✓ signed</span>' : '<span class="badge warn">unsigned</span>'),
    kv('Abuse contact', esc([r.abuse_email, r.abuse_phone].filter(Boolean).join(' · '))),
  ].join('');
}

function renderChips() {
  const types = ['ALL'].concat(ORDER.filter(t => DATA.counts[t] > 0));
  $('chips').innerHTML = types.map(t => `<span class="chip ${t === TYPE ? 'active' : ''}" data-t="${t}">${t === 'ALL' ? 'All' : t} ${t === 'ALL' ? DATA.records.length : DATA.counts[t]}</span>`).join('');
  $('chips').querySelectorAll('.chip').forEach(c => c.onclick = () => { TYPE = c.dataset.t; renderChips(); renderRecords(); });
}
function renderRecords() {
  const q = $('filter').value.trim().toLowerCase();
  const rows = DATA.records.filter(r => (TYPE === 'ALL' || r.type === TYPE) && (!q || (r.name + ' ' + r.data).toLowerCase().includes(q)));
  $('recCount').textContent = `${DATA.records.length} records`;
  $('recBody').innerHTML = rows.length ? rows.map(r => `<tr>
      <td><span class="badge info" title="${esc(MEAN[r.type] || '')}">${esc(r.type)}</span></td>
      <td class="mono">${esc(r.name)}</td>
      <td class="mono">${esc(r.data)}<div class="small" style="font-family:Inter,sans-serif">${esc(MEAN[r.type] || '')}</div></td>
      <td class="mono">${r.ttl != null ? esc(r.ttl) + 's' : ''}</td></tr>`).join('')
    : '<tr><td colspan="4" style="color:var(--muted)">No records match.</td></tr>';
}
$('filter').addEventListener('input', renderRecords);

function renderMail() {
  const m = DATA.email, s = m.spf_info, dm = m.dmarc_info || {};
  const b = (cls, txt) => `<span class="badge ${cls}">${txt}</span>`;
  const rows = [];
  rows.push(['MX (receiving mail)', m.mx.length ? b('ok', '✓ ' + m.mx.length + ' server' + (m.mx.length > 1 ? 's' : '')) : b('info', 'none'),
    m.mx.join('\n'), DATA.providers.email ? 'Provider: ' + DATA.providers.email : 'This domain does not receive email.']);
  let spfB = b('error', '✕ missing'), spfN = 'Tells receivers which servers may send email as you.';
  if (m.spf.length > 1) spfB = b('error', '✕ duplicate');
  else if (s) {
    const bad = s.all === '+' || s.lookups > 10, weak = s.all === '?' || s.all == null;
    spfB = bad ? b('error', '✕ unsafe') : weak ? b('warn', '! weak') : b('ok', '✓ valid');
    spfN = `${s.lookups} of 10 DNS lookups · ends with ${s.all ? s.all + 'all' : 'no all'}` + (s.services.length ? ' · senders: ' + s.services.join(', ') : '');
  }
  rows.push(['SPF', spfB, m.spf.join('\n'), spfN]);
  let dmB = b('error', '✕ missing'), dmN = 'Tells receivers what to do with emails that fail SPF / DKIM.';
  if (m.dmarc.length > 1) dmB = b('error', '✕ duplicate');
  else if (m.dmarc.length) {
    dmB = dm.p === 'reject' || dm.p === 'quarantine' ? b('ok', '✓ p=' + dm.p) : dm.p === 'none' ? b('warn', '! p=none') : b('error', '✕ invalid');
    dmN = { reject: 'Spoofed email is rejected. Strongest protection.', quarantine: 'Spoofed email goes to spam.',
            none: 'Monitoring only. Spoofed email still gets delivered.' }[dm.p] || dmN;
  }
  rows.push(['DMARC', dmB, m.dmarc.join('\n'), dmN]);
  rows.push(['DKIM', m.dkim.length ? b('ok', '✓ found') : b('info', 'not found'),
    m.dkim.map(k => k.selector + '._domainkey: ' + (k.record.length > 90 ? k.record.slice(0, 90) + '…' : k.record)).join('\n'),
    m.dkim.length ? 'Emails are digitally signed.' : 'Not on common selectors. Enter yours in Advanced options.']);
  rows.push(['BIMI (brand logo in inbox)', m.bimi.length ? b('ok', '✓ found') : b('info', 'not set'), m.bimi.join('\n'),
    'Shows your logo next to emails in Gmail / Yahoo. Needs DMARC enforcement first.']);
  $('mailBody').innerHTML = rows.map(([k, st, rec, note]) => `<tr><td><div class="code-pill">${esc(k)}</div><div class="small">${esc(note)}</div></td>
      <td>${st}</td><td class="mono" style="white-space:pre-wrap">${esc(rec) || '<span style="color:var(--muted)">—</span>'}</td></tr>`).join('');
}

$('csvBtn').addEventListener('click', () => {
  if (!DATA) return;
  const q = v => '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"';
  const out = [['Section', 'Type / Field', 'Name', 'Value', 'TTL'].map(q).join(',')];
  DATA.records.forEach(r => out.push(['DNS', r.type, r.name, r.data, r.ttl].map(q).join(',')));
  const r = DATA.registration;
  if (r) [['Registrar', r.registrar], ['Registered on', r.created_fmt], ['Expires on', r.expires_fmt], ['Domain age', r.age],
    ['Registrant', r.registrant], ['Status', (r.status || []).join(' | ')], ['Nameservers', (r.nameservers || []).join(' | ')]]
    .forEach(([k, v]) => out.push(['Registration', k, DATA.domain, v, ''].map(q).join(',')));
  DATA.checks.filter(c => c.sev !== 'pass').forEach(c => out.push(['Issue (' + c.sev + ')', c.area, c.title, c.detail, ''].map(q).join(',')));
  const blob = new Blob(['﻿' + out.join('\r\n')], { type: 'text/csv;charset=utf-8' });
  const link = document.createElement('a'); link.href = URL.createObjectURL(blob);
  link.download = `dns-whois-${DATA.domain}.csv`; link.click(); setTimeout(() => URL.revokeObjectURL(link.href), 1000);
});
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(debug=True, port=5000)
