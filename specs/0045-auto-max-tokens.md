# 0045 — auto-max-tokens

Status: implemented
Flags: `CODE_MODEL_MAX_TOKENS` gains an `auto` sentinel; `CODE_MODEL_MAX_OUTPUT_TOKENS` (default empty/0 =
off); `CODE_OUTPUT_MARGIN_TOKENS` (default 4096)

## Goal

Stop hardcoding gpt-oss-120b's `131072` context window for a different model. Two independent, independently
flag-gated capabilities:

- **3a — auto window.** `CODE_MODEL_MAX_TOKENS=auto` resolves the served model's real context window at
  startup, so the compaction budgets derived from it (`COMPACT_HARD_AT_TOKENS`, `SUMMARIZE_INPUT_MAX_TOKENS`)
  fit the actual model instead of a stale constant.
- **3b — output cap.** An optional per-request `max_tokens` so a long-generation model can be bounded (or
  told to use the remaining window), computed with headroom for reasoning tokens.

## Concepts

- **One derivation, recomputable.** The window→budget derivation moved into `config._recompute_window_budgets(window)`:
  `MODEL_MAX_TOKENS = max(8000, window)`, `COMPACT_HARD_AT_TOKENS = max(8000, window-12000)`,
  `SUMMARIZE_INPUT_MAX_TOKENS = max(8000, window-35000)`. It is called at import (from the parsed env) and
  again by the resolver after an auto-detect — so there is a single source of the formula.
  `_recompute_window_budgets(131072)` reproduces the exact pre-0045 `131072 / 119072 / 96072`.
- **Resolution is a startup step that never raises.** `model.resolve_model_window()` is a no-op unless the
  `auto` sentinel is set; when it is, it tries `litellm.get_model_info(MODEL)` (offline, instant) then a
  best-effort GET `{API_BASE}/models` `context_length` (Together / vLLM expose it), and on success calls
  `_recompute_window_budgets`. On ANY failure it leaves the `131072` fallback in place. It is called once,
  before any `ContextManager` is built, at every entry point that warms the model (cli.py ×2, eval/harness.py,
  train/capture.py).
- **The output cap lives in `complete()`, never `_params()`.** `_params()` has no `messages`, and it feeds
  `summarize()` / `warm_up()` too — capping those is wrong. `complete()` calls `_output_cap(messages)` after
  building kwargs: a fixed `CODE_MODEL_MAX_OUTPUT_TOKENS` is sent as-is; `auto` computes
  `MODEL_MAX_TOKENS - estimate_tokens(messages) - CODE_OUTPUT_MARGIN_TOKENS`, floored at `MIN_OUTPUT_TOKENS`
  (512) so a large prompt can never yield a non-positive cap; unset returns None → no `max_tokens` key added.
- **Coupling with reasoning (specs/0044).** The auto cap's margin must cover the model's OUTPUT including
  reasoning tokens (Inkling's `reasoning_content` is output-side), so `CODE_OUTPUT_MARGIN_TOKENS` must be
  sized against the active reasoning setting.

## Acceptance

Each item is an assertion in `scripts/check_auto_maxtokens.py` (14/14, dep-free — fakes litellm; the
import-time sentinel parse runs in clean subprocesses).

- `_recompute_window_budgets(131072)` reproduces `131072 / 119072 / 96072` exactly (byte-identity lock); for
  a smaller window the ordering holds and the `8000` floors are honored.
- Parse: `auto` → `MODEL_MAX_TOKENS_AUTO=True` + 131072 fallback; a plain int parses as today; garbage falls
  back to 131072.
- Output cap: `auto` = window − prompt − margin, floored at 512; fixed int sent as-is; unset → None (no
  `max_tokens` key).
- Resolver: on failure it is swallowed and the fallback kept; it is a no-op (and does not call
  `get_model_info`) when the sentinel is off.
- `SCHEMA_VERSION` == `0.13.0`; the new flags are absent from `safety_fingerprint`; the flags default off.

## Non-goals

- No live-per-turn window re-resolution — the window is resolved once at startup (models do not change
  mid-run).
- No new `litellm` import in `config.py` — all litellm/network logic stays in `model.py` and writes results
  back onto the config module, so the dep-free harnesses keep working.
- The `usage` record shape is unchanged — `reasoning_tokens` is deliberately NOT added to the trajectory
  usage dict (that would be a record-shape change forcing a `SCHEMA_VERSION` bump).

## Byte-identity

With both defaults (`CODE_MODEL_MAX_TOKENS=131072`, `CODE_MODEL_MAX_OUTPUT_TOKENS` empty),
`_recompute_window_budgets(131072)` yields the exact prior triple and `_output_cap` returns None (no
`max_tokens` key), so the request body and the compaction budgets are byte-for-byte unchanged (verified:
`check_context` 21/21, `check_config_provenance` 24/24 unchanged; the byte-identity assertions in
`check_auto_maxtokens.py`).

## Notes / open

- **Blocker for 3a:** Inkling's authoritative window is unconfirmed — `litellm.get_model_info` likely
  KeyErrors on the local cost map, and Together `/v1/models` `context_length` for the served id is
  unverified. Keep `auto` opt-in with the 131072 fallback and pin `CODE_MODEL_MAX_TOKENS=<int>` once the
  model card is checked (`scratchpad/probe_inkling_window.py` fetches it). 3b does not depend on this.
- `docker/code/Dockerfile` keeps its literal `131072` default (byte-identical); operator guidance for the
  `auto` sentinel and the output vars lives in `.env.example` / README / DATASHEET.
