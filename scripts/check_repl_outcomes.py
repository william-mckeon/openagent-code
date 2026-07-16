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


def _mc(step, content, calls=(), reasoning=None):
    tcs = [{"id": str(i), "name": n, "arguments": "{}"} for i, n in enumerate(calls)]
    return {"type": "model_call", "step": step,
            "request": {"messages": [{"role": "user", "content": "ctx"}], "tools": []},
            "response": {"content": content, "reasoning": reasoning, "tool_calls": tcs}}


def _tc(ok=True):
    return {"type": "tool_call", "tool": "read_file", "ok": ok, "step": 0}


def _verif(ok):
    return {"type": "verification", "command": "py_compile", "ok": ok, "output": ""}


def _tout(turn, outcome, terminated="final", tc=1):
    return {"type": "turn_outcome", "turn": turn, "outcome": outcome, "terminated": terminated,
            "tool_calls": tc}


def _end(outcome="completed", tc=1):
    return {"type": "session_end", "outcome": outcome, "tool_calls": tc}


def _perm(step, allowed, action="deny"):
    return {"type": "permission", "session_id": "s", "step": step, "tool": "delete_file", "target": "x",
            "allowed": allowed, "action": action, "reason": "guardian denied", "rule": None, "mode": "default"}


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

    # 5b. ride-5 corpus integrity: a turn holding a guardian/permission DENIAL is CONTESTED -> excluded,
    #     but a clean turn beside it survives; the denied action never becomes a positive SFT target.
    contested = [_ss(),
                 _user("t1"), _mc(0, "clean answer", calls=["read_file"]), _tc(True), _tout(1, "completed"),
                 _user("t2"), _perm(1, False), _mc(1, "I could not delete it", calls=["delete_file"]), _tc(False), _tout(2, "completed"),
                 _end("completed", tc=2)]
    check("a contested turn doesn't drop the whole session (clean turn 1 survives)",
          convert.is_trainable(contested) == (True, "kept"))
    crows = convert.to_rows(contested, "as_sent")
    check("the contested turn is excluded (only the clean turn's step is a row)", len(crows) == 1)
    check("the denied delete_file action is NOT a training target",
          all("could not delete" not in c for c in _contents(crows)))
    check("_contested_turns pinpoints the denied turn", convert._contested_turns(contested) == {2})

    onedenied = [_ss(), _user("t"), _perm(0, False), _mc(0, "blocked", calls=["delete_file"]), _tc(False),
                 _end("completed", tc=1)]
    check("a one-shot run that hit a denial is dropped as guardian_contested",
          convert.is_trainable(onedenied) == (False, "guardian_contested"))

    approved = [_ss(), _user("t"), _perm(0, True, "ask"), _mc(0, "done", calls=["delete_file"]), _tc(True), _tout(1, "completed"),
                _end("completed", tc=1)]
    check("an APPROVED (allowed) ask-tier call is NOT contested (turn kept)",
          convert.is_trainable(approved)[0] and convert._contested_turns(approved) == set())

    # 5c. Phase 20: a goal loop that THRASHED to exhaustion ends 'goal_unmet' — not a keeper, so it drops
    #     itself. Teaching "pursue a bar, never meet it, stop" is exactly the thrash we must not train.
    check("classify: goal_unmet is preserved as an honest outcome", outcomes.classify("goal_unmet", 6) == "goal_unmet")
    unmet_turn = [_ss(),
                  _user("t1"), _mc(0, "good", calls=["read_file"]), _tc(True), _tout(1, "completed"),
                  _user("t2"), _mc(1, "I could not make the bar pass", calls=["edit_file"]), _tc(True),
                  _tout(2, "goal_unmet", terminated="goal_unmet"),
                  _end("completed", tc=2)]
    check("a goal_unmet TURN is dropped while the good turn beside it survives",
          convert.is_trainable(unmet_turn)[0] and len(convert.to_rows(unmet_turn, "as_sent")) == 1)
    unmet_one = [_ss(), _user("t"), _mc(0, "never converged", calls=["edit_file"]), _tc(True),
                 _end("goal_unmet", tc=1)]
    check("a one-shot goal_unmet run is dropped whole", convert.is_trainable(unmet_one) == (False, "goal_unmet"))

    # 6. reasoning channel: a TOOL-CALL target folds its reasoning into content (matching the runtime
    #    planner) instead of dropping it, and the fold is NOT preamble-stripped; a FINAL answer stays
    #    clean and preamble-stripped. Dropping reasoning had trained reasoning-free (looping) tool calls.
    rr = [_ss(), _user("t"),
          _mc(0, "", calls=["read_file"], reasoning="First read the file, then edit the import."),
          _tc(True),
          _mc(1, "Done - the import is fixed."),
          _end("completed", tc=1)]
    rows = convert.to_rows(rr, "as_sent")
    tool_target, final_target = rows[0]["completion"], rows[1]["completion"]
    check("a tool-call SFT target FOLDS the reasoning into content (not dropped, not stripped)",
          "First read the file, then edit the import." in tool_target.get("content", "")
          and bool(tool_target.get("tool_calls")))
    check("a final-answer SFT target has no tool_calls and keeps its user-facing content",
          (not final_target.get("tool_calls")) and "Done - the import is fixed." in final_target.get("content", ""))

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
