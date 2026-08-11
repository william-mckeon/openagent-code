"""
src/netfence.py

specs/0079: an SSRF guard for web_fetch.

web_fetch did `httpx.get(url, follow_redirects=True)` with ZERO host validation, so a URL — or any redirect
hop — to `http://169.254.169.254/` (cloud-metadata), `http://localhost:8080/` (an internal service), or an
RFC1918 host could reach the machine's OWN network. That is the classic SSRF that turns a fetch into a pivot /
credential-theft primitive. netfence resolves a URL's host and refuses any that maps to a non-public address,
re-checked after EVERY redirect hop (so a public host that 302s to an internal one, or a DNS rebind, is caught).

Stdlib only (socket + ipaddress); the fence is enforced by web_fetch when CODE_NETFENCE is on.
"""
import ipaddress
import socket
from urllib.parse import urlparse

from . import config   # noqa: F401 — imported for symmetry / future flag reads; the caller gates on config.NETFENCE


def _addrs(host):
    """Every IP `host` resolves to (a literal IP resolves to itself). [] on resolution failure — the caller
    treats an unresolvable host as BLOCKED (fail-closed)."""
    try:
        return [ipaddress.ip_address(host)]              # a literal IP — no DNS
    except ValueError:
        pass
    out = []
    try:
        for _fam, _type, _proto, _canon, sa in socket.getaddrinfo(host, None):
            try:
                out.append(ipaddress.ip_address(sa[0]))
            except ValueError:
                pass
    except socket.gaierror:
        return []
    return out


def is_blocked_ip(ip):
    """True for a NON-PUBLIC destination — loopback, private (RFC1918 / ULA), link-local (incl. 169.254
    metadata), CGNAT 100.64/10, unspecified, multicast, reserved. `not is_global` is the blanket check; the
    explicit flags are belt-and-suspenders across Python versions."""
    return not ip.is_global or ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved


def check_url(url):
    """None if `url` is safe to fetch; else a human error string. Blocks a non-http(s) scheme and any host that
    resolves to a non-public IP (SSRF to metadata / internal services). Fail-CLOSED: an unparseable URL or an
    unresolvable host is blocked. Enforced only when CODE_NETFENCE is on (the caller gates)."""
    try:
        u = urlparse(url)
    except Exception:  # noqa: BLE001
        return "netfence: unparseable URL — blocked"
    if u.scheme not in ("http", "https"):
        return f"netfence: refusing a non-http(s) URL scheme ({u.scheme or 'none'!r})"
    host = u.hostname
    if not host:
        return "netfence: URL has no host — blocked"
    addrs = _addrs(host)
    if not addrs:
        return f"netfence: could not resolve {host!r} — blocked (fail-closed)"
    for ip in addrs:
        if is_blocked_ip(ip):
            return (f"netfence: refusing {host!r} -> {ip} — a non-public / internal / cloud-metadata address "
                    "(SSRF blocked)")
    return None
