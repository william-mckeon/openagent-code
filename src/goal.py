"""
src/goal.py

Phase 20 (specs/0020) — the goal loop: pursue a MACHINE-CHECKABLE bar, unattended.

The agent declares an objective plus a bar (a runnable check) via the `pursue` tool; the HARNESS then
iterates — work, run the bar, feed the REAL output back — until the bar passes or a budget runs out. The
model never decides "done"; the bar command does. The entry filter IS the loop-shape heuristic: no
runnable bar, no loop.

THE TRUST INVERSION. verify_edits (0014) runs ARGV lists from an OPERATOR-configured file — "there is no
shell to inject". This bar is MODEL-proposed and re-run every iteration, unattended, so it is defended in
four layers: argv-only shape (a shell string is refused at the tool boundary), an entry filter (no
execpolicy-DANGEROUS bar, no shell/interpreter argv[0], an optional operator allowlist), the permission
gate (decide('run_command', ...) once at entry, so deny rules / fence / execpolicy / guardian / hooks all
apply), and execution with shell=False + cwd + timeout.

Pure functions + an injected run_fn (so the harness is testable with no model, no network, no shell).
Imports only config + logsetup + execpolicy — no cycle with permissions/agent. NEVER raises.
"""
import subprocess

from . import config
from . import execpolicy
from .logsetup import get_logger

log = get_logger("goal")

# argv[0] basenames that would re-open the shell the argv discipline just closed. A bar is a CHECK
# (pytest / npm test / go build), never an interpreter we hand a script to.
_SHELL_ARGV0 = {
    "sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh",
    "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe",
    "eval", "exec", "source", "env", "sudo", "su", "nohup", "xargs", "start",
}
# Interpreters are fine as a bar (`python -m pytest`) but NOT with an inline-code flag, which is a shell
# by another name (`python -c "import os; os.system(...)"`).
_INLINE_CODE_FLAGS = {"-c", "-e", "--eval", "--exec", "--command", "-Command", "-EncodedCommand"}
_INTERPRETERS = {"python", "python3", "py", "node", "ruby", "perl", "php", "deno", "bun"}


def normalize_bar(bar):
    """A bar is a non-empty ARGV LIST of strings, or None. A shell STRING is never accepted (that is the
    whole point) — it returns None so the caller refuses it."""
    if not isinstance(bar, (list, tuple)):
        return None
    argv = [str(x) for x in bar if isinstance(x, (str, int, float))]
    if len(argv) != len(bar) or not argv or not any(a.strip() for a in argv):
        return None
    return argv


def render(bar):
    """The bar as a display/gating STRING (for the permission engine, the log line, the label). Rendering
    is one-way: nothing ever executes this string — execution takes the argv list."""
    argv = normalize_bar(bar) or []
    return " ".join(argv)


def _argv0(argv):
    tok = (argv[0] or "").lower().strip("'\"")
    tok = tok.replace("\\", "/").rsplit("/", 1)[-1]
    return tok[:-4] if tok.endswith(".exe") else tok


def entry_ok(bar):
    """(ok, why) — the entry filter (specs/0020). Refuse a bar we must never hand a loop:
      * not an argv list (a shell string / empty),
      * a shell or interpreter-with-inline-code argv[0] (re-opens the shell),
      * execpolicy-DANGEROUS (rm -rf, find -delete, a redirect to an absolute path, ...) — a BAR is a
        check; a destructive one is never a legitimate bar, and the destructive cap can't bound its
        REPETITION (it counts distinct targets, not runs), so it's refused outright rather than gated,
      * outside the operator allowlist, when CODE_GOAL_BARS_CONFIG is configured.
    Never raises."""
    argv = normalize_bar(bar)
    if argv is None:
        return False, "a bar must be a non-empty ARGV LIST of strings (a shell string is not accepted)"
    tok = _argv0(argv)
    if tok in _SHELL_ARGV0:
        return False, f"a bar may not be a shell/interpreter ({tok!r}) - give the check's own argv"
    if tok in _INTERPRETERS and any(f in _INLINE_CODE_FLAGS for f in argv[1:]):
        return False, f"a bar may not run inline code ({tok} -c ...) - give the check's own argv"
    try:
        if execpolicy.assess(render(argv)).worst == execpolicy.DANGEROUS:
            return False, "a bar must be a CHECK, not a destructive command"
    except Exception:  # noqa: BLE001 - classification must never break the filter
        pass
    allow = config.load_goal_bars()
    if allow and argv not in allow:
        return False, "this bar is not in the operator's allowlist (CODE_GOAL_BARS_CONFIG)"
    return True, ""


def gate(bar, ctx):
    """Gate the bar through the permission engine ONCE, at loop entry, as the run_command it is — so deny
    rules, the workspace fence, execpolicy, the guardian and hooks all apply to a model-proposed command.
    Returns (allowed, reason). No engine on ctx -> (True, '') (the entry filter still ran)."""
    perms = getattr(ctx, "permissions", None)
    if perms is None:
        return True, ""
    try:
        d = perms.decide("run_command", {"command": render(bar)}, ctx)
    except Exception as e:  # noqa: BLE001 - a gate failure must refuse, never crash the tool
        log.warning("goal: bar gate raised (%s) - refusing", e)
        return False, f"permission check failed: {e}"
    return bool(d.allowed), getattr(d, "reason", "")


def _default_run_fn(cwd, timeout=None):
    """Run a bar ARGV with NO shell (no injection surface), in the workspace, bounded by a timeout."""
    def run(argv):
        try:
            p = subprocess.run(argv, cwd=cwd, capture_output=True, encoding="utf-8",
                               errors="replace", timeout=timeout or config.GOAL_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"(bar could not run: {e})"
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        return p.returncode == 0, out
    return run


def run_bar(bar, cwd, run_fn=None):
    """(ok, output) for ONE bar run. ok is the process's own verdict (exit 0), never the model's opinion.
    Never raises — an unrunnable bar is a FAILING bar (the loop reports it, it doesn't crash)."""
    argv = normalize_bar(bar)
    if argv is None:
        return False, "(invalid bar)"
    fn = run_fn or _default_run_fn(cwd)
    try:
        ok, out = fn(argv)
    except Exception as e:  # noqa: BLE001
        log.warning("goal: bar run raised (%s)", e)
        return False, f"(bar could not run: {e})"
    return bool(ok), (out or "")


def challenge(objective, bar, output, iteration, max_iterations):
    """The re-prompt after a FAILING bar: the real output, and what to do. Directive and de-echoed (a
    ride-5 lesson: answer-shaped instructions get parroted back into the answer)."""
    tail = (output or "").strip()
    if len(tail) > 2000:
        tail = tail[-2000:]
    return (f"The bar has NOT passed yet (attempt {iteration} of {max_iterations}).\n\n"
            f"  GOAL: {objective}\n"
            f"  BAR:  {render(bar)}\n\n"
            f"Its real output:\n{tail}\n\n"
            "Fix the underlying cause and continue. Do not explain this instruction, do not restate these "
            "steps, and do not claim success - the bar decides, and it will be re-run. If the goal is "
            "impossible or the bar is wrong, say so plainly instead of looping.")
