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


def _parse_flags(argv):
    """Pull launcher flags out of argv so the common knobs are FLAGS, not CODE_* env
    vars (the env-juggling that makes local use painful). Applies the config-level
    overrides in place and returns (mode_override, [add_dirs], remaining_argv):

      -C / --workspace <path>   the repo to work in (default: current directory)
      --mode <name>             permission mode (default/acceptEdits/plan/bypass)
      --add-dir <path>          grant a reference folder beyond the workspace (repeatable)
      --memory / --no-memory    toggle cross-session memory for this run
      --warmup <seconds>        cold-start warm-up budget
    """
    mode, dirs, rest = None, [], []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--workspace", "-C") and i + 1 < len(argv):
            config.WORKSPACE = os.path.abspath(argv[i + 1]); i += 2
        elif a == "--add-dir" and i + 1 < len(argv):
            dirs.append(argv[i + 1]); i += 2
        elif a == "--mode" and i + 1 < len(argv):
            mode = argv[i + 1]; i += 2
        elif a == "--memory":
            config.MEMORY = True; i += 1
        elif a == "--no-memory":
            config.MEMORY = False; i += 1
        elif a == "--warmup" and i + 1 < len(argv):
            config.WARMUP_BUDGET = float(argv[i + 1]); i += 2
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
          f"mode={perms.mode} | effort={config.REASONING_EFFORT or 'default'} | workspace={workspace}")
    _warn_if_empty_workspace(workspace)
    agent = build_agent(traj, memory=_load_memory(workspace), granted_dirs=perms.extra_roots)

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


_MODES = {"default", "acceptEdits", "plan", "bypass"}


def _repl_add_dir(agent, ctx, path):
    """`/add-dir <path>` — grant a reference folder mid-session (0003 host access).
    Widens the LIVE permission fence and tells the agent it can now read there."""
    path = path.strip().strip('"')
    if not path:
        print("usage: /add-dir <path>"); return
    ap = os.path.abspath(path)
    if not os.path.isdir(ap):
        print(f"  not a directory: {ap}"); return
    real = os.path.realpath(ap)
    if real not in ctx.permissions.extra_roots:
        ctx.permissions.extra_roots.append(real)
    # Tell the agent (human-grant -> the model needs to KNOW the folder is readable).
    agent.cm.add({"role": "user", "content":
                  f"(system) Read access granted to: {ap}\n"
                  f"You may now read files there with absolute paths, and pass that path to "
                  f"grep/glob to search it. It is READ-only reference unless told otherwise."})
    print(f"  granted (read): {ap}")


def _repl_set_mode(ctx, name):
    """`/mode <name>` — switch the permission mode mid-session."""
    name = name.strip()
    if name not in _MODES:
        print(f"  current mode: {ctx.permissions.mode}\n  usage: /mode <{' | '.join(sorted(_MODES))}>")
        return
    ctx.permissions.mode = name
    print(f"  mode -> {name}")


def _run_session(traj, agent, ctx):
    """The interactive chat loop, shared by a fresh REPL and a resumed session."""
    print(f"openagent-code REPL | model={config.display_model()} | mode={ctx.permissions.mode} | "
          f"effort={config.REASONING_EFFORT or 'default'} | workspace={ctx.cwd}")
    print("Type a task and press enter. Commands: /exit  /plan  /add-dir <path>  /mode <name>")
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
            if user.startswith("/add-dir"):
                _repl_add_dir(agent, ctx, user[len("/add-dir"):])
                continue
            if user.startswith("/mode"):
                _repl_set_mode(ctx, user[len("/mode"):])
                continue
            turns += 1
            try:
                result = agent.run(user, ctx)
            except Exception as e:
                # A model error (500, context overflow, a flaky worker) must NOT kill the
                # REPL — end the turn with a message and keep the session alive.
                print(f"\n[error] that turn failed: {type(e).__name__}: {str(e)[:200]}\n"
                      "(the session is still alive — try again, rephrase, or /exit)")
                continue
            if result.final:
                print("\n" + result.final)
            else:
                print("\n(no output — the model may have dropped the response, often a cold/"
                      "flaky endpoint. Try again; the warm-up should recover it.)")
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
    agent = build_agent(traj, memory=_load_memory(workspace), granted_dirs=perms.extra_roots)
    return _run_session(traj, agent, ctx)


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


def _force_utf8_stdout():
    """Make stdout/stderr UTF-8 so printing the model's output can't crash the run.

    On Windows the console defaults to a legacy codepage (cp1252), so printing any
    character the model routinely emits — em dash, non-breaking hyphen, smart quotes,
    bullets — raises UnicodeEncodeError and kills the turn. errors='replace' is a
    belt-and-suspenders fallback for any glyph the target encoding still can't render.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass  # redirected to something without reconfigure() — leave it as-is


def main(argv=None):
    _force_utf8_stdout()
    argv = list(argv if argv is not None else sys.argv[1:])
    mode_override, add_dirs, argv = _parse_flags(argv)
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
