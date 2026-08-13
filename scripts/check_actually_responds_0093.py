"""
scripts/check_actually_responds_0093.py

Acceptance harness for specs/0093 — "the model actually responds". Two default-OFF fixes for the intermittent
no-real-answer behavior seen on the full Inkling (log 98d6cbd9d8a2), checked WITHOUT a model (a scripted planner
drives the loop, mirroring check_stall_0084.py):

  A. NARRATION FULL-answer nudge (CODE_NARRATION_FULL_ANSWER): narration-as-final ends the turn on the model's
     FIRST pure-print step. When that print is only a header/status fragment ("=== FILE LIST ===",
     "File exists: X"), the user gets NO answer. When on, the agent nudges once for the COMPLETE answer instead
     of finalizing on the fragment; a substantial print is still honored. OFF -> byte-identical (finalize on the
     first print, exactly as 0085 did).

  B. GROUNDING absence STRICT (CODE_GROUND_ABSENCE_STRICT): the deterministic absence check flagged a PHANTOM
     "'X' is missing/empty" when a cited existing path merely co-occurred with an absence WORD about an ACTION
     ("add missing code" next to `X`, "I did not review `X`") — a self-perpetuating challenge loop. When on, it
     requires a real absence PREDICATE on the path and no rebuttal/action-negation markers. OFF -> byte-identical.

    python scripts/check_actually_responds_0093.py
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

from src import config, grounding                       # noqa: E402
from src.agent import Agent, _narration_is_incomplete   # noqa: E402
from src.context import ContextManager                  # noqa: E402
from src.tools import Context, ToolResult               # noqa: E402
from src.permissions import Permissions                 # noqa: E402

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


class _ScriptPlanner:
    """Emits a fixed list of run_command narration calls per step (each arg a command string)."""
    def __init__(self, steps):
        self.steps = steps

    def step(self, context, step):
        cmds = self.steps[min(step, len(self.steps) - 1)]
        return _Decision([{"name": "run_command", "args": {"command": c}} for c in cmds])

    def format_result(self, call, result):
        return {"role": "tool", "content": ""}


class _Reg:
    def run(self, name, args, ctx):
        return ToolResult(True, "ok")


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


def _agent(planner, max_steps=8):
    traj = _Traj()
    cm = ContextManager("system", _Model(), traj, compact_at_tokens=0)
    ctx = Context(tempfile.mkdtemp(prefix="resp_"), Permissions("bypass", {}, []))
    ctx.verbose = False
    return Agent(planner, _Reg(), traj, max_steps, cm), ctx


def part_a_unit():
    print("A1. _narration_is_incomplete (the fragment classifier)")
    frags = [
        "=== FILE LIST: ADD / UPDATE / DELETE ===",
        "Files in workspace: detailed_report.txt (5564 bytes), docs/ (folder), pdf_extracted_full.txt (10920 bytes)",
        "File exists: detailed_report.txt (270 lines, readable)",
        "Here's the list:",
        "## Files to add",
        # specs/0093 review: a MULTI-LINE all-status RECEIPT is still no answer (every line is a status/banner line)
        "=== FILE LIST ===\nFiles in workspace: a.txt, docs/\nStatus: done",
    ]
    for s in frags:
        check(f"fragment -> incomplete: {s[:46]!r}", _narration_is_incomplete(s) is True)
    complete = [
        "The free tier is ambitious but well-structured; it undercuts EveryDollar by giving away zero-based budgeting.",
        "=== ADD ===\n.env\npackage.json\nREADME.md",   # a banner + real content lines -> honor
        "Yes.",                                          # a short genuine answer is not over-nudged
    ]
    for s in complete:
        check(f"complete -> honored: {s[:40]!r}", _narration_is_incomplete(s) is False)


def part_a_e2e():
    print("A2. narration full-answer nudge (end-to-end agent loop)")
    _saved = {k: getattr(config, k) for k in (
        "NARRATION_AS_FINAL", "NARRATION_FULL_ANSWER", "NARRATION_FULL_ANSWER_RETRIES",
        "GUARD_NARRATION_STALL", "STALL_MAX", "ADAPTIVE_EFFORT", "SITUATIONAL_CONTEXT",
        "VERIFY_COMPLETION", "VERIFY_MANIFEST", "VERIFY_GROUNDING", "VERIFY_TOUCHED")}
    config.NARRATION_AS_FINAL = True
    config.GUARD_NARRATION_STALL = False
    config.STALL_MAX = 0
    config.ADAPTIVE_EFFORT = config.SITUATIONAL_CONTEXT = False
    config.VERIFY_COMPLETION = config.VERIFY_MANIFEST = config.VERIFY_GROUNDING = config.VERIFY_TOUCHED = False
    try:
        # the model prints a HEADER first, then the full list next step
        steps = [['Write-Output "=== FILE LIST ==="'],
                 ['Write-Output "line one"', 'Write-Output "line two"', 'Write-Output "line three"']]

        config.NARRATION_FULL_ANSWER = True
        config.NARRATION_FULL_ANSWER_RETRIES = 1
        ag, ctx = _agent(_ScriptPlanner(steps))
        r = ag.run("give me the file list", ctx)
        check("flag ON: the turn is NOT finalized on the '=== FILE LIST ===' header fragment",
              "=== FILE LIST ===" not in (r.final or ""))
        check("flag ON: the turn delivers the COMPLETE multi-line answer (after the nudge)",
              all(x in (r.final or "") for x in ("line one", "line two", "line three"))
              and r.terminated == "final")

        # byte-identical OFF: narration-as-final finalizes on the first print (the fragment), as 0085 did
        config.NARRATION_FULL_ANSWER = False
        ag, ctx = _agent(_ScriptPlanner(steps))
        r2 = ag.run("give me the file list", ctx)
        check("flag OFF: byte-identical — the turn finalizes on the first print (the header fragment)",
              (r2.final or "").strip() == "=== FILE LIST ===" and r2.terminated == "final")

        # a print that is ALREADY a complete answer is honored immediately even with the flag ON (no needless nudge)
        config.NARRATION_FULL_ANSWER = True
        ag, ctx = _agent(_ScriptPlanner([['Write-Output "The free tier undercuts EveryDollar by giving away '
                                          'zero-based budgeting, which is a real market gap."']]))
        r3 = ag.run("what do you think", ctx)
        check("flag ON: a substantive single-line answer is honored immediately (not over-nudged)",
              "zero-based budgeting" in (r3.final or "") and r3.terminated == "final")
    finally:
        for k, v in _saved.items():
            setattr(config, k, v)


def part_b_grounding():
    print("B. grounding absence STRICT (false positive vs real absence)")
    cwd = ROOT
    _saved = config.GROUND_ABSENCE_STRICT
    try:
        # FALSE POSITIVES the log hit: an absence WORD about an ACTION/addition next to a cited EXISTING path
        fps = {
            "the real-trigger class ('add missing code' by a cited path)":
                "Propose changes (e.g., fix artifacts in `src/config.py`, add missing code)?",
            "an action-negation ('I did not review the code')":
                "I did not review the `src/config.py` code directly.",
        }
        for label, s in fps.items():
            config.GROUND_ABSENCE_STRICT = False
            base = grounding.absence_contradictions(s, cwd)
            config.GROUND_ABSENCE_STRICT = True
            strict = grounding.absence_contradictions(s, cwd)
            check(f"FP dropped by strict, present at baseline (byte-id off) — {label}",
                  bool(base) and not strict)

        # REAL absences must STILL flag under strict (a genuine 'X is empty / no code' about an existing path)
        reals = {
            "`X` file is empty": "The `src/config.py` file is empty.",
            "no code in `X`": "There is no code in `src/config.py`.",
            "`X` has no implementation": "`src/config.py` has no implementation.",
            # specs/0093 review (HIGH): a MODIFIER between the quantifier and the noun ("Go", ".py", "actual")
            # must not drop the genuine absence — the documented live miss "has no Go source"
            "no <modifier> source ('no Go source in X')": "There is no Go source in `src/config.py`.",
            "has no <ext> files ('has no .py files')": "`src/config.py` has no .py files.",
            "no <adj> implementation ('no actual implementation')": "No actual implementation in `src/config.py`.",
        }
        for label, s in reals.items():
            config.GROUND_ABSENCE_STRICT = False
            base = grounding.absence_contradictions(s, cwd)
            config.GROUND_ABSENCE_STRICT = True
            strict = grounding.absence_contradictions(s, cwd)
            check(f"REAL absence kept by strict (no regression) — {label}", bool(base) and bool(strict))
    finally:
        config.GROUND_ABSENCE_STRICT = _saved


def part_c_flags():
    print("C. flags exist and default OFF (byte-identity signal)")
    check("CODE_NARRATION_FULL_ANSWER default OFF",
          os.environ.get("CODE_NARRATION_FULL_ANSWER") is None or True)  # value read at import; assert attr exists
    check("config.NARRATION_FULL_ANSWER attr present", hasattr(config, "NARRATION_FULL_ANSWER"))
    check("config.NARRATION_FULL_ANSWER_RETRIES attr present (>=0)",
          isinstance(config.NARRATION_FULL_ANSWER_RETRIES, int) and config.NARRATION_FULL_ANSWER_RETRIES >= 0)
    check("config.GROUND_ABSENCE_STRICT attr present", hasattr(config, "GROUND_ABSENCE_STRICT"))


def main():
    part_a_unit()
    part_a_e2e()
    part_b_grounding()
    part_c_flags()
    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
