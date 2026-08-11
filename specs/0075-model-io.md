# 0075 — model-io & import robustness (seven bug-hunt findings)

Status: implemented
Flag: none new (correctness fixes; several are import-time / default-path)

## Goal

Fix the model-io / config-import cluster — including the one that could crash EVERY run at import, and the
Bedrock throttle that defeated the retry/backoff the endpoint most needs.

## The seven fixes

- **A malformed CODE_* value crashed at import** (`config.py`). ~34 env vars used bare `int()`/`float()`
  parses, so a single typo (`CODE_MAX_STEPS=abc`) raised an uncaught `ValueError` before the agent started —
  flag-off included. New `_env_int` / `_env_float` helpers parse with a fallback + a stderr warning; every bare
  parse now routes through them.
- **A Bedrock throttle was treated as fatal** (`model.py`). `_non_retryable`'s substring net matched Bedrock's
  throttle message ("Too many tokens, please wait…"), so a retryable `RateLimitError` raised with retries
  still left. It now short-circuits a rate-limit/throttle (`ratelimit`/`throttl` in the class name) as
  RETRYABLE before the substring check — backoff is exactly the fix.
- **The WEB note clobbered the workdir pin** (`prompts.py`). The WEB block used `note = (...)` instead of
  `note += (...)`, so with `CODE_WORKDIR_PROMPT` and a web tool both on, the specs/0030 WORKING DIRECTORY pin
  (and any earlier note) was silently dropped. `+=` fixes it.
- **`--warmup <non-number>` crashed with a traceback** (`cli.py`). The bare `float()` now yields a clean usage
  error + `sys.exit(2)`.
- **show_reasoning left the console dimmed on a mid-stream error** (`model.py`). `_assemble_stream`'s
  `\x1b[0m` reset only fired on clean completion; the stream loop is now wrapped in `try/finally` so the dim
  style is always reset even if the stream raises mid-way (exactly what the retry loop exists for).
- **A custom reasoning param was overridden by the ladder** (`config.py`). `reasoning_pin_overrides_ladder`
  checked only the VALUE, so a ladder-shaped value on a CUSTOM `CODE_REASONING_PARAM` returned False and
  adaptive effort silently replaced the pin. A non-`reasoning_effort` param now overrides regardless of value
  shape (the ladder can't set it at all).
- **An output-cap truncation was misdiagnosed as a cold worker** (`model.py`). The dropped-call check flagged
  any empty content + no tool_calls as a cold/scale-to-zero drop and re-warmed (a ~30-60s wait). It now
  excludes a `finish_reason == 'length'` truncation and a turn that produced `reasoning_content` — only a
  genuinely empty turn is a drop.

## Acceptance

`scripts/check_modelio_0075.py` (10/10, dep-free via a fake litellm), full dep-free suite 58/58:

- `_env_int`/`_env_float` fall back on a bad value and parse a valid one; `_non_retryable` retries a
  rate-limit/throttle and still raises on a real overflow/bad-request; `_assemble_stream` resets the dim style
  when the stream raises; the WORKING DIRECTORY pin survives a web tool; `reasoning_pin_overrides_ladder`
  respects a custom param; `--warmup abc` exits 2 not a traceback.

## Non-goals

- The dropped-call finish_reason guard (#7) is verified by review + the reassembled object carrying
  `finish_reason` (it lives inline in `complete()`, which needs the real retry loop to exercise).
- Not a change to WHICH errors litellm raises, only how OAC classifies them for retry.

## Byte-identity

A VALID config parses to the exact same values (only a malformed value changes — from a crash to a warned
fallback). `_non_retryable` only reclassifies rate-limit/throttle errors. The WEB `+=` restores dropped text.
The stream reset only differs on the error path. Verified: full dep-free suite 58/58.
