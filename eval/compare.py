"""
eval/compare.py

Stage 6 promotion gate — run the eval suite against TWO endpoints (the BASE student model
and your fine-tuned STUDENT) and report the delta. Promote only if the student MEETS OR BEATS
the base on BOTH the verify pass-rate AND the agentic behavior score (specs/0005). This is the
decision that keeps a regression from ever being deployed.

Run (each --*-api-base points at a served OpenAI-compatible endpoint):
  python -m eval.compare \
    --base-model    openai/Qwen2.5-3B-Instruct --base-api-base    http://localhost:8001/v1 \
    --student-model openai/student             --student-api-base http://localhost:8000/v1

(You can serve one at a time and re-point, or run two `serve` containers on different ports.)
"""
import os
import sys
import glob
import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config  # noqa: E402
from src.mcp_client import connect, disconnect  # noqa: E402
from eval import harness  # noqa: E402


def _configure(model, api_base, api_key):
    """Re-point the model gateway at a different endpoint between batches."""
    config.MODEL = model
    config.API_BASE = api_base or ""
    config.API_KEY = api_key or "EMPTY"


def _run_suite(label):
    verify = sorted(glob.glob(os.path.join(ROOT, "eval", "tasks", "*.yaml")))
    agentic = sorted(glob.glob(os.path.join(ROOT, "eval", "agentic", "*.yaml")))
    print(f"\n=== {label}: {config.display_model()} @ {config.API_BASE or '(default)'} ===")
    results = []
    for t in verify:
        r = harness.run_task(t)
        results.append(r)
        print(f"  [{'PASS' if r['passed'] else 'FAIL'}] {r['task']:<28} {r['outcome']}")
    behavior = []
    for t in agentic:
        b = harness.run_agentic_task(t)
        behavior.append(b)
        print(f"  [{'OK ' if b['score'] == 1.0 else '...'}] {b['task']:<28} behavior={b['score']:.2f}")
    vp = sum(1 for r in results if r["passed"]) / (len(results) or 1)
    bp = sum(b["score"] for b in behavior) / (len(behavior) or 1)
    return {"label": label, "verify": vp, "behavior": bp}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Promotion gate: eval base vs student.")
    ap.add_argument("--base-model", required=True)
    ap.add_argument("--base-api-base", required=True)
    ap.add_argument("--base-api-key", default="EMPTY")
    ap.add_argument("--student-model", required=True)
    ap.add_argument("--student-api-base", required=True)
    ap.add_argument("--student-api-key", default="EMPTY")
    args = ap.parse_args(argv)

    connect()
    try:
        _configure(args.base_model, args.base_api_base, args.base_api_key)
        base = _run_suite("BASE")
        _configure(args.student_model, args.student_api_base, args.student_api_key)
        student = _run_suite("STUDENT")
    finally:
        disconnect()

    print("\n" + "=" * 48)
    print(f"{'':10}{'verify':>10}{'behavior':>12}")
    print(f"{'base':<10}{base['verify'] * 100:9.0f}%{base['behavior']:12.2f}")
    print(f"{'student':<10}{student['verify'] * 100:9.0f}%{student['behavior']:12.2f}")
    dv = (student["verify"] - base["verify"]) * 100
    db = student["behavior"] - base["behavior"]
    print(f"{'delta':<10}{dv:+9.0f}%{db:+12.2f}")
    promote = student["verify"] >= base["verify"] and student["behavior"] >= base["behavior"]
    print("\n[gate] " + ("PROMOTE the student — it meets/beats base on both axes. Swap CODE_API_BASE."
                         if promote else
                         "KEEP base — the student regressed on at least one axis. Do not deploy."))
    return 0 if promote else 1


if __name__ == "__main__":
    sys.exit(main())
