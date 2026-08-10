# 0070 — a crashed / interrupted REPL turn is honest, not "completed"

Status: implemented
Flag: none new (a correctness fix to the REPL loop + its outcome capture)

## Goal

Close the one CRITICAL finding from the full bug hunt (plus its two convert.py twins and the Ctrl-C REPL-kill),
all in the REPL turn-exception path.

**The corpus poison.** When `agent.run` raised inside the REPL (a Bedrock 503, a context overflow), the
`except Exception` branch logged the error and `continue`d — but logged **no `turn_outcome`**. So a session
whose only turn crashed still had `traj.tool_calls > 0`, the `finally` stamped `session_end outcome='completed'`,
and `train/convert.py`'s legacy one-shot branch (reached precisely because there were zero `turn_outcome`
records) kept the truncated partial turn as a trainable **success**. A failed run trained as completed — the
exact corpus-poison class the whole outcome-honesty system exists to prevent. Two hunt findings were the same
root cause (`convert.py:214` legacy misroute; `convert.py:371` the segment counter skewing later turns' rows
onto the wrong index because the crashed turn advanced nothing).

**The Ctrl-C kill.** `KeyboardInterrupt` is a `BaseException`, not an `Exception`, so the guard never caught it:
pressing Ctrl-C to stop the weak model mid-loop unwound the entire REPL with a raw traceback instead of
returning to the `you>` prompt.

## Concepts

- **Stamp the crashed turn honestly.** The `except Exception` branch now calls
  `traj.log_turn_outcome(turns, "error", type(e).__name__, 0)` before `continue`. The outcome is written
  **directly as `"error"`**, NOT through `outcomes.classify` — `classify("error", tool_calls>0)` returns
  `"completed"`, which would re-introduce the very wash. `"error"` is not in `KEEP_OUTCOMES`, so
  `trainable_turns` marks the turn `False`.
- **One fix, three findings.** Because the crashed turn now emits a `turn_outcome`, `is_trainable` takes the
  per-turn path (not the legacy branch), drops the crashed turn as `no_trainable_turn` when it's the only turn,
  keeps a good turn beside a later crashed one, and keeps `to_rows`' segment counter aligned so honest
  `completed` turns are no longer shifted onto the wrong index.
- **Ctrl-C ends the turn, not the session.** A new `except KeyboardInterrupt` arm (before `except Exception`)
  stamps the turn `error/"interrupted"`, prints a short notice, and returns to the prompt — matching how a
  model error already behaves.

## Acceptance

`scripts/check_completion_honesty.py` (33/33, dep-free) gains:

- A crashed REPL turn stamped `turn_outcome='error'` → `is_trainable == (False, "no_trainable_turn")` (dropped,
  not trained as completed).
- A good turn beside a later crashed turn → session kept, and `to_rows` emits exactly the one good turn's row
  (per-turn honesty, counter aligned).

## Non-goals

- The `session_end` label at end-of-REPL still derives from `traj.tool_calls`; with per-turn `turn_outcome`
  records now always present, convert ignores it, so the poison is closed without touching that line (avoids
  disturbing existing session-label behavior).
- Not a change to what counts as a keeper outcome, nor to the one-shot path (which already labels errors).

## Byte-identity

A turn that does NOT raise is unchanged (same `log_turn_outcome` as before). The new records only appear on a
crash or a Ctrl-C — cases that previously produced a dishonest or fatal outcome. Verified:
`check_completion_honesty` 33/33, full dep-free suite green.
