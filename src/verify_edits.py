"""
src/verify_edits.py

Auto-verify (specs/0014, sub-phase A).

After the completion gate proves the agent's file changes are REAL, run a configured check on just the
TOUCHED files and feed any errors back as a bounded reflection turn, then record pass/fail as an
objective reward label. This module is the caller-agnostic core: pure functions + an injected run_fn,
mirroring src/grounding.py. It imports only config + logsetup - no import cycle, never raises.

Safety (why the harness-run command is safe even before OS sandboxing, specs/0017):
  - Commands are ARGV LISTS, never a shell string, run with shell=False - there is no shell to inject
    into, so a hostile filename cannot smuggle a command.
  - The command is OPERATOR-configured (the safe built-in default merged with CODE_VERIFY_CMDS_CONFIG),
    never model-controlled.
  - The only variable is the touched-file PATH (from ctx.mutations, already workspace-fenced).
  - Timeout + workspace cwd + fail-OPEN: a run error / unconfigured extension yields NO problem, so an
    infra hiccup never traps the agent (the completion gate already guaranteed the work was done).
"""
import json
import os
import subprocess

from . import config
from .logsetup import get_logger

log = get_logger("verify")

# ext -> ARGV template (a LIST, not a shell string). '{file}' is replaced with the touched path. The
# default is dependency-free (no external tool, no network) and clean on PowerShell + bash.
_DEFAULT_CMDS = {".py": ["python", "-m", "py_compile", "{file}"]}


def verifier_cmds():
    """ext -> argv template. The safe built-in default merged with an optional JSON map at
    CODE_VERIFY_CMDS_CONFIG (each value an argv LIST). Missing/bad file -> defaults only; never raises."""
    cmds = dict(_DEFAULT_CMDS)
    path = config.VERIFY_CMDS_CONFIG
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):               # a valid-JSON non-dict ([...] / "x" / 42) has no
                raise ValueError("expected a JSON object (ext -> argv list)")   # .items(); fail open, don't raise
            for ext, argv in data.items():
                if isinstance(argv, list) and argv:      # argv only - never accept a shell string
                    cmds[str(ext)] = [str(a) for a in argv]
        except (OSError, ValueError):
            log.warning("bad CODE_VERIFY_CMDS_CONFIG (%s) - using defaults only", path)
    return cmds


def select(touched, cmds=None):
    """Map each TOUCHED write/edit path to its verifier argv (by extension), '{file}' substituted. Skips
    deletes and any file with no configured verifier. `touched` is ctx.mutations {relpath: action}.
    Returns [(path, argv)]."""
    cmds = verifier_cmds() if cmds is None else cmds
    out = []
    for path, action in (touched or {}).items():
        if action not in ("write", "edit"):
            continue
        argv = cmds.get(os.path.splitext(path)[1].lower())
        if argv:
            out.append((path, [a.replace("{file}", path) for a in argv]))
    return out


def run_checks(selected, run_fn):
    """Run each (path, argv) via the injected run_fn(argv) -> (ok, output). Returns the FULL per-file
    result list [{file, cmd, ok, error, output}] — both passes and fails (sub-phase C needs the passes
    for the reward label). Fail-OPEN: a run_fn that RAISES is skipped (no entry), so an infra failure is
    never counted as a code failure."""
    out = []
    for path, argv in selected:
        try:
            ok, output = run_fn(argv)
        except Exception as e:  # noqa: BLE001 - a verifier failure must never crash the parent turn
            log.warning("verify run_fn raised for %s (%s) - skipping, fail-open", path, e)
            continue
        out.append({"file": path, "cmd": " ".join(argv), "ok": bool(ok), "output": output or "",
                    "error": "" if ok else (parse_errors(output) or "(check failed)")})
    return out


def problems_from(results):
    """The 'file: error' strings for the FAILING checks — what challenge() lists and problems() returns."""
    return [f"{r['file']}: {r['error']}" for r in results if not r["ok"]]


def parse_errors(output):
    """Tolerantly pull the first few non-blank 'file:line: message' style lines from a check's output,
    truncated so a huge log can't dominate the re-prompt."""
    lines = [ln.strip() for ln in (output or "").splitlines() if ln.strip()]
    return " | ".join(lines[:4])[:400]


def challenge(problems):
    """The re-prompt when a touched-file check fails. `problems` is the list of 'file: error' strings
    from problems() / problems_from(). Targeted + NON-HIJACKING (mirrors grounding.challenge)."""
    return ("A check on the file(s) you just changed reported errors:\n"
            + "\n".join(f"- {p}" for p in problems)
            + "\nFix ONLY those files so the check passes, then finish - do not refactor unrelated code "
              "or re-do work that already succeeded.")


def _default_run_fn(cwd):
    """The runtime run_fn: run a verifier ARGV as a subprocess with NO shell (no injection surface), in
    the workspace cwd, with a timeout and utf-8/replace decoding (mirrors run_command). ok == exit 0.
    specs/0073: a TIMEOUT is a FAILED check — a verifier that never finished is NOT a pass (returning True
    logged a passing verification reward and flipped ctx._verified_ok, corpus poison, exactly when the code
    was likely broken). A genuine OSError (a missing/unrunnable verifier binary = infra, not a code failure)
    still fails OPEN."""
    def run(argv):
        try:
            p = subprocess.run(argv, cwd=cwd, capture_output=True, encoding="utf-8",
                               errors="replace", timeout=config.VERIFY_TIMEOUT)
        except subprocess.TimeoutExpired:
            log.warning("verify command %r timed out after %ss - marking FAILED", argv[:1], config.VERIFY_TIMEOUT)
            return False, f"(verifier timed out after {config.VERIFY_TIMEOUT}s)"
        except OSError as e:
            log.warning("verify command %r failed to run (%s) - skipping, fail-open", argv[:1], e)
            return True, ""   # fail-OPEN: a missing/unrunnable verifier binary is not a code failure
        return p.returncode == 0, (p.stdout or "") + (p.stderr or "")
    return run


def results(ctx, run_fn=None):
    """Runtime entry: run the configured verifier over this run's TOUCHED files; return the FULL per-file
    result list ([] when there's nothing to verify). Fail-OPEN throughout. The caller (agent.py) gates on
    config.VERIFY_TOUCHED first, uses problems_from() for the challenge, and logs each result as a reward."""
    selected = select(getattr(ctx, "mutations", None) or {})
    if not selected:
        return []
    rf = run_fn or _default_run_fn(getattr(ctx, "cwd", "."))
    return run_checks(selected, rf)


def problems(ctx, run_fn=None):
    """The failing-check strings ([] == clean) — the simple interface over results()."""
    return problems_from(results(ctx, run_fn))
