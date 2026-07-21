# 0034 - compaction window safety (resume must never overflow the model's context window)

## Goal
Resuming a long session crashed EVERY turn. The untruncated Bedrock error was
`Input length (136607) exceeds model's maximum context length (131072)`, and the traceback pointed at
`context.py _compact -> model.summarize(old)`. Two bugs on the same resume seam:

1. **Unbounded summarize (the crash).** On resume the FULL raw history is rehydrated at once; the first
   compaction calls `model.summarize(old)` where `old` is nearly the whole history, and `summarize` renders
   ALL of it into ONE `litellm.completion` - which itself exceeds the window, so compaction can't even run and
   the turn dies. (The live session never hit this because it compacted INCREMENTALLY as it grew.)
2. **No hard ceiling.** Compaction only ever fired on the SOFT `CODE_COMPACT_AT_TOKENS` (16000) trigger, once
   per `context()` call, with no knowledge of the model's TRUE window - so nothing guaranteed the sent context
   fits, even after a compaction pass.

Plus a latent third issue on the same seam: resume reloads the raw trajectory verbatim, so a prior turn that
died mid-flight can leave a DANGLING assistant tool_call the next step sends unpaired (Bedrock rejects it).

## Concepts
- **Bounded summarize** (`context.bounded_summary`, PURE) - a map-reduce fold: if the render fits
  `SUMMARIZE_INPUT_MAX_TOKENS`, one call (byte-identical fast path); else `chunk_messages` into groups each
  under budget, summarize each, and fold the partials until one fits. `summarize_once` / `render` are injected,
  so it is unit-testable with NO model. `model.summarize` delegates to it - a single call can never overflow.
- **Hard model-window ceiling** (`ContextManager._enforce_hard_cap`) - a SEPARATE block after the existing soft
  `if` (the soft path is untouched -> byte-identical). It loops `_compact()` (now returns a bool so no-progress
  is detectable) until under `COMPACT_HARD_AT_TOKENS`; when compaction can't shrink further (summary not
  smaller, or the kept tail alone exceeds the cap) it falls back to `_trim_oldest` - so it always converges and
  the SENT context provably fits the window. A no-op for a normal session already under the cap.
- **The ceiling config** (`config.py`) - ONE env flag `CODE_MODEL_MAX_TOKENS` (default 131072); the two
  internal budgets are DERIVED (window minus headroom): `COMPACT_HARD_AT_TOKENS` (~120k) and
  `SUMMARIZE_INPUT_MAX_TOKENS` (~96k). Headroom because `estimate_tokens` undercounts and the window must hold
  the model's OUTPUT too. Not a litellm lookup (offline/fast); the reactive `model._non_retryable` overflow
  guard stays as the last line of defense.
- **Tail sanitization** (`context.sanitize_tail`, used by `session.resume`) - drop a trailing dangling
  assistant tool_call / leading orphan tool result from the rehydrated history. A strict no-op on a clean tail.

## Acceptance
- `src/context.py`: pure `chunk_messages` / `chunk_text` / `bounded_summary` / `sanitize_tail`; `hard_cap` on
  the ContextManager; `context()` calls `_enforce_hard_cap` (soft `if` unchanged); `_compact()` returns a
  bool; `_trim_oldest` last-resort trim.
- `src/model.py`: `summarize` delegates to `bounded_summary` (via `_render` + `_summarize_once`); fast path
  byte-identical.
- `src/config.py` + `.env.example` + `docs/DATASHEET.md` + `README.md` + `docker/code/Dockerfile`: the single
  `CODE_MODEL_MAX_TOKENS` (default 131072); the derived budgets are internal.
- `src/session.py`: `sanitize_tail(working)` on the rehydrated history.
- `scripts/check_resume.py` - dep-free (no model/network): `bounded_summary` never hands a call more than the
  budget on a huge input (instrumented stub); `context()` on an over-window working returns under the hard cap
  (both when compaction shrinks it AND when only trimming can, via a big-summary stub); `sanitize_tail` drops a
  dangling tail and is a no-op on a clean one; a normal-size session is unchanged (byte-identity guard).
- **Byte-identical** for a normal session: the soft compaction path is untouched; `_enforce_hard_cap` is a
  no-op under the ceiling; `summarize`'s fast path renders + calls identically; `_compact` returning a bool
  doesn't change the soft caller (it ignores the value).

## Traps (each is a test)
- **Bounding summarize is MANDATORY, not the ceiling loop alone.** Without it `_compact` still feeds the whole
  `old` to one call and overflows before any loop can iterate.
- **Don't loop the SOFT budget.** Keep the single `if`; the hard loop is a distinct block gated on
  `estimate > hard_cap`, or a normal session's sent context changes (not byte-identical).
- **Convergence.** `_compact` must return a truthful bool; the trim fallback + a guard guarantee the loop ends.
- **Headroom.** `estimate_tokens` undercounts and the output must fit, so the budgets sit well below 131072.
- **Sanitize is a no-op on a clean tail.** Only drop a genuinely dangling trailing tool_call / leading orphan.

## Non-goals (the resume-robustness backlog - a SEPARATE follow-up phase)
- Re-stamping the safety fingerprint on `session_resume` (specs/0033 non-goal).
- Restoring + re-arming `ctx.spec` so the acceptance gate holds for a resumed spec.
- Fixing the plan pin being dropped by the per-task reset + restoring `plan_items`.
- Enriching `session_resume` provenance (model / cwd / mode) and refreshing `tool_schemas`.
- Auto-detecting the window via litellm (env override + hardcoded default is enough today).
- Partial tool-call/result pairing (only a fully-dangling trailing tool_call is sanitized in v1).
