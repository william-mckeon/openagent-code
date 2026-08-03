# 0056 — broaden the runtime-done net

Status: implemented
Flag: none new (broadens the specs/0053 `CODE_VERIFY_RUNTIME_DONE` net)

## Goal

Catch the runtime-success claims the specs/0053 net missed. On a fresh Centpilot run (after 0053 shipped) the
model still declared **"Centpilot runs"**, **"verified running"**, and **"deploy fixed"** while every
`http.server` / `curl :8080` attempt had FAILED — and it had just reverted the compose to a config that will
not build. None were flagged, because 0053's `_RUNTIME_SUCCESS` only knew generic subjects
(server/app/service/…) with predicates like up/serving, and had no vocabulary for the project's PROPER NAME
as subject, the verb "runs", the "verified running" phrasing, or a deploy/build success claim.

## Concepts

- **Verified/confirmed running.** Add a subjectless alternative: `verified|confirmed … running|serving|built|
  up|deployed|working` — the "verified running", "confirm it runs" phrasing the run used to sound rigorous.
- **Deploy / build success.** Add `deploy(ment)|the build|build (is) fixed|works|builds|succeeds|passes|…` —
  so "deploy fixed" / "the build works" is treated as a claim that needs an actual successful build.
- **The project by name.** `_app_runtime_re(app_name)` builds a PER-CALL matcher for the workspace basename
  as subject — `Centpilot runs`, `Centpilot is updated and running`. `problems()` passes
  `os.path.basename(ctx.cwd)`. This is precise to the current project, so it does NOT false-flag a generic
  "Docker runs the container" in prose (a bare `[A-Z]\w+ runs` would). `None`/short names are skipped.
- **Same guards.** Every path stays PER-SENTENCE and `_HEDGED`- + `_MUT_NEGATED`-guarded, so "Centpilot is
  NOT running yet", "run curl to confirm", "should be serving" are still not flagged; and the whole net still
  only fires when `runtime_verified` is False (no health-check succeeded this turn) and
  `CODE_VERIFY_RUNTIME_DONE` is on.

## Acceptance

New assertions in `scripts/check_runtime_done.py` (on top of the 0053 set):

- `Centpilot runs.` / `Centpilot is updated and running.` (with `app_name="Centpilot"`) are flagged.
- `Verified running …`, `The deploy is fixed.`, `deploy fixed` are flagged (no app name needed).
- `Centpilot is NOT running yet.` (negated) is NOT flagged; `Centpilot runs.` with `runtime_verified=True`
  is cleared.
- All prior 0053 assertions still hold; the generic subjects and hedge/negation guards are unchanged.

## Non-goals

- Not a new flag — this is a vocabulary broadening of the existing 0053 net, gated by the same
  `CODE_VERIFY_RUNTIME_DONE`; off is byte-identical (the net never runs).
- Not a fix-until-up loop (still the goal-loop's job, recorded debt) — this only flags the false claim.
- Deliberately narrow on the app name (exact workspace basename + a runtime verb), not a broad
  `<Capitalized> runs`, to avoid false-flagging descriptive prose.

## Byte-identity

With `CODE_VERIFY_RUNTIME_DONE` off (default) the net never runs, so the broadening is invisible — byte-
identical. The new `app_name` parameter defaults `None` (skips the app matcher), so any caller that doesn't
pass it behaves exactly as before. Verified: `check_runtime_done` (0053 + 0056 cases), full suite unchanged.
