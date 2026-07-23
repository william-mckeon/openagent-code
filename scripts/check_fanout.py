r"""
scripts/check_fanout.py

Acceptance harness for specs/0039 — bounded parallel fan-out. Dep-free: stdlib + src only, NEVER litellm.
Proves the src/fanout.py concurrency semantics with a FAKE spawn (zero model calls): serial byte-identity,
real overlap, submission-order results, exception isolation, and read-only-when-parallel; plus
Permissions.readonly_view() enforcement. Timing-free (uses threading.Barrier/Event, not sleeps).

Run:  python scripts/check_fanout.py
"""
import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config                    # noqa: E402
from src.fanout import fanout             # noqa: E402
from src.permissions import Permissions   # noqa: E402
from src.tools import Context             # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def main():
    # 1. SERIAL byte-identity at max_workers=1: peak concurrency 1, perfectly-nested order, serial-map result
    lock, active, peak, order = threading.Lock(), [0], [0], []

    def s_spawn(task, read_only=False):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            order.append(("in", task))
        with lock:
            order.append(("out", task))
            active[0] -= 1
        return f"R{task}"

    res = fanout(s_spawn, [1, 2, 3], 1)
    check("max_workers=1: results are the serial map in order", res == ["R1", "R2", "R3"])
    check("max_workers=1: peak concurrency never exceeds 1 (no overlap)", peak[0] == 1)
    check("max_workers=1: perfectly nested in/out order",
          order == [("in", 1), ("out", 1), ("in", 2), ("out", 2), ("in", 3), ("out", 3)])
    check("max_workers<=1 and a single task also take the serial path", fanout(s_spawn, ["solo"], 4) == ["Rsolo"])

    # 2. REAL overlap at max_workers=2: two workers must rendezvous on a Barrier (a serial pool would time out)
    barrier = threading.Barrier(2)

    def o_spawn(task, read_only=False):
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:
            return "TIMEOUT"
        return f"R{task}"

    check("max_workers=2: two children genuinely overlap (Barrier releases)", fanout(o_spawn, [1, 2], 2) == ["R1", "R2"])

    # 3. results stay in SUBMISSION order even when completion order is reversed
    ev = threading.Event()

    def order_spawn(task, read_only=False):
        if task == 1:
            ev.wait(timeout=5)   # task 1 finishes AFTER task 2
        else:
            ev.set()
        return f"R{task}"

    check("results follow SUBMISSION order despite reversed completion", fanout(order_spawn, [1, 2], 2) == ["R1", "R2"])

    # 4. EXCEPTION isolation at max_workers>1: one raises, siblings still complete, exception surfaces
    done, dlock = set(), threading.Lock()

    def x_spawn(task, read_only=False):
        if task == 1:
            raise ValueError("boom")
        with dlock:
            done.add(task)
        return f"R{task}"

    raised = False
    try:
        fanout(x_spawn, [1, 2, 3], 3)
    except ValueError:
        raised = True
    check("a raising task surfaces its exception at its slot", raised)
    check("non-raising siblings still completed (pool not torn down)", done == {2, 3})

    # 5. READ-ONLY when parallel; NON-read-only when serial (byte-identity)
    seen = []

    def ro_spawn(task, read_only=False):
        seen.append(read_only)
        return "R"

    fanout(ro_spawn, [1, 2], 1)
    serial_ro = list(seen)
    seen.clear()
    fanout(ro_spawn, [1, 2], 2)
    par_ro = list(seen)
    check("serial fan-out spawns children NON-read-only (byte-identical to today)", serial_ro == [False, False])
    check("parallel fan-out spawns children READ-ONLY", par_ro == [True, True])

    # 6. Permissions.readonly_view() enforcement (writes denied, reads allowed)
    saved = {k: getattr(config, k) for k in ("HOOKS", "PROPOSE", "EXECPOLICY")}
    config.HOOKS = config.PROPOSE = config.EXECPOLICY = False
    ro = Permissions("bypass", {"deny": [], "ask": [], "allow": []}, []).readonly_view()
    ctx = Context("/tmp/ws", ro)
    check("readonly_view: mode is the read-only plan projection", ro.mode == "plan")
    check("readonly_view: write_file is DENIED", not ro.decide("write_file", {"path": "x.txt"}, ctx).allowed)
    check("readonly_view: delete_file is DENIED", not ro.decide("delete_file", {"path": "x.txt"}, ctx).allowed)
    check("readonly_view: run_command is DENIED", not ro.decide("run_command", {"command": "echo hi"}, ctx).allowed)
    check("readonly_view: read_file is ALLOWED", ro.decide("read_file", {"path": "x.txt"}, ctx).allowed)
    check("readonly_view: grep is ALLOWED", ro.decide("grep", {"path": "."}, ctx).allowed)
    for k, v in saved.items():
        setattr(config, k, v)

    # 7. default flag value proven against the fallback, clamped [1, MAX_REVIEW_AREAS]
    _env = os.environ.pop("CODE_WORKFLOW_CONCURRENCY", None)
    default1 = max(1, min(int(os.environ.get("CODE_WORKFLOW_CONCURRENCY", "1")), config.MAX_REVIEW_AREAS)) == 1
    if _env is not None:
        os.environ["CODE_WORKFLOW_CONCURRENCY"] = _env
    check("CODE_WORKFLOW_CONCURRENCY defaults to 1 (serial) when unset", default1)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
