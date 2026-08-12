"""
scripts/check_narration_final_0085.py

Acceptance harness for specs/0085 — narration-as-final: the ROOT fix for the narration loop, checked WITHOUT a
model. A scripted planner emits the EXACT loop from logs/9f1891b0af8d.log — run_command(Write-Output 'No
narration - direct reply delivered.') every step. With the flag ON the agent ends the turn at STEP 0 with the
printed text as the final answer (no loop, no guard needed); with it OFF the same planner loops to max_steps
(byte-identical). Also checks the multi-statement print extraction and that a real (non-narration) tool call is
unaffected. Run:

    python scripts/check_narration_final_0085.py
"""
import os
import sys
import types
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

if "litellm" not in sys.modules:
    _lit = types.ModuleType("litellm")
    _lit.completion = lambda *a, **k: None
    for _n in ("APIError", "APIConnectionError", "RateLimitError", "Timeout", "BadRequestError",
               "AuthenticationError"):
        setattr(_lit, _n, type(_n, (Exception,), {}))
    sys.modules["litellm"] = _lit

from src import config                                   # noqa: E402
from src.agent import Agent, _narration_text             # noqa: E402
from src.context import ContextManager                   # noqa: E402
from src.tools import Context, ToolResult                # noqa: E402
from src.permissions import Permissions                  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


class _Decision:
    def __init__(self, calls):
        self.assistant = {"role": "assistant", "content": ""}
        self.final = None
        self.calls = calls
        self.nudge = None
        self.gave_up = False
        self.dropped = False


class _CmdPlanner:
    """Emits the SAME run_command every step (the stuck-loop shape from the live log)."""
    def __init__(self, cmd):
        self.cmd = cmd

    def step(self, context, step):
        return _Decision([{"id": f"c{step}", "name": "run_command", "args": {"command": self.cmd}}])

    def format_result(self, call, result):
        return {"role": "tool", "content": ""}


class _Reg:
    def run(self, name, args, ctx):
        return ToolResult(True, "printed")


class _Model:
    def summarize(self, msgs):
        return "summary"


class _Traj:
    def __init__(self):
        self.tool_calls = 0
        self.steps = 0

    def log_turn(self, m): pass
    def log_compaction(self, *a): pass
    def log_tool_call(self, *a, **k): pass
    def log_permission(self, *a, **k): pass
    def log_verification(self, *a, **k): pass


def _run(cmd, max_steps=6):
    traj = _Traj()
    cm = ContextManager("system", _Model(), traj, compact_at_tokens=0)
    ctx = Context(tempfile.mkdtemp(prefix="narrfin_"), Permissions("bypass", {}, []))
    ctx.verbose = False
    return Agent(_CmdPlanner(cmd), _Reg(), traj, max_steps, cm).run("hi", ctx), traj


def main():
    _saved = {k: getattr(config, k) for k in (
        "NARRATION_AS_FINAL", "GUARD_NARRATION_STALL", "STALL_MAX", "ADAPTIVE_EFFORT", "SITUATIONAL_CONTEXT",
        "VERIFY_COMPLETION", "VERIFY_MANIFEST", "VERIFY_GROUNDING", "VERIFY_TOUCHED")}
    config.ADAPTIVE_EFFORT = config.SITUATIONAL_CONTEXT = False
    config.VERIFY_COMPLETION = config.VERIFY_MANIFEST = config.VERIFY_GROUNDING = config.VERIFY_TOUCHED = False
    config.GUARD_NARRATION_STALL = False   # isolate: no bandaid guard, so OFF must loop to max_steps
    config.STALL_MAX = 0

    # -- helper: pull the printed text out of narration prints (incl. multi-statement) --------------------
    check("_narration_text: extracts the Write-Output text (the model's intended reply)",
          _narration_text([{"name": "run_command", "args": {"command": "Write-Output 'hello there'"}}]) == "hello there")
    check("_narration_text: multi-statement 'Write-Output a; Write-Output b' -> both lines",
          _narration_text([{"name": "run_command",
                            "args": {"command": "Write-Output 'part one'; Write-Output 'part two'"}}]) == "part one\npart two")

    live = "Write-Output 'No narration - direct reply delivered.'"

    # -- flag ON: the EXACT live loop ends at STEP 0 with the printed text as the final answer -------------
    config.NARRATION_AS_FINAL = True
    r, traj = _run(live)
    check("flag ON: a narration-only reply ENDS the turn (terminated='final', not a loop)",
          r.terminated == "final")
    check("flag ON: the final answer IS the model's printed text",
          r.final == "No narration - direct reply delivered.")
    check("flag ON: it ends at STEP 0 (the FIRST narration — no wasted steps)", traj.steps == 1)

    # -- flag OFF: byte-identical — the same planner runs the print as a command and loops to max_steps ----
    config.NARRATION_AS_FINAL = False
    r_off, _ = _run(live)
    check("flag OFF: the same narration loop is NOT ended early as 'final' (byte-identical)",
          r_off.terminated != "final")

    # -- a REAL (non-narration) tool call is never converted (only pure prints are 'the model talking') ----
    config.NARRATION_AS_FINAL = True
    r_real, _ = _run("Get-Content README.md")
    check("flag ON: a real command (Get-Content) is NOT treated as a final answer",
          r_real.terminated != "final")

    for k, v in _saved.items():
        setattr(config, k, v)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
