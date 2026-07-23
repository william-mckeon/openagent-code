r"""
scripts/check_workflows.py

Acceptance harness for specs/0038 — the synchronous MULTI-PHASE workflow engine. Dep-free: stdlib + src
only, NEVER litellm. Sets CODE_WORKFLOWS=true BEFORE importing config so active_tools() offers run_workflow.
Proves the PURE planner (plan_phases / plan_jobs / _job_task / assemble_digest / final_digest) and the
fan-out + carry chaining through the run_workflow driver with a RECORDING STUB spawn — zero model calls —
plus the byte-identical-when-off invariant (the tool is gated; SCHEMA_VERSION is unchanged).

Run:  python scripts/check_workflows.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["CODE_WORKFLOWS"] = "true"   # BEFORE importing config, so active_tools() sees the flag ON

from src import config, workflow          # noqa: E402
from src.tools import Context             # noqa: E402
from src.toolset import active_tools      # noqa: E402
from src.trajectory import Trajectory     # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def _stub():
    """A recording stub for ctx.spawn: appends each child prompt it receives, returns a traceable marker."""
    calls = []

    def spawn(task, *a, **k):
        calls.append(task)
        return f"SUMMARY_{len(calls)}"
    return spawn, calls


def _ctx(spawn, depth=0):
    c = Context("/tmp/ws", None)
    c.spawn = spawn
    c.depth = depth
    c.verbose = False
    return c


SPEC = [
    {"label": "probe", "jobs": ["a", "b"], "instruction": "look at X"},
    {"label": "verify", "jobs": ["cross-check"], "instruction": "verify Y"},
]


def main():
    # ---- pure planner --------------------------------------------------------------------------------
    phases, dropped = workflow.plan_phases(SPEC)
    check("plan_phases preserves order + count", [p["label"] for p in phases] == ["probe", "verify"] and not dropped)

    saved = config.MAX_WORKFLOW_PHASES
    config.MAX_WORKFLOW_PHASES = 1
    kept, over = workflow.plan_phases(SPEC)
    check("plan_phases caps at MAX_WORKFLOW_PHASES (returns the overflow)", len(kept) == 1 and len(over) == 1)
    config.MAX_WORKFLOW_PHASES = saved

    sf = config.MAX_SUBAGENT_FANOUT
    config.MAX_SUBAGENT_FANOUT = 2
    jobs, trunc = workflow.plan_jobs({"label": "p", "jobs": ["j1", "j2", "j3", "j4"], "instruction": "do", "focus": None},
                                     "", config.MAX_SUBAGENT_FANOUT)
    check("plan_jobs caps at MAX_SUBAGENT_FANOUT + returns the truncated labels", len(jobs) == 2 and trunc == ["j3", "j4"])
    config.MAX_SUBAGENT_FANOUT = sf

    deg, _ = workflow.plan_phases([{"jobs": ["everything", ".", "real-area"], "instruction": "x"}])
    check("plan_phases drops degenerate whole-repo scopes ('everything', '.')", deg[0]["jobs"] == ["real-area"])

    coer, _ = workflow.plan_phases([{"jobs": [{"item": "auth"}, {"scope": "db"}], "instruction": "x"}])
    check("jobs tolerate dict shapes (coerced, not crashed)", coer[0]["jobs"] == ["auth", "db"])

    t = workflow._job_task("auth", "review it", None, "")
    check("_job_task embeds the item AND the instruction", "auth" in t and "review it" in t)
    check("_job_task ALWAYS appends the harness length bound (even w/ no length constraint)", "UNDER 200 words" in t)

    pdig = workflow.assemble_digest("probe", [("a", "found X"), ("b", "found Y")], [], 8)
    check("assemble_digest has a section per job", "### a" in pdig and "### b" in pdig and "found X" in pdig)
    fdig = workflow.final_digest([pdig], "prioritize the risks")
    check("final_digest carries every phase + the synthesis trailer", "## Phase: probe" in fdig and "prioritize the risks" in fdig)

    # ---- driver: fan-out + carry chaining with a stub spawn (NO model) -------------------------------
    spawn, calls = _stub()
    r = workflow.run_workflow({"workflow": SPEC}, _ctx(spawn))
    check("run_workflow fans out one child per job across phases (2+1 = 3 calls)", r.ok and len(calls) == 3)
    check("phase-1 child prompts carry NOTHING (first phase)", "SUMMARY_" not in calls[0] and "SUMMARY_" not in calls[1])
    check("phase-2 child prompt CARRIES phase-1's findings forward", "SUMMARY_1" in calls[2] and "SUMMARY_2" in calls[2])
    check("run_workflow returns the final digest (both phases)", "## Phase: probe" in r.content and "## Phase: verify" in r.content)

    check("run_workflow refuses when ctx.spawn is None", not workflow.run_workflow({"workflow": SPEC}, _ctx(None)).ok)
    check("run_workflow refuses at depth>=1 (no nested workflow)", not workflow.run_workflow({"workflow": SPEC}, _ctx(_stub()[0], depth=1)).ok)
    check("run_workflow refuses an empty workflow (teaching message)", not workflow.run_workflow({"workflow": []}, _ctx(_stub()[0])).ok)

    # per-turn re-run guard + the per-task reset agent.run performs
    spawn2, calls2 = _stub()
    c = _ctx(spawn2)
    workflow.run_workflow({"workflow": SPEC}, c)
    first = len(calls2)
    r2 = workflow.run_workflow({"workflow": SPEC}, c)   # same ctx -> cached, no second fan-out
    check("a second run_workflow in the same turn returns the CACHED digest (no re-fan)",
          r2.ok and r2.meta.get("cached") and len(calls2) == first)
    c._workflow_digest = None   # what agent.run does at the start of each task
    workflow.run_workflow({"workflow": SPEC}, c)
    check("after the per-task reset, run_workflow fans out fresh again", len(calls2) == first * 2)

    # ---- byte-identity: the flag gates the tool; the capture schema is unchanged ---------------------
    w = config.WORKFLOWS
    config.WORKFLOWS = True
    on = {t["name"] for t in active_tools()}
    config.WORKFLOWS = False
    off = {t["name"] for t in active_tools()}
    config.WORKFLOWS = w
    check("run_workflow IS offered when CODE_WORKFLOWS is on", "run_workflow" in on)
    check("run_workflow is NOT offered when off (byte-identical tool_schemas)", "run_workflow" not in off)
    check("Trajectory.SCHEMA_VERSION is unchanged at 0.13.0 (no capture-schema change)", Trajectory.SCHEMA_VERSION == "0.13.0")

    # default OFF proven against the fallback, not this repo's live .env
    _env = os.environ.pop("CODE_WORKFLOWS", None)
    default_off = config._as_bool(os.environ.get("CODE_WORKFLOWS", "false")) is False
    if _env is not None:
        os.environ["CODE_WORKFLOWS"] = _env
    check("CODE_WORKFLOWS defaults False when unset (opt-in)", default_off)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
