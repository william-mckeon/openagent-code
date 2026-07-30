# 0047 — grounding-early-stage

Status: implemented
Flag: `CODE_GROUND_GREENFIELD_MAX` (default 0 = empty-only; extends `CODE_GROUND_SKIP_GREENFIELD`)

## Goal

Stop grounding from derailing an early-stage build. The greenfield guard (specs/0042) only silences the
path checks on a strictly EMPTY workspace. But the moment a scaffold exists — even a handful of stub files —
the workspace is "populated," so grounding runs full-bore and flags every cited-but-not-yet-real path as a
phantom. The live Inkling Centpilot run showed the failure clearly: after 11 stub files were written,
grounding flagged claims on almost every turn (5, 4, 7, 2, 6, 3, 2 "not backed") and the agent spent turn
after turn defending file-existence trivia ("categories.json EXISTS", "favicon.ico missing") instead of
building — eventually arguing against a claim the user never made. On an early scaffold a cited path is
still a PROPOSAL, not a phantom.

## Concepts

- **A tunable greenfield threshold.** `is_greenfield(cwd, max_files)` now returns True when the workspace
  holds AT MOST `max_files` reviewable source files, counting up to `max_files+1` then bailing (a populated
  repo still pays only a couple of readdirs; the walk is bounded to `_cap` directories). `max_files` comes
  from `config.GROUND_GREENFIELD_MAX`, read in `problems()`.
- **Default 0 is the old behavior.** With `max_files == 0`, the first reviewable file makes it non-greenfield
  — identical to the specs/0042 empty-only guard. Raising it (e.g. 15) treats a small early scaffold as
  greenfield too, so the path-existence check and the Tier-2 path verifier are skipped there.
- **Still gated by the master switch.** Nothing happens unless `CODE_GROUND_SKIP_GREENFIELD` is on; this
  flag only widens what that switch considers greenfield. The success-claim / absence / web nets are
  untouched, and a genuinely populated repo (more files than the threshold) grounds normally.

## Acceptance

Each item is an assertion in `scripts/check_oac_fixes.py` (17/17, dep-free).

- `is_greenfield(5-file scaffold, max_files=0)` → False (empty-only default, byte-identical); `max_files=10`
  → True (early-stage); `max_files=3` → False (over threshold).
- With `CODE_GROUND_SKIP_GREENFIELD` on and `GROUND_GREENFIELD_MAX=10`, a proposal answer in a 5-file
  scaffold is not flagged; back at the default 0 the same scaffold grounds normally (the proposed path is
  flagged).
- `CODE_GROUND_GREENFIELD_MAX` defaults 0 when unset.

## Non-goals

- No content-based "is this answer a plan vs a review" detection — the file-count threshold is a cheap,
  predictable proxy the operator tunes; over-reaching on prose risks suppressing real phantom catches on a
  small but genuine repo.
- Does not address the model-quality side (a model fixating on grounding feedback and losing the task
  thread) — that is corpus/training signal, not a harness change.

## Byte-identity

`CODE_GROUND_GREENFIELD_MAX` defaults 0, and `is_greenfield(cwd, 0)` is exactly the specs/0042 empty-only
predicate, so with the default the guard behaves byte-for-byte as before (verified: `check_grounding` /
`check_grounding_paths` unchanged; the `max_files=0` and default-0 assertions in `check_oac_fixes.py`).
`SCHEMA_VERSION` unchanged; the flag is not in `safety_fingerprint`.
