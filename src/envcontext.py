"""
src/envcontext.py

Situational-context injection (Phase 12 / specs/0012).

A small, per-turn block of the agent's REAL environment — cwd, OS, shell, today's date, the granted
reference dirs, and (opt-in) the git branch + a bounded status — built fresh each turn so the model
conditions on live state instead of confabulating the date, the OS, or which branch it is on.

Why it lives here and not in prompts.BASE_PROMPT: the block is DYNAMIC runtime state. BASE_PROMPT is a
fixed, curated fragment logged once as the first raw turn (and server-cached); putting a date or branch
there would pin it STALE across the whole session. So the block is injected as a REFRESHED pin
(ContextManager.set_env_context) that is always sent, never compacted, and replaced every turn.

Dependency-light + never-raising: only stdlib (os, platform, datetime, subprocess, shutil). git is
OPT-IN and fully injectable (git_status_fn) so the acceptance harness is deterministic and never shells
out; a missing git binary / non-repo / any git error degrades to no git line and NEVER breaks the turn.
"""
import os
import platform
import shutil
import subprocess
from datetime import datetime

_MAX_DIRS = 12          # bound the granted-dir list (the block is ALWAYS sent, so it must stay small)
_MAX_GIT_FILES = 20     # bound the changed-file list
_GIT_TIMEOUT = 5        # seconds; git runs at most twice per TURN (not per model step)


def _shell_name():
    """The shell run_command will actually use, named for the model — mirrors tools.run_command's
    os.name switch (PowerShell on Windows, else $SHELL / bash)."""
    if os.name == "nt":
        return "PowerShell"
    return os.environ.get("SHELL") or "bash"


def build_env_context(cwd, granted_dirs=None, include_git=False, git_status_fn=None, now=None):
    """Return a bounded, plain-text environment block (never None, never raises).

    Pure + injectable: `now` (a datetime) and `git_status_fn` are injected by the acceptance harness so
    it needs no real clock or git binary. `include_git` appends a git line via git_status_fn or the real
    `_git_status`; any failure there is swallowed (no git line) so a git problem can't break the turn."""
    when = now or datetime.now()  # LOCAL date so "today" matches the user's wall clock, not UTC
    lines = [
        "Environment context (refreshed each turn - trust this over assumptions):",
        f"- cwd: {cwd}",
        f"- os: {(platform.system() + ' ' + platform.release()).strip()}",
        f"- shell: {_shell_name()}",
        f"- date: {when.strftime('%Y-%m-%d')}",
    ]
    dirs = [d for d in (granted_dirs or []) if d]
    if dirs:
        shown = dirs[:_MAX_DIRS]
        extra = len(dirs) - len(shown)
        lines.append("- granted dirs: " + ", ".join(shown)
                     + (f", +{extra} more" if extra > 0 else ""))
    if include_git:
        try:
            git = (git_status_fn or _git_status)(cwd)
        except Exception:  # noqa: BLE001 - a git problem must never break the turn
            git = None
        if git:
            lines.append(f"- {git}")
    return "\n".join(lines)


def _format_git(branch, porcelain):
    """Format a bounded git line from a branch name + `git status --porcelain` text. PURE (no I/O), so
    the acceptance harness can exercise the branch/count/cap logic with a canned porcelain."""
    b = (branch or "").strip() or "(detached)"
    changed = [ln for ln in (porcelain or "").splitlines() if ln.strip()]
    summary = f"git: branch {b} | {len(changed)} changed"
    if changed:
        # porcelain lines are 'XY <path>' (2 status chars + a space); strip to the path.
        files = [ln[3:].strip() for ln in changed[:_MAX_GIT_FILES]]
        extra = len(changed) - len(files)
        summary += " (" + ", ".join(files) + (f", +{extra} more" if extra > 0 else "") + ")"
    return summary


def _git_status(cwd):
    """A bounded one-line git summary, or None on a non-repo / missing git / any error. NEVER raises.
    Runs git at most twice, once per turn (guarded by the caller's per-turn cadence)."""
    if not shutil.which("git"):
        return None
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd, capture_output=True,
            encoding="utf-8", errors="replace", timeout=_GIT_TIMEOUT)
        if branch.returncode != 0:
            return None
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=cwd, capture_output=True,
            encoding="utf-8", errors="replace", timeout=_GIT_TIMEOUT)
        return _format_git(branch.stdout, status.stdout)
    except (OSError, subprocess.SubprocessError):
        return None
