"""
scripts/check_goal.py

Acceptance harness for specs/0020 — the goal loop, checked WITHOUT a model, a network, or a shell.
A scripted planner emits a REAL `pursue` tool call (through a real Registry, so the tool's own entry
filter + permission gate run), then no-tool-call "done"s; goal.run_bar is monkeypatched to canned
outcomes, so the loop's control flow is exercised deterministically. Run:

    python scripts/check_goal.py

Exits 0 only if every check holds — including that CODE_GOAL_LOOP off is byte-identical to today.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, goal, outcomes, toolset  # noqa: E402
from src.agent import Agent  # noqa: E402
from src.context import ContextManager  # noqa: E402
from src.permissions import MUTATING, Permissions  # noqa: E402
from src.tools import Context, GOAL_TOOLS, Registry, pursue  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


class _Decision:
    def __init__(self, final=None, calls=()):
        self.assistant = {"role": "assistant", "content": final or ""}
        self.final = final
        self.calls = list(calls)
        self.nudge = None
        self.gave_up = False


class _Planner:
    """Step 0 declares the goal (a REAL pursue call); every later step says 'done' with no tool calls,
    which is what drives the gate chain (and therefore the bar) each iteration."""
    def __init__(self, bar=("npm", "test"), objective="make the tests pass", iters=3):
        self.args = {"objective": objective, "bar": list(bar), "max_iterations": iters}
        self.declared = False

    def step(self, context, step):
        if not self.declared:
            self.declared = True
            return _Decision(calls=[{"id": "1", "name": "pursue", "args": self.args}])
        return _Decision(final="Done - I believe the goal is met.")

    def format_result(self, call, result):
        return {"role": "tool", "content": str(result.content)}


class _Model:
    def summarize(self, msgs):
        return "summary"


class _Traj:
    def __init__(self):
        self.steps = 0
        self.tool_calls = 0
        self.verifs = []
        self.goals = []

    def log_turn(self, m): pass
    def log_compaction(self, *a): pass
    def log_tool_call(self, *a, **k): pass
    def log_permission(self, *a, **k): pass
    def log_verification(self, cmd, ok, output): self.verifs.append((cmd, ok, output))
    def log_goal(self, objective, bar, used, mx, met): self.goals.append((objective, list(bar), used, mx, met))


def _agent(traj, planner=None, max_steps=12):
    cm = ContextManager("system", _Model(), traj, compact_at_tokens=0)
    return Agent(planner or _Planner(), Registry(GOAL_TOOLS), traj, max_steps, cm)


def _ctx(mode="bypass"):
    return Context(tempfile.mkdtemp(prefix="goal_"), Permissions(mode, {}, []))


def main():
    _saved = (config.GOAL_LOOP, config.GOAL_MAX_ITERATIONS, config.GOAL_STEP_HEADROOM)
    _orig_run = goal.run_bar
    config.GOAL_LOOP = True
    config.GOAL_MAX_ITERATIONS = 3
    config.GOAL_STEP_HEADROOM = 2
    calls = {"n": 0}

    def _bar(results):
        """A stub bar: pops the next canned (ok, output) and counts runs."""
        seq = list(results)

        def run(bar, cwd, run_fn=None):
            calls["n"] += 1
            return seq[min(calls["n"] - 1, len(seq) - 1)]
        return run

    # -- entry filter (the loop-shape heuristic): a bar must be a runnable, non-destructive argv ---------
    check("entry filter: a shell STRING is refused (the whole argv discipline)", not goal.entry_ok("npm test")[0])
    check("entry filter: an argv list is accepted", goal.entry_ok(["npm", "test"])[0])
    check("entry filter: a shell/interpreter bar is refused (re-opens the shell)",
          not goal.entry_ok(["bash", "-c", "x"])[0] and not goal.entry_ok(["powershell", "-Command", "x"])[0])
    check("entry filter: inline code is refused (python -c is a shell by another name)",
          not goal.entry_ok(["python", "-c", "import os"])[0])
    check("entry filter: an interpreter WITHOUT inline code is fine (python -m pytest)",
          goal.entry_ok(["python", "-m", "pytest"])[0])
    check("entry filter: a DESTRUCTIVE bar is refused outright (a bar is a CHECK)",
          not goal.entry_ok(["rm", "-rf", "docs"])[0] and not goal.entry_ok(["find", ".", "-delete"])[0])
    check("entry filter: an empty / non-list bar is refused",
          not goal.entry_ok([])[0] and not goal.entry_ok(None)[0] and not goal.entry_ok([""])[0])

    # -- the tool: depth, gating, the operator ceiling ---------------------------------------------------
    c = _ctx()
    c.depth = 1
    check("pursue is TOP-LEVEL only (a child must not pursue its own loop)",
          not pursue({"objective": "x", "bar": ["npm", "test"]}, c).ok)
    c = _ctx("plan")            # plan mode is read-only -> a MUTATING tool is denied
    check("the bar is PERMISSION-GATED: a plan-mode run refuses it",
          not pursue({"objective": "x", "bar": ["npm", "test"]}, c).ok and c.goal is None)
    check("'pursue' is in MUTATING (else decide() would auto-allow it as read-only and the bar faces NO gate)",
          "pursue" in MUTATING)
    c = _ctx()
    pursue({"objective": "x", "bar": ["npm", "test"], "max_iterations": 99}, c)
    check("the operator's iteration ceiling always wins over the model's ask",
          c.goal["max_iterations"] == config.GOAL_MAX_ITERATIONS)
    c = _ctx()
    check("a refused bar sets NO goal (no loop without a bar)",
          not pursue({"objective": "x", "bar": "npm test"}, c).ok and c.goal is None)

    # -- toolset gating: flag-off means the tool isn't even OFFERED (byte-identical schemas) -------------
    config.GOAL_LOOP = False
    check("CODE_GOAL_LOOP off -> 'pursue' is not offered to the model",
          "pursue" not in [t["name"] for t in toolset.active_tools()])
    config.GOAL_LOOP = True
    check("CODE_GOAL_LOOP on -> 'pursue' is offered",
          "pursue" in [t["name"] for t in toolset.active_tools()])

    # -- the loop: bar fails then passes -> converge; ONE final reward, not one per attempt -------------
    calls["n"] = 0
    goal.run_bar = _bar([(False, "2 failing"), (False, "1 failing"), (True, "all green")])
    traj = _Traj()
    ctx = _ctx()
    r = _agent(traj).run("make the tests pass", ctx)
    check("loop: a failing bar re-prompts and iterates until it PASSES (not goal_unmet)",
          r.terminated != "goal_unmet" and calls["n"] == 3)
    check("loop: ONLY the FINAL bar result is logged as a reward (an intermediate failure would drop a "
          "CONVERGED loop from the corpus)", traj.verifs == [("npm test", True, "all green")])
    check("loop: the goal record marks it met", traj.goals and traj.goals[-1][4] is True)
    check("loop: ctx.goal is cleared once the bar passes (never re-run on a later re-prompt)", ctx.goal is None)

    # -- the loop: a bar that never passes -> honest goal_unmet, bounded by the iteration ceiling --------
    calls["n"] = 0
    goal.run_bar = _bar([(False, "still failing")])
    traj = _Traj()
    r = _agent(traj).run("make the tests pass", _ctx())
    check("loop: a bar that never passes -> honest 'goal_unmet'", r.terminated == "goal_unmet")
    check("loop: the iteration ceiling bounds the bar's REPETITION (the destructive cap can't)",
          calls["n"] == config.GOAL_MAX_ITERATIONS)
    check("loop: an unmet goal logs ONE failing reward + a goal record", len(traj.verifs) == 1
          and traj.verifs[0][1] is False and traj.goals[-1][4] is False)

    # -- step headroom: run() falls THROUGH the chain when steps run out (returning 'max_steps'), so the
    #    gate must own the honest label BEFORE the ceiling -------------------------------------------
    calls["n"] = 0
    goal.run_bar = _bar([(False, "nope")])
    traj = _Traj()
    r = _agent(traj, max_steps=3).run("make the tests pass", _ctx())   # ceiling < iterations
    check("step headroom: near max_steps the gate returns 'goal_unmet', not a max_steps fall-through",
          r.terminated == "goal_unmet")

    # -- flag OFF -> byte-identical: the gate never runs the bar, logs nothing --------------------------
    config.GOAL_LOOP = False
    calls["n"] = 0
    goal.run_bar = _bar([(False, "would fail IF consulted")])
    traj = _Traj()
    r = _agent(traj).run("make the tests pass", _ctx())
    check("CODE_GOAL_LOOP off: the gate is skipped - no bar run, no records, no goal_unmet",
          calls["n"] == 0 and traj.verifs == [] and traj.goals == [] and r.terminated != "goal_unmet")
    config.GOAL_LOOP = True   # positive control: the SAME setup DOES run the bar when on
    calls["n"] = 0
    _agent(_Traj()).run("make the tests pass", _ctx())
    check("positive control: the same setup DOES run the bar when the flag is on", calls["n"] > 0)

    # -- per-task reset: a prior task's bar must never be pursued on the next turn ----------------------
    calls["n"] = 0
    goal.run_bar = _bar([(True, "green")])
    ctx = _ctx()
    a = _agent(_Traj())
    a.run("make the tests pass", ctx)
    a.planner = _Planner()          # a fresh turn that declares nothing
    a.planner.declared = True       # ...emits only "done", never calls pursue
    calls["n"] = 0
    a.run("what project is this?", ctx)
    check("per-task reset: a stale goal from a prior task does NOT hijack the next turn",
          ctx.goal is None and calls["n"] == 0)

    # -- honest outcome plumbing: the THREE classify sites agree ----------------------------------------
    # src/subagent.py and eval/rubric.py each used to hand-COPY the gate list, so a new outcome was added
    # in one place and silently mislabeled in the others (AUDIT-FINDINGS row 3 fixed eval/harness.py and
    # left subagent.py). Both now READ the shared mapping — asserted at the source level because importing
    # subagent would drag in runtime -> model -> litellm and break this harness's dep-free contract.
    from train.convert import KEEP_OUTCOMES
    _sub = open(os.path.join(ROOT, "src", "subagent.py"), encoding="utf-8").read()
    _rub = open(os.path.join(ROOT, "eval", "rubric.py"), encoding="utf-8").read()
    check("outcomes: 'goal_unmet' is a gate outcome and classifies as itself",
          "goal_unmet" in outcomes.GATE_OUTCOMES and outcomes.classify("goal_unmet", 5) == "goal_unmet")
    check("outcomes: subagent._classify DELEGATES to the shared mapping (no private copy to drift)",
          "outcomes.classify(result.terminated, tool_calls)" in _sub
          and '"unverified_completion"' not in _sub)
    check("outcomes: eval/rubric reads the shared GATE_OUTCOMES (no hand-copied tuple)",
          "outcomes.GATE_OUTCOMES" in _rub and '"verify_failed_edits"' not in _rub)
    check("corpus: 'goal_unmet' is not a keeper -> a thrashing loop auto-drops from training",
          "goal_unmet" not in KEEP_OUTCOMES)

    goal.run_bar = _orig_run
    config.GOAL_LOOP, config.GOAL_MAX_ITERATIONS, config.GOAL_STEP_HEADROOM = _saved

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
