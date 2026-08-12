"""
scripts/check_review_digest_0088.py

Acceptance harness for specs/0088 — review digest fallback. Dep-free (no model). Proves _review_digest_body
strips the internal trailer, and that when a weak model COLLAPSES a review_repo synthesis into a receipt — via a
Write-Output print (narration-as-final path) OR as content (completion path) — the agent delivers the
substantive per-area DIGEST instead; a genuine substantive synthesis is kept; and the flag OFF is byte-identical.

    python scripts/check_review_digest_0088.py
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

from src import config                              # noqa: E402
from src.agent import Agent, _review_digest_body    # noqa: E402
from src.context import ContextManager              # noqa: E402
from src.tools import Context, ToolResult           # noqa: E402
from src.permissions import Permissions             # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


DIGEST_BODY = ("### root\n" + "index.html + style.css; favicon is a PNG misnamed .ico; dead footer CSS. " * 6
               + "\n### src\n" + "src holds only stubs, no real code; content is skeletal. " * 6)
DIGEST = ("Deterministic review fan-out over 2 area(s):\n" + DIGEST_BODY
          + "\n\nYou now have what you need. Write the FINAL review for the user NOW by synthesizing ALL 2 "
            "summaries above. Do NOT call read_file / tree again. Your next reply must be the finished review.")
RECEIPT = "Review complete. 2 areas covered. No edits made."
SYNTH = "Here is my real synthesis. " * 40   # a substantive answer (>400 chars) that must NOT be replaced


class _Decision:
    def __init__(self, calls=None, final=None):
        self.assistant = {"role": "assistant", "content": final or ""}
        self.final = final
        self.calls = calls or []
        self.nudge = None
        self.gave_up = False
        self.dropped = False


class _SeqPlanner:
    """steps: list of ('call', cmd) | ('final', text). A 'call' runs a tool (the fake registry sets the digest,
    simulating review_repo having run this turn); a 'final' is the model's closing answer."""
    def __init__(self, steps):
        self.steps = steps
        self.i = 0

    def step(self, context, step):
        kind, val = self.steps[min(self.i, len(self.steps) - 1)]
        self.i += 1
        if kind == "call":
            return _Decision(calls=[{"id": f"c{step}", "name": "run_command", "args": {"command": val}}])
        return _Decision(final=val)

    def format_result(self, call, result):
        return {"role": "tool", "content": ""}


class _Reg:
    def run(self, name, args, ctx):
        ctx._reviewed_digest = DIGEST      # simulate review_repo having set the digest this turn
        return ToolResult(True, "ok")


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


def _run(steps):
    traj = _Traj()
    cm = ContextManager("system", _Model(), traj, compact_at_tokens=0)
    c = Context(tempfile.mkdtemp(prefix="rd88_"), Permissions("bypass", {}, []))
    c.verbose = False
    return Agent(_SeqPlanner(steps), _Reg(), traj, 6, cm).run("review the project", c)


def main():
    _saved = {k: getattr(config, k) for k in (
        "REVIEW_DELIVER_DIGEST", "NARRATION_AS_FINAL", "GROUND_ANTI_COLLAPSE", "VERIFY_GROUNDING",
        "ADAPTIVE_EFFORT", "SITUATIONAL_CONTEXT", "VERIFY_COMPLETION", "VERIFY_MANIFEST", "VERIFY_TOUCHED",
        "GOAL_LOOP", "SPEC_FIRST", "GUARD_NARRATION_STALL", "STALL_MAX")}
    for k in ("ADAPTIVE_EFFORT", "SITUATIONAL_CONTEXT", "VERIFY_COMPLETION", "VERIFY_MANIFEST", "VERIFY_TOUCHED",
              "GOAL_LOOP", "SPEC_FIRST", "GUARD_NARRATION_STALL", "GROUND_ANTI_COLLAPSE"):
        setattr(config, k, False)
    config.VERIFY_GROUNDING = False
    config.STALL_MAX = 0

    # -- _review_digest_body strips the internal trailer --------------------------------------------------
    body = _review_digest_body(DIGEST)
    check("_review_digest_body: keeps the per-area summaries, DROPS the 'You now have what you need' trailer",
          "### root" in body and "### src" in body and "You now have what you need" not in body
          and "Your next reply" not in body)

    read_then = lambda last: [("call", "Get-Content index.html"), last]

    # -- narration-as-final path: a receipt PRINT after review_repo -> deliver the digest -----------------
    config.REVIEW_DELIVER_DIGEST = True
    config.NARRATION_AS_FINAL = True
    r = _run(read_then(("call", f"Write-Output '{RECEIPT}'")))
    check("flag ON (print receipt): the per-area DIGEST is delivered, not the receipt",
          r.final.startswith("Here's the review, area by area:") and "### root" in r.final and "### src" in r.final)

    # -- completion path: a receipt as CONTENT after review_repo -> deliver the digest --------------------
    r2 = _run(read_then(("final", RECEIPT)))
    check("flag ON (content receipt): the per-area DIGEST is delivered, not the receipt",
          "### root" in (r2.final or "") and "### src" in (r2.final or ""))

    # -- a SUBSTANTIVE synthesis is delivered as-is (not replaced by the digest) --------------------------
    r3 = _run(read_then(("final", SYNTH)))
    check("flag ON: a substantive synthesis is kept (NOT replaced by the digest)",
          r3.final == SYNTH)

    # -- flag OFF: byte-identical — the receipt is delivered, digest never substituted --------------------
    config.REVIEW_DELIVER_DIGEST = False
    r4 = _run(read_then(("final", RECEIPT)))
    check("flag OFF: the receipt is delivered as before (byte-identical, no digest fallback)",
          r4.final == RECEIPT)

    for k, v in _saved.items():
        setattr(config, k, v)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
