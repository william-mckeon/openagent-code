"""
scripts/check_netfence_0079.py

Acceptance harness for specs/0079 — the SSRF guard for web_fetch. Dep-free: netfence uses only stdlib
(socket + ipaddress); every case here uses a LITERAL IP or the never-resolving .invalid TLD, so no network is
touched. Run:

    python scripts/check_netfence_0079.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import netfence as nf   # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def blocked(url):
    return nf.check_url(url) is not None


def main():
    check("cloud-metadata 169.254.169.254 is blocked (the classic SSRF target)",
          blocked("http://169.254.169.254/latest/meta-data/iam/security-credentials/"))
    check("loopback (127.0.0.1 / localhost / [::1]) is blocked",
          blocked("http://127.0.0.1:8080/") and blocked("http://localhost:9000/") and blocked("http://[::1]/"))
    check("private RFC1918 (10/8, 172.16/12, 192.168/16) is blocked",
          blocked("http://10.0.0.5/") and blocked("http://172.16.9.9/") and blocked("http://192.168.1.1/admin"))
    check("CGNAT 100.64/10 and link-local 169.254 are blocked",
          blocked("http://100.64.0.1/") and blocked("http://169.254.1.1/"))
    check("a non-http(s) scheme (file:// / gopher://) is blocked",
          blocked("file:///etc/passwd") and blocked("gopher://127.0.0.1/"))
    check("an unresolvable host is blocked FAIL-CLOSED (no DNS answer -> refuse)",
          blocked("http://this-host-does-not-exist-zzz.invalid/"))
    check("a URL with no host is blocked",
          blocked("http:///nohost") and blocked("notaurl"))

    check("a PUBLIC literal address (8.8.8.8 / 1.1.1.1) is ALLOWED (check_url -> None)",
          nf.check_url("http://8.8.8.8/") is None and nf.check_url("https://1.1.1.1/dns") is None)

    check("is_blocked_ip: public True->not-blocked, private/loopback/link-local blocked (unit)",
          (not nf.is_blocked_ip(__import__("ipaddress").ip_address("8.8.8.8")))
          and nf.is_blocked_ip(__import__("ipaddress").ip_address("192.168.0.1"))
          and nf.is_blocked_ip(__import__("ipaddress").ip_address("169.254.169.254"))
          and nf.is_blocked_ip(__import__("ipaddress").ip_address("::1")))

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
