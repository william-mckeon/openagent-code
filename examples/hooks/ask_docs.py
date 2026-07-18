"""
PreToolUse example hook — gate docs/ writes with an ASK, ACROSS TOOLS.

Demonstrates two things static deny rules can't do: (1) a policy about the EFFECT, not the tool name, and
(2) the 'ask' verdict (specs/0022) — an at-risk write is ESCALATED to the approver (the guardian when
headless, or a [y/N] prompt) instead of hard-denied, so a legitimate docs/ change can still go through on
approval rather than dead-ending the agent. (This started life as deny_docs.py returning 'deny'; a live run
then looped propose -> approve -> hook-deny on a docs/ write — "ask, don't deny" is the fix.)

It escalates an edit/write/apply_patch/delete whose target is under docs/, AND a run_command that redirects
INTO docs/ (the classic "route around the deny" move). It reads the runner's uniform `paths` field (every
file the call touches, ACROSS tools — including the targets parsed out of an apply_patch body), so
apply_patch can't slip the protection.

Protocol: reads the call context as JSON on stdin, prints a JSON verdict on stdout (or nothing = no
opinion). Fail-open by the runner: if this script errors, the runner ignores it.
"""
import json
import sys

try:
    p = json.load(sys.stdin)
except Exception:
    sys.exit(0)   # can't read -> no opinion (the runner fails open)

tool = p.get("tool", "")
paths = [str(x).replace("\\", "/").lower() for x in (p.get("paths") or [])]
cmd = str((p.get("args") or {}).get("command") or "").replace("\\", "/").lower()

gated = False
# delete INCLUDED: "gated" means writes AND deletes under docs/ — the ride-5 log showed a bulk delete of
# docs/*.md slip a hook that only guarded write/edit/apply_patch.
if tool in ("write_file", "edit_file", "apply_patch", "delete_file") and any("docs/" in x for x in paths):
    gated = True
elif tool == "run_command" and ">docs/" in cmd.replace(" ", ""):   # a redirect writing into docs/
    gated = True

if gated:
    # 'ask' (not 'deny'): the engine ESCALATES this to the approver instead of hard-blocking, so an approved
    # change can proceed. Deny still wins if another hook or a deny rule blocks the same call.
    print(json.dumps({"decision": "ask", "message": "a write under docs/ - confirm before applying"}))
