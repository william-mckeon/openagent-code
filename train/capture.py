"""
train/capture.py

Corpus capture — run the teacher across the training task pool to GENERATE trajectories.

Stage 4 of the distillation flywheel (specs/0005): the harness, eval gate, and curation
exist; this is the step that actually SPINS the flywheel — point the strong teacher
(gpt-oss-120b on Bedrock, whatever .env selects) at many diverse tasks and capture every
run as training data.

Run:  python -m train.capture            # run all train/tasks/*.yaml
      python -m train.capture --repeat 3 # N passes (temperature>0 gives varied rows)

Each task runs in a throwaway sandbox (reusing eval.harness.run_task) with its verify
command, and the trajectory is written to trajectories/corpus/ — a dir DISTINCT from
trajectories/eval/ so the held-out gate and the training corpus never mix (train/convert.py
excludes the eval dir for the same reason). Then run `python -m train.convert` to curate.

This is deliberately NOT the eval harness's main(): the eval suite is the GATE (held out,
judged); this is CORPUS GENERATION (kept, trained on). Same runner, different purpose and
different output dir — that separation is the whole point of Stage 4.
"""
import os
import sys
import glob
import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config  # noqa: E402
from src.mcp_client import connect, disconnect  # noqa: E402
from src.model import warm_up  # noqa: E402
from eval.harness import run_task  # noqa: E402  (reused runner; corpus uses a different traj_dir)

TASK_GLOB = os.path.join(ROOT, "train", "tasks", "*.yaml")
CORPUS_TRAJ_DIR = os.path.join(ROOT, "trajectories", "corpus")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Capture training trajectories from the teacher.")
    ap.add_argument("--repeat", type=int, default=1,
                    help="passes over the task pool (temperature>0 yields varied trajectories)")
    args = ap.parse_args(argv)

    tasks = sorted(glob.glob(TASK_GLOB))
    if not tasks:
        print(f"No tasks in {os.path.relpath(TASK_GLOB, ROOT)} — add some train/tasks/*.yaml first.")
        return 1

    print(f"corpus capture | model={config.display_model()} | tasks={len(tasks)} "
          f"x{args.repeat} pass(es) | temp={config.TEMPERATURE}")
    print(f"trajectories -> {os.path.relpath(CORPUS_TRAJ_DIR, ROOT)}  "
          f"(separate from the eval gate)\n")

    connect()
    warm_up()  # no-op on Bedrock; absorbs a cold start on a scale-to-zero endpoint
    passed = total = 0
    try:
        for p in range(args.repeat):
            for t in tasks:
                r = run_task(t, traj_dir=CORPUS_TRAJ_DIR)
                total += 1
                passed += 1 if r["passed"] else 0
                mark = "PASS" if r["passed"] else "FAIL"
                tag = f"[{mark}] {r['task']:<28}"
                tag += "" if args.repeat == 1 else f" (pass {p + 1})"
                print(f"{tag} outcome={r['outcome']:<14} tool_calls={r['tool_calls']}")
    finally:
        disconnect()

    print(f"\ncaptured {total} run(s), {passed} passed -> {os.path.relpath(CORPUS_TRAJ_DIR, ROOT)}")
    print("next: python -m train.convert   (curate -> train/dataset/sft.jsonl, eval gate excluded)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
