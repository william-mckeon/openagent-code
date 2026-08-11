# 0080 — permission-policy hardening (guardian circuit-breaker + protected-path defaults)

Status: implemented
Flag: `CODE_GUARDIAN_MAX_DENIALS` (0 = off) + `CODE_PROTECT_PATHS` (default off)

## Goal

Two Phase-1 permission-policy quick-wins from the Codex-vs-OAC security review, both adopting a Codex posture.

- **Guardian circuit-breaker (#7).** OAC's deny/guardian is fail-closed PER call but STATELESS across calls, so
  a prompt-injected loop can retry a denied destructive op indefinitely — and the only backstop counted APPROVED
  ops, not denials. Codex bounds the review loop; OAC now counts consecutive denials and aborts the turn.
- **Protected-path deny-write defaults (#10).** OAC's deny precedence and fence are correct, but NO protective
  deny rules shipped — an operator had to know to add `deny write_file(.git/**)` / `(.env)` themselves, and most
  won't. Codex ships deny entries for `.git`/credentials; OAC now offers an opt-in baseline.

## Concepts

- **Circuit-breaker** (`agent.py` + `outcomes.py`): a per-task `ctx._denial_streak` increments on a denied tool
  call and resets on any allowed one. Past `CODE_GUARDIAN_MAX_DENIALS` (> 0) the turn ends via `_finish` with a
  new honest **`denial_loop`** outcome (added to `GATE_OUTCOMES`, so it's never washed to `completed` and is
  dropped from SFT). `0` (default) → the counter never runs (byte-identical).
- **Protected paths** (`config.is_protected_path` + `permissions.py`): when `CODE_PROTECT_PATHS` is on,
  `write_file`/`edit_file`/`delete_file` targeting a protected path (`.git` internals, `.env` / `.env.*`, nested
  or top-level) are denied in `_decide_core` — placed with the specs/0078 deny-read gate, before the read-only
  allow. `apply_patch` is re-gated per file (`patch.py`), so its ops hit this too. A user's own deny still runs
  and wins; the baseline is additive.

## Acceptance

`scripts/check_permhardening_0080.py` (7/7, dep-free):

- `is_protected_path` matches `.git` internals (nested + top-level) and `.env`/`.env.*`, not source files;
  `write_file`/`delete_file` to `.git`/`.env` are denied while a normal file is allowed; protect is WRITE-only
  (read is specs/0078's job); off → modifying `.git` is allowed again (byte-identical).
- `denial_loop` is an honest gate outcome; a scripted loop of denied ops ends as `denial_loop` past the
  threshold; with the breaker off (0) the same loop is not `denial_loop`.

## Non-goals

- The breaker counts CONSECUTIVE denials — a legitimate mix of allowed + occasionally-denied ops never trips it;
  it only fires on a genuine denial loop.
- The protected set is intentionally small (`.git`, `.env`); an operator adds more via their own deny rules. Not
  a replacement for the secrets deny-READ (specs/0078) — this denies WRITES.

## Byte-identity

`CODE_GUARDIAN_MAX_DENIALS=0` and `CODE_PROTECT_PATHS=false` (defaults): the counter and the protected-path
gate never run — byte-for-byte the prior behavior. Verified: full dep-free suite 62/62.
