"""
scripts/check_permhardening_0080.py

Acceptance harness for specs/0080 — permission-policy hardening: the guardian circuit-breaker and the
protected-path deny-write defaults. Dep-free (a scripted planner + fake registry, no model/network). Run:

    python scripts/check_permhardening_0080.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, outcomes                       # noqa: E402
from src.agent import Agent                            # noqa: E402
from src.context import ContextManager                 # noqa: E402
from src.tools import Context, ToolResult              # noqa: E402
from src.permissions import Permissions                # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


class _Model:
    def summarize(self, msgs):
        return "s"


class _Traj:
    def __init__(self):
        self.steps = 0
        self.tool_calls = 0

    def log_turn(self, m): pass
    def log_compaction(self, *a): pass
    def log_tool_call(self, *a, **k): pass
    def log_permission(self, *a, **k): pass
    def log_verification(self, *a, **k): pass


class _Decision:
    def __init__(self):
        self.assistant = {"role": "assistant", "content": ""}
        self.final = ""
        self.calls = [{"name": "write_file", "args": {"path": "x.py", "content": "y"}}]  # always DENIED below
        self.nudge = None
        self.gave_up = False
        self.dropped = False


class _Planner:
    def step(self, context, step):
        return _Decision()

    def format_result(self, call, result):
        return {"role": "tool", "content": ""}


class _Reg:
    def run(self, name, args, ctx):
        return ToolResult(True, "")


def _agent(traj):
    cm = ContextManager("s", _Model(), traj, compact_at_tokens=0)
    return Agent(_Planner(), _Reg(), traj, 12, cm)


def _ctx():
    c = Context(tempfile.mkdtemp(prefix="ph80_"), Permissions("bypass", {"deny": ["write_file(**)"]}, []))
    c.verbose = False
    return c


def main():
    _saved = {k: getattr(config, k) for k in ("GUARDIAN_MAX_DENIALS", "PROTECT_PATHS", "ADAPTIVE_EFFORT",
                                              "SITUATIONAL_CONTEXT", "VERIFY_COMPLETION", "VERIFY_MANIFEST")}
    config.ADAPTIVE_EFFORT = config.SITUATIONAL_CONTEXT = False
    config.VERIFY_COMPLETION = config.VERIFY_MANIFEST = False

    # -- #10 protected-path deny-write defaults ------------------------------------------------------------
    config.PROTECT_PATHS = True
    ws = tempfile.mkdtemp(prefix="ph80w_")
    perms = Permissions("bypass", {}, [])
    ctx = Context(ws, perms)
    check("#10 is_protected_path: .git internals + .env (nested + top-level) match; source files don't",
          config.is_protected_path(".git/config") and config.is_protected_path("a/b/.git/HEAD")
          and config.is_protected_path(".env") and config.is_protected_path(".env.local")
          and not config.is_protected_path("src/app.py"))
    check("#10 write_file/delete_file to .git or .env is DENIED; a normal file is allowed",
          not perms.decide("write_file", {"path": ".git/config"}, ctx).allowed
          and not perms.decide("delete_file", {"path": ".env"}, ctx).allowed
          and perms.decide("write_file", {"path": "src/app.py"}, ctx).allowed)
    check("#10 protect is WRITE-only: read_file(.env) is unaffected here (deny-read is specs/0078's job)",
          perms.decide("read_file", {"path": ".env"}, ctx).allowed)
    config.PROTECT_PATHS = False
    check("#10 PROTECT_PATHS off -> modifying .git is allowed again (byte-identical)",
          perms.decide("write_file", {"path": ".git/config"}, ctx).allowed)

    # -- #7 guardian circuit-breaker (denial loop) ---------------------------------------------------------
    check("denial_loop is an honest gate outcome (not washed to completed)",
          "denial_loop" in outcomes.GATE_OUTCOMES and outcomes.classify("denial_loop", 9) == "denial_loop")
    config.GUARDIAN_MAX_DENIALS = 3
    r = _agent(_Traj()).run("keep writing a denied file", _ctx())
    check("#7 a loop of DENIED ops ends as 'denial_loop' past CODE_GUARDIAN_MAX_DENIALS (not max_steps)",
          r.terminated == "denial_loop")
    config.GUARDIAN_MAX_DENIALS = 0
    r = _agent(_Traj()).run("keep writing a denied file", _ctx())
    check("#7 breaker OFF (0) -> the same loop is NOT denial_loop (guard skipped, byte-identical)",
          r.terminated != "denial_loop")

    for k, v in _saved.items():
        setattr(config, k, v)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
