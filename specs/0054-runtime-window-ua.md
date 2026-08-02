# 0054 — auto context-window probe: browser User-Agent

Status: implemented
Flag: none (a bug fix to the specs/0045 auto path; no new config)

## Goal

Make the `CODE_MODEL_MAX_TOKENS=auto` context-window probe actually work behind Cloudflare. On Tinker, the
startup line `[model] auto window unresolved — keeping fallback 131072` was never a "Tinker doesn't expose the
window" problem — `_fetch_context_length` (specs/0045) does a raw `urllib` GET to `{api_base}/models`, and
Tinker is fronted by **Cloudflare**, which returns **HTTP 403 / error 1010** ("banned by browser signature")
to the default `python-urllib/x.y` User-Agent. The GET failed, the `except` swallowed it to `None`, and the
window fell back to 131072 — half Inkling-Small's real 262144 (256k). Discovered when a stdlib `urllib` probe
of the same endpoint returned 403/1010 and adding a browser UA fixed it (litellm's httpx client already passes
Cloudflare for the same reason, which is why generation worked while the window probe didn't).

## Concepts

- **Send a normal browser User-Agent.** `_fetch_context_length` now sets a `Mozilla/5.0 … Chrome/120` UA (and
  `Accept: application/json`) on the `/models` request, then adds `Authorization` if a key is present.
  Cloudflare's browser-signature check passes, so the probe can actually read `context_length` /
  `context_window` / `max_model_len`. Harmless on endpoints without Cloudflare (a UA header is always valid).
- **Best-effort, unchanged contract.** Still wrapped in the same try/except → `None` on any failure, still
  timeout-bounded, still stdlib-only. It only changes the request HEADERS, so a resolvable endpoint now
  resolves instead of silently falling back; an unreachable one behaves exactly as before.
- **Pin still preferred.** Independently, `.env.example` now documents pinning `CODE_MODEL_MAX_TOKENS=262144`
  for Inkling-Small on Tinker rather than relying on `auto` — the pin is instant and needs no network. This
  fix makes `auto` correct for anyone who leaves it on.

## Acceptance

- `_fetch_context_length` sends a browser `User-Agent` (and `Accept`) header; `Authorization` is still added
  only when a key is present. Verified by inspection + `scripts/check_auto_maxtokens.py` still green (the
  dep-free harness exercises the resolve/derive logic with injected values; the header change doesn't affect
  it). The live effect was confirmed out-of-band: the same endpoint that returned 403/1010 to the bare
  `urllib` UA returns 200 with a browser UA (`scripts/check_system_role_online.py` needed the identical fix).
- No new flag, no `SCHEMA_VERSION` bump, not in `safety_fingerprint`.

## Non-goals

- Not a change to the auto-resolution ORDER (litellm model-info first, then `/models`) or the fallback value.
- Does not guarantee Tinker's `/models` exposes `context_length` — it only removes the Cloudflare block so the
  probe can read it if present; if the field is absent the fallback still applies (hence the pin recommendation).

## Byte-identity

The change is confined to the request headers inside the specs/0045 best-effort probe, which runs ONLY when
`CODE_MODEL_MAX_TOKENS=auto`. A pinned window (the recommended Tinker config) never calls it, so pinned runs
are byte-identical. `check_auto_maxtokens` unchanged.
