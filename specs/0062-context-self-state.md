# 0062 — reasoning-effort self-state in the situational block

Status: implemented
Flag: `CODE_CONTEXT_SELF_STATE` (default off)

## Goal

Let the agent report the reasoning level it's actually running at. Asked "what level of reasoning are you at,"
a live run said "I don't have a fixed level" — a model can't introspect its own request parameters, so it
confabulated, which is unsatisfying when the operator has deliberately pinned `CODE_REASONING_VALUE=xhigh`
and wants to confirm it's applied (and after specs/0060 fixed adaptive-effort silently downgrading it).

## Concepts

- **Put the effort where the agent can see it.** The situational block (specs/0012) already injects the
  agent's real environment (cwd / OS / shell / date) as a refreshed, trusted per-turn pin. When
  `CODE_CONTEXT_SELF_STATE` is on, `build_env_context` appends `- reasoning effort: <value>`, where the value
  is `config.display_effort()` (the same string the banner shows — e.g. `xhigh`). So when asked, the agent
  reports the real level from its environment instead of guessing.
- **Effort only — never the model.** The model id is DELIBERATELY omitted. specs/0061 forbids the agent
  revealing its base model/provider; injecting the model id into context would undermine that. So self-state
  carries the reasoning effort and nothing that identifies the underlying model.
- **Pure + injectable.** `build_env_context` gains a `reasoning_effort=None` parameter (like `shell_hints`),
  so it stays a pure function the harness drives directly; `agent.py` passes `config.display_effort()` only
  when the flag is on. Needs `CODE_SITUATIONAL_CONTEXT` (the block itself) to have any effect.

## Acceptance

Assertions in `scripts/check_situational.py` (17/17):

- No `reasoning_effort` passed (default): the block has no "reasoning effort" line — byte-identical.
- `reasoning_effort="xhigh"`: the block contains `- reasoning effort: xhigh`, and never the model name
  (`Inkling` absent) — specs/0061 respected.
- `CODE_CONTEXT_SELF_STATE` defaults False when unset.

## Non-goals

- Not a prompt instruction to always volunteer the level (specs/0051 says don't announce identity unprompted)
  — it just makes the value available so an ASKED question is answered accurately.
- Not the model id or provider (specs/0061).
- No `SCHEMA_VERSION` bump; the flag is not in `safety_fingerprint`.

## Byte-identity

`CODE_CONTEXT_SELF_STATE` off (default): `agent.py` passes `reasoning_effort=None`, so `build_env_context`
appends nothing and the situational block is byte-for-byte pre-0062. Verified by the OFF assertion in
`check_situational`. Requires `CODE_SITUATIONAL_CONTEXT` on to render at all.
