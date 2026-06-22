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
                "tier": spec.get("tier", "core"), "tool_calls": traj.tool_calls}
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


def run_agentic_task(task_path):
    """Run an agentic task (eval/agentic/*.yaml) and score its BEHAVIOR via the rubric —
    no verify command. Measures review depth / no-refusal / completion (specs/0004), the
    quality the verify eval is blind to. Returns the rubric's score dict + task name."""
    from eval import rubric
    name = os.path.basename(task_path)
    spec = yaml.safe_load(open(task_path, encoding="utf-8"))
    sandbox = tempfile.mkdtemp(prefix="openagent-code-agentic-")
    traj = None
    try:
        for rel, content in (spec.get("setup") or {}).items():
            fp = os.path.join(sandbox, rel)
            os.makedirs(os.path.dirname(fp) or sandbox, exist_ok=True)
            with open(fp, "w", encoding="utf-8") as f:
                f.write(content)

        traj = Trajectory(EVAL_TRAJ_DIR, spec["prompt"], config.MODEL, sandbox)
        ctx = make_context(sandbox, Permissions.from_config(mode_override="bypass"),
                           traj.session_id, depth=0, verbose=False)
        result = build_agent(traj).run(spec["prompt"], ctx)
        traj.end("completed" if traj.tool_calls else "no_action", result.final,
                 terminated=result.terminated)
        sc = rubric.score(rubric.load_records(traj.path), spec.get("rubric"))
        return {"task": name, "tier": spec.get("tier", "core"), **sc}
    except Exception as e:
        if traj is not None:
            try:
                traj.end("error", None, terminated="exception")
            except Exception:
                pass
        print(f"  [ERROR] {name}: {type(e).__name__}: {e}")
        return {"task": name, "tier": "core", "score": 0.0, "checks": {}, "files_read": 0,
                "tool_calls": (traj.tool_calls if traj is not None else 0),
                "refused": True, "missed_mentions": []}
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def _tier_of(task_path):
    """Read just the `tier` field from a task yaml (default 'core'), for filtering."""
    try:
        return (yaml.safe_load(open(task_path, encoding="utf-8")) or {}).get("tier", "core")
    except Exception:
        return "core"


def main(argv=None):
    # Optional `--tier <name>` filter so you can run just the smoke / core / hard tier.
    argv = sys.argv[1:] if argv is None else argv
    tier_filter = None
    if "--tier" in argv:
        i = argv.index("--tier")
        tier_filter = argv[i + 1] if i + 1 < len(argv) else None

    verify_tasks = sorted(glob.glob(os.path.join(ROOT, "eval", "tasks", "*.yaml")))
    agentic_tasks = sorted(glob.glob(os.path.join(ROOT, "eval", "agentic", "*.yaml")))
    if tier_filter:
        verify_tasks = [t for t in verify_tasks if _tier_of(t) == tier_filter]
        agentic_tasks = [t for t in agentic_tasks if _tier_of(t) == tier_filter]
    if not verify_tasks and not agentic_tasks:
        print(f"No tasks{' for tier ' + tier_filter if tier_filter else ''}.")
        return

    print(f"eval | model={config.display_model()} | tool_mode={config.TOOL_MODE} | "
          f"verify={len(verify_tasks)} agentic={len(agentic_tasks)}"
          + (f" | tier={tier_filter}" if tier_filter else ""))
    print(f"trajectories -> {EVAL_TRAJ_DIR}\n")

    from src.mcp_client import connect, disconnect
    from src.model import warm_up
    connect()
    # Warm a scale-to-zero endpoint once up front, so the first task isn't the one
    # that eats the cold start (and fails on a cold worker's empty tool_calls).
    warm_up()
    results, behavior = [], []
    try:
        for t in verify_tasks:
            r = run_task(t)
            results.append(r)
            mark = "PASS" if r["passed"] else "FAIL"
            print(f"[{mark}] {r['task']:<28} tier={r['tier']:<5} outcome={r['outcome']:<16} "
                  f"tool_calls={r['tool_calls']}")
        if agentic_tasks:
            print()
            for t in agentic_tasks:
                b = run_agentic_task(t)
                behavior.append(b)
                missed = [k for k, v in (b.get("checks") or {}).items() if not v]
                tag = "OK  " if b["score"] == 1.0 else "... "
                miss_topics = b.get("missed_mentions") or []
                print(f"[{tag}] {b['task']:<28} tier={b['tier']:<5} behavior={b['score']:.2f} "
                      f"files_read={b['files_read']} tool_calls={b['tool_calls']}"
                      + (f"  missed={','.join(missed)}" if missed else "")
                      + (f"  unmentioned={','.join(miss_topics)}" if miss_topics else ""))
    finally:
        disconnect()

    _summarize(results, behavior)


def _summarize(results, behavior):
    """Print overall + per-tier results, and the Stage-3 gate verdict: a suite that still
    reads 100% can't discriminate a good student from a bad one."""
    tiers = sorted({r["tier"] for r in results} | {b["tier"] for b in behavior})

    if results:
        passed = sum(1 for r in results if r["passed"])
        print(f"\nverify:   {passed}/{len(results)} passed ({100 * passed // len(results)}%)")
        for tier in tiers:
            sub = [r for r in results if r["tier"] == tier]
            if sub:
                p = sum(1 for r in sub if r["passed"])
                print(f"   - {tier:<5} {p}/{len(sub)}")
    if behavior:
        avg = sum(b["score"] for b in behavior) / len(behavior)
        print(f"behavior: {avg:.2f} avg across {len(behavior)} agentic task(s)")
        for tier in tiers:
            sub = [b for b in behavior if b["tier"] == tier]
            if sub:
                a = sum(b["score"] for b in sub) / len(sub)
                print(f"   - {tier:<5} {a:.2f}")

    verify_perfect = results and all(r["passed"] for r in results)
    behavior_perfect = behavior and all(b["score"] == 1.0 for b in behavior)
    if (results or behavior) and (not results or verify_perfect) and (not behavior or behavior_perfect):
        print("\n[gate] this model reads 100% — it saturates the current tasks. The hard tier + "
              "findings checks (must_mention) give the gate resolution, so a WEAKER student should "
              "score below 100% and reveal the gap. If even a student saturates it, add harder tasks.")
    else:
        print("\n[gate] suite discriminates (something scored below 100%) — usable as a "
              "promotion gate.")


if __name__ == "__main__":
    main()
