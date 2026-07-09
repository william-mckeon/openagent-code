"""
src/subagent.py

Subagents (Phase 4). `spawn_agent` delegates a self-contained subtask to a fresh
agent that has its OWN clean ContextManager and its OWN trajectory (linked to the
spawning agent by `parent_session_id` + `depth`). The child runs in isolation —
it never sees the parent's conversation — and returns only its final answer, which
re-enters the parent as a tool result. So the parent's context stays focused while
the child's full work is captured separately as training data (subagents *multiply*
the dataset).

Depth is capped by CODE_MAX_SUBAGENT_DEPTH, enforced at the spawn_agent tool.

Import direction is one-way (subagent -> runtime), so wiring `ctx.spawn` here keeps
tools.py free of any agent/runtime import and avoids a cycle.
"""
from . import config
from .tools import Context
from .trajectory import Trajectory
from .runtime import build_agent


def _terminal_ask(question):
    print(f"\n[agent asks] {question}")
    try:
        ans = input("> ").strip()
    except EOFError:
        ans = ""
    return ans or "(no answer given)"


def make_context(cwd, permissions, session_id, depth=0, verbose=False, interactive=False,
                 traj_dir=None):
    """A Context with `ctx.spawn` and `ctx.ask` wired.

    `session_id` is THIS agent's trajectory id — a spawned child records it as its
    `parent_session_id`, which is how nested runs link together. `interactive`
    enables ask_user to actually prompt a human (else it degrades). `traj_dir` is where
    spawned children write their trajectory: None -> the corpus (config.trajectory_dir());
    an eval run passes trajectories/eval/ so children (e.g. the Phase-10 grounding verifier)
    stay behind the train/eval firewall (specs/0005) instead of leaking held-out eval
    content into the SFT corpus.
    """
    ctx = Context(cwd, permissions)
    ctx.verbose = verbose
    ctx.depth = depth
    ctx.session_id = session_id
    ctx.interactive = interactive
    ctx.traj_dir = traj_dir
    # effort is optional so a caller (the grounding verifier) can run the child at its own reasoning
    # effort; a bare spawn(task) keeps the parent's/global effort.
    ctx.spawn = lambda task, effort=None: run_subagent(task, ctx, effort=effort)
    ctx.ask = _terminal_ask if interactive else None
    return ctx


def _classify(result, tool_calls):
    """Honest outcome for a subagent (no verify command). Mirrors cli.py — honest gate outcomes are
    checked BEFORE the tool_calls==0 fallback (a gate can fire with zero tool calls)."""
    if result.terminated == "nudge_exhausted":
        return "protocol_stalled"
    if result.terminated == "unverified_completion":
        return "unverified_completion"
    if result.terminated == "ungrounded_completion":
        return "ungrounded_completion"
    if result.terminated == "degenerate":
        return "degenerate"
    if tool_calls == 0:
        return "no_action"
    if result.terminated == "max_steps":
        return "max_steps"
    return "completed"


def run_subagent(task, parent_ctx, effort=None):
    """Build a child agent for `task`, run it in isolation, return its final text. `effort` overrides
    the child's reasoning effort (None = the global) — e.g. a grounding verifier at CODE_GROUNDING_EFFORT."""
    child_depth = parent_ctx.depth + 1
    # Children write to the parent's trajectory dir (None -> the corpus). This keeps subagents spawned
    # INSIDE an eval — e.g. the Phase-10 grounding verifier — under trajectories/eval/ (the firewall),
    # not in the training corpus (specs/0005).
    traj_dir = getattr(parent_ctx, "traj_dir", None) or config.trajectory_dir()
    traj = Trajectory(
        traj_dir, task, config.MODEL, parent_ctx.cwd,
        parent_session_id=parent_ctx.session_id,
        depth=child_depth,   # tool_schemas defaults to the active toolset
    )
    # Children are ALWAYS non-interactive, even from a REPL: a spawned worker reviewing one
    # folder must never prompt the human (ask_user degrades to "no human - proceed"). Inheriting
    # the parent's interactive flag let a child hijack the REPL with "Could you specify the path?"
    # mid-review. The human talks to the lead; children just do their bounded task and report.
    child_ctx = make_context(parent_ctx.cwd, parent_ctx.permissions, traj.session_id,
                             depth=child_depth, verbose=parent_ctx.verbose,
                             interactive=False, traj_dir=getattr(parent_ctx, "traj_dir", None))
    if parent_ctx.verbose:
        print(f"  [subagent depth={child_depth}] {task[:70]}")

    agent = build_agent(traj, effort=effort)
    try:
        result = agent.run(task, child_ctx)
        traj.end(_classify(result, traj.tool_calls), result.final, terminated=result.terminated)
        return result.final or ""
    except Exception as e:
        traj.end("error", None, terminated="exception")
        return f"(subagent error: {type(e).__name__}: {e})"
