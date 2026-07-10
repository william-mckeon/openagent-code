"""
scripts/check_repl_outcomes.py

Acceptance harness for the per-turn corpus-integrity fix (0.7.0), checked WITHOUT a model or network.
Proves the seam that the audit found silently poisoning the corpus:

  * src/outcomes.classify maps every terminated label to an honest outcome (the ONE mapping shared by
    the one-shot CLI, each REPL turn, and the eval harness).
  * train/convert keeps a multi-turn REPL session PER TURN: a degenerate/ungrounded/verify-failed turn
    is dropped WITHOUT discarding the good turns beside it, and a session with no good turn is dropped.
  * a one-shot / legacy trajectory (no turn_outcome records) converts EXACTLY as before (regression).

Run:  python scripts/check_repl_outcomes.py
Exits 0 only if every check holds.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import outcomes  # noqa: E402
from train import convert  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


# -- tiny record builders --------------------------------------------------------
def _ss():
    return {"type": "session_start", "session_id": "s", "schema_version": "0.7.0", "tool_schemas": []}


def _user(t):
    return {"type": "turn", "message": {"role": "user", "content": t}}


def _mc(step, content, calls=()):
    tcs = [{"id": str(i), "name": n, "arguments": "{}"} for i, n in enumerate(calls)]
    return {"type": "model_call", "step": step,
            "request": {"messages": [{"role": "user", "content": "ctx"}], "tools": []},
            "response": {"content": content, "reasoning": None, "tool_calls": tcs}}


def _tc(ok=True):
    return {"type": "tool_call", "tool": "read_file", "ok": ok, "step": 0}


def _verif(ok):
    return {"type": "verification", "command": "py_compile", "ok": ok, "output": ""}


def _tout(turn, outcome, terminated="final", tc=1):
    return {"type": "turn_outcome", "turn": turn, "outcome": outcome, "terminated": terminated,
            "tool_calls": tc}


def _end(outcome="completed", tc=1):
    return {"type": "session_end", "outcome": outcome, "tool_calls": tc}


def _contents(rows):
    return [r["completion"].get("content", "") for r in rows]


def main():
    # 1. classify() — the shared honest mapping
    check("classify: clean finish with tool calls -> completed", outcomes.classify("final", 3) == "completed")
    check("classify: zero tool calls -> no_action", outcomes.classify("final", 0) == "no_action")
    check("classify: a gate outcome wins even at 0 tool calls",
          outcomes.classify("ungrounded_completion", 0) == "ungrounded_completion")
    check("classify: degenerate is preserved", outcomes.classify("degenerate", 1) == "degenerate")
    check("classify: nudge_exhausted -> protocol_stalled", outcomes.classify("nudge_exhausted", 5) == "protocol_stalled")
    check("classify: max_steps with tool calls -> max_steps", outcomes.classify("max_steps", 5) == "max_steps")
    check("classify: max_steps with zero tool calls -> no_action", outcomes.classify("max_steps", 0) == "no_action")

    # 2. a REPL session: turn 1 clean, turn 2 a repetition loop -> keep turn 1, DROP turn 2
    repl = [_ss(),
            _user("t1"), _mc(0, "", calls=["read_file"]), _tc(True), {"type": "turn", "message": {"role": "assistant", "content": "reading"}},
            _mc(1, "turn 1 answer"), _tout(1, "completed"),
            _user("t2"), _mc(2, "loop loop loop loop"), _tout(2, "degenerate", terminated="degenerate", tc=0),
            _end("completed", tc=1)]
    keep, reason = convert.is_trainable(repl)
    check("REPL with one good + one degenerate turn is KEPT (not dropped whole)", keep and reason == "kept")
    rows = convert.to_rows(repl, "as_sent")
    check("only the GOOD turn's steps become rows (degenerate turn dropped)", len(rows) == 2)
    check("the repetition-loop model_call is NOT a training target",
          all("loop loop loop" not in c for c in _contents(rows)))

    # 3. per-turn VERIFY scoping (the all-or-nothing fix): turn 1 verify PASS, turn 2 verify FAIL ->
    #    keep turn 1, drop turn 2. The OLD converter dropped the whole session on any failing verify.
    vrepl = [_ss(),
             _user("t1"), _mc(0, "a", calls=["edit_file"]), _tc(True), _verif(True), _tout(1, "completed"),
             _user("t2"), _mc(1, "b", calls=["edit_file"]), _tc(True), _verif(False), _tout(2, "completed"),
             _end("completed", tc=2)]
    keep, reason = convert.is_trainable(vrepl)
    check("a late verify-FAIL turn no longer drops the whole session (turn 1 survives)", keep and reason == "kept")
    check("only the verify-PASS turn's step is kept", len(convert.to_rows(vrepl, "as_sent")) == 1)

    # 4. a REPL session where EVERY turn is bad -> dropped
    allbad = [_ss(),
              _user("t1"), _mc(0, "x"), _tout(1, "degenerate", terminated="degenerate", tc=0),
              _user("t2"), _mc(1, "y"), _tout(2, "max_steps", terminated="max_steps", tc=0),
              _end("completed", tc=0)]
    keep, reason = convert.is_trainable(allbad)
    check("a REPL session with no trainable turn is dropped", (not keep) and reason == "no_trainable_turn")

    # 5. REGRESSION: a one-shot / legacy trajectory (no turn_outcome) converts exactly as before
    legacy_ok = [_ss(), _user("t"), _mc(0, "", calls=["read_file"]), _tc(True), _mc(1, "done"),
                 _end("completed", tc=1)]
    keep, reason = convert.is_trainable(legacy_ok)
    check("legacy clean one-shot still KEPT", keep and reason == "kept")
    check("legacy one-shot keeps every step (2 rows)", len(convert.to_rows(legacy_ok, "as_sent")) == 2)

    legacy_fail = [_ss(), _user("t"), _mc(0, "", calls=["edit_file"]), _tc(True), _verif(False),
                   _end("completed", tc=1)]
    keep, reason = convert.is_trainable(legacy_fail)
    check("legacy one-shot with a failing verify is still dropped whole (unchanged)",
          (not keep) and reason == "verify_failed")

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
