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

from . import config, logsetup, outcomes, userdirs, installshim, tasks
from .permissions import Permissions
from .runtime import build_agent
from .subagent import make_context
from .trajectory import Trajectory

log = logsetup.get_logger("cli")


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


def _load_todos(workspace):
    """Load the project backlog (outstanding items) as markdown for the SYSTEM PROMPT (Phase 23 / specs/
    0023). Mirrors _load_memory but returns the rendered checklist; display is _show_todos' job, so this one
    stays silent (the REPL path must not double-print). "" when off/empty."""
    if not config.PROJECT_TODOS:
        return ""
    from . import todos
    return todos.backlog_text(workspace)


def _show_todos(workspace):
    """Print the outstanding backlog as a startup SECTION for the user (Phase 23). Separate from _load_todos
    (which feeds the prompt) so the REPL doesn't print it twice. Shows ONLY when there are outstanding items
    (no zero-count noise); returns the rendered text so a caller can detect a change after a turn."""
    if not config.PROJECT_TODOS:
        return ""
    from . import todos
    text = todos.backlog_text(workspace)
    if text:
        n = len(todos.outstanding(todos.load(workspace)))
        print(f"\nProject todos ({n} outstanding):\n{text}\n")
    return text


def _load_spec(workspace):
    """Load the ACTIVE spec (Phase 25 / specs/0025) as markdown for the SYSTEM PROMPT. Silent (display is
    _show_spec's job, so the REPL doesn't print it twice). '' when off / no spec."""
    if not config.SPEC_FIRST:
        return ""
    from . import specstore
    return specstore.active_text(workspace)


def _show_spec(workspace):
    """Print the active spec's title + outstanding-acceptance count at startup for the user (Phase 25).
    Shows only when a spec exists; no-op when off."""
    if not config.SPEC_FIRST:
        return None
    from . import specstore
    spec = specstore.load_active(workspace)
    if spec and (spec.get("goal") or spec.get("acceptance")):
        left = len(specstore.outstanding(spec.get("acceptance") or []))
        print(f"\nActive spec: {spec.get('title') or '(untitled)'} "
              f"({left} acceptance item(s) outstanding) - see .openagent/specs/\n")
    return spec


def _parse_flags(argv):
    """Pull launcher flags out of argv so the common knobs are FLAGS, not CODE_* env
    vars (the env-juggling that makes local use painful). Applies the config-level
    overrides in place and returns (mode_override, [add_dirs], remaining_argv):

      -C / --workspace <path>   the repo to work in (default: current directory)
      --mode <name>             permission mode (default/acceptEdits/plan/bypass)
      --add-dir <path>          grant a reference folder beyond the workspace (repeatable)
      --memory / --no-memory    toggle cross-session memory for this run
      --todos / --no-todos      toggle the persistent project backlog for this run
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
        elif a == "--todos":
            config.PROJECT_TODOS = True; i += 1
        elif a == "--no-todos":
            config.PROJECT_TODOS = False; i += 1
        elif a == "--spec-first":
            config.SPEC_FIRST = True; i += 1
        elif a == "--no-spec-first":
            config.SPEC_FIRST = False; i += 1
        elif a == "--warmup" and i + 1 < len(argv):
            config.WARMUP_BUDGET = float(argv[i + 1]); i += 2
        else:
            rest.append(a); i += 1
    return mode, dirs, rest


def _one_shot(task, perms):
    """Autonomous single-task run: one agent loop, mandated verify, honest outcome."""
    workspace = config.WORKSPACE
    traj = Trajectory(config.trajectory_dir(), task, config.MODEL, workspace,
                      safety=config.safety_fingerprint(perms))   # specs/0033: which guards were on this run
    logsetup.configure(traj.session_id)
    ctx = make_context(workspace, perms, traj.session_id,
                       depth=0, verbose=config.VERBOSE, interactive=False)
    print(f"{config.agent_name()} | model={config.display_model()} | tool_mode={config.TOOL_MODE} | "
          f"mode={perms.mode} | effort={config.display_effort()} | workspace={workspace}")
    log.info("one-shot start | model=%s mode=%s workspace=%s", config.display_model(), perms.mode, workspace)
    log.info("task: %s", task)
    _warn_if_empty_workspace(workspace)
    agent = build_agent(traj, memory=_load_memory(workspace), todos=_load_todos(workspace),
                        spec=_load_spec(workspace), granted_dirs=perms.extra_roots, cwd=workspace,
                        show_reasoning=config.SHOW_REASONING)   # specs/0064: top-level tees the thinking live
    _show_todos(workspace)   # surface the backlog at startup (Phase 23; no-op when the flag is off)
    _show_spec(workspace)    # surface the active spec at startup (Phase 25; no-op when the flag is off)

    try:
        result = agent.run(task, ctx)
        final, terminated = result.final, result.terminated
        print("\n=== RESULT ===\n" + (final or "(no output)"))
        log.info("result (terminated=%s): %s", terminated, (final or "(no output)")[:500])
    except Exception as e:
        traj.end("error", None, terminated="exception")
        log.exception("one-shot FAILED: %s: %s", type(e).__name__, e)
        print(f"\n=== ERROR === {type(e).__name__}: {e}")
        print(f"\nTrajectory: {traj.path}  (outcome=error)")
        _print_log_path()
        return 1

    # Shared honest mapping (src/outcomes) — identical across one-shot, the REPL per-turn record, and
    # eval. Honest gate outcomes win over the tool_calls==0 fallback: grounding (unlike the completion
    # gate, which needs update_plan) can fire with ZERO tool calls, so a 0-tool-call ungrounded run must
    # NOT be relabeled no_action. Only a plain 'completed' is eligible for the verify relabel below.
    outcome = outcomes.classify(terminated, traj.tool_calls)

    vc = config.VERIFY_COMMAND
    if vc:
        p = subprocess.run(vc, shell=True, cwd=workspace, capture_output=True, text=True)
        ok = p.returncode == 0
        traj.log_verification(vc, ok, (p.stdout or "") + (p.stderr or ""))
        print(f"\n=== VERIFY [{'PASS' if ok else 'FAIL'}]: {vc} ===")
        if outcome == "completed":
            outcome = "success" if ok else "verify_failed"

    traj.end(outcome, final, terminated=terminated)
    log.info("one-shot end | outcome=%s tool_calls=%s", outcome, traj.tool_calls)
    print(f"\nTrajectory: {traj.path}  (outcome={outcome}, tool_calls={traj.tool_calls})")
    _print_log_path()
    return 0 if outcome in ("success", "completed") else 1


def _print_log_path():
    """Tell the user where the readable run log is — the file to hand off for a review."""
    p = logsetup.log_path()
    if p:
        print(f"Run log: {p}  (hand this to a reviewer / Claude to debug the run)")


_MODES = {"default", "acceptEdits", "plan", "bypass", "propose"}


def _closest_mode(name):
    """The nearest valid permission mode to a mistyped one — 'perpose' -> 'propose' (difflib, stdlib) — or
    None if nothing is close. Used only to HINT: an unknown --mode is always REJECTED, never auto-applied,
    because silently running a different permission mode than the operator typed is exactly the failure this
    guards against (specs/0042)."""
    import difflib
    m = difflib.get_close_matches(name or "", sorted(_MODES), n=1, cutoff=0.6)
    return m[0] if m else None


def _log_dir_grant(agent, path, tier):
    """specs/0074: persist a mid-session directory grant as a typed record so --resume can restore the fence.
    Best-effort — logging a grant must never break the REPL."""
    traj = getattr(agent, "traj", None)
    if traj is not None:
        try:
            traj.log_dir_grant(path, tier)
        except Exception:  # noqa: BLE001
            pass


def _repl_add_dir(agent, ctx, path):
    """`/add-dir <path>` — grant a reference folder mid-session (0003 host access).
    Widens the LIVE permission fence for READS and tells the agent it can now read there.

    specs/0071: routes into read_only_roots, NOT extra_roots. It prints 'granted (read)' and tells the model
    the folder is READ-only — but extra_roots is write-capable and the acceptEdits/bypass baseline auto-allows
    write_file there, so the enforced grant was strictly WIDER than the one shown to operator and model (a
    'read-only' reference repo could be silently edited). read_only_roots widens reads only."""
    path = path.strip().strip('"')
    if not path:
        print("usage: /add-dir <path>"); return
    ap = os.path.abspath(path)
    if not os.path.isdir(ap):
        print(f"  not a directory: {ap}"); return
    real = os.path.realpath(ap)
    if real not in ctx.permissions.read_only_roots:
        ctx.permissions.read_only_roots.append(real)
        _log_dir_grant(agent, real, "read_only")   # specs/0074: typed record so resume restores the grant
    # Tell the agent (human-grant -> the model needs to KNOW the folder is readable).
    agent.cm.add({"role": "user", "content":
                  f"(system) Read access granted to: {ap}\n"
                  f"You may now read files there with absolute paths, and pass that path to "
                  f"grep/glob to search it. It is READ-only reference unless told otherwise."})
    print(f"  granted (read): {ap}")


def _repl_grant_readonly(agent, ctx, ap):
    """Grant READ-only access to a directory the USER TYPED (specs/0035 fix A). Routes into read_only_roots
    (NOT extra_roots: a trusted-user-dir grant widens READS only, never writes) and tells the agent the
    CORRECT absolute path so it reads there instead of a mis-typed one — the whole point is that the grant
    is keyed off the user's own text, immune to the model corrupting the path. Distinct from _repl_add_dir,
    which is the human-explicit /add-dir grant (write-capable extra_roots). Prints so the auto-grant is
    never silent (the session-lived widening stays visible)."""
    real = os.path.realpath(ap)
    if real not in ctx.permissions.read_only_roots:
        ctx.permissions.read_only_roots.append(real)
        _log_dir_grant(agent, real, "read_only")   # specs/0074: restore this grant on --resume
    agent.cm.add({"role": "user", "content":
                  f"(system) Read access granted to: {ap}\n"
                  f"You may now read files there with absolute paths, and pass that path to grep/glob to "
                  f"search it. It is READ-only reference unless told otherwise."})
    print(f"  auto-granted READ: {ap}  (a directory you named; read-only)")


def _repl_set_mode(ctx, name):
    """`/mode <name>` — switch the permission mode mid-session."""
    name = name.strip()
    if name not in _MODES:
        print(f"  current mode: {ctx.permissions.mode}\n  usage: /mode <{' | '.join(sorted(_MODES))}>")
        return
    ctx.permissions.mode = name
    print(f"  mode -> {name}")
    # propose mode needs propose_changes in the toolset, which is fixed at launch from CODE_PROPOSE. If it
    # was off, switching now leaves the mode read-only with no way to approve a plan — say so, don't strand.
    if name == "propose" and not config.PROPOSE:
        print("  note: this session launched without CODE_PROPOSE, so propose_changes isn't available - "
              "edits fall back to per-edit [y/N] approval (default-mode behavior), NOT read-only. Restart "
              "with --mode propose (or CODE_PROPOSE=true) to use propose mode.")


def _repl_approve(ctx):
    """`/approve` (specs/0052) — manually unlock a propose-mode session when the model never called
    propose_changes. Sets ctx.propose_graduated so the specs/0048 + autoplan relaxations let further edits /
    commands fall to per-op [y/N] approval instead of a read-only dead-end (deny-rules + the workspace fence
    still gate every op). Only meaningful in propose mode with CODE_PROPOSE_AUTOPLAN on; says so otherwise,
    never strands."""
    if getattr(ctx.permissions, "mode", None) != "propose":
        print(f"  /approve applies only in propose mode (current: {ctx.permissions.mode}).")
        return
    if not config.PROPOSE_AUTOPLAN:
        print("  note: this session launched without CODE_PROPOSE_AUTOPLAN, so /approve can't unlock it - "
              "set CODE_PROPOSE_AUTOPLAN=true (and restart) to use it.")
        return
    ctx.propose_graduated = True
    print("  approved - session unlocked. Edits and commands now fall to per-op [y/N] approval "
          "(deny-rules and the workspace fence still apply).")


def _run_session(traj, agent, ctx):
    """The interactive chat loop, shared by a fresh REPL and a resumed session."""
    print(f"{config.agent_name()} REPL | model={config.display_model()} | mode={ctx.permissions.mode} | "
          f"effort={config.display_effort()} | workspace={ctx.cwd}")
    cmds = "/exit  /plan  /add-dir <path>  /mode <name>"
    if config.PROPOSE and config.PROPOSE_AUTOPLAN:
        cmds += "  /approve"   # specs/0052: unlock a propose session the model didn't propose in
    if config.WORKFLOWS_ASYNC:
        cmds += "  /tasks  /result <id>"
    print("Type a task and press enter. Commands: " + cmds)
    log.info("REPL start | model=%s mode=%s workspace=%s", config.display_model(), ctx.permissions.mode, ctx.cwd)
    last_todos = _show_todos(ctx.cwd)   # surface the project backlog at startup (Phase 23; no-op when off)
    _show_spec(ctx.cwd)                 # surface the active spec at startup (Phase 25; no-op when off)
    turns = 0
    # Async background runtime (specs/0040): a registry + a pending-result queue, built ONLY when the flag is
    # on, so a flag-off session builds nothing and every gated block below executes zero lines (byte-identical).
    reg = tasks.TaskRegistry() if config.WORKFLOWS_ASYNC else None
    ctx.task_registry = reg
    pending_results = []
    try:
        while True:
            if reg is not None:
                try:
                    reg.refresh()
                    for line in reg.drain_finished():   # completion banner, drained-once, before the prompt
                        print(line)
                except Exception:  # noqa: BLE001 - a drain error must never kill the REPL
                    pass
            try:
                user = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not user:
                continue
            if user in ("/exit", "/quit", "\\exit", "\\quit", ":q"):   # tolerate the common backslash typo
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
            if user == "/approve":   # specs/0052: unlock a propose-mode session directly
                _repl_approve(ctx)
                continue
            if reg is not None and user == "/tasks":   # specs/0040: list background tasks
                print(reg.render())
                continue
            if reg is not None and user.startswith("/result"):   # specs/0040: pull a finished digest to fold in
                pulled = reg.pull(user[len("/result"):])
                if pulled is None:
                    print("  no finished task matches that id - /tasks to list.")
                else:
                    print(tasks.render_result(pulled))
                    pending_results.append(pulled)
                continue
            # Trusted user dirs (specs/0035 fix A): a directory the user LITERALLY typed, if it exists and
            # is safe (userdirs applies the denylist + negation veto), is granted READ access keyed off the
            # user's own text. Off by default -> the extractor never runs and nothing is granted or printed
            # (byte-identical). A grant widens reads only (read_only_roots); it is never write-capable.
            if config.TRUST_USER_DIRS:
                for _ap in userdirs.user_typed_dirs(user):
                    _repl_grant_readonly(agent, ctx, _ap)
            turns += 1
            log.info("turn %d | you> %s", turns, user)
            # Fold any pulled background results into THIS turn as ONE user message (specs/0040); no-op + the
            # exact same `user` object when there are none, so a flag-off/no-pull turn is byte-identical.
            task_for_model = tasks.fold_result(pending_results, user) if (reg is not None and pending_results) else user
            try:
                result = agent.run(task_for_model, ctx)
            except KeyboardInterrupt:
                # specs/0070: Ctrl-C mid-turn stops THIS turn, not the whole REPL (Ctrl-C is the standard way
                # to stop the weak model when it loops). KeyboardInterrupt is a BaseException, so the
                # `except Exception` below never caught it and it unwound the whole session with a traceback.
                # Stamp the turn honestly (so it's dropped from the corpus, never washed to 'completed') and
                # return to the you> prompt like a model error does.
                log.warning("turn %d interrupted (Ctrl-C)", turns)
                traj.log_turn_outcome(turns, "error", "interrupted", 0)
                print("\n[interrupted] turn stopped — back at the prompt (/exit to quit).")
                continue
            except Exception as e:
                # A model error (500, context overflow, a flaky worker) must NOT kill the
                # REPL — end the turn with a message and keep the session alive.
                log.exception("turn %d FAILED: %s: %s", turns, type(e).__name__, e)
                # specs/0070: stamp the CRASHED turn with an honest 'error' outcome (written directly, NOT via
                # outcomes.classify — which would wash a tool_calls>0 crash to 'completed'). Without this the
                # turn logged NO turn_outcome, so a session whose only turn crashed had traj.tool_calls>0 ->
                # session_end 'completed', and train/convert.py's legacy one-shot branch kept the truncated
                # partial turn as a trainable success (corpus poison). An 'error' turn_outcome instead routes
                # convert to the per-turn path, which drops this turn and keeps the segment counter aligned.
                traj.log_turn_outcome(turns, "error", type(e).__name__, 0)
                print(f"\n[error] that turn failed: {type(e).__name__}: {str(e)[:200]}\n"
                      "(the session is still alive — try again, rephrase, or /exit)")
                continue
            if reg is not None:
                pending_results.clear()   # delivered -> clear ONLY after a successful run (a failed turn keeps them)
            # Stamp THIS turn's honest outcome (0.7.0) so convert can drop a degenerate/ungrounded/
            # unverified turn WITHOUT discarding the good turns in the same session. result.tool_calls is
            # this turn's own count; classify() is the same mapping the one-shot path uses.
            traj.log_turn_outcome(turns, outcomes.classify(result.terminated, result.tool_calls),
                                  result.terminated, result.tool_calls)
            if result.final:
                print("\n" + result.final)
                log.info("turn %d result: %s", turns, result.final[:500])
            else:
                log.warning("turn %d produced no output (dropped response?)", turns)
                print("\n(no output — the model may have dropped the response, often a cold/"
                      "flaky endpoint. Try again; the warm-up should recover it.)")
            # Re-surface the backlog ONLY if the agent changed it this turn (Phase 23) — an item added or
            # checked off. Change-gated so it never nags or double-prints the unchanged list.
            if config.PROJECT_TODOS:
                from . import todos as _todos
                new_todos = _todos.backlog_text(ctx.cwd)
                if new_todos != last_todos:
                    print(f"\nProject todos updated:\n{new_todos}\n" if new_todos
                          else "\nProject todos: all clear.\n")
                    last_todos = new_todos
    finally:
        if reg is not None:
            _teardown_tasks(reg)   # specs/0040: prompt to keep-running or cancel any still-running tasks
        traj.end("completed" if traj.tool_calls else "no_action", None, terminated="session_end")
        log.info("REPL end | %d turn(s) tool_calls=%s", turns, traj.tool_calls)
        print(f"\nsession ended ({turns} turn(s)). resume later with:"
              f"  python -m src --resume {traj.session_id}")
        _print_log_path()
    return 0


def _teardown_tasks(reg):
    """On session end, handle any still-running background tasks (specs/0040). PROMPT the user (per request):
    KEEP them running after exit (they finish + write result files under trajectories/tasks/, unattended), or
    CANCEL them now. No human present (EOF/Ctrl-C) -> cancel, so an unattended exit never leaves orphaned
    subprocesses hitting the model."""
    reg.refresh()
    running = reg.non_terminal()
    if not running:
        return
    try:
        ans = input(f"\n{len(running)} background task(s) still running. "
                    "Keep them running after you exit, or cancel them? [k]eep / [c]ancel: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        ans = "c"
    if ans.startswith("k"):
        print(f"  left {len(running)} task(s) running; results will land in {tasks.tasks_dir()}.")
        return
    for t in running:
        reg.cancel(t)
    print(f"  cancelled {len(running)} background task(s).")


def _repl(perms):
    """Fresh interactive session."""
    workspace = config.WORKSPACE
    traj = Trajectory(config.trajectory_dir(), "(interactive session)", config.MODEL, workspace,
                      safety=config.safety_fingerprint(perms))   # specs/0033: launch-time safety snapshot
    logsetup.configure(traj.session_id)
    ctx = make_context(workspace, perms, traj.session_id,
                       depth=0, verbose=config.VERBOSE, interactive=True)
    agent = build_agent(traj, memory=_load_memory(workspace), todos=_load_todos(workspace),
                        spec=_load_spec(workspace), granted_dirs=perms.extra_roots, cwd=workspace,
                        show_reasoning=config.SHOW_REASONING)   # specs/0064: top-level tees the thinking live
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
    logsetup.configure(traj.session_id)
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


def _venv_python_and_exe(root):
    """Resolve the venv openagent-code launcher exe + python for the generated launcher to call (specs/0036).
    Prefers the install-root venv layout; falls back to the installed console script on PATH. Never a bare
    'python' (a generated launcher must point at THIS install's interpreter, not a Store/global one)."""
    import shutil
    win = os.name == "nt"
    scripts = os.path.join(root, ".venv", "Scripts" if win else "bin")
    exe = os.path.join(scripts, "openagent-code.exe" if win else "openagent-code")
    py = os.path.join(scripts, "python.exe" if win else "python")
    if not os.path.isfile(exe):
        found = shutil.which("openagent-code")
        if found:
            exe = found
            py = os.path.join(os.path.dirname(found), "python.exe" if win else "python")
    return exe, py


def _atomic_write(path, text):
    """Write text atomically (temp + os.replace) so a crash / OneDrive lock can't leave a half-written file."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _parse_set_name(args):
    """Parse `<name> [--persona "..."] [--no-profile]` -> (name|None, persona, no_profile)."""
    rest = list(args)
    no_profile = "--no-profile" in rest
    rest = [a for a in rest if a != "--no-profile"]
    name, persona = None, ""
    if rest and not rest[0].startswith("--"):
        name, rest = rest[0], rest[1:]
    if "--persona" in rest:
        j = rest.index("--persona")
        if j + 1 < len(rest):
            persona = rest[j + 1]
    return name, persona, no_profile


def _powershell_profiles():
    """The CurrentUserCurrentHost $PROFILE path for each installed PowerShell (pwsh/PS7, then powershell/PS5),
    resolved by ASKING PowerShell itself (Python can't read the $PROFILE automatic variable), with -NoProfile
    so resolution never runs the user's own profile (specs/0037). Empty on POSIX / no PowerShell -> the caller
    falls back to printing the manual line. Never raises."""
    import shutil
    out = []
    for name in ("pwsh", "powershell"):
        exe = shutil.which(name)
        if not exe:
            continue
        try:
            r = subprocess.run([exe, "-NoProfile", "-Command", "$PROFILE"],
                               capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        path = (r.stdout or "").strip()
        if path and path not in out:
            out.append(path)
    return out


def _apply_to_profiles(line, remove=False):
    """Register (or un-register, remove=True) the launcher dot-source `line` in each resolved PowerShell
    profile - idempotently, atomically, creating the file + dir when registering (specs/0037). Returns a list
    of (path, action) for the report; empty when no PowerShell profile is found."""
    results = []
    for prof in _powershell_profiles():
        try:
            text = ""
            if os.path.isfile(prof):
                with open(prof, encoding="utf-8") as f:
                    text = f.read()
            elif remove:
                continue   # nothing to remove from a profile that doesn't exist
            if remove:
                new, changed = installshim.profile_remove(text, line)
            else:
                os.makedirs(os.path.dirname(prof), exist_ok=True)
                new, changed = installshim.profile_ensure(text, line)
            if changed:
                _atomic_write(prof, new)
            results.append((prof, ("removed" if changed else "not present") if remove
                            else ("added" if changed else "already present")))
        except OSError as e:
            results.append((prof, f"failed: {e}"))
    return results


def _set_name(args):
    """`openagent-code --set-name <name> [--persona "..."]` (specs/0036): write CODE_AGENT_NAME (+persona) to
    the install-root .env and generate a launcher named <name> mirroring scripts/oac.ps1. Set-and-exit — no
    network I/O. 0 on success, 2 on a usage/validation error."""
    name, persona, no_profile = _parse_set_name(args)
    if not name:
        print('usage: openagent-code --set-name <name> [--persona "..."] [--no-profile]')
        return 2
    try:
        name = installshim.validate_name(name)
    except ValueError as e:
        print(f"  invalid name: {e}")
        return 2
    root = config.INSTALL_ROOT
    exe, py = _venv_python_and_exe(root)
    plan = installshim.plan_launcher(name, root, exe, py, windows=(os.name == "nt"))
    import shutil
    existing = shutil.which(name)   # a DIFFERENT real command already on PATH -> refuse (our .ps1 fn isn't found here)
    if existing and os.path.realpath(existing) != os.path.realpath(plan.path):
        print(f"  refusing: a command named '{name}' already exists at {existing}. Pick another name.")
        return 2
    os.makedirs(os.path.dirname(plan.path), exist_ok=True)
    _atomic_write(plan.path, plan.content)
    if plan.chmod is not None:
        os.chmod(plan.path, plan.chmod)
    env_path = os.path.join(root, ".env")
    env_text = ""
    if os.path.isfile(env_path):
        with open(env_path, encoding="utf-8") as f:
            env_text = f.read()
    _atomic_write(env_path, installshim.compute_env_update(env_text, name, persona))
    print(f"  wrote launcher: {plan.path}")
    print(f"  set CODE_AGENT_NAME={name}" + ("  (+ persona)" if persona.strip() else "") + f" in {env_path}")
    line = plan.profile_line
    if line and not no_profile:
        reg = _apply_to_profiles(line)   # specs/0037: register the launcher in $PROFILE automatically
        if reg:
            for prof, action in reg:
                print(f"  $PROFILE [{action}]: {prof}")
            print(f"  Reload with '. $PROFILE' (or just open a new shell), then type:  {name}")
            return 0
        print("  (no PowerShell profile found to auto-register)")
    if line:
        print("  Add this line to your PowerShell $PROFILE (notepad $PROFILE), then reload (. $PROFILE):")
        print(f"      {line}")
    else:
        print(f"  {plan.note}")
    print(f"  Then launch it by typing:  {name}")
    return 0


def _remove_name(_args):
    """`openagent-code --remove-name` (specs/0036): revert CODE_AGENT_NAME / CODE_AGENT_PERSONA to the OAC
    default and remove the generated launcher for the current name. Set-and-exit. Returns 0."""
    root = config.INSTALL_ROOT
    current = config.agent_name()
    env_path = os.path.join(root, ".env")
    env_text = ""
    if os.path.isfile(env_path):
        with open(env_path, encoding="utf-8") as f:
            env_text = f.read()
    _atomic_write(env_path, installshim.compute_env_update(env_text, None, None))
    print(f"  reverted CODE_AGENT_NAME / CODE_AGENT_PERSONA to the default (OAC) in {env_path}")
    if current and current not in ("OAC", "openagent-code"):
        plan = installshim.plan_remove(current, root, windows=(os.name == "nt"))
        if os.path.isfile(plan.path):
            try:
                os.remove(plan.path)
                print(f"  removed launcher: {plan.path}")
            except OSError as e:
                print(f"  could not remove {plan.path}: {e}")
        if plan.profile_line:
            unreg = _apply_to_profiles(plan.profile_line, remove=True)   # specs/0037: un-register from $PROFILE
            for prof, action in unreg:
                print(f"  $PROFILE [{action}]: {prof}")
            if not unreg:
                print("  If you added it manually, remove this line from your PowerShell $PROFILE:")
                print(f"      {plan.profile_line}")
    print("  Done. Reload $PROFILE (or open a new shell) for the change to take effect.")
    return 0


def _run_task(task_id, spec_path, perms):
    """Background-worker entry (specs/0040): run ONE submitted workflow to a result file and exit. Launched as
    a subprocess by an async run_workflow submit; never interactive; READ-ONLY (perms is --mode plan). Its own
    run_workflow can never re-enter the async branch (interactive=False + _OAC_BG_WORKER=1 + no registry), so
    it runs the workflow INLINE and writes {status, digest, session_id, path} for the parent REPL to drain."""
    from . import workflow
    workspace = config.WORKSPACE
    spec = tasks.read_spec(spec_path)
    if spec is None:
        tasks.write_result(task_id, {"status": "error", "digest": "(could not read the task spec)"})
        return 1
    traj = Trajectory(config.trajectory_dir(), f"(background workflow {task_id})", config.MODEL, workspace,
                      safety=config.safety_fingerprint(perms))
    logsetup.configure(traj.session_id)
    ctx = make_context(workspace, perms, traj.session_id, depth=0, verbose=config.VERBOSE, interactive=False)
    agent = build_agent(traj, granted_dirs=perms.extra_roots, cwd=workspace)  # noqa: F841 - warms the toolset/prompt
    try:
        result = workflow.run_workflow({"workflow": spec.get("phases"), "synthesis": spec.get("synthesis")}, ctx)
        ok = result.ok
        traj.end("completed" if ok else "error", result.content if ok else None, terminated="workflow_done")
        tasks.write_result(task_id, {"status": "done" if ok else "error", "digest": result.content,
                                     "session_id": traj.session_id, "path": traj.path})
        return 0 if ok else 1
    except Exception as e:
        traj.end("error", None, terminated="exception")
        tasks.write_result(task_id, {"status": "error",
                                     "digest": f"(background workflow crashed: {type(e).__name__}: {e})"})
        return 1


def main(argv=None):
    _force_utf8_stdout()
    argv = list(argv if argv is not None else sys.argv[1:])
    # Agent-name install verbs (specs/0036): set-and-exit BEFORE _parse_flags / Permissions / MCP connect() /
    # warm_up(), so they do ZERO network I/O and need no configured endpoint. They must be the LEADING token;
    # appearing anywhere else is a usage error, never allowed to slip through into the task prompt (trap D).
    if argv and argv[0] == "--set-name":
        return _set_name(argv[1:])
    if argv and argv[0] == "--remove-name":
        return _remove_name(argv[1:])
    if "--set-name" in argv or "--remove-name" in argv:
        print('usage: openagent-code --set-name <name> [--persona "..."]   |   openagent-code --remove-name'
              "\n(run the name verb as the FIRST argument)")
        return 2
    mode_override, add_dirs, argv = _parse_flags(argv)
    # Validate the --mode LAUNCH FLAG before it reaches Permissions (specs/0042). An unknown mode — the live
    # `--mode perpose` typo — used to sail straight through: Permissions stored the bogus string, `propose`
    # never turned on, and propose-mode auto-allow silently degraded into a per-write approval prompt for
    # every single edit (27 prompts after one approved manifest). A permission-mode typo must FAIL LOUD and
    # never be guessed at. (An invalid CODE_PERMISSION_MODE *env* value is a config default, not this-run
    # intent, so it keeps its back-compat fallback in resolved_permission_mode — only the explicit flag is
    # hard-rejected here.)
    if mode_override is not None and mode_override not in _MODES:
        hint = _closest_mode(mode_override)
        sugg = f" (did you mean --mode {hint}?)" if hint else ""
        print(f"unknown --mode '{mode_override}'{sugg}\n  valid modes: {' | '.join(sorted(_MODES))}")
        return 2
    perms = Permissions.from_config(mode_override=mode_override, extra_dirs=add_dirs)
    # Selecting propose mode (--mode propose / CODE_PERMISSION_MODE=propose) turns the propose machinery on,
    # so `propose_changes` is actually offered (toolset reads config.PROPOSE, built next in build_agent).
    # Without this the mode would validate but stay a dead read-only mode with no way to ever approve a plan.
    if perms.mode == "propose":
        config.PROPOSE = True
    # Background worker entry (specs/0040): a subprocess launched by an async run_workflow submit. Runs ONE
    # submitted workflow to a result file and exits. Minimal bring-up — it does NOT connect the human-facing
    # MCP servers (N workers each opening the full set can conflict on exclusive resources); it warms its own
    # model. The launcher passes `-C <workspace> --mode plan` (applied above), so the worker is READ-ONLY.
    if argv and argv[0] == "--run-task":
        if len(argv) < 3:
            print("usage: python -m src --run-task <task_id> <spec_path>")
            return 2
        from .model import warm_up, resolve_model_window
        resolve_model_window()   # specs/0045: resolve an auto window before ContextManager budgets are read
        warm_up()
        return _run_task(argv[1], argv[2], perms)
    from .mcp_client import connect, disconnect
    from .model import warm_up, resolve_model_window
    n = connect()
    if n:
        print(f"MCP: connected {n} tool(s)")
    # Absorb a scale-to-zero cold start once, so the first task runs warm.
    resolve_model_window()   # specs/0045: resolve an auto window before any ContextManager budgets are read
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
