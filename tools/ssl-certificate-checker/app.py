"""
SSL Certificate Checker  -  Flask app
Check any website's SSL/TLS certificate in seconds - and the HTTPS setup
that matters for SEO.

  * Trust, hostname match, expiry countdown, issuer, validation type
  * Certificate chain (missing intermediates, wrong order, expired links)
  * TLS version + key / signature strength
  * HTTPS SEO checks: http -> https 301, www / non-www, HSTS, mixed content
  * Bulk check up to 10 domains, health score, CSV export

Built by Mahalakshmi Marimuthu - Digital Marketing Strategist & AI-Powered SEO Expert
"""
import hashlib
import re
import select
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import certifi
import requests
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed448, ed25519, rsa
from cryptography.x509.oid import ExtensionOID, NameOID
from flask import Flask, Response, jsonify, request
from OpenSSL import SSL, crypto

app = Flask(__name__)

TIMEOUT = 8
MAX_DOMAINS = 10
HTML_LIMIT = 1_500_000
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": BROWSER_UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}

POLICY_TYPES = {
    "2.23.140.1.1": "EV (Extended Validation)",
    "2.23.140.1.2.1": "DV (Domain Validated)",
    "2.23.140.1.2.2": "OV (Organization Validated)",
    "2.23.140.1.2.3": "IV (Individual Validated)",
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def parse_target(raw):
    """'https://Example.com:8443/path' -> ('example.com', 8443)."""
    s = (raw or "").strip()
    if not s:
        return None, None
    if not re.match(r"^[a-z][a-z0-9+.\-]*://", s, re.I):
        s = "https://" + s
    p = urlparse(s)
    host = (p.hostname or "").strip(".").lower()
    if not host or " " in host:
        return None, None
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None, None
    try:
        port = p.port or 443
    except ValueError:
        return None, None
    return host, port


def cert_dates(c):
    if hasattr(c, "not_valid_after_utc"):          # cryptography 42+
        return c.not_valid_before_utc, c.not_valid_after_utc
    return (c.not_valid_before.replace(tzinfo=timezone.utc),
            c.not_valid_after.replace(tzinfo=timezone.utc))


def name_attr(name, oid):
    try:
        v = name.get_attributes_for_oid(oid)
        return v[0].value if v else ""
    except Exception:
        return ""


def short_name(name):
    return name_attr(name, NameOID.COMMON_NAME) or name_attr(name, NameOID.ORGANIZATION_NAME) or name.rfc4514_string()


def sans(c):
    try:
        ext = c.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
    except x509.ExtensionNotFound:
        return []
    out = [n.lower() for n in ext.get_values_for_type(x509.DNSName)]
    out += [str(ip) for ip in ext.get_values_for_type(x509.IPAddress)]
    return out


def host_matches(host, names):
    for n in names:
        if n == host:
            return True
        if n.startswith("*."):
            base = n[2:]
            if host.endswith("." + base) and "." not in host[: -len(base) - 1]:
                return True
    return False


def key_info(c):
    k = c.public_key()
    if isinstance(k, rsa.RSAPublicKey):
        return "RSA", k.key_size
    if isinstance(k, ec.EllipticCurvePublicKey):
        return f"ECDSA {k.curve.name}", k.key_size
    if isinstance(k, ed25519.Ed25519PublicKey):
        return "Ed25519", 256
    if isinstance(k, ed448.Ed448PublicKey):
        return "Ed448", 456
    if isinstance(k, dsa.DSAPublicKey):
        return "DSA", k.key_size
    return "Unknown", 0


def sig_name(c):
    try:
        h = c.signature_hash_algorithm.name.upper() if c.signature_hash_algorithm else ""
    except Exception:
        h = ""
    oid_name = getattr(c.signature_algorithm_oid, "_name", "") or ""
    return oid_name or h or "Unknown", h


def validation_type(c):
    try:
        pol = c.extensions.get_extension_for_oid(ExtensionOID.CERTIFICATE_POLICIES).value
        for p in pol:
            t = POLICY_TYPES.get(p.policy_identifier.dotted_string)
            if t:
                return t
    except x509.ExtensionNotFound:
        pass
    return "OV (Organization Validated)" if name_attr(c.subject, NameOID.ORGANIZATION_NAME) else "DV (Domain Validated)"


def fmt(dt):
    return dt.strftime("%d %b %Y")


# --------------------------------------------------------------------------- #
# TLS
# --------------------------------------------------------------------------- #
def served_chain(host, port, ip):
    """Unverified handshake -> every certificate the server sends + protocol/cipher."""
    ctx = SSL.Context(SSL.TLS_CLIENT_METHOD)
    ctx.set_verify(SSL.VERIFY_NONE, lambda *a: True)
    sock = socket.create_connection((ip, port), timeout=TIMEOUT)
    conn = SSL.Connection(ctx, sock)
    try:
        conn.set_tlsext_host_name(host.encode("ascii"))
        conn.set_connect_state()
        deadline = time.time() + TIMEOUT
        while True:
            try:
                conn.do_handshake()
                break
            except SSL.WantReadError:
                if not select.select([sock], [], [], max(0.1, deadline - time.time()))[0]:
                    raise socket.timeout("TLS handshake timed out")
            except SSL.WantWriteError:
                if not select.select([], [sock], [], max(0.1, deadline - time.time()))[1]:
                    raise socket.timeout("TLS handshake timed out")
            if time.time() > deadline:
                raise socket.timeout("TLS handshake timed out")
        chain = []
        for c in conn.get_peer_cert_chain() or []:
            der = crypto.dump_certificate(crypto.FILETYPE_ASN1, c)
            chain.append(x509.load_der_x509_certificate(der))
        return chain, conn.get_protocol_version_name(), conn.get_cipher_name()
    finally:
        try:
            conn.shutdown()
        except Exception:
            pass
        sock.close()


def verify(host, port, ip):
    """Browser-style verification against Mozilla's trusted root list."""
    ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        with socket.create_connection((ip, port), timeout=TIMEOUT) as s:
            with ctx.wrap_socket(s, server_hostname=host):
                return True, None, None
    except ssl.SSLCertVerificationError as e:
        return False, getattr(e, "verify_code", None), getattr(e, "verify_message", str(e))
    except ssl.SSLError as e:
        return False, None, str(e.reason or e)
    except OSError as e:
        return False, None, str(e)


def explain_verify(code, msg, host):
    m = (msg or "").lower()
    if code == 10 or "expired" in m:
        return "The certificate has expired", "Renew the certificate now — every browser shows a full-page security warning."
    if code == 9 or "not yet valid" in m:
        return "The certificate isn't valid yet", "Its start date is in the future. Check the server clock or reissue the certificate."
    if code in (18, 19) or "self-signed" in m or "self signed" in m:
        return "Self-signed certificate", "Browsers don't trust it. Replace it with one from a trusted CA (Let's Encrypt is free)."
    if code in (20, 21) or "local issuer" in m or "unable to get issuer" in m:
        return "Incomplete certificate chain", ("The server isn't sending its intermediate certificate. Desktop Chrome may "
                                                "hide this, but many phones, apps and crawlers fail. Install the full chain "
                                                "(often called fullchain.pem or the CA bundle).")
    if code == 62 or "hostname" in m or "mismatch" in m:
        return f"Certificate doesn't cover {host}", "Reissue it with this hostname added, or redirect this hostname elsewhere."
    if code == 23 or "revoked" in m:
        return "The certificate has been revoked", "Install a new certificate from your CA."
    return "Browsers won't trust this certificate", f"Verification error: {msg}"


# --------------------------------------------------------------------------- #
# HTTPS / SEO checks
# --------------------------------------------------------------------------- #
def follow(url, max_hops=6):
    """Follow redirects by hand so every hop (and its status code) is visible."""
    hops, final, cur = [], None, url
    for _ in range(max_hops):
        try:
            r = requests.get(cur, headers=HEADERS, timeout=TIMEOUT, allow_redirects=False, stream=True)
        except requests.exceptions.SSLError:
            hops.append({"url": cur, "status": None, "error": "SSL error"})
            return hops, None
        except requests.exceptions.RequestException:
            hops.append({"url": cur, "status": None, "error": "No response"})
            return hops, None
        hops.append({"url": cur, "status": r.status_code})
        loc = r.headers.get("Location")
        if 300 <= r.status_code < 400 and loc:
            r.close()
            cur = urljoin(cur, loc)
            continue
        final = r
        break
    return hops, final


def alt_host(host):
    if host.startswith("www."):
        return host[4:]
    parts = host.split(".")
    if len(parts) == 2 or (len(parts) == 3 and parts[1] in ("co", "com", "org", "net", "gov", "ac", "edu") and len(parts[2]) == 2):
        return "www." + host
    return None


MIXED_RX = re.compile(r"""<(?:img|script|iframe|source|video|audio|embed)\b[^>]*?\ssrc\s*=\s*["']?(http://[^"'\s>]+)"""
                      r"""|<link\b[^>]*?rel\s*=\s*["']?stylesheet[^>]*?href\s*=\s*["']?(http://[^"'\s>]+)""", re.I)


def https_checks(host, port):
    out = {}
    base = host if port == 443 else f"{host}:{port}"
    if port == 443:
        hops, _ = follow(f"http://{host}/")
        out["http_hops"] = hops
    else:
        out["http_hops"] = []
    s_hops, final = follow(f"https://{base}/")
    out["https_hops"] = s_hops
    out["final_url"] = s_hops[-1]["url"] if s_hops else ""
    out["final_status"] = s_hops[-1].get("status") if s_hops else None
    out["hsts"] = None
    out["mixed"] = []
    if final is not None:
        out["hsts"] = final.headers.get("Strict-Transport-Security")
        ctype = final.headers.get("Content-Type", "")
        if "html" in ctype.lower() and final.url.startswith("https://"):
            try:
                raw = final.raw.read(HTML_LIMIT, decode_content=True)
                html = raw.decode(final.encoding or "utf-8", errors="replace")
                found = []
                for m in MIXED_RX.finditer(html):
                    u = m.group(1) or m.group(2)
                    if u and u not in found:
                        found.append(u)
                out["mixed"] = found[:20]
            except Exception:
                pass
        final.close()
    # www / non-www
    alt = alt_host(host) if port == 443 else None
    out["alt"] = None
    if alt:
        try:
            socket.getaddrinfo(alt, 443)
            a_hops, a_final = follow(f"https://{alt}/")
            if a_final is not None:
                a_final.close()
            out["alt"] = {"host": alt, "exists": True, "hops": a_hops,
                          "final_url": a_hops[-1]["url"] if a_hops else "",
                          "ssl_error": any(h.get("error") == "SSL error" for h in a_hops)}
        except socket.gaierror:
            out["alt"] = {"host": alt, "exists": False}
    return out


# --------------------------------------------------------------------------- #
# One domain
# --------------------------------------------------------------------------- #
def check_domain(raw):
    host, port = parse_target(raw)
    res = {"input": raw.strip(), "host": host or raw.strip(), "port": port, "ok": False}
    if not host:
        res["error"] = "That doesn't look like a valid domain."
        return res
    try:
        ip = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0][4][0]
    except socket.gaierror:
        res["error"] = "Domain not found — it doesn't resolve in DNS. Check the spelling."
        return res
    res["ip"] = ip
    try:
        chain, proto, cipher = served_chain(host, port, ip)
    except (socket.timeout, TimeoutError):
        res["error"] = f"No response on port {port} — the site may be down or not support HTTPS."
        return res
    except ConnectionRefusedError:
        res["error"] = f"Connection refused on port {port} — this site doesn't accept HTTPS connections."
        return res
    except (SSL.Error, OSError) as e:
        res["error"] = f"The secure (TLS) handshake failed — the server may not support HTTPS properly. ({str(e)[:120]})"
        return res
    if not chain:
        res["error"] = "The server didn't send a certificate."
        return res

    trusted, vcode, vmsg = verify(host, port, ip)
    leaf = chain[0]
    nb, na = cert_dates(leaf)
    now = datetime.now(timezone.utc)
    days_left = (na - now).total_seconds() / 86400
    lifetime = round((na - nb).total_seconds() / 86400)
    names = sans(leaf)
    cn = name_attr(leaf.subject, NameOID.COMMON_NAME)
    match = host_matches(host, names or ([cn.lower()] if cn else []))
    ktype, ksize = key_info(leaf)
    sname, shash = sig_name(leaf)

    res.update({
        "ok": True, "trusted": trusted, "verify_error": vmsg, "tls": proto, "cipher": cipher,
        "days_left": int(days_left) if days_left >= 0 else min(-1, int(days_left)),
        "cert": {
            "cn": cn, "sans": names, "org": name_attr(leaf.subject, NameOID.ORGANIZATION_NAME),
            "issuer_cn": name_attr(leaf.issuer, NameOID.COMMON_NAME),
            "issuer_org": name_attr(leaf.issuer, NameOID.ORGANIZATION_NAME) or short_name(leaf.issuer),
            "validation": validation_type(leaf),
            "not_before": nb.isoformat(), "not_after": na.isoformat(),
            "not_before_h": fmt(nb), "not_after_h": fmt(na), "lifetime": lifetime,
            "serial": format(leaf.serial_number, "X"),
            "sig": sname, "key": f"{ktype} {ksize}-bit" if ksize else ktype,
            "sha256": hashlib.sha256(leaf.public_bytes(serialization.Encoding.DER)).hexdigest().upper(),
            "match": match,
        },
    })

    # chain table
    rows = []
    for i, c in enumerate(chain):
        cnb, cna = cert_dates(c)
        self_signed = c.subject == c.issuer
        rows.append({"subject": short_name(c.subject), "issuer": short_name(c.issuer),
                     "expires": fmt(cna), "days_left": int((cna - now).total_seconds() / 86400),
                     "role": "Server certificate" if i == 0 else ("Root" if self_signed else "Intermediate")})
    res["chain"] = rows
    order_ok = all(chain[i].issuer == chain[i + 1].subject for i in range(len(chain) - 1))

    try:
        h = https_checks(host, port)
    except Exception:
        h = {"http_hops": [], "https_hops": [], "hsts": None, "mixed": [], "alt": None, "final_url": ""}
    res["https"] = h

    # ------------------------------------------------------------------ checks
    checks = []
    add = lambda sev, area, title, detail: checks.append({"sev": sev, "area": area, "title": title, "detail": detail})

    if trusted:
        add("pass", "Certificate", "Trusted by browsers", f"Issued by {res['cert']['issuer_org']} and chains to a trusted root.")
    else:
        t, d = explain_verify(vcode, vmsg, host)
        add("fail", "Certificate", t, d)

    if match:
        add("pass", "Certificate", f"Covers {host}", "The hostname is listed on the certificate.")
    elif trusted is False and vcode == 62:
        pass  # already reported above
    else:
        add("fail", "Certificate", f"Certificate doesn't cover {host}",
            f"It only covers: {', '.join(names[:6]) or cn}. Reissue it with this hostname added.")

    if days_left < 0:
        if not any("expired" in c["title"] for c in checks):
            add("fail", "Certificate", "The certificate has expired",
                f"It expired on {fmt(na)}. Renew it now — every browser shows a full-page security warning.")
    elif now < nb:
        add("fail", "Certificate", "The certificate isn't valid yet", f"It starts on {fmt(nb)}.")
    elif days_left <= 7:
        add("fail", "Certificate", f"Expires in {res['days_left']} day{'s' if res['days_left'] != 1 else ''}",
            f"Renew before {fmt(na)} or visitors will see a security warning.")
    elif days_left <= 30:
        add("warn", "Certificate", f"Expires in {res['days_left']} days",
            f"Renew before {fmt(na)}. If renewal is automatic, check that it's actually running.")
    else:
        add("pass", "Certificate", f"Valid for {res['days_left']} more days", f"Expires on {fmt(na)}.")

    if days_left < 0:
        pass
    elif lifetime <= 100:
        add("pass", "Certificate", "Short-lived certificate",
            f"{lifetime}-day lifetime — this usually means renewal is automated, which is where the industry is heading.")
    else:
        add("info", "Certificate", "Automate renewal before lifetimes shrink",
            f"This certificate lasts {lifetime} days. New public certificates are capped at 200 days (from March 2026), "
            "100 days (March 2027) and 47 days (March 2029). Manual renewal won't keep up — automate it (ACME).")

    if ktype == "RSA" and ksize < 2048:
        add("fail", "Security", f"Weak key ({ksize}-bit RSA)", "Use at least a 2048-bit RSA or an ECDSA key.")
    if shash in ("SHA1", "MD5"):
        add("fail", "Security", f"Weak signature ({shash})", "Reissue the certificate with SHA-256.")

    if not order_ok:
        add("warn", "Chain", "Chain is in the wrong order",
            "The certificates aren't sent server → intermediate → root. Most browsers cope, but some clients fail.")
    for i, r in enumerate(rows[1:], start=1):
        if r["days_left"] < 0 and r["role"] == "Intermediate":
            add("fail", "Chain", f"Intermediate “{r['subject']}” has expired", "Download the current intermediate from your CA.")
    if len(rows) > 1 and rows[-1]["role"] == "Root":
        add("info", "Chain", "Root certificate is sent", "Harmless, but unnecessary — browsers already have it.")
    if trusted and order_ok:
        add("pass", "Chain", "Certificate chain is complete", f"{len(rows)} certificate{'s' if len(rows) != 1 else ''} sent in the right order.")

    if proto in ("TLSv1.3",):
        add("pass", "Security", "TLS 1.3 supported", "Fastest and most secure protocol.")
    elif proto == "TLSv1.2":
        add("pass", "Security", "TLS 1.2 in use", "Secure. Enabling TLS 1.3 would make connections a little faster.")
    else:
        add("fail", "Security", f"Outdated protocol ({proto})", "TLS 1.0/1.1 are blocked by modern browsers. Enable TLS 1.2 and 1.3.")

    # HTTPS / SEO
    hh = h.get("http_hops") or []
    if port != 443:
        pass
    elif not hh or hh[0].get("status") is None:
        add("info", "HTTPS & SEO", "http:// doesn't respond",
            "Port 80 is closed, so visitors who type http:// get an error. Consider a 301 redirect to https://, plus HSTS.")
    else:
        first = hh[0]["status"]
        last = hh[-1]
        redirects = [x for x in hh if x.get("status") and 300 <= x["status"] < 400]
        ends_https = last["url"].startswith("https://") and last.get("status") and last["status"] < 400
        if not redirects:
            add("fail", "HTTPS & SEO", "http:// doesn't redirect to https://",
                "Both versions load, so Google can index duplicates and visitors can land on the insecure page. "
                "Add a site-wide 301 redirect to https://.")
        elif not ends_https:
            add("fail", "HTTPS & SEO", "http:// doesn't end on a working https:// page",
                f"The redirect ends at {last['url']} ({last.get('status') or last.get('error')}).")
        else:
            if first in (302, 303, 307):
                add("warn", "HTTPS & SEO", f"http:// → https:// uses a temporary {first} redirect",
                    "Use a permanent 301 (or 308) so search engines move all signals to the https:// version.")
            if len(redirects) > 1:
                add("warn", "HTTPS & SEO", f"Redirect chain of {len(redirects)} hops",
                    "Redirect http:// straight to the final https:// URL in one hop — chains waste crawl budget and slow visitors.")
            if first in (301, 308) and len(redirects) == 1:
                add("pass", "HTTPS & SEO", "http:// → https:// in one 301", "Exactly what Google recommends.")

    hsts = h.get("hsts")
    if hsts:
        m = re.search(r"max-age\s*=\s*\"?(\d+)", hsts, re.I)
        age = int(m.group(1)) if m else 0
        if age < 15768000:
            add("info", "HTTPS & SEO", "HSTS max-age is short",
                f"max-age is {age:,} seconds. Six months (15768000) to one year (31536000) is recommended.")
        else:
            add("pass", "HTTPS & SEO", "HSTS enabled", f"Browsers will always use https:// ({hsts[:80]}).")
    elif h.get("https_hops"):
        add("warn", "HTTPS & SEO", "HSTS header missing",
            "Add “Strict-Transport-Security: max-age=31536000” so browsers never load the http:// version.")

    if h.get("mixed"):
        add("warn", "HTTPS & SEO", f"Mixed content on the homepage ({len(h['mixed'])})",
            f"Files loaded over http://, e.g. {h['mixed'][0]}. Browsers block or flag these — switch them to https://.")

    alt = h.get("alt")
    if alt and alt.get("exists"):
        if alt.get("ssl_error"):
            add("fail", "HTTPS & SEO", f"https://{alt['host']} shows a certificate error",
                f"Anyone typing {alt['host']} sees a warning. Add it to the certificate and redirect it to the main version.")
        else:
            main_final = urlparse(h.get("final_url") or "").netloc
            alt_final = urlparse(alt.get("final_url") or "").netloc
            if main_final and alt_final and main_final == alt_final:
                add("pass", "HTTPS & SEO", "www and non-www point to one version", f"Both end on {main_final}.")
            elif main_final and alt_final:
                add("warn", "HTTPS & SEO", "www and non-www both load separately",
                    f"{host} ends on {main_final} but {alt['host']} ends on {alt_final}. Pick one and 301 the other to it.")

    fails = sum(c["sev"] == "fail" for c in checks)
    warns = sum(c["sev"] == "warn" for c in checks)
    score = 100 - fails * 20 - warns * 7
    if not trusted:
        score = min(score, 40)
    if days_left < 0:
        score = min(score, 10)
    res["score"] = max(0, min(100, score))
    res["checks"] = checks

    if days_left < 0:
        res["status"] = ["Expired", "error"]
    elif not trusted or not match:
        res["status"] = ["Not trusted", "error"]
    elif days_left <= 30:
        res["status"] = ["Expiring soon", "warn"]
    else:
        res["status"] = ["Valid", "ok"]
    return res


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/api/check", methods=["POST"])
def api_check():
    data = request.get_json(silent=True) or {}
    items = [d.strip() for d in (data.get("domains") or []) if d and d.strip()]
    seen, uniq = set(), []
    for d in items:
        h, pt = parse_target(d)
        k = f"{h}:{pt}" if h else d.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(d)
    if not uniq:
        return jsonify({"ok": False, "error": "Please enter a domain, e.g. example.com"}), 400
    truncated = len(uniq) > MAX_DOMAINS
    uniq = uniq[:MAX_DOMAINS]
    with ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(check_domain, uniq))
    return jsonify({"ok": True, "results": results, "truncated": truncated,
                    "checked_at": datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")})


@app.route("/")
def home():
    return Response(PAGE, mimetype="text/html")



PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SSL Certificate Checker – Check SSL Expiry, Chain &amp; HTTPS Setup</title>
<meta name="description" content="Free SSL Certificate Checker: check any site's SSL expiry, issuer, certificate chain and TLS version, plus the HTTPS setup that matters for SEO — 301 redirects, www vs non-www, HSTS and mixed content.">
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%237c6af7'/%3E%3Ctext x='50%25' y='50%25' dominant-baseline='central' text-anchor='middle' font-family='Georgia%2C serif' font-size='18' font-weight='900' fill='white'%3EM%3C/text%3E%3C/svg%3E">
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@600;700;800&family=Inter:wght@400;500;600;700&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
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

  .life{margin-top:.4rem}
  .life-bar{height:10px;border-radius:100px;background:var(--surface2);border:1px solid var(--border);overflow:hidden}
  .life-fill{height:100%;border-radius:100px}
  .life-row{display:flex;justify-content:space-between;font-family:'DM Mono',monospace;font-size:.66rem;color:var(--muted);margin-top:.45rem}
  .life-big{font-family:'Sora',sans-serif;font-size:2.2rem;font-weight:800;line-height:1.1;margin-top:.6rem}
  .life-big small{font-size:.8rem;color:var(--muted);font-weight:500;font-family:'Inter',sans-serif;margin-left:.3rem}
  .life-note{font-size:.76rem;color:var(--muted);margin-top:.8rem;line-height:1.5}
  table.kv td:first-child{width:200px;font-family:'DM Mono',monospace;font-size:.66rem;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);white-space:nowrap}
  .san{display:inline-block;font-family:'DM Mono',monospace;font-size:.7rem;padding:.15rem .5rem;border-radius:6px;background:var(--surface2);border:1px solid var(--border);margin:.12rem .2rem .12rem 0}
  .san.hit{border-color:rgba(106,247,200,.45);color:var(--mint)}
  tr.pick{cursor:pointer}
  tr.pick.sel td{background:rgba(124,106,247,.09)}
  .err-card{border-left:3px solid var(--red);background:var(--surface);border-radius:0 10px 10px 0;padding:1rem 1.2rem;font-size:.85rem;margin-top:1.4rem}
  details.passed{margin-top:.6rem}
  details.passed summary{cursor:pointer;font-size:.78rem;color:var(--mint);font-family:'DM Mono',monospace}
  details.passed .issues{margin-top:.6rem}
  .issue.pass{border-color:var(--mint)}
  .detail-title{font-family:'Sora',sans-serif;font-size:1.05rem;font-weight:700;margin-top:2rem;overflow-wrap:anywhere}
  .detail-title span{color:var(--accent)}
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
      <a href="#" class="sidebar-link active">🔒&nbsp; SSL Certificate Checker</a>
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
      <h1>🔒 SSL Certificate <span>Checker</span></h1>
    </div>

    <form class="card" id="form">
      <label class="lbl" for="site">Website or domain</label>
      <div class="row-inline">
        <input type="text" id="site" placeholder="https://www.yourwebsite.com">
        <button class="btn" type="submit" id="runBtn">🔍 Check SSL</button>
      </div>
      <details class="adv">
        <summary>+ Check more domains at once</summary>
        <div class="adv-body">
          <div>
            <label class="lbl" for="more">More domains — one per line, up to 10 in total</label>
            <textarea id="more" rows="4" placeholder="blog.yourwebsite.com&#10;shop.yourwebsite.com"></textarea>
          </div>
        </div>
      </details>
      <div class="spinner" id="sp">Connecting securely and reading the certificate…</div>
      <div class="err" id="err"></div>
    </form>

    <div id="results" style="display:none">

      <div id="multi" style="display:none">
        <div class="sec-title">🌐 Domains Checked <span class="count" id="multiCount"></span></div>
        <div class="tbl-tools">
          <span class="hint" style="margin:0">Click a row to see its full report below.</span>
          <button type="button" class="btn ghost" id="csvBtn2" style="margin-left:auto">⬇ Download CSV</button>
        </div>
        <div class="tbl-wrap">
          <table>
            <thead><tr><th>Domain</th><th>Status</th><th>Days left</th><th>Expires</th><th>Issuer</th><th>Score</th></tr></thead>
            <tbody id="multiBody"></tbody>
          </table>
        </div>
        <div class="detail-title" id="detailTitle"></div>
      </div>

      <div id="detail"></div>
    </div>
  </main>
</div>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let DATA = null, SEL = 0;

$('form').addEventListener('submit', async e => {
  e.preventDefault();
  const list = [$('site').value, ...$('more').value.split('\n')].map(s => s.trim()).filter(Boolean);
  $('err').textContent = '';
  if (!list.length) { $('err').textContent = 'Please enter a website or domain.'; return; }
  $('sp').classList.add('show'); $('runBtn').disabled = true;
  try {
    const r = await fetch('/api/check', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ domains: list }) });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'Something went wrong.');
    DATA = j; SEL = 0; render();
    $('results').style.display = '';
    if (j.truncated) $('err').textContent = 'Only the first 10 domains were checked.';
  } catch (err) {
    $('err').textContent = err.message || 'Could not reach the server — please try again.';
  } finally {
    $('sp').classList.remove('show'); $('runBtn').disabled = false;
  }
});

function dayCls(d) { return d == null ? '' : d < 0 ? 'red' : d <= 30 ? 'orange' : 'mint'; }

function render() {
  const rs = DATA.results;
  $('multi').style.display = rs.length > 1 ? '' : 'none';
  if (rs.length > 1) {
    $('multiCount').textContent = `${rs.length} domains · checked ${DATA.checked_at}`;
    $('multiBody').innerHTML = rs.map((r, i) => {
      const st = r.ok ? r.status : ['Error', 'error'];
      return `<tr class="pick ${i === SEL ? 'sel' : ''}" data-i="${i}">
        <td class="mono">${esc(r.host)}${r.port && r.port !== 443 ? ':' + r.port : ''}</td>
        <td><span class="badge ${st[1]}">${esc(st[0])}</span></td>
        <td><span class="v ${dayCls(r.days_left)}" style="font-weight:700">${r.ok ? r.days_left : '—'}</span></td>
        <td>${r.ok ? esc(r.cert.not_after_h) : '—'}</td>
        <td>${r.ok ? esc(r.cert.issuer_org) : `<span class="small" style="color:var(--red)">${esc(r.error)}</span>`}</td>
        <td>${r.ok ? r.score : 0}</td></tr>`;
    }).join('');
    document.querySelectorAll('tr.pick').forEach(tr => tr.addEventListener('click', () => {
      SEL = +tr.dataset.i; render();
      $('detailTitle').scrollIntoView({ behavior: 'smooth', block: 'start' });
    }));
    $('detailTitle').innerHTML = `Report for <span>${esc(rs[SEL].host)}${rs[SEL].port && rs[SEL].port !== 443 ? ':' + rs[SEL].port : ''}</span>`;
  }
  renderDetail(rs[SEL]);
}

function ring(sc) {
  const col = sc >= 80 ? '#6af7c8' : sc >= 50 ? '#f7a26a' : '#f76a6a';
  return `<svg viewBox="0 0 160 160" width="160" height="160">
      <circle cx="80" cy="80" r="68" fill="none" stroke="#23233a" stroke-width="12"/>
      <circle cx="80" cy="80" r="68" fill="none" stroke="${col}" stroke-width="12" stroke-linecap="${sc > 0 ? 'round' : 'butt'}"
        stroke-dasharray="${(427.26 * sc / 100).toFixed(2)} 427.26" transform="rotate(-90 80 80)"/></svg>
    <div class="ring-center"><div class="score" style="color:${col}">${sc}</div><div class="sub">out of 100</div></div>`;
}

function issueHtml(c) {
  const map = { fail: 'error', warn: 'warn', info: 'info', pass: 'pass' };
  const label = { fail: '✕ error', warn: '! warning', info: 'i info', pass: '✓ passed' };
  const cls = map[c.sev];
  return `<div class="issue ${cls}"><div class="top">
      <span class="badge ${cls === 'pass' ? 'ok' : cls}">${label[c.sev]}</span><div class="msg">${esc(c.title)}</div>
      <span class="area-tag">${esc(c.area)}</span></div>
      <div class="fix">${esc(c.detail)}</div></div>`;
}

function hopsHtml(hops) {
  if (!hops || !hops.length) return '<span class="small">—</span>';
  return hops.map(h => {
    const s = h.status, cls = !s ? 'error' : s < 300 ? 'ok' : s < 400 ? 'warn' : 'error';
    return `<div style="display:flex;gap:.5rem;align-items:center;margin:.15rem 0"><span class="badge ${cls}">${s || esc(h.error)}</span><span class="mono" style="font-family:'DM Mono',monospace;font-size:.72rem;word-break:break-all">${esc(h.url)}</span></div>`;
  }).join('');
}

function renderDetail(r) {
  if (!r.ok) {
    $('detail').innerHTML = `<div class="err-card"><b>${esc(r.host)}</b><div class="fix" style="color:var(--muted);margin-top:.3rem">${esc(r.error)}</div></div>`;
    return;
  }
  const c = r.cert, h = r.https || {};
  const nErr = r.checks.filter(x => x.sev === 'fail').length, nWarn = r.checks.filter(x => x.sev === 'warn').length;

  // lifetime bar
  const nb = new Date(c.not_before).getTime(), na = new Date(c.not_after).getTime(), now = Date.now();
  const pct = Math.max(0, Math.min(100, (now - nb) / (na - nb) * 100));
  const dcol = r.days_left < 0 ? 'var(--red)' : r.days_left <= 30 ? 'var(--orange)' : 'var(--mint)';

  const redirect = (() => {
    const hh = h.http_hops || [];
    if (r.port !== 443) return ['n/a (custom port)', ''];
    if (!hh.length || !hh[0].status) return ['No http://', 'orange'];
    const first = hh[0].status;
    if (first >= 300 && first < 400) return [first === 301 || first === 308 ? `${first} → https` : `${first} (temporary)`, first === 301 || first === 308 ? 'mint' : 'orange'];
    return ['Not redirected', 'red'];
  })();

  const issues = r.checks.filter(x => x.sev !== 'pass').sort((a, b) => ({ fail: 0, warn: 1, info: 2 }[a.sev] - { fail: 0, warn: 1, info: 2 }[b.sev]));
  const passed = r.checks.filter(x => x.sev === 'pass');

  $('detail').innerHTML = `
    <div class="overview">
      <div class="chart-card">
        <h3>SSL health score</h3>
        <div class="ring-wrap">${ring(r.score)}</div>
        <div style="display:flex;justify-content:center;gap:.4rem;margin-top:.8rem;flex-wrap:wrap">
          <span class="badge error">${nErr} errors</span><span class="badge warn">${nWarn} warnings</span></div>
      </div>
      <div class="chart-card">
        <h3>At a glance</h3>
        <div class="metrics">
          ${[
            ['Status', r.status[0], r.status[1] === 'ok' ? 'mint' : r.status[1] === 'warn' ? 'orange' : 'red'],
            ['Issuer', c.issuer_org, 'purple'],
            ['Protocol', (r.tls || '').replace('TLSv', 'TLS '), r.tls === 'TLSv1.3' || r.tls === 'TLSv1.2' ? 'mint' : 'red'],
            ['http → https', redirect[0], redirect[1]],
            ['HSTS', h.hsts ? 'Enabled' : 'Missing', h.hsts ? 'mint' : 'orange'],
            ['Type', c.validation.split(' ')[0], ''],
          ].map(([k, v, cl]) => `<div class="metric"><div class="k">${k}</div><div class="v ${cl}">${esc(v)}</div></div>`).join('')}
        </div>
        <div class="hint" style="overflow-wrap:anywhere">Checked: <span style="color:var(--text)">${esc(r.host)}${r.port !== 443 ? ':' + r.port : ''}</span> · ${esc(r.ip)}</div>
      </div>
      <div class="chart-card bars">
        <h3>Certificate lifetime</h3>
        <div class="life-big" style="color:${dcol}">${r.days_left < 0 ? Math.abs(r.days_left) : r.days_left}<small>${r.days_left < 0 ? 'days since it expired' : 'days left'}</small></div>
        <div class="life" style="margin-top:1rem">
          <div class="life-bar"><div class="life-fill" style="width:${pct.toFixed(1)}%;background:${dcol}"></div></div>
          <div class="life-row"><span>Issued ${esc(c.not_before_h)}</span><span>Expires ${esc(c.not_after_h)}</span></div>
        </div>
        <div class="life-note">${c.lifetime}-day certificate · ${Math.round(pct)}% of its life used.<br>Max lifetime for new certificates: 200 days now → 100 days (Mar 2027) → 47 days (Mar 2029).</div>
      </div>
    </div>

    <div class="sec-title">🛠 Issues &amp; Fixes <span class="count">${issues.length} found · ${passed.length} passed</span></div>
    <div class="issues">${issues.length ? issues.map(issueHtml).join('') : '<div class="all-good">✓ No issues found — this SSL setup looks healthy.</div>'}</div>
    ${passed.length ? `<details class="passed"><summary>✓ Show ${passed.length} passed checks</summary><div class="issues">${passed.map(issueHtml).join('')}</div></details>` : ''}

    <div class="sec-title">📜 Certificate Details</div>
    <div class="tbl-wrap"><table class="kv"><tbody>
      <tr><td>Common name</td><td class="mono">${esc(c.cn || '—')}</td></tr>
      <tr><td>Domains covered <span style="color:var(--text)">(${c.sans.length})</span></td><td>${c.sans.length ? c.sans.slice(0, 60).map(s => `<span class="san ${s === r.host || (s.startsWith('*.') && r.host.endsWith(s.slice(1)) && !r.host.slice(0, -s.length + 1).includes('.')) ? 'hit' : ''}">${esc(s)}</span>`).join('') + (c.sans.length > 60 ? `<span class="small"> +${c.sans.length - 60} more</span>` : '') : '—'}</td></tr>
      <tr><td>Hostname match</td><td><span class="badge ${c.match ? 'ok' : 'error'}">${c.match ? '✓ matches ' + esc(r.host) : '✕ does not cover ' + esc(r.host)}</span></td></tr>
      <tr><td>Issued by</td><td>${esc(c.issuer_org)}${c.issuer_cn && c.issuer_cn !== c.issuer_org ? ` <span class="small">(${esc(c.issuer_cn)})</span>` : ''}</td></tr>
      <tr><td>Organization</td><td>${esc(c.org || '— (not shown on DV certificates)')}</td></tr>
      <tr><td>Validation type</td><td>${esc(c.validation)}</td></tr>
      <tr><td>Valid from</td><td>${esc(c.not_before_h)}</td></tr>
      <tr><td>Valid until</td><td>${esc(c.not_after_h)} <span class="small" style="color:${dcol}">(${r.days_left < 0 ? 'expired' : r.days_left + ' days left'})</span></td></tr>
      <tr><td>Key</td><td>${esc(c.key)}</td></tr>
      <tr><td>Signature</td><td>${esc(c.sig)}</td></tr>
      <tr><td>Protocol / cipher</td><td class="mono">${esc((r.tls || '').replace('TLSv', 'TLS '))} · ${esc(r.cipher)}</td></tr>
      <tr><td>Serial number</td><td class="mono">${esc(c.serial)}</td></tr>
      <tr><td>SHA-256 fingerprint</td><td class="mono">${esc(c.sha256.match(/.{2}/g).join(':'))}</td></tr>
    </tbody></table></div>

    <div class="sec-title">🔗 Certificate Chain <span class="count">${r.chain.length} sent by the server</span></div>
    <div class="tbl-wrap"><table>
      <thead><tr><th>#</th><th>Certificate</th><th>Role</th><th>Issued by</th><th>Expires</th></tr></thead>
      <tbody>${r.chain.map((x, i) => `<tr><td>${i + 1}</td><td><div class="code-pill">${esc(x.subject)}</div></td>
        <td><span class="badge ${i === 0 ? 'info' : x.role === 'Root' ? 'info' : 'info'}">${esc(x.role)}</span></td>
        <td>${esc(x.issuer)}</td>
        <td><span style="color:${x.days_left < 0 ? 'var(--red)' : x.days_left <= 30 ? 'var(--orange)' : 'inherit'}">${esc(x.expires)}</span></td></tr>`).join('')}</tbody>
    </table></div>

    <div class="sec-title">↪️ HTTPS Redirects</div>
    <div class="tbl-wrap"><table>
      <thead><tr><th>Starting from</th><th>Redirect path</th></tr></thead>
      <tbody>
        ${r.port === 443 ? `<tr><td class="mono">http://${esc(r.host)}</td><td>${hopsHtml(h.http_hops)}</td></tr>` : ''}
        <tr><td class="mono">https://${esc(r.host)}${r.port !== 443 ? ':' + r.port : ''}</td><td>${hopsHtml(h.https_hops)}</td></tr>
        ${h.alt && h.alt.exists ? `<tr><td class="mono">https://${esc(h.alt.host)}</td><td>${hopsHtml(h.alt.hops)}</td></tr>` : ''}
      </tbody>
    </table></div>
    ${h.mixed && h.mixed.length ? `<div class="sec-title">⚠️ Mixed Content <span class="count">http:// files on an https:// page</span></div>
      <div class="tbl-wrap"><table><tbody>${h.mixed.map(u => `<tr><td class="mono">${esc(u)}</td></tr>`).join('')}</tbody></table></div>` : ''}

    <div class="dl-row" style="margin-top:1.4rem">
      <button type="button" class="btn ghost" onclick="downloadCsv()">⬇ Download CSV report</button>
    </div>`;
}

function downloadCsv() {
  if (!DATA) return;
  const q = v => '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"';
  const out = [['Domain', 'Status', 'Score', 'Days left', 'Valid from', 'Expires', 'Issuer', 'Validation', 'Protocol',
    'Hostname match', 'Trusted', 'http→https', 'HSTS', 'Errors', 'Warnings', 'Issues'].map(q).join(',')];
  DATA.results.forEach(r => {
    if (!r.ok) { out.push([r.host, 'Error', 0, '', '', '', '', '', '', '', '', '', '', '', '', r.error].map(q).join(',')); return; }
    const hh = (r.https && r.https.http_hops) || [];
    const iss = r.checks.filter(c => c.sev === 'fail' || c.sev === 'warn');
    out.push([r.host, r.status[0], r.score, r.days_left, r.cert.not_before_h, r.cert.not_after_h, r.cert.issuer_org,
      r.cert.validation, r.tls, r.cert.match ? 'Yes' : 'No', r.trusted ? 'Yes' : 'No',
      hh.length && hh[0].status ? hh[0].status : 'No response', r.https && r.https.hsts ? 'Yes' : 'No',
      iss.filter(c => c.sev === 'fail').length, iss.filter(c => c.sev === 'warn').length,
      iss.map(c => c.title).join(' | ')].map(q).join(','));
  });
  const blob = new Blob(['﻿' + out.join('\r\n')], { type: 'text/csv;charset=utf-8' });
  const name = DATA.results.length === 1 ? DATA.results[0].host.replace(/^www\./, '') : 'bulk';
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
  a.download = `ssl-check-${name}.csv`; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
$('csvBtn2').addEventListener('click', downloadCsv);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(debug=True, port=5000)
