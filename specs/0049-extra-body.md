# 0049 — extra-body

Status: implemented
Flag: `CODE_EXTRA_BODY` (default empty `{}` = off)

## Goal

Let the operator send ANY OpenAI-compatible request params a served model needs, ALONGSIDE the reasoning
knob, without a code change. The trigger is the move to Inkling-small on Thinking Machines Lab's Tinker
endpoint: if Tinker wants `separate_reasoning: true` in addition to `reasoning_effort`, that is two
`extra_body` keys — and the specs/0044 reasoning pass-through only sends ONE key (`{CODE_REASONING_PARAM:
CODE_REASONING_VALUE}`). Rather than hardcode a Tinker-specific flag, add a general merge so the provider
swap stays what we built it to be: a `.env` change, not a code change.

## Concepts

- **One merge point, one source of truth.** `Model._params()` builds the litellm kwargs and calls
  `_reasoning_kwargs()` (which may set `extra_body`). Right after, it merges `config.EXTRA_BODY`:
  `kw["extra_body"] = {**config.EXTRA_BODY, **kw.get("extra_body", {})}`. Because `_params()` feeds
  `complete()`, `summarize()`, and `warm_up()`, the merge applies to every call — provider params are not
  turn-specific.
- **The reasoning knob wins on a key collision.** `EXTRA_BODY` is the base and the reasoning-derived
  `extra_body` overrides it, so a dedicated `CODE_REASONING_*` value is never silently replaced by a stray
  `EXTRA_BODY` duplicate.
- **Provider-agnostic.** `EXTRA_BODY` always lands in `extra_body`. On a `bedrock/` model the reasoning knob
  is sent top-level (unchanged), and `EXTRA_BODY` still merges into `extra_body` beside it.
- **Typed by JSON, fail-safe.** `CODE_EXTRA_BODY` is `json.loads`-parsed; a non-object or malformed value
  degrades to `{}` and never raises at import (config is imported on every run).

## Acceptance

Each item is an assertion in `scripts/check_extra_body.py` (9/9, dep-free — fakes litellm; the import-time
parse runs in clean subprocesses).

- Flag off (`{}`) + no reasoning: `_params()` adds no `extra_body` key (byte-identical).
- Flag on, no reasoning: `extra_body` equals the operator dict.
- Merged alongside the reasoning pass-through: `reasoning_effort` and `separate_reasoning` coexist.
- Key collision: the reasoning knob wins over `CODE_EXTRA_BODY`'s same key.
- Bedrock: `reasoning_effort` stays top-level and `CODE_EXTRA_BODY` still lands in `extra_body`.
- Parse: a JSON object → the dict; a non-object / garbage → `{}`; unset → `{}`.

## Non-goals

- No schema/validation of the params — the endpoint is the authority; an unaccepted key surfaces as a
  provider 400, which the retry/backoff already handles.
- Not a replacement for `CODE_REASONING_*` — those stay the dedicated, precedence-winning reasoning path;
  `EXTRA_BODY` is the general escape hatch for everything else (and the second key reasoning can't carry).

## Byte-identity

`CODE_EXTRA_BODY` defaults `{}`, so the merge is skipped and `_params()` produces the exact pre-0049 kwargs
(verified: the flag-off assertion in `check_extra_body.py`; full suite unchanged). No `SCHEMA_VERSION` bump;
not in `safety_fingerprint`.

## Notes

Motivating swap (env-only, no code): Inkling-small on Tinker —
`CODE_MODEL=openai/<inkling-small-id>`, `CODE_API_BASE=https://tinker.thinkingmachines.dev/...oai/api/v1`,
`CODE_API_KEY=<tinker key>`. Whether `CODE_EXTRA_BODY={"separate_reasoning": true}` is needed at all is a
live-probe question — Together's Inkling returned `reasoning_content` without it; confirm against Tinker
before setting it.
