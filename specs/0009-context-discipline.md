# 0009 — Context discipline (bounded fragments)

> Every DYNAMIC fragment that enters the model-visible context is bounded, so no single item can grow
> unbounded and blow the window. Adapted from OpenAI Codex's "model-visible context" rules (reference,
> not copied). Phase C3 of the capability track (ROADMAP.md).

## Why

`src/context.py` already caps each *added* message (`_capped`, `CODE_MAX_MESSAGE_CHARS`) and compacts
older turns when the live context exceeds `CODE_COMPACT_AT_TOKENS`. But two injection points slipped
the cap:

- **`set_pinned`** — the pinned plan is ALWAYS sent AND NEVER compacted, so an unbounded plan would
  silently eat the window every single turn. This is the real gap C3 closes.
- **the compaction summary** — set directly in `_compact`, bounded only *indirectly* by the shrink
  guard (`after >= before` skips it), never hard-capped.

Codex's discipline ("Model visible context": no unbounded items; everything injected has a hard cap;
flag oversized items). C3 makes that an enforced invariant here rather than an ad-hoc per-caller habit.

## The invariant

**Every dynamic fragment entering the live context passes through `_capped`** — tool results, user
turns, the pinned plan, the compaction summary, and the rehydrated history on session resume. `_capped`
bounds **both** places a huge string can hide in a message:

- the message **`content`** — a big file READ, a long subagent return; and
- a native-mode tool call's **`tool_calls[].function.arguments`** — for `write_file`/`edit_file` this
  is the *entire file body* the model emits, while `content` is only short reasoning. Without this the
  symmetric huge-WRITE case slips the cap that the huge-READ case (a capped tool *result*) is caught by.
  The tool call's `id`/`name` are preserved, so the `tool_call`↔`result` pairing the API requires stays
  intact; the historical `arguments` is only re-templated by the serving layer, never re-parsed by our
  code (the executable call is parsed from the fresh model output in `planner.py`, before `add()`).

For each, `_capped` truncates content over `CODE_MAX_MESSAGE_CHARS` (default 48000 chars ≈ 12k tokens),
appends a note pointing at the trajectory (which keeps the full text raw), and **logs** the truncation
so an oversized fragment is visible for review, not silent (Codex flags large model-visible items).

The **system prompt** is exempt: it is a fixed, curated fragment (base prompt + already-capped memory
+ short notes), not a dynamic item, and truncating it would corrupt the agent's own instructions.

Capture is unaffected: the trajectory logs every message RAW (lossless) — the bound applies only to
what the model SEES (the capture-vs-context split, specs/0007 lineage).

## Changes

- `src/context.py`: `set_pinned` and the `_compact` summary now go through `_capped`; `_capped` logs
  on truncation; the module docstring states the invariant.
- `scripts/check_context.py`: dep-free acceptance (stub model + trajectory, no network) — the bounding
  primitive, add/pin/summary bounding, the system prompt preserved, and "every fragment in `context()`
  is bounded."

## Acceptance (checkable — `python scripts/check_context.py`)

- [ ] `_capped` bounds an oversized fragment and leaves a small one untouched.
- [ ] A native-mode tool call's oversized `arguments` (a `write_file` file body) is capped, with its
      `id`/`name` preserved; a small tool call is untouched.
- [ ] `add()`, `set_pinned()`, a post-compaction working set (with an *oversized* summary — the check
      is non-vacuous), and a resumed session's rehydrated history all hold only bounded fragments.
- [ ] The system prompt is preserved intact.
- [ ] Every fragment returned by `context()` is ≤ the cap (+ the truncation note).
- [ ] Passes with no model and no network.

## Non-goals

- **Structured fragment types** (Codex's `ContextualUserFragment` trait) — our fragments are plain
  message dicts; a type system is more than a small Python agent needs.
- **Retuning the cap** — 48000 chars is the existing default; C3 enforces the invariant, it does not
  retune. (Codex's 10k-token rule is theirs; ours is ~12k, close and configurable via the knob.)
- **Cache-miss avoidance** (Codex rule 2 — "avoid frequent context changes") — a serving-layer
  concern, out of scope for the harness.
