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
from .permissions import Permissions
from .trajectory import Trajectory
from .runtime import build_agent
from .subagent import make_context


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
    plan = _restore_plan(records)

    traj = Trajectory.resume(path)
    mem = memory.load(workspace) if config.MEMORY else ""
    agent = build_agent(traj, initial_working=working, pinned_plan=plan, memory=mem)
    ctx = make_context(workspace, permissions, traj.session_id, depth=0,
                       verbose=verbose, interactive=interactive)
    ctx.plan = plan   # keep the loop pinning it going forward
    return traj, agent, ctx
