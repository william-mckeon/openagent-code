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


def make_context(cwd, permissions, session_id, depth=0, verbose=False, interactive=False):
    """A Context with `ctx.spawn` and `ctx.ask` wired.

    `session_id` is THIS agent's trajectory id — a spawned child records it as its
    `parent_session_id`, which is how nested runs link together. `interactive`
    enables ask_user to actually prompt a human (else it degrades).
    """
    ctx = Context(cwd, permissions)
    ctx.verbose = verbose
    ctx.depth = depth
    ctx.session_id = session_id
    ctx.interactive = interactive
    ctx.spawn = lambda task: run_subagent(task, ctx)
    ctx.ask = _terminal_ask if interactive else None
    return ctx


def _classify(result, tool_calls):
    """Honest outcome for a subagent (no verify command). Mirrors cli.py."""
    if result.terminated == "nudge_exhausted":
        return "protocol_stalled"
    if tool_calls == 0:
        return "no_action"
    if result.terminated == "max_steps":
        return "max_steps"
    return "completed"


def run_subagent(task, parent_ctx):
    """Build a child agent for `task`, run it in isolation, return its final text."""
    child_depth = parent_ctx.depth + 1
    traj = Trajectory(
        config.trajectory_dir(), task, config.MODEL, parent_ctx.cwd,
        parent_session_id=parent_ctx.session_id,
        depth=child_depth,   # tool_schemas defaults to the active toolset
    )
    child_ctx = make_context(parent_ctx.cwd, parent_ctx.permissions, traj.session_id,
                             depth=child_depth, verbose=parent_ctx.verbose,
                             interactive=parent_ctx.interactive)
    if parent_ctx.verbose:
        print(f"  [subagent depth={child_depth}] {task[:70]}")

    agent = build_agent(traj)
    try:
        result = agent.run(task, child_ctx)
        traj.end(_classify(result, traj.tool_calls), result.final, terminated=result.terminated)
        return result.final or ""
    except Exception as e:
        traj.end("error", None, terminated="exception")
        return f"(subagent error: {type(e).__name__}: {e})"
