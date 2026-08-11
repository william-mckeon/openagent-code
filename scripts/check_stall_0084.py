"""
scripts/check_stall_0084.py

Acceptance harness for specs/0084 — the subagent propose-deadlock fix + the no-progress stall breaker, checked
WITHOUT a model or network (a scripted planner + fake registry, mirroring check_narration_stall.py). Proves:
  - _child_permissions never lets a serial child inherit PROPOSE mode when CODE_SUBAGENT_NO_PROPOSE is on
    (it gets the honest plan-mode read-only view), and is byte-identical when off;
  - the no-progress stall breaker ends a DUPLICATE / DENIED loop as an honest 'stall', leaves a genuinely
    progressing run alone, and is byte-identical when CODE_STALL_MAX=0;
  - the pure helpers (_is_noop_narration multi-line, _canon_call novelty) and the 'stall' outcome registration.

    python scripts/check_stall_0084.py
"""
import os
import sys
import types
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# src.subagent -> runtime -> model imports litellm, which isn't installed in this harness env. Inject a stub so
# the import chain resolves (we never call the model here — a scripted planner drives the loop).
if "litellm" not in sys.modules:
    _lit = types.ModuleType("litellm")
    _lit.completion = lambda *a, **k: None
    for _n in ("APIError", "APIConnectionError", "RateLimitError", "Timeout", "BadRequestError",
               "AuthenticationError"):
        setattr(_lit, _n, type(_n, (Exception,), {}))
    sys.modules["litellm"] = _lit

from src import config, outcomes                       # noqa: E402
from src.agent import Agent, _is_noop_narration, _canon_call  # noqa: E402
from src.context import ContextManager                 # noqa: E402
from src.tools import Context, ToolResult              # noqa: E402
from src.permissions import Permissions                # noqa: E402
from src.subagent import _child_permissions            # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


class _Decision:
    def __init__(self, calls):
        self.assistant = {"role": "assistant", "content": ""}
        self.final = ""
        self.calls = calls
        self.nudge = None
        self.gave_up = False
        self.dropped = False


class _StepPlanner:
    """Emits one tool call per step; `fn(step)` returns the (name, args) so a test can drive duplicate / novel /
    denied loops. Ignores nudges (a stuck weak model)."""
    def __init__(self, fn):
        self.fn = fn

    def step(self, context, step):
        name, args = self.fn(step)
        return _Decision([{"name": name, "args": args}])

    def format_result(self, call, result):
        return {"role": "tool", "content": ""}


class _Reg:
    def run(self, name, args, ctx):
        return ToolResult(True, "ok")       # an ALLOWED call "succeeds"; denials never reach here


class _Model:
    def summarize(self, msgs):
        return "summary"


class _Traj:
    def __init__(self):
        self.tool_calls = 0

    def log_turn(self, m): pass
    def log_compaction(self, *a): pass
    def log_tool_call(self, *a, **k): pass
    def log_permission(self, *a, **k): pass
    def log_verification(self, *a, **k): pass


def _agent(planner, mode="bypass", rules=None, max_steps=14):
    traj = _Traj()
    cm = ContextManager("system", _Model(), traj, compact_at_tokens=0)
    ctx = Context(tempfile.mkdtemp(prefix="stall_"), Permissions(mode, rules or {}, []))
    ctx.verbose = False
    return Agent(planner, _Reg(), traj, max_steps, cm), ctx


def main():
    _saved = {k: getattr(config, k) for k in (
        "STALL_MAX", "STALL_RETRIES", "SUBAGENT_NO_PROPOSE", "GUARD_NARRATION_STALL", "PROPOSE",
        "ADAPTIVE_EFFORT", "SITUATIONAL_CONTEXT",
        "VERIFY_COMPLETION", "VERIFY_MANIFEST", "VERIFY_GROUNDING", "VERIFY_TOUCHED")}
    config.ADAPTIVE_EFFORT = config.SITUATIONAL_CONTEXT = False
    config.VERIFY_COMPLETION = config.VERIFY_MANIFEST = config.VERIFY_GROUNDING = config.VERIFY_TOUCHED = False
    config.GUARD_NARRATION_STALL = False   # isolate the stall breaker from the narration guard

    # -- pure helpers -------------------------------------------------------------------------------------
    check("_is_noop_narration: multi-line 'Write-Output a; Write-Output b' IS narration (the 0067 escape)",
          _is_noop_narration('Write-Output "a"; Write-Output "b"')
          and _is_noop_narration('Write-Output "x"\nWrite-Output "y"'))
    check("_is_noop_narration: a single print is narration; a real command / mixed is NOT",
          _is_noop_narration('Write-Output "status"')
          and not _is_noop_narration('Get-Content f.txt')
          and not _is_noop_narration('Write-Output "a"; Remove-Item b'))
    check("_canon_call: same (tool,args) collide; a different path is novel",
          _canon_call("read_file", {"path": "a"}) == _canon_call("read_file", {"path": "a"})
          and _canon_call("read_file", {"path": "a"}) != _canon_call("read_file", {"path": "b"}))

    # -- outcome registration -----------------------------------------------------------------------------
    check("outcomes: 'stall' is a gate outcome, returned as-is (never washed to completed)",
          "stall" in outcomes.GATE_OUTCOMES and outcomes.classify("stall", 9) == "stall")

    # -- subagent propose-deadlock projection (_child_permissions) ----------------------------------------
    propose_parent = Permissions("propose", {}, [])
    config.SUBAGENT_NO_PROPOSE = False
    check("SUBAGENT_NO_PROPOSE off: a serial child INHERITS the parent mode (byte-identical)",
          _child_permissions(propose_parent, False).mode == "propose")
    config.SUBAGENT_NO_PROPOSE = True
    check("SUBAGENT_NO_PROPOSE on: a serial child of a PROPOSE parent gets plan-mode read-only (no deadlock)",
          _child_permissions(propose_parent, False).mode == "plan")
    check("SUBAGENT_NO_PROPOSE on: a serial child of a BYPASS parent is unchanged (only propose is projected)",
          _child_permissions(Permissions("bypass", {}, []), False).mode == "bypass")
    check("read_only=True always projects to plan (parallel fan-out, specs/0039 unchanged)",
          _child_permissions(propose_parent, True).mode == "plan")
    # behavioral: the projected child denies a mutation with the HONEST terminal, not the unsatisfiable
    # 'call propose_changes' bait a depth>0 child can never satisfy.
    config.PROPOSE = True
    child = _child_permissions(propose_parent, False)   # plan mode
    cctx = Context(tempfile.mkdtemp(prefix="child_"), child)
    cctx.depth = 1
    d = child.decide("write_file", {"path": "x.txt", "content": "y"}, cctx)
    check("projected (plan) child denies a mutation WITHOUT the 'manifest/propose_changes' deadlock bait",
          (not d.allowed) and "manifest" not in (d.reason or "").lower())

    # -- the no-progress stall breaker (end-to-end) -------------------------------------------------------
    config.STALL_MAX = 3
    config.STALL_RETRIES = 1

    # (a) DUPLICATE loop: the same real read every step (first novel, then repeats) -> honest 'stall'
    ag, ctx = _agent(_StepPlanner(lambda s: ("run_command", {"command": "Get-Content data.txt"})))
    check("flag ON: a DUPLICATE-action loop (re-reading the same file) ends as 'stall'",
          ag.run("review the file over and over", ctx).terminated == "stall")

    # (b) DENIED loop: a mutation the plan-mode gate always denies -> no progress -> 'stall'
    ag, ctx = _agent(_StepPlanner(lambda s: ("write_file", {"path": "a.txt", "content": "x"})), mode="plan")
    check("flag ON: a DENIED-action loop (mutation refused every step) ends as 'stall'",
          ag.run("try to write despite being read-only", ctx).terminated == "stall")

    # (c) genuine PROGRESS: a NOVEL read each step never trips the breaker (-> max_steps, not stall)
    ag, ctx = _agent(_StepPlanner(lambda s: ("run_command", {"command": f"Get-Content file{s}.txt"})))
    check("flag ON: a genuinely progressing run (novel reads) is NOT flagged as a stall",
          ag.run("read many distinct files", ctx).terminated != "stall")

    # (d) flag OFF: byte-identical — the same duplicate loop is NOT a stall
    config.STALL_MAX = 0
    ag, ctx = _agent(_StepPlanner(lambda s: ("run_command", {"command": "Get-Content data.txt"})))
    check("flag OFF (STALL_MAX=0): the duplicate loop is NOT 'stall' (breaker skipped, byte-identical)",
          ag.run("review the file over and over", ctx).terminated != "stall")

    for k, v in _saved.items():
        setattr(config, k, v)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
