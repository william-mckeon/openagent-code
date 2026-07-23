"""
src/workflow.py

Workflows P1 (specs/0038): a synchronous MULTI-PHASE fan-out+reduce engine.

review_repo (orchestrator.py) is a SINGLE fan-out — split a repo into areas, spawn one captured child per
area, reduce to a digest. run_workflow generalizes it to N ORDERED PHASES the model authors: each phase fans
out one captured child per job, reduces to a phase digest, and that digest is CARRIED into the next phase —
so a probe -> critique -> synthesize pipeline runs deterministically, and the lead never reads the raw
material itself (the discipline that keeps review_repo from overflowing the context).

P1 is synchronous / in-turn: run_workflow blocks inside its tool dispatch, exactly like review_repo. Parallel
fan-out (P2), background execution + notify (P3), and front-end decoupling (P4) are separate phases.

The PLANNER (plan_phases / plan_jobs / _job_task / assemble_digest / final_digest) is PURE and model-free, so
the dep-free harness stub-tests the whole fan-out + carry chaining with a fake ctx.spawn and no litellm.
Capture needs no change here: the run_workflow tool_call (its spec + the returned digest) is logged by the
agent loop, and each fanned-out child is already a linked trajectory via ctx.spawn (parent_session_id+depth).
"""
from . import config
from .fanout import fanout                    # specs/0039: bounded parallel fan-out within a phase
from .orchestrator import _degenerate_scope   # reuse the "names the whole repo, not a part" guard (no cycle)

# Harness-owned per-child bound: appended to EVERY child prompt regardless of the model's `instruction`, so a
# workflow can't emit an unbounded child whose summary then overflows the lead's context (the review_repo
# _child_task:120 discipline). The model owns WHAT to ask; the harness owns HOW MUCH comes back.
_LENGTH_BOUND = ("In UNDER 200 words, give a short, structured summary; ground every point in what you "
                 "actually opened or were given, and say so if you could not cover something. Return only "
                 "the summary.")


def _coerce_items(jobs):
    """A phase's `jobs` -> a list of non-empty item strings. Tolerates a bare string, a list of strings, or a
    list of dicts (pull a canonical field), skipping anything unusable — so a slightly-off model spec
    degrades instead of crashing (the orchestrator.py:172-177 dict-or-string tolerance). Degenerate
    whole-repo scopes are dropped."""
    if isinstance(jobs, str):
        jobs = [jobs]
    out = []
    for j in (jobs or []):
        if isinstance(j, dict):
            v = (j.get("item") or j.get("label") or j.get("scope")
                 or j.get("question") or j.get("job") or "")
        else:
            v = str(j)
        v = v.strip()
        if v and not _degenerate_scope(v):
            out.append(v)
    return out


def plan_phases(spec):
    """PURE: normalize the model's `workflow` spec into an ordered list of phase dicts
    {label, jobs, instruction, focus}, capped at config.MAX_WORKFLOW_PHASES. Order is preserved (it is a
    pipeline). A phase with no usable jobs is dropped. Tolerates a single phase dict or a list. Returns
    (kept_phases, dropped_over_cap)."""
    if isinstance(spec, dict):
        spec = [spec]
    phases = []
    for p in (spec or []):
        if not isinstance(p, dict):
            continue
        jobs = _coerce_items(p.get("jobs") or p.get("items") or p.get("scopes"))
        if not jobs:
            continue
        n = len(phases) + 1
        label = (p.get("label") or p.get("name") or "").strip() or f"phase {n}"
        instruction = (p.get("instruction") or p.get("item_prompt") or p.get("prompt") or "").strip()
        focus = (p.get("focus") or "").strip() or None
        phases.append({"label": label, "jobs": jobs, "instruction": instruction, "focus": focus})
    cap = config.MAX_WORKFLOW_PHASES
    return phases[:cap], phases[cap:]


def _job_task(item, instruction, focus, carry):
    """PURE: the prompt for ONE fanned-out child — its item, the phase instruction, an optional focus, the
    prior phase's digest as carry, and ALWAYS the harness-owned length bound appended LAST (regardless of
    what `instruction` said)."""
    parts = ["You are one worker in a multi-phase workflow, running in isolation on ONE item."]
    if carry:
        parts.append("Findings from the previous phase (build on these; do not just repeat them):\n" + carry)
    parts.append(f"YOUR ITEM: {item}")
    if instruction:
        parts.append(f"YOUR TASK: {instruction}")
    if focus:
        parts.append(f"Focus specifically on {focus}.")
    parts.append(_LENGTH_BOUND)
    return "\n\n".join(parts)


def plan_jobs(phase, carry, cap):
    """PURE: a phase dict -> ([(item_label, child_prompt)], truncated_labels). One job per item, capped at
    `cap` (config.MAX_SUBAGENT_FANOUT). Each child_prompt is built by _job_task with `carry` threaded in."""
    jobs = [(item, _job_task(item, phase["instruction"], phase["focus"], carry)) for item in phase["jobs"]]
    kept, over = jobs[:cap], jobs[cap:]
    return kept, [label for label, _ in over]


def assemble_digest(label, results, truncated, cap):
    """PURE: reduce ONE phase's child results into a compact phase digest (### block per job + a truncation
    note). Small by construction — N short summaries, never raw material."""
    parts = [f"## Phase: {label} ({len(results)} job(s))"]
    for job_label, summary in results:
        parts.append(f"### {job_label}\n{summary}")
    if truncated:
        parts.append(f"[NOTE] {len(truncated)} job(s) not run (fan-out cap {cap}): " + ", ".join(truncated))
    return "\n\n".join(parts)


def final_digest(records, synthesis):
    """PURE: reduce all phase digests into the final digest the lead synthesizes from — the phase sections
    plus a synthesis trailer (the model's `synthesis` instruction + the mandatory synthesize-now guardrail
    that parallels orchestrator.py:228-235)."""
    parts = [f"Deterministic workflow over {len(records)} phase(s):\n"]
    parts.extend(records)
    trailer = ("\nYou now have every phase's findings. Write your FINAL answer for the user NOW by "
               "synthesizing ALL phases above — do not let one phase crowd out the rest. Do NOT call "
               "run_workflow / review_repo / spawn_agent / read_file / grep again: the workers already did "
               "the work, and re-running only wastes budget and overflows your context. Your next reply "
               "must be the finished result, as a clean report.")
    if synthesis:
        trailer = f"\nHow to synthesize: {synthesis}" + trailer
    parts.append(trailer)
    return "\n".join(parts)


def run_workflow(args, ctx):
    """Run a synchronous multi-phase workflow (specs/0038). The model authors `workflow` (ordered phases,
    each with `jobs` + an `instruction`); the harness fans out one captured child per job, reduces each
    phase, carries its digest forward, and returns the final digest for the lead to synthesize next turn.
    IMPURE only via ctx.spawn — the planning + reducing is the pure seam above."""
    from .tools import ToolResult  # lazy: avoid the tools<->workflow import cycle (mirrors orchestrator.py)
    if ctx.spawn is None:
        return ToolResult(False, "Subagents are unavailable here, so run_workflow cannot fan out. Do the "
                                 "work directly, or run one scoped subtask with spawn_agent.")
    if ctx.depth >= 1:
        return ToolResult(False, "run_workflow is a TOP-LEVEL orchestration tool; you are already a scoped "
                                 "worker — do your assigned item directly, do not start a nested workflow.")
    prev = getattr(ctx, "_workflow_digest", None)
    if prev is not None:
        return ToolResult(True, "run_workflow already ran this turn — do NOT run it again. Write your FINAL "
                                "answer by synthesizing the digest below.\n\n" + prev, {"cached": True})

    phases, dropped = plan_phases(args.get("workflow") or args.get("phases"))
    if not phases:
        return ToolResult(False, "run_workflow needs `workflow`: an ordered list of phases, each an object "
                                 "with `jobs` (the items/questions to fan out over) and an `instruction` "
                                 "(what each worker should do). Add at least one phase with real jobs.")
    synthesis = (args.get("synthesis") or args.get("synthesis_prompt") or "").strip()
    cap = config.MAX_SUBAGENT_FANOUT

    records, carry, total_jobs = [], "", 0
    for phase in phases:
        jobs, truncated = plan_jobs(phase, carry, cap)
        # Fan the phase's jobs out (specs/0039): parallel within a phase at CODE_WORKFLOW_CONCURRENCY>1 (the
        # children run read-only then), serial + byte-identical at 1. The OUTER phase loop stays serial —
        # each phase's digest is the `carry` the next phase's workers build on.
        raw = fanout(ctx.spawn, [p for _, p in jobs], config.WORKFLOW_CONCURRENCY)
        results = [(job_label, (r or "").strip() or "(no summary returned)") for (job_label, _), r in zip(jobs, raw)]
        total_jobs += len(results)
        digest = assemble_digest(phase["label"], results, truncated, cap)
        records.append(digest)
        carry = digest   # feed this phase's digest forward to the next phase's workers

    if ctx.verbose:
        print(f"  [run_workflow] {len(records)} phase(s), {total_jobs} worker(s)"
              + (f", {len(dropped)} phase(s) over cap" if dropped else ""))

    final = final_digest(records, synthesis)
    ctx._workflow_digest = final   # per-turn re-run guard (reset per task in agent.run)
    return ToolResult(True, final, {"phases": len(records), "jobs": total_jobs})
