"""
scripts/check_ground_anticollapse_0087.py

Acceptance harness for specs/0087 — grounding anti-collapse / anti-hijack. Dep-free (no model). Proves:
  - drop_contradicted_flags drops a semantic flag the REAL tree contradicts (a cited path flagged 'not found'
    that EXISTS; a file claimed 'present' that is ABSENT) and KEEPS a genuine flag;
  - the challenge is reworded to re-send the COMPLETE answer when the flag is on (byte-identical text off);
  - _answer_collapsed detects a receipt-sized correction;
  - end-to-end: when a grounding correction COLLAPSES a real review into a receipt, the agent delivers the fuller
    ORIGINAL (flag on) but the receipt when off (byte-identical).

    python scripts/check_ground_anticollapse_0087.py
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
from src.agent import Agent, _answer_collapsed          # noqa: E402
from src.context import ContextManager                  # noqa: E402
from src.tools import Context, ToolResult               # noqa: E402
from src.permissions import Permissions                 # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


class _Ctx:
    def __init__(self, cwd):
        self.cwd = cwd


class _Decision:
    def __init__(self, final):
        self.assistant = {"role": "assistant", "content": final}
        self.final = final
        self.calls = []
        self.nudge = None
        self.gave_up = False
        self.dropped = False


class _AnswerPlanner:
    """Emits a preset final answer per step (calls=[], so it hits the completion/grounding gate each step)."""
    def __init__(self, answers):
        self.answers = answers
        self.i = 0

    def step(self, context, step):
        a = self.answers[min(self.i, len(self.answers) - 1)]
        self.i += 1
        return _Decision(a)

    def format_result(self, call, result):
        return {"role": "tool", "content": ""}


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


def main():
    _saved = {k: getattr(config, k) for k in (
        "GROUND_ANTI_COLLAPSE", "VERIFY_GROUNDING", "VERIFY_GROUNDING_RETRIES", "NARRATION_AS_FINAL",
        "ADAPTIVE_EFFORT", "SITUATIONAL_CONTEXT", "VERIFY_COMPLETION", "VERIFY_MANIFEST", "VERIFY_TOUCHED",
        "GOAL_LOOP", "SPEC_FIRST")}
    for k in ("ADAPTIVE_EFFORT", "SITUATIONAL_CONTEXT", "VERIFY_COMPLETION", "VERIFY_MANIFEST",
              "VERIFY_TOUCHED", "GOAL_LOOP", "SPEC_FIRST", "NARRATION_AS_FINAL"):
        setattr(config, k, False)
    config.VERIFY_GROUNDING = True
    config.VERIFY_GROUNDING_RETRIES = 2

    # -- drop_contradicted_flags: a tree with style.css + index.html but NO Agent.py --------------------
    ws = tempfile.mkdtemp(prefix="g87_")
    open(os.path.join(ws, "style.css"), "w").write("/* css */")
    open(os.path.join(ws, "index.html"), "w").write("<html></html>")
    ctx = _Ctx(ws)
    check("drops a flag the tree contradicts: '../style.css not found' but style.css EXISTS",
          grounding.drop_contradicted_flags(["'../style.css' - cited but not found in the workspace"], ctx) == [])
    check("drops a flag the tree contradicts: 'Agent.py present in repo' but Agent.py is ABSENT",
          grounding.drop_contradicted_flags(["'Agent.py'/tools.py present in repo; absence claim false"], ctx) == [])
    check("KEEPS a genuine contradiction flag (path exists, no present/absent polarity)",
          grounding.drop_contradicted_flags(["index.html says port 80 but compose says 8081"], ctx)
          == ["index.html says port 80 but compose says 8081"])
    check("KEEPS a flag with no path token (nothing to cross-check)",
          grounding.drop_contradicted_flags(["the closing summary overstates completeness"], ctx)
          == ["the closing summary overstates completeness"])
    check("KEEPS a CORRECT absence flag: 'ghost.md is missing' and ghost.md truly absent",
          grounding.drop_contradicted_flags(["'ghost.md' is missing from the repo"], ctx)
          == ["'ghost.md' is missing from the repo"])
    # basename-collision guard: a SPECIFIC missing path isn't false-dropped by an unrelated same-basename file
    os.makedirs(os.path.join(ws, "src"), exist_ok=True)
    open(os.path.join(ws, "config.py"), "w").write("x = 1")   # a ROOT config.py exists
    check("KEEPS a genuine catch by PATH-SUFFIX: 'src/auth/config.py missing' NOT dropped by root config.py",
          grounding.drop_contradicted_flags(["'src/auth/config.py' is missing"], ctx)
          == ["'src/auth/config.py' is missing"])
    check("still drops a bare same-suffix false flag: 'config.py not found' when root config.py exists",
          grounding.drop_contradicted_flags(["'config.py' cited but not found"], ctx) == [])
    # content-absence (bug #1): 'missing IN <existing file>' is a genuine catch — file existence must NOT drop it
    open(os.path.join(ws, "auth.py"), "w").write("def login(): pass")
    check("KEEPS a CONTENT-absence catch: 'signature validation is missing in auth.py' (auth.py exists)",
          grounding.drop_contradicted_flags(["signature validation is missing in auth.py"], ctx)
          == ["signature validation is missing in auth.py"])

    # -- challenge reword (gated) -------------------------------------------------------------------------
    config.GROUND_ANTI_COLLAPSE = True
    ch_on = grounding.challenge(["'../style.css' not found"])
    check("flag ON: challenge tells the model to RE-SEND its COMPLETE answer (not 'nothing else')",
          "RE-SEND your COMPLETE answer" in ch_on and "nothing else" not in ch_on)
    config.GROUND_ANTI_COLLAPSE = False
    ch_off = grounding.challenge(["'../style.css' not found"])
    check("flag OFF: challenge is the original text (byte-identical)",
          "and nothing else" in ch_off and "RE-SEND your COMPLETE answer" not in ch_off)

    # -- _answer_collapsed --------------------------------------------------------------------------------
    long_review = "This portfolio is a clean static site. " * 40   # ~1500 chars
    receipt = "Confirmed: style.css exists."
    check("_answer_collapsed: a long review -> a one-line receipt is a collapse",
          _answer_collapsed(receipt, long_review) is True)
    check("_answer_collapsed: a similar-length correction is NOT a collapse",
          _answer_collapsed(long_review.replace("clean", "tidy"), long_review) is False)
    check("_answer_collapsed: a short original never triggers the guard",
          _answer_collapsed("ok", "a short answer") is False)

    # -- end-to-end: collapse fallback delivers the original ONLY if it re-verifies clean (bugs #1/#2) ----
    _orig_problems = grounding.problems

    class _FakeGrounder:
        """Flags a long answer for its first N calls (a flaky verifier that clears on re-check); a short receipt
        always passes. N=1 => the original re-verifies CLEAN (deliver it); N=big => it stays flagged (keep the
        correction)."""
        def __init__(self, flag_first_n):
            self.calls = 0
            self.n = flag_first_n

        def __call__(self, final, ctx):
            self.calls += 1
            if len(final or "") > 400 and self.calls <= self.n:
                return ["'../style.css' - cited but not found in the workspace"]
            return []

    def _run():
        traj = _Traj()
        cm = ContextManager("system", _Model(), traj, compact_at_tokens=0)
        c = Context(tempfile.mkdtemp(prefix="g87e_"), Permissions("bypass", {}, []))
        c.verbose = False
        return Agent(_AnswerPlanner([long_review, receipt]), None, traj, 6, cm).run("review the project", c)

    try:
        # WIN: the original re-verifies clean (flag was a flaky false positive) -> deliver the fuller original
        config.GROUND_ANTI_COLLAPSE = True
        grounding.problems = _FakeGrounder(flag_first_n=1)
        r_win = _run()
        check("flag ON + original re-verifies CLEAN: a collapsed correction delivers the FULLER ORIGINAL",
              r_win.final == long_review and r_win.terminated == "final")

        # SAFETY (bug #2): the original STAYS flagged on re-verify -> do NOT resurrect it, keep the correction
        grounding.problems = _FakeGrounder(flag_first_n=99)
        r_safe = _run()
        check("flag ON + original still FLAGGED on re-verify: keep the correction, do NOT ship the flawed original",
              r_safe.final == receipt)

        # flag OFF: byte-identical — the receipt is delivered, no anti-collapse
        config.GROUND_ANTI_COLLAPSE = False
        grounding.problems = _FakeGrounder(flag_first_n=1)
        r_off = _run()
        check("flag OFF: the receipt is delivered as before (byte-identical, no anti-collapse)",
              r_off.final == receipt)
    finally:
        grounding.problems = _orig_problems

    for k, v in _saved.items():
        setattr(config, k, v)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
