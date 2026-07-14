"""
src/guardian.py

Phase 19 (specs/0019) — a fail-CLOSED LLM approval reviewer for the ASK tier.

When a tool call hits the `ask` tier and NO human is present (a headless / unattended run), the guardian
spawns a CAPTURED reviewer subagent that judges the specific request and returns APPROVE / DENY. Unlike
the grounding gate (which fails OPEN — an infra hiccup never traps the agent, because completion already
proved the work was done), the guardian fails CLOSED: no spawn, an error, a timeout, or any verdict that
isn't a clean APPROVE -> DENY. It governs the `ask` tier ONLY (never allow / deny / read-only), so it can
only make an unattended run MORE restrictive, never less. The review rides the same captured-subagent
path as the grounding verifier, so every approval decision is a first-class trajectory that feeds the
flywheel. Imports only config + logsetup — no import cycle with permissions.
"""
import re
from collections import namedtuple

from . import config
from .logsetup import get_logger

log = get_logger("guardian")

# The verdict words (word-boundaried, case-insensitive), tolerating the markdown a gpt-oss reviewer adds.
_APPROVE = re.compile(r"\bAPPROVE\b", re.IGNORECASE)
_DENY = re.compile(r"\bDENY\b", re.IGNORECASE)

# A decision plus a short human reason — the reason feeds the console `[deny]` line and the trajectory.
Verdict = namedtuple("Verdict", "approved reason")


def review(tool, target, reason, ctx):
    """Approve or DENY one ask-tier tool call, returning a Verdict(approved, reason). FAIL-CLOSED: any
    failure returns Verdict(False, ...). Spawns a captured reviewer via ctx.spawn (the run_subagent path),
    at ctx.depth+1 — the caller gates this to depth 0 + headless, so the reviewer's own tool calls
    (depth>0, non-interactive) can't re-enter the guardian."""
    spawn = getattr(ctx, "spawn", None)
    if spawn is None:
        log.warning("guardian: no reviewer available - DENY (fail-closed)")
        return Verdict(False, "no reviewer available")
    task = _review_task(tool, target, reason)
    label = f"guardian: {tool}({_short(target)})"
    try:
        out = (spawn(task, effort=config.GUARDIAN_EFFORT, label=label)
               if config.GUARDIAN_EFFORT else spawn(task, label=label))
    except Exception as e:  # noqa: BLE001 - a reviewer failure must DENY, never crash the parent turn
        log.warning("guardian reviewer raised (%s) - DENY (fail-closed)", e)
        return Verdict(False, f"reviewer error: {e}")
    return _parse_verdict(out)


def _parse_verdict(out):
    """A clear APPROVE with NO 'DENY' ANYWHERE -> approve; everything else -> DENY: empty, a subagent
    error, any DENY, an ambiguous 'APPROVE ... but DENY', or prose with no verdict. Fail-closed by
    construction — an approve reason that even mentions 'deny' errs to a (safe) denial."""
    text = (out or "").strip()
    if not text or text.startswith("(subagent error"):
        log.warning("guardian: no usable verdict - DENY (fail-closed)")
        return Verdict(False, "no usable verdict")
    approved = bool(_APPROVE.search(text)) and not _DENY.search(text)
    return Verdict(approved, _verdict_reason(text))


def _verdict_reason(text):
    """Pull a short human reason out of the reviewer's output — the tail of the first line that carries a
    verdict word, else the first non-empty line. Never raises; only for display / the trajectory."""
    for line in text.splitlines():
        s = line.strip().strip("*#>-_ ").strip()
        if _APPROVE.match(s) or _DENY.match(s):
            tail = re.sub(r"^\**\s*(APPROVE|DENY)\b\s*:?\s*", "", s, flags=re.IGNORECASE).strip()
            return (tail or s)[:160]
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return first[:160]


def _short(target, n=48):
    s = str(target).replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _review_task(tool, target, reason):
    return (
        "You are a fail-CLOSED SECURITY GUARDIAN. You approve or deny ONE tool action another agent wants "
        "to take. You are NOT the coder and you do NOT perform the action. The call was gated for approval:\n\n"
        f"  TOOL:          {tool}\n"
        f"  TARGET:        {target}\n"
        f"  WHY IT ASKED:  {reason}\n\n"
        "APPROVE only if it is clearly SAFE and a reasonable step for a coding agent working INSIDE this "
        "workspace - e.g. a routine git / test / build command, an edit or write to a project file. A "
        "routine dependency install or build INSIDE this workspace (npm install / npm ci / npm run build, "
        "pip install, yarn, pnpm, go build / go mod download / go test) is EXPECTED and safe - APPROVE it, "
        "even though a package manager reaches the network to fetch its packages. DENY anything "
        "destructive, exfiltrating, or out-of-scope: deleting or moving many files, touching .git "
        "internals or secret files, ARBITRARY network calls to unknown hosts (curl / wget to a URL, "
        "piping a remote script into a shell), writing OUTSIDE the workspace, or a command whose effect "
        "you cannot determine. When UNSURE, DENY - denying is always safe here.\n"
        "You may read files to inform your decision, but do NOT change anything.\n\n"
        "Output EXACTLY one line: 'APPROVE: <short reason>' OR 'DENY: <short reason>'.")
