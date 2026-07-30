# 0044 — reasoning-control

Status: implemented
Flags: `CODE_REASONING_PARAM` (default `reasoning_effort`), `CODE_REASONING_VALUE` (default empty = off),
`CODE_REASONING_TOPLEVEL` (default off)

## Goal

Let the operator control the served model's reasoning depth with finer granularity than the fixed
`low | medium | high` `reasoning_effort` knob. thinkingmachines/Inkling exposes several reasoning layers, and
different providers accept reasoning control under different request shapes — a string effort, an integer
token budget, or a nested object. Rather than hardcode any one of those (and re-code every time the shape
changes while the exact Inkling parameter is still being pinned down), add a pass-through so the reasoning
key AND value are configuration, not code.

## Concepts

- **A pass-through layer ABOVE the legacy path, not a replacement.** `_reasoning_kwargs()` now resolves in
  three precedence tiers:
  1. An explicit per-Model `effort` override (the grounding / guardian subagents, the adaptive ladder) →
     always the legacy `low/medium/high` string path (`_effort_kwargs`), so a per-subagent effort is never
     silently overwritten by a global pass-through.
  2. Else the global pass-through: when `CODE_REASONING_VALUE` is set, send `{CODE_REASONING_PARAM: value}`.
  3. Else the legacy global `CODE_REASONING_EFFORT` string path.
- **The value is typed by JSON.** `CODE_REASONING_VALUE` is `json.loads`-parsed if it parses — so `2048`
  becomes an int budget and `{"type":"enabled","budget_tokens":2048}` becomes an object — else it is used as
  a literal string (`xhigh`). The parse is wrapped so a malformed value can never raise at import (config is
  imported on every run).
- **Routing mirrors the effort path.** The payload goes via `extra_body` for OpenAI-compatible endpoints
  (vLLM / Together), or top-level when `CODE_REASONING_TOPLEVEL` is set or the model is a `bedrock/` one
  (which takes reasoning params top-level as `additionalModelRequestFields`).
- **Provenance stays scalar.** The pass-through never sets `CODE_REASONING_EFFORT`, so the `effort` field
  recorded per model_call (`self.effort or config.REASONING_EFFORT`) is unchanged — a scalar or None, never
  a dict. No stringifying needed, and the record shape is untouched.

## Acceptance

Each item is an assertion in `scripts/check_reasoning_passthrough.py` (12/12, dep-free — fakes litellm in
`sys.modules`).

- Flag OFF (`CODE_REASONING_VALUE` empty): `_reasoning_kwargs(arg)` equals the pre-0044 output for every
  combination of global effort ∈ {"", low, medium, high}, per-Model arg ∈ {None, low, high}, and model ∈
  {openai/…, bedrock/…}.
- String pass-through: `VALUE=xhigh`, `PARAM=reasoning_effort` → `{"extra_body":{"reasoning_effort":"xhigh"}}`
  (bypasses the `_EFFORTS` allowlist).
- Numeric budget: `VALUE=2048`, `PARAM=reasoning_tokens` → int 2048 inside `extra_body`.
- Object: `VALUE={"type":"enabled","budget_tokens":2048}`, `PARAM=thinking` → the dict under `thinking`.
- Top-level routing: `CODE_REASONING_TOPLEVEL=true` (and, independently, a `bedrock/` model) → payload
  top-level, not under `extra_body`.
- Per-Model override precedence: `_reasoning_kwargs("low")` with the pass-through set still returns the
  legacy `{"extra_body":{"reasoning_effort":"low"}}`.
- `_EFFORTS` unchanged; `CODE_REASONING_EFFORT` untouched by the pass-through; `SCHEMA_VERSION` == `0.13.0`;
  `CODE_REASONING_*` absent from `safety_fingerprint`; flags default OFF.

## Non-goals

- No validation of the value against Inkling's accepted shapes — the pass-through is intentionally dumb; the
  operator is responsible for a value the provider accepts (an unaccepted value would surface as a provider
  400, which the retry/backoff already handles).
- Not per-subagent configurable via env yet — the pass-through is a single global base; per-subagent depth
  still rides the existing effort override.

## Byte-identity

With `CODE_REASONING_VALUE` empty (default), `_reasoning_kwargs` reduces to
`_effort_kwargs(effort or config.REASONING_EFFORT)` — the exact pre-0044 expression — so every request body
and the recorded effort are byte-for-byte unchanged (verified: `check_effort` 21/21 unchanged; the flag-OFF
sweep in `check_reasoning_passthrough.py`).

## Notes / open

- **Efficacy blocker (not safety):** the code ships flag-off-safe and shape-agnostic, but the feature does
  nothing useful until the exact Inkling reasoning parameter is pinned from Together's model card — one of a
  string `reasoning_effort`, an object `thinking`/`reasoning`, or an integer `reasoning_tokens`.
- `warm_up()` applies the global pass-through (it passes no per-Model effort); a pass-through value the
  provider rejects would fail the warm-up probe too. Leave `CODE_REASONING_VALUE` empty until a value is
  confirmed.
