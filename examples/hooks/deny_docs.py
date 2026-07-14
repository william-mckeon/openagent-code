"""
PreToolUse example hook — write-protect docs/ ACROSS TOOLS.

Demonstrates the thing static deny rules can't do: a policy about the EFFECT, not the tool name. It
refuses an edit/write/apply_patch whose target is under docs/, AND a run_command that redirects INTO
docs/ (the classic "route around the deny" move). One effect-based hook closes the hole a per-tool deny
list leaves open.

It reads the runner's uniform `paths` field (every file the call touches, ACROSS tools — including the
targets parsed out of an apply_patch body), so apply_patch can't slip the protection.

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

blocked = False
if tool in ("write_file", "edit_file", "apply_patch") and any("docs/" in x for x in paths):
    blocked = True
elif tool == "run_command" and ">docs/" in cmd.replace(" ", ""):   # a redirect writing into docs/
    blocked = True

if blocked:
    print(json.dumps({"decision": "deny", "message": "docs/ is write-protected by a PreToolUse hook"}))
