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
from .trajectory import Trajectory
from .context import sanitize_tail
from .logsetup import get_logger
# NOTE: build_agent (runtime) and make_context (subagent) pull in the model/litellm stack; they are imported
# LAZILY inside resume() so this module — and its pure helpers (_load_records/_working_from/_replay_dir_grants)
# — stay import-light and dep-free for the harness.

log = get_logger("session")


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


def _load_records(path):
    """specs/0074: parse a trajectory file line-by-line, SKIPPING a corrupt/truncated record instead of
    raising. A process killed mid-write leaves a half-written final JSON line; the old list comprehension
    raised JSONDecodeError and made --resume crash (the caller catches only FileNotFoundError), so a single
    bad byte lost the whole session."""
    records = []
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("resume: skipping a corrupt/truncated trajectory line in %s", os.path.basename(path))
    return records


def _working_from(records):
    """The rehydrated working set from the raw `turn` stream. specs/0074: filter out ALL role:'system' turns,
    not just a leading one — with CODE_SITUATIONAL_CONTEXT on, log_env_capture writes a role:'system' env
    block PER TURN (cwd/date/git); re-sending those stale mid-conversation blocks each step violates the
    single-sent-copy invariant (specs/0035) and feeds the model a stale date/cwd. The ContextManager owns the
    real system prompt; the env pin is refreshed per turn by set_env_context. Then snap to a valid tool-pairing
    (sanitize_tail) so a dangling tool_use can't poison the resumed session."""
    turns = [r["message"] for r in records if r.get("type") == "turn"]
    return sanitize_tail([m for m in turns if m.get("role") != "system"])


def _replay_dir_grants(records, permissions):
    """specs/0074: re-apply mid-session directory grants (typed dir_grant records) onto the fence so the
    resumed session admits the paths the rehydrated history tells the model it may read — otherwise the model
    is told it has access the fence now denies, and (being a weak model) retries the denied read in a loop.
    Old trajectories carry no dir_grant records -> nothing replayed (byte-identical). BY TIER so a read-only
    grant never comes back write-capable."""
    for r in records:
        if r.get("type") == "dir_grant" and r.get("path"):
            bucket = permissions.read_only_roots if r.get("tier") == "read_only" else permissions.extra_roots
            if r["path"] not in bucket:
                bucket.append(r["path"])


def resume(session_id, workspace, permissions, verbose=False, interactive=False):
    """Rehydrate a session -> (trajectory, agent, ctx) ready to continue."""
    path = find_session(session_id)
    if not path:
        raise FileNotFoundError(f"no trajectory found for session {session_id!r} under "
                                f"{config.trajectory_dir()}")
    from .runtime import build_agent          # lazy: keep session.py import-light / dep-free (see top note)
    from .subagent import make_context
    records = _load_records(path)
    working = _working_from(records)
    _replay_dir_grants(records, permissions)
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
