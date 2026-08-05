"""
src/session.py

Resume a stopped session (Phase 4 interactivity).

The payoff of the capture-vs-context discipline: the trajectory already IS the saved
session. Resuming is rehydration, not a new persistence layer — we read a session's
raw `turn` records back into a ContextManager, restore the pinned plan from the last
update_plan, reopen the trajectory in append mode, and continue.
"""
import os
import glob
import json

from . import config
from . import memory
from . import todos
from . import specstore
from .permissions import Permissions
from .trajectory import Trajectory
from .runtime import build_agent
from .subagent import make_context
from .context import sanitize_tail


def find_session(session_id):
    """Locate a trajectory file by session id under the trajectory dir (any subdir)."""
    base = config.trajectory_dir()
    hits = glob.glob(os.path.join(base, "**", session_id + ".jsonl"), recursive=True)
    hits += glob.glob(os.path.join(base, session_id + ".jsonl"))
    return hits[0] if hits else None


def _restore_plan(records):
    """The plan text from the last successful update_plan call (or None)."""
    last = None
    for r in records:
        if r.get("type") == "tool_call" and r.get("tool") == "update_plan" and r.get("ok"):
            last = r
    if not last:
        return None
    result = last.get("result", "") or ""
    return result.split("Plan updated:\n", 1)[-1] if "Plan updated:" in result else None


def resume(session_id, workspace, permissions, verbose=False, interactive=False):
    """Rehydrate a session -> (trajectory, agent, ctx) ready to continue."""
    path = find_session(session_id)
    if not path:
        raise FileNotFoundError(f"no trajectory found for session {session_id!r} under "
                                f"{config.trajectory_dir()}")
    records = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]

    # Rebuild the working set from the raw `turn` stream (the full history). The first
    # turn is the system prompt, which the ContextManager owns separately.
    turns = [r["message"] for r in records if r.get("type") == "turn"]
    working = turns[1:] if turns and turns[0].get("role") == "system" else list(turns)
    # specs/0034: snap the rehydrated tail to a clean tool-pairing boundary. The raw turn stream can end on a
    # DANGLING assistant tool_call (a prior turn that died mid-flight logged the assistant-with-tool_calls but
    # not its results; agent rollback trims only the LIVE view, never the file), which Bedrock rejects on the
    # next step. (The oversized-history OVERFLOW is handled by the ContextManager's hard-cap compaction.)
    working = sanitize_tail(working)
    plan = _restore_plan(records)

    traj = Trajectory.resume(path)
    mem = memory.load(workspace) if config.MEMORY else ""
    # Reload the project backlog too (Phase 23) so a resumed session sees what's still to do — the display
    # at startup is handled by cli._run_session, which every resumed session routes through.
    tdo = todos.backlog_text(workspace) if config.PROJECT_TODOS else ""
    # Reload the active spec into the prompt (Phase 25). This is the PROMPT text only; the acceptance gate's
    # in-memory ctx.spec stays None until the agent re-registers/re-approves a spec this task (like the
    # manifest - the disk artifact feeds context; the approval is per-task so a resumed unrelated turn isn't gated).
    spc = specstore.active_text(workspace) if config.SPEC_FIRST else ""
    agent = build_agent(traj, initial_working=working, pinned_plan=plan, memory=mem, todos=tdo, spec=spc,
                        granted_dirs=permissions.extra_roots, cwd=workspace,
                        show_reasoning=config.SHOW_REASONING)   # specs/0064: a resumed REPL streams thinking too
    ctx = make_context(workspace, permissions, traj.session_id, depth=0,
                       verbose=verbose, interactive=interactive)
    ctx.plan = plan   # keep the loop pinning it going forward
    return traj, agent, ctx
