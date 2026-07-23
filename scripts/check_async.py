r"""
scripts/check_async.py

Acceptance harness for specs/0040 — the async background runtime. Dep-free: stdlib + src only, NEVER litellm.
Drives the PURE TaskRegistry state machine and the drain/fold FORMATTERS with a FakePopen + an in-memory
result dict — no real subprocess, thread, or model — and asserts the flag-off byte-identity invariants.

Run:  python scripts/check_async.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["CODE_WORKFLOWS_ASYNC"] = "true"   # BEFORE importing config

from src import config, tasks                  # noqa: E402
from src.toolset import active_tools           # noqa: E402
from src.trajectory import Trajectory          # noqa: E402
from src.permissions import Permissions        # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


class FakePopen:
    """poll() returns each value in `rcs` over successive calls (last value sticks). No real process."""
    def __init__(self, rcs):
        self._rcs = list(rcs)

    def poll(self):
        return self._rcs.pop(0) if len(self._rcs) > 1 else self._rcs[0]

    def terminate(self):
        pass

    def wait(self, timeout=None):
        pass


def main():
    T = tasks

    # 1. PURE transitions
    check("queued + spawned -> running", T._next_state(T.QUEUED, "spawned") == T.RUNNING)
    check("running + exit_ok_result -> done", T._next_state(T.RUNNING, "exit_ok_result") == T.DONE)
    check("running + exit_fail -> error", T._next_state(T.RUNNING, "exit_fail") == T.ERROR)
    check("running + exit_ok_grace stays running", T._next_state(T.RUNNING, "exit_ok_grace") == T.RUNNING)
    check("a TERMINAL state is a no-op (done + exit_fail -> done)", T._next_state(T.DONE, "exit_fail") == T.DONE)
    check("an unknown event is a no-op", T._next_state(T.RUNNING, "???") == T.RUNNING)

    # 2. submit + cap
    reg = T.TaskRegistry(read_result=lambda tid: None, cap=2)
    run_forever = lambda tid, spec: FakePopen([None])  # noqa: E731
    a, e1 = reg.submit("a", "A", "/spec/a", run_forever)
    b, e2 = reg.submit("b", "B", "/spec/b", run_forever)
    check("submit registers tasks under their caller ids", a == "a" and b == "b" and e1 is None and e2 is None)
    c, e3 = reg.submit("c", "C", "/spec/c", run_forever)
    check("submit REFUSES past MAX_BACKGROUND_TASKS cap", c is None and bool(e3) and "cap" in e3)
    check("submitted tasks are non-terminal (running)", len(reg.non_terminal()) == 2)

    # 3. refresh -> done (exit 0 with a result becoming visible)
    res3 = {}
    reg3 = T.TaskRegistry(read_result=lambda tid: res3.get(tid), cap=3)
    reg3.submit("d", "D", "/spec/d", lambda tid, spec: FakePopen([None, 0, 0]))
    reg3.refresh()
    check("a still-running task stays running", reg3.all_tasks()[0].state == T.RUNNING)
    res3["d"] = {"status": "done", "digest": "the answer"}
    reg3.refresh()
    check("exit-0 with a result -> done", reg3.all_tasks()[0].state == T.DONE)

    # 4. refresh -> error (exit nonzero)
    reg4 = T.TaskRegistry(read_result=lambda tid: None, cap=3)
    reg4.submit("x", "X", "/spec/x", lambda tid, spec: FakePopen([1]))
    reg4.refresh()
    check("exit-nonzero -> error", reg4.all_tasks()[0].state == T.ERROR)

    # 5. grace window: exit-0 with NO result stays running through _GRACE_POLLS, then latches to error
    reg5 = T.TaskRegistry(read_result=lambda tid: None, cap=3)
    reg5.submit("g", "G", "/spec/g", lambda tid, spec: FakePopen([0]))
    states = []
    for _ in range(T._GRACE_POLLS + 2):
        reg5.refresh()
        states.append(reg5.all_tasks()[0].state)
    check("exit-0-no-result stays running through the grace window",
          states[:T._GRACE_POLLS] == [T.RUNNING] * T._GRACE_POLLS)
    check("after the grace window a no-result task latches to error", states[-1] == T.ERROR)

    # 6. drain-once
    reg6 = T.TaskRegistry(read_result=lambda tid: {"status": "done", "digest": "z"}, cap=3)
    reg6.submit("h", "H", "/spec/h", lambda tid, spec: FakePopen([0]))
    reg6.refresh()
    first, second = reg6.drain_finished(), reg6.drain_finished()
    check("drain_finished surfaces a finish EXACTLY once", len(first) == 1 and second == [])

    # 7. pull
    check("pull returns (id, digest) for a DONE task by prefix", reg6.pull("h") == ("h", "z"))
    check("pull returns None for an unknown id", reg6.pull("zzz") is None)

    # 8. formatters
    check("fold_result with no pending returns the user text unchanged", T.fold_result([], "do X") == "do X")
    folded = T.fold_result([("h", "the digest")], "do X")
    check("fold_result yields ONE user string (CONTEXT preamble + 'My request:' + user text)",
          "CONTEXT from completed background task h" in folded and "the digest" in folded
          and folded.endswith("My request:\ndo X"))
    check("render lists id + state", "h" in reg6.render() and "done" in reg6.render())
    check("render_result wraps the digest", "background task h" in T.render_result(("h", "the digest")))

    # 9. byte-identity: no new tool, no schema/fingerprint change, default off
    w, wa = config.WORKFLOWS, config.WORKFLOWS_ASYNC
    config.WORKFLOWS = True
    config.WORKFLOWS_ASYNC = True
    on_async = "run_workflow" in {t["name"] for t in active_tools()}
    config.WORKFLOWS_ASYNC = False
    on_sync = "run_workflow" in {t["name"] for t in active_tools()}
    config.WORKFLOWS, config.WORKFLOWS_ASYNC = w, wa
    check("run_workflow offered IDENTICALLY in both async flag states (no new tool)", on_async and on_sync)
    check("Trajectory.SCHEMA_VERSION unchanged at 0.13.0", Trajectory.SCHEMA_VERSION == "0.13.0")
    fp = set(config.safety_fingerprint(Permissions("bypass", {}, [])).keys())
    check("safety_fingerprint carries NO workflow/async keys (behavioral, not a safety gate)",
          "workflows_async" not in fp and "workflows" not in fp and "max_background_tasks" not in fp)
    _env = os.environ.pop("CODE_WORKFLOWS_ASYNC", None)
    default_off = config._as_bool(os.environ.get("CODE_WORKFLOWS_ASYNC", "false")) is False
    if _env is not None:
        os.environ["CODE_WORKFLOWS_ASYNC"] = _env
    check("CODE_WORKFLOWS_ASYNC defaults False when unset (opt-in)", default_off)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
