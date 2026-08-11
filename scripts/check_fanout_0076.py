"""
scripts/check_fanout_0076.py

Acceptance harness for specs/0076 — six fan-out / robustness fixes. Dep-free (fake litellm for the subagent
import; a real 1s hook timeout for the process-tree kill). Run:

    python scripts/check_fanout_0076.py
"""
import os
import sys
import time
import types
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_f = types.ModuleType("litellm")
_f.suppress_debug_info = True
_f.modify_params = True
_f.completion = lambda **k: None
sys.modules["litellm"] = _f

from src import config, workflow, orchestrator, subagent, hooks   # noqa: E402
from src.agent import _is_narration_command                       # noqa: E402
from src.tools import Context, grep                               # noqa: E402
from src.permissions import Permissions                           # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def main():
    # #6 narration guard: a MULTILINE command (narration on line 1, real work after) is NOT pure narration
    check("#6 a multiline 'Write-Output ...\\nRemove-Item ...' is NOT narration (a 2nd statement follows)",
          not _is_narration_command('Write-Output "status"\nRemove-Item foo')
          and _is_narration_command('Write-Output "just a status line"'))

    # #5 grep on a FILE path searches the file (was a false "(no matches)")
    d = tempfile.mkdtemp(prefix="fan76_")
    fp = os.path.join(d, "x.txt")
    open(fp, "w", encoding="utf-8").write("alpha\nBETA match here\ngamma\n")
    ctx = Context(d, None)
    check("#5 grep on a FILE finds the match (not a false '(no matches)')",
          "match here" in grep({"pattern": "match", "path": "x.txt"}, ctx).content)
    check("#5 grep on a FILE with no match returns '(no matches)'",
          grep({"pattern": "zzz", "path": "x.txt"}, ctx).content == "(no matches)")

    # #1 hooks: a hung hook is killed (whole tree) and fails open within a bound
    t0 = time.time()
    res = hooks._run({"command": 'python -c "import time; time.sleep(30)"', "timeout": 1}, {"cwd": d})
    dt = time.time() - t0
    check("#1 a hook that sleeps 30s (timeout=1) fails open within a few seconds (tree actually killed)",
          res is None and dt < 12)

    # #3 workflow: phases beyond the cap are RETURNED as dropped (so the digest can surface them)
    _mw = config.MAX_WORKFLOW_PHASES
    config.MAX_WORKFLOW_PHASES = 2
    kept, dropped = workflow.plan_phases([{"label": f"P{i}", "jobs": ["a"], "instruction": "do"} for i in range(5)])
    config.MAX_WORKFLOW_PHASES = _mw
    check("#3 plan_phases keeps the cap and returns the over-cap phases as `dropped`",
          len(kept) == 2 and len(dropped) == 3 and dropped[0].get("label") == "P2")

    # #2 orchestrator: cover-check is exact per folder — a folder whose name is a SUBSTRING of an area label
    #    ('app' is inside 'mapper') is NOT falsely treated as covered.
    ws = tempfile.mkdtemp(prefix="orch76_")
    os.makedirs(os.path.join(ws, "app"))
    os.makedirs(os.path.join(ws, "mapper"))
    units = [("mapper/", "Review ONLY the files under 'mapper/'.", "")]
    balanced = orchestrator._balance_plan(units, ws, "")
    labels = {u[0] for u in balanced}
    check("#2 a folder ('app/') whose name is a substring of an area ('mapper/') still gets its own area",
          "app/" in labels and "mapper/" in labels)

    # #4 subagent: a failure DURING child construction is contained as '(subagent error: ...)', not raised
    _orig = subagent.Trajectory

    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("construction blew up")
    subagent.Trajectory = _Boom
    pctx = types.SimpleNamespace(depth=0, permissions=Permissions("bypass", {}, []),
                                 session_id="s", cwd=ws, verbose=False, traj_dir=None)
    try:
        out = subagent.run_subagent("do a thing", pctx)
    finally:
        subagent.Trajectory = _orig
    check("#4 a Trajectory()/build_agent() construction error becomes '(subagent error: ...)', not a raise",
          isinstance(out, str) and out.startswith("(subagent error:") and "construction blew up" in out)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
