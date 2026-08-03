# 0060 — adaptive effort yields to a pinned reasoning pass-through

Status: implemented
Flag: none new (a precedence fix between specs/0021 and specs/0044)

## Goal

Stop adaptive effort from silently DOWNGRADING a pinned `xhigh`. Two effort mechanisms coexist:

- **Reasoning pass-through (specs/0044):** `CODE_REASONING_VALUE=xhigh` sends `reasoning_effort: xhigh` — a
  value ABOVE the old ladder.
- **Adaptive effort (specs/0021):** works on the fixed ladder `_EFFORTS = {"low","medium","high"}`, capped at
  `EFFORT_MAX="high"`, and it takes PRECEDENCE over the pass-through in `_reasoning_kwargs` (an explicit
  per-Model `effort` is branch 1; the pass-through is branch 2).

With both on (the operator's live config), the base model effort is `None` so `xhigh` is sent on calm turns —
but the moment the run STRUGGLES, `agent.py` "escalates" and stamps `model.effort = "high"`, which overrides
`xhigh` with `high`. So exactly when the agent is flailing (the case that most needs deep reasoning), it drops
BELOW the configured level. Since the ladder tops out at `high < xhigh`, adaptive effort can only ever
downgrade a pinned `xhigh` — it can never reach or exceed it.

## Concepts

- **The explicit pin wins.** `config.reasoning_pin_overrides_ladder()` is True when `CODE_REASONING_VALUE` is
  set to something the ladder cannot represent or exceed — `xhigh`, a numeric budget, or an object (anything
  that is not empty and not a plain `low`/`medium`/`high` string). `agent.py`'s adaptive-effort apply point
  gains `and not config.reasoning_pin_overrides_ladder()`, so when such a value is pinned the whole
  escalation block is skipped: `model.effort` stays at its baseline (`None`) and the `xhigh` pass-through is
  applied on EVERY turn, struggling or not.
- **Ladder values still adapt.** If `CODE_REASONING_VALUE` is empty or a plain ladder value, the helper
  returns False and adaptive effort runs exactly as before — byte-identical. This is only a guard against a
  pin the ladder can't honor.
- **Safe on any type.** The helper checks `isinstance(str)` before the set membership, so a JSON object/int
  `REASONING_VALUE` never raises.

## Acceptance

New assertions in `scripts/check_effort.py` (24/24, dep-free; now isolates `CODE_REASONING_VALUE`):

- `reasoning_pin_overrides_ladder()`: empty / a ladder value (`high`) -> False; `xhigh` / an int budget / an
  object -> True.
- With `CODE_REASONING_VALUE=xhigh` pinned, the SAME struggling turn that escalates to `high` without a pin
  does NOT escalate — `model.effort` stays `None`, so the pass-through survives.
- Every prior specs/0021 assertion still holds (escalation on struggle, depth-0 only, no cross-turn leak,
  the tool path, flag-off byte-identity) once `REASONING_VALUE` is isolated to empty for those tests.

## Non-goals

- Not a change to the ladder, the reactive/online policy, or `_reasoning_kwargs` precedence — only a gate on
  WHEN adaptive effort runs.
- Not a merge of the two systems into one scale (e.g. adding `xhigh` to the ladder) — the pass-through is
  deliberately open-ended (strings/budgets/objects), so "pin outranks ladder -> adaptive stands down" is the
  clean rule.
- No new flag, no `SCHEMA_VERSION` bump, nothing added to `safety_fingerprint`.

## Byte-identity

With `CODE_REASONING_VALUE` empty (default) or a plain ladder value, `reasoning_pin_overrides_ladder()` is
False and the adaptive-effort condition is unchanged — byte-for-byte specs/0021. The operator's immediate
fix (setting `CODE_ADAPTIVE_EFFORT=false`) also resolves it; this spec makes the two features SAFE to combine
so a re-enabled adaptive effort can never downgrade a pinned `xhigh` again. Verified: `check_effort` 24/24.
