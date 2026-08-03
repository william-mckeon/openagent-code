"""
scripts/check_effort.py

Acceptance harness for specs/0021 — adaptive reasoning effort, checked WITHOUT a model or a network. The
pure policy is exercised directly; the agent APPLY point is driven with a scripted planner carrying a fake
Model (so effort is a plain attribute we can assert), a tool that always fails (to manufacture struggle),
and the gates off. Run:

    python scripts/check_effort.py

Exits 0 only if every check holds — including that CODE_ADAPTIVE_EFFORT off is byte-identical to today.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, effort  # noqa: E402
from src.agent import Agent  # noqa: E402
from src.context import ContextManager  # noqa: E402
from src.permissions import Permissions  # noqa: E402
from src.tools import Context, Registry, ToolResult, escalate_effort  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


# -- stubs (a planner with a fake model, a tool that fails to manufacture struggle) ---------------------
class _Model:
    def __init__(self, effort=None):
        self.effort = effort

    def summarize(self, m):
        return "s"

    def complete(self, *a, **k):          # only reached at the max_steps synthesis tail
        class _M:
            content, tool_calls, reasoning_content = "done", None, None
        return _M()


class _Decision:
    def __init__(self, final=None, calls=()):
        self.assistant = {"role": "assistant", "content": final or ""}
        self.final = final
        self.calls = list(calls)
        self.nudge = None
        self.gave_up = False


class _Planner:
    """Emits a FAILING tool call for `fails` steps (each drives consecutive_fail up), then 'done'."""
    def __init__(self, fails=2, effort=None):
        self.model = _Model(effort)
        self.fails = fails
        self.i = 0

    def step(self, ctx, step):
        self.i += 1
        if self.i <= self.fails:
            return _Decision(calls=[{"id": str(self.i), "name": "boom", "args": {}}])
        return _Decision(final="done")

    def format_result(self, call, result):
        return {"role": "tool", "content": str(result.content)}


class _Traj:
    def __init__(self):
        self.steps = 0
        self.tool_calls = 0
        self.changes = []

    def log_turn(self, m): pass
    def log_compaction(self, *a): pass
    def log_tool_call(self, *a, **k): pass
    def log_permission(self, *a, **k): pass
    def log_verification(self, *a, **k): pass
    def log_effort_change(self, old, new, struggle, req): self.changes.append((old, new, struggle))


_BOOM = {"name": "boom", "fn": lambda a, c: ToolResult(False, "boom"), "description": "",
         "parameters": {"type": "object", "properties": {}}}


def _agent(planner, traj, max_steps=8):
    cm = ContextManager("system", _Model(), traj, compact_at_tokens=0)
    return Agent(planner, Registry([_BOOM]), traj, max_steps, cm)


def _ctx(depth=0):
    c = Context(tempfile.mkdtemp(prefix="effort_"), Permissions("bypass", {}, []))
    c.depth = depth
    return c


def main():
    _saved = (config.ADAPTIVE_EFFORT, config.EFFORT_POLICY, config.EFFORT_THRESHOLD,
              config.EFFORT_FLOOR, config.EFFORT_MAX)
    # keep the honest-completion gates OUT of the way so a no-tool 'done' ends cleanly
    _g = (config.VERIFY_COMPLETION, config.VERIFY_GROUNDING, config.VERIFY_TOUCHED)
    config.VERIFY_COMPLETION = config.VERIFY_GROUNDING = config.VERIFY_TOUCHED = False
    config.EFFORT_THRESHOLD, config.EFFORT_FLOOR, config.EFFORT_MAX = 2, "medium", "high"
    # isolate the specs/0060 pin from the live .env — CODE_REASONING_VALUE=xhigh would (correctly) suppress
    # adaptive escalation, breaking the ladder tests below; the 0060 block sets it deliberately.
    _rv0 = config.REASONING_VALUE
    config.REASONING_VALUE = ""

    # -- the ladder + pure helpers -----------------------------------------------------------------------
    check("ladder is ORDERED (rank low<medium<high)", effort.rank("low") < effort.rank("medium") < effort.rank("high"))
    check("cap never exceeds the max", effort.cap("high", "medium") == "medium" and effort.cap("low", "high") == "low")
    check("resolve_baseline: a rung passes, None/'' -> the floor",
          effort.resolve_baseline("high") == "high" and effort.resolve_baseline(None) == config.EFFORT_FLOOR)
    check("struggle_score sums the signals", effort.struggle_score(consec=1, retries=1, goal_fails=1) == 3)

    # -- reactive policy: escalate-only, threshold, cap, floor -------------------------------------------
    r = effort.ReactivePolicy()
    check("reactive: no struggle, no request -> stays at floor",
          r.decide("medium", None, 0, "high") == "medium")
    check("reactive: struggle >= threshold -> one rung up", r.decide("medium", None, 2, "high") == "high")
    check("reactive: a tool request raises immediately", r.decide("medium", "high", 0, "high") == "high")
    check("reactive: capped, and never below the floor",
          r.decide("medium", None, 9, "medium") == "medium" and r.decide("high", None, 0, "high") == "high")

    # -- load_policy: switchable + fail-safe -------------------------------------------------------------
    config.EFFORT_POLICY = "off"; check("policy 'off' selected", effort.load_policy().name == "off")
    config.EFFORT_POLICY = "reactive"; check("policy 'reactive' selected", effort.load_policy().name == "reactive")
    config.EFFORT_POLICY = "online"; check("policy 'online' selected (opt-in learner)", effort.load_policy().name == "online")
    config.EFFORT_POLICY = "nope.nope:X"; check("a broken custom policy FALLS BACK to reactive",
                                                effort.load_policy().name == "reactive")
    config.EFFORT_POLICY = "reactive"

    # -- the escalate_effort tool: sticky, escalate-only -------------------------------------------------
    c = _ctx()
    escalate_effort({"level": "high"}, c)
    check("escalate_effort stashes the level on ctx.effort", c.effort == "high")
    escalate_effort({"level": "low"}, c)
    check("escalate_effort is escalate-ONLY (a lower level doesn't lower it)", c.effort == "high")
    check("an invalid level is refused", not escalate_effort({"level": "ludicrous"}, c).ok)

    # -- the APPLY point: auto-escalate on struggle (depth 0) -------------------------------------------
    config.ADAPTIVE_EFFORT = True
    p = _Planner(fails=3)
    _agent(p, _Traj()).run("fix the tangled bug", _ctx())
    check("ADAPTIVE on: repeated tool failures auto-escalate the model to 'high'", p.model.effort == "high")

    # -- specs/0060: a REASONING pass-through that OUTRANKS the ladder (xhigh) makes adaptive a NO-OP -------
    def _pin(v):
        _old = config.REASONING_VALUE
        config.REASONING_VALUE = v
        try:
            return config.reasoning_pin_overrides_ladder()
        finally:
            config.REASONING_VALUE = _old
    check("pin helper: empty / a ladder value ('high') -> False (adaptive runs as before)",
          _pin("") is False and _pin("high") is False)
    check("pin helper: xhigh / an int budget / an object -> True (ladder can't represent or exceed it)",
          _pin("xhigh") is True and _pin(8000) is True and _pin({"budget": 1}) is True)
    _rv = config.REASONING_VALUE
    config.REASONING_VALUE = "xhigh"
    p_pin = _Planner(fails=3)   # the SAME struggle that escalated to 'high' just above
    _agent(p_pin, _Traj()).run("fix the tangled bug", _ctx())
    check("specs/0060: with xhigh pinned, a struggling turn does NOT escalate — the pass-through is preserved",
          p_pin.model.effort is None)
    config.REASONING_VALUE = _rv

    # -- depth-0 ONLY: a subagent's effort is never clobbered -------------------------------------------
    p2 = _Planner(fails=3, effort="low")   # a child built with its own effort (e.g. GUARDIAN_EFFORT)
    _agent(p2, _Traj()).run("subtask", _ctx(depth=1))
    check("depth>0: a subagent keeps its own effort (never auto-escalated)", p2.model.effort == "low")

    # -- no cross-turn LEAK: a bumped effort is restored to baseline next task --------------------------
    p3 = _Planner(fails=3)
    ag = _agent(p3, _Traj())
    ag.run("hard task one", _ctx())
    check("turn 1 escalated", p3.model.effort == "high")
    p3.fails = 0                            # turn 2 does not struggle
    ag.run("what project is this?", _ctx())
    check("turn 2 does NOT inherit turn 1's escalation (reset to baseline; no struggle -> stays as-built)",
          p3.model.effort == p3.__class__(fails=0).model.effort)   # baseline == None (as built)

    # -- the tool path applies too ---------------------------------------------------------------------
    p4 = _Planner(fails=0)

    class _PlannerAsk(_Planner):
        def step(self, ctx, step):
            if self.i == 0:                 # first step: the model asks for more effort
                self.i += 1
                return _Decision(calls=[{"id": "1", "name": "escalate_effort", "args": {"level": "high"}}])
            return _Decision(final="done")
    pa = _PlannerAsk(fails=0)
    ag2 = _agent(pa, _Traj())
    ag2.registry = Registry([_BOOM, {"name": "escalate_effort", "fn": escalate_effort, "description": "",
                                     "parameters": {"type": "object", "properties": {}}}])
    ag2.run("please think hard", _ctx())
    check("the escalate_effort TOOL raises the model effort on the next step", pa.model.effort == "high")

    # -- flag OFF -> byte-identical: the model effort is never touched ----------------------------------
    config.ADAPTIVE_EFFORT = False
    p5 = _Planner(fails=5)
    tr = _Traj()
    _agent(p5, tr).run("hard task", _ctx())
    check("ADAPTIVE off: the model effort is NEVER changed and nothing is logged",
          p5.model.effort is None and tr.changes == [])

    config.ADAPTIVE_EFFORT, config.EFFORT_POLICY, config.EFFORT_THRESHOLD, config.EFFORT_FLOOR, config.EFFORT_MAX = _saved
    config.VERIFY_COMPLETION, config.VERIFY_GROUNDING, config.VERIFY_TOUCHED = _g
    config.REASONING_VALUE = _rv0

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
