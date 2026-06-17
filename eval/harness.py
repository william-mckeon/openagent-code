"""
eval/harness.py

Eval harness — the steering wheel of the flywheel.

Without this, training can quietly DEGRADE the model and you won't notice. Each
task sets up a sandbox repo, runs the agent against it, then checks an objective
verify command. Pass rate on a held-out task set is how you decide whether a new
model (or harness change) is actually better.

Run:  python -m eval.harness
Tasks live in eval/tasks/*.yaml:
  prompt:  what to ask the agent
  setup:   files to write into the sandbox before the run  (path -> content)
  verify:  shell command; exit 0 == task passed

Unlike a one-off run, the trajectories are PERSISTED to trajectories/eval/ (the
sandbox code dir is discarded, the captured trajectory is kept) so an eval run
doubles as your first real batch of training data.
"""
import os
import sys
import glob
import shutil
import tempfile
import subprocess

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.permissions import Permissions  # noqa: E402
from src.runtime import build_agent  # noqa: E402
from src.subagent import make_context  # noqa: E402
from src.trajectory import Trajectory  # noqa: E402
from src import config  # noqa: E402

EVAL_TRAJ_DIR = os.path.join(ROOT, "trajectories", "eval")


def run_task(task_path):
    name = os.path.basename(task_path)
    spec = yaml.safe_load(open(task_path, encoding="utf-8"))
    sandbox = tempfile.mkdtemp(prefix="openagent-code-eval-")
    traj = None
    try:
        for rel, content in (spec.get("setup") or {}).items():
            fp = os.path.join(sandbox, rel)
            os.makedirs(os.path.dirname(fp) or sandbox, exist_ok=True)
            with open(fp, "w", encoding="utf-8") as f:
                f.write(content)

        # Trajectory persists outside the sandbox so the eval batch survives.
        traj = Trajectory(EVAL_TRAJ_DIR, spec["prompt"], config.MODEL, sandbox)
        # Force bypass: eval runs headless and must auto-approve to exercise the agent.
        ctx = make_context(sandbox, Permissions.from_config(mode_override="bypass"),
                           traj.session_id, depth=0, verbose=False)
        agent = build_agent(traj)

        result = agent.run(spec["prompt"], ctx)
        p = subprocess.run(spec["verify"], shell=True, cwd=sandbox,
                           capture_output=True, text=True)
        passed = p.returncode == 0
        traj.log_verification(spec["verify"], passed, (p.stdout or "") + (p.stderr or ""))

        # Same honest classification the CLI uses, so eval distinguishes a real
        # failure from the agent doing nothing.
        if result.terminated == "nudge_exhausted":
            outcome = "protocol_stalled"
        elif traj.tool_calls == 0:
            outcome = "no_action"
        elif passed:
            outcome = "success"
        else:
            outcome = "verify_failed"
        traj.end(outcome, result.final, terminated=result.terminated)

        return {"task": name, "passed": passed, "outcome": outcome,
                "tool_calls": traj.tool_calls}
    except Exception as e:
        # Isolate per-task failures (e.g. a transient network/DNS drop) so one bad
        # task doesn't crash the whole eval run — mark it error and move on.
        if traj is not None:
            try:
                traj.end("error", None, terminated="exception")
            except Exception:
                pass
        print(f"  [ERROR] {name}: {type(e).__name__}: {e}")
        return {"task": name, "passed": False, "outcome": "error",
                "tool_calls": (traj.tool_calls if traj is not None else 0)}
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def main():
    tasks = sorted(glob.glob(os.path.join(ROOT, "eval", "tasks", "*.yaml")))
    if not tasks:
        print("No tasks in eval/tasks/.")
        return

    print(f"eval | model={config.display_model()} | tool_mode={config.TOOL_MODE} | tasks={len(tasks)}")
    print(f"trajectories -> {EVAL_TRAJ_DIR}\n")

    from src.mcp_client import connect, disconnect
    from src.model import warm_up
    connect()
    # Warm a scale-to-zero endpoint once up front, so the first task isn't the one
    # that eats the cold start (and fails on a cold worker's empty tool_calls).
    warm_up()
    try:
        results = []
        for t in tasks:
            r = run_task(t)
            results.append(r)
            mark = "PASS" if r["passed"] else "FAIL"
            print(f"[{mark}] {r['task']:<28} outcome={r['outcome']:<16} tool_calls={r['tool_calls']}")
    finally:
        disconnect()

    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    print(f"\n{passed}/{total} passed ({100 * passed // total}%)")


if __name__ == "__main__":
    main()
