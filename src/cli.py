"""
src/cli.py

Entry points (Phase 4 interactivity):
  python -m src "task"        one-shot autonomous run (the original path; deterministic,
                              non-interactive — ask_user degrades). Used by eval/Docker.
  python -m src               interactive REPL: a multi-turn chat session sharing one
                              ContextManager + Trajectory across turns; ask_user is live.

Outcome labels and the mandated verification step live on the one-shot path. The REPL
is a continuing conversation, so it ends with a single session outcome and no per-turn
verify. Configuration is read from src/config.py (CODE_* env vars / .env).
"""
import os
import sys
import subprocess

from . import config
from .permissions import Permissions
from .runtime import build_agent
from .subagent import make_context
from .trajectory import Trajectory


def _warn_if_empty_workspace(workspace):
    try:
        entries = [e for e in os.listdir(workspace)
                   if e not in (".gitkeep", "trajectories") and not e.startswith(".")]
    except OSError:
        entries = []
    if not entries:
        print(f"WARNING: workspace {workspace!r} looks empty - set CODE_WORKSPACE to a real "
              "repo, or the agent will have nothing to work on.")


def _load_memory(workspace):
    """Load cross-session project memory (Phase 4 #7) for the workspace, if enabled."""
    if not config.MEMORY:
        return ""
    from . import memory
    mem = memory.load(workspace)
    if mem:
        print(f"memory: {len(mem)} chars loaded from {config.MEMORY_FILE}")
    return mem


def _parse_perm_flags(argv):
    """Pull permission overrides out of argv: --mode <name> and --add-dir <path>
    (repeatable). Returns (mode_override, [dirs], remaining_argv)."""
    mode, dirs, rest = None, [], []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--mode" and i + 1 < len(argv):
            mode = argv[i + 1]; i += 2
        elif a == "--add-dir" and i + 1 < len(argv):
            dirs.append(argv[i + 1]); i += 2
        else:
            rest.append(a); i += 1
    return mode, dirs, rest


def _one_shot(task, perms):
    """Autonomous single-task run: one agent loop, mandated verify, honest outcome."""
    workspace = config.WORKSPACE
    traj = Trajectory(config.trajectory_dir(), task, config.MODEL, workspace)
    ctx = make_context(workspace, perms, traj.session_id,
                       depth=0, verbose=config.VERBOSE, interactive=False)
    print(f"openagent-code | model={config.display_model()} | tool_mode={config.TOOL_MODE} | "
          f"mode={perms.mode} | workspace={workspace}")
    _warn_if_empty_workspace(workspace)
    agent = build_agent(traj, memory=_load_memory(workspace))

    try:
        result = agent.run(task, ctx)
        final, terminated = result.final, result.terminated
        print("\n=== RESULT ===\n" + (final or "(no output)"))
    except Exception as e:
        traj.end("error", None, terminated="exception")
        print(f"\n=== ERROR === {type(e).__name__}: {e}")
        print(f"\nTrajectory: {traj.path}  (outcome=error)")
        return 1

    if terminated == "nudge_exhausted":
        outcome = "protocol_stalled"
    elif traj.tool_calls == 0:
        outcome = "no_action"
    elif terminated == "max_steps":
        outcome = "max_steps"
    else:
        outcome = "completed"

    vc = config.VERIFY_COMMAND
    if vc:
        p = subprocess.run(vc, shell=True, cwd=workspace, capture_output=True, text=True)
        ok = p.returncode == 0
        traj.log_verification(vc, ok, (p.stdout or "") + (p.stderr or ""))
        print(f"\n=== VERIFY [{'PASS' if ok else 'FAIL'}]: {vc} ===")
        if outcome == "completed":
            outcome = "success" if ok else "verify_failed"

    traj.end(outcome, final, terminated=terminated)
    print(f"\nTrajectory: {traj.path}  (outcome={outcome}, tool_calls={traj.tool_calls})")
    return 0 if outcome in ("success", "completed") else 1


def _run_session(traj, agent, ctx):
    """The interactive chat loop, shared by a fresh REPL and a resumed session."""
    print(f"openagent-code REPL | model={config.display_model()} | workspace={ctx.cwd}")
    print("Type a task and press enter. Commands: /exit  /plan")
    turns = 0
    try:
        while True:
            try:
                user = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not user:
                continue
            if user in ("/exit", "/quit"):
                break
            if user == "/plan":
                print(ctx.plan or "(no plan yet)")
                continue
            turns += 1
            result = agent.run(user, ctx)
            print("\n" + (result.final or "(no output)"))
    finally:
        traj.end("completed" if traj.tool_calls else "no_action", None, terminated="session_end")
        print(f"\nsession ended ({turns} turn(s)). resume later with:"
              f"  python -m src --resume {traj.session_id}")
    return 0


def _repl(perms):
    """Fresh interactive session."""
    workspace = config.WORKSPACE
    traj = Trajectory(config.trajectory_dir(), "(interactive session)", config.MODEL, workspace)
    ctx = make_context(workspace, perms, traj.session_id,
                       depth=0, verbose=config.VERBOSE, interactive=True)
    return _run_session(traj, build_agent(traj, memory=_load_memory(workspace)), ctx)


def _resume_repl(session_id, perms):
    """Continue a stopped session by rehydrating it from its trajectory."""
    from .session import resume
    try:
        traj, agent, ctx = resume(session_id, config.WORKSPACE, perms,
                                  verbose=config.VERBOSE, interactive=True)
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        return 1
    print(f"resumed session {session_id}")
    return _run_session(traj, agent, ctx)


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    mode_override, add_dirs, argv = _parse_perm_flags(argv)
    perms = Permissions.from_config(mode_override=mode_override, extra_dirs=add_dirs)
    from .mcp_client import connect, disconnect
    from .model import warm_up
    n = connect()
    if n:
        print(f"MCP: connected {n} tool(s)")
    # Absorb a scale-to-zero cold start once, so the first task runs warm.
    warm_up()
    try:
        if argv and argv[0] == "--resume":
            if len(argv) < 2:
                print("usage: python -m src [--mode <name>] [--add-dir <path>] --resume <session_id>")
                return 2
            return _resume_repl(argv[1], perms)
        task = " ".join(argv).strip()
        return _one_shot(task, perms) if task else _repl(perms)
    finally:
        disconnect()


if __name__ == "__main__":
    sys.exit(main())
