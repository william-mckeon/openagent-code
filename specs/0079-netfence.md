# 0079 — net-fence: SSRF guard for web_fetch

Status: implemented
Flag: `CODE_NETFENCE` (default off)

## Goal

Close the SSRF hole the security review found: `web_fetch` did `httpx.get(url, follow_redirects=True)` with
ZERO host validation, so a URL — or any redirect hop — to `http://169.254.169.254/` (cloud-metadata),
`http://localhost:8080/` (an internal service), or an RFC1918 host reached the machine's own network. That
turns a fetch into a pivot / credential-theft primitive (Codex fences egress through a default-deny proxy; the
pragmatic Python equivalent is an SSRF host check, since Windows has no netns to kernel-confine).

## Concepts

- **`src/netfence.py`** (stdlib only): `check_url(url)` returns `None` if safe, else an error string. It refuses
  a non-`http(s)` scheme and any host whose resolved IP is non-public — `is_blocked_ip` = `not ip.is_global` OR
  loopback/private/link-local/reserved (belt-and-suspenders across Python versions), covering loopback, RFC1918,
  ULA, link-local incl. **169.254 metadata**, and CGNAT `100.64/10`. **Fail-closed**: an unparseable URL or an
  unresolvable host is blocked.
- **Per-redirect-hop check** (`tools.web_fetch`): when `CODE_NETFENCE` is on, redirects are followed MANUALLY in
  a bounded loop (`follow_redirects=False`), calling `check_url` before EVERY hop — so a public URL that 302s to
  an internal address, or a DNS rebind, is caught, not just the initial URL.

## Acceptance

`scripts/check_netfence_0079.py` (9/9, dep-free — literal IPs + the `.invalid` TLD, no network):

- 169.254 metadata, loopback (v4/v6/localhost), RFC1918, CGNAT, link-local are all blocked; a non-http scheme
  and a URL with no host are blocked; an unresolvable host is blocked fail-closed; a public literal (8.8.8.8 /
  1.1.1.1) is allowed; `is_blocked_ip` classifies public vs private correctly.

## Non-goals

- `run_command` network egress is NOT confined here — Windows has no netns/seccomp for a pure-Python kernel
  boundary, so that is advisory (the Phase-2 egress-command classification) and, ultimately, the deferred OS
  sandbox. This spec fences the ONE OAC tool that makes an outbound request itself.
- Not a proxy; no allow-listing of specific public hosts (a public destination is allowed) — the threat closed
  is reaching INTERNAL/metadata addresses.

## Byte-identity

With `CODE_NETFENCE` off, `web_fetch` takes the original single `httpx.get(url, follow_redirects=True)` path —
byte-for-byte unchanged. Verified: full dep-free suite green.
