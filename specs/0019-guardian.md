# 0019 — guardian (fail-closed approval reviewer for the ask tier)

## Goal
Give the `ask` tier a captured, automated reviewer so an **unattended** run can proceed on a *reviewed*
ask-tier action instead of only blocking. When a tool call hits `ask` **and no human is present**, the
guardian spawns a CAPTURED reviewer subagent that judges the specific request and returns APPROVE / DENY.
Unlike the grounding gate — which fails OPEN (an infra hiccup never traps the agent, because completion
already proved the work was done) — the guardian fails **CLOSED**: no reviewer, an error, a timeout, or
any verdict that isn't a clean APPROVE → DENY. It governs the `ask` tier ONLY (never allow / deny /
read-only), so it can only make a run MORE restrictive, never less. Reviews ride the same
captured-subagent path as the grounding verifier, so every approval decision is a first-class trajectory
that feeds the flywheel.

**Headless-only (ride-3 decision).** The guardian is for *unattended* runs. When a human IS present (an
interactive REPL), the ask tier prompts the human `[y/N]` exactly as before — the guardian does not fire.
It engages only when `ctx.interactive` is false (eval / Docker / one-shot / headless), which is precisely
the "operate at scale" case. An identical `(tool, target)` is reviewed **once per turn** (a cache on
`ctx`), so a repeated command isn't re-litigated by a fresh subagent each time.

**Mass-destruction cap (ride-5 decision).** The reviewer is aggregate-blind — it judges one call at a
time — so a bulk deletion decomposed into single `delete_file` calls was rubber-stamped file-by-file
(each "matches the request"), and the prompt's "deny MANY files" clause was structurally unreachable. A
deterministic **hard ceiling** (`CODE_GUARDIAN_MAX_DESTRUCTIVE`, default 5) counts DISTINCT destructive
ops (delete / move / dangerous command) APPROVED this turn (`ctx._destructive_targets`) and DENIES the
(N+1)-th regardless of the verdict — *"escalate to a human."* **No enumeration bypass**: the ceiling is
inviolable (predictability = trust for a partner staked on a real repo); to go further you raise the flag,
a deliberate, auditable act. The guardian also receives the running count so it can escalate a broad
*sweep* even before the cap. A guardian-denied op does NOT consume budget; a plain edit/write/install is
not destructive and is never capped. Composes with corpus integrity (specs/convert): the denials the cap
generates make the whole mass-delete turn *contested* → excluded from training.

**Useful-autonomy (ride-5 decision).** The reviewer sees the user's **pinned request** (`ctx.request`),
so it can APPROVE a *destructive-but-requested* op — "delete the file X" → `delete_file(X)` — instead of
reflexively denying every deletion when no human is present, while still denying anything that EXCEEDS or
DEVIATES from the request (mass-deletion, files the user never named, exfiltration, out-of-workspace,
`.git`/secret touches). Without this, an over-denial both blocks the task *and* trains give-up behavior
into the corpus (the trajectory is labeled `completed`). And because `apply_patch` hides its targets in
the patch body, the `_target` for it is summarized (`patch.patch_summary` → "delete CONTRIBUTING.md"), so
the reviewer, the label, and the log line all see WHAT the patch does rather than an opaque "apply_patch".

## Acceptance
- `src/guardian.py`: `review(tool, target, reason, ctx) -> bool` — spawns a captured reviewer via
  `ctx.spawn` (with `CODE_GUARDIAN_EFFORT`), parses its verdict, and returns True only on a clean
  APPROVE. Fail-CLOSED: no spawn / raise / empty / subagent-error / a `DENY` anywhere / an ambiguous
  `APPROVE … but DENY` / prose-with-no-verdict → **False**. Pure of side effects, never raises.
- `src/permissions.py`: every `ask`-tier / prompt site (the ask rule + the default-mode baseline in
  `decide()`, the two in `_decide_command`, the `decide_move` prompt) consults the guardian FIRST when
  `CODE_GUARDIAN` is on, `ctx.depth == 0`, **and `ctx.interactive` is false**; otherwise it falls through
  to the interactive human prompt / headless block **unchanged**. `_guardian` returns a
  `Verdict(approved, reason)` (or None to fall through); the reason rides into the `Decision` so the run
  log explains itself. It never overrides a deny rule or the fence (ask tier only).
- Calibration: the reviewer prompt treats a routine **in-workspace** dependency install / build
  (`npm install`, `pip install`, `go build/test`, yarn, pnpm) as EXPECTED-and-safe → APPROVE, while still
  denying ARBITRARY network calls to unknown hosts, out-of-workspace writes, and `.git`/secret touches.
- **Recursion-safe**: the reviewer runs at `depth+1`, and the guardian is gated on `depth == 0`, so the
  reviewer's own tool calls never re-enter the guardian.
- **Flag OFF (default) is byte-identical to today** — the guardian is a pre-branch that returns None when
  off, leaving the human-prompt / block path exactly as it was.
- `scripts/check_guardian.py` proves: APPROVE→allow, DENY→block, every failure mode → DENY (fail-closed),
  markdown-tolerant + ambiguous-denies verdict parsing with a reason extracted, the depth>0 recursion
  gate, **headless-only** (interactive → not consulted), **per-turn caching** (an identical call spawns
  one review), the install-calibration prompt, ask-tier-only (a deny rule still blocks under an APPROVE),
  and flag-off parity. Dep-free, no model, no network.

## Non-goals
- Reviewing allow / deny / read-only tiers — ask ONLY.
- Replacing the fence, the deny rules, or execpolicy/sandbox — the guardian sits ON TOP of them, at the
  approval point.
- A perfectly-calibrated reviewer — that is model quality (the flywheel). The harness guarantees only the
  fail-closed CONTRACT: uncertainty always denies.

## Notes
- Off by default (`CODE_GUARDIAN=false`), like every adoption-track phase.
- Pairs naturally with execpolicy (0016) + the expanded `ask` tier: the ops now on `ask` (rm/sudo/curl/
  wget/.git/.env-delete) are exactly what the guardian reviews for an unattended run.
