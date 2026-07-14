"""
PreToolUse example hook — keep secrets OUT of the trajectory / corpus.

The review ride read CentPilot's raw .env (secrets and all) into the trajectory, which is the accepted
"raw-readable" tradeoff. If you'd rather secrets never enter the corpus, this hook denies READING a
secret-looking file (.env, *.key, *.pem, id_rsa, ...) via read_file OR a shell cat/type. It's a policy
you switch on per project by adding it to a PreToolUse entry in hooks.json.

Note: this only stops the AGENT from reading them through its tools — it is not a filesystem ACL. And
because hooks are fail-open, it is a corpus-hygiene tool, not a hard security boundary (that stays the
deny rules + fence). Add the paths you consider sensitive to SECRET_HINTS.
"""
import json
import sys

SECRET_HINTS = (".key", ".pem", "id_rsa", "secrets", "credentials")


def _is_secret(text):
    """True if any whitespace/redirect-separated token looks like a secret file: a .env / .env.local
    (but NOT .env.example, a sample), or one of the SECRET_HINTS. Basename-aware so 'cat .env' and
    'config/.env' both match while 'environment' does not."""
    t = text.replace("\\", "/").lower()
    for tok in t.replace(">", " ").replace("<", " ").split():
        base = tok.rsplit("/", 1)[-1]
        if (base == ".env" or base.startswith(".env.")) and base != ".env.example":
            return True
        if any(h in tok for h in SECRET_HINTS):
            return True
    return False


try:
    p = json.load(sys.stdin)
except Exception:
    sys.exit(0)

tool = p.get("tool", "")
paths = [str(x) for x in (p.get("paths") or [])]
cmd = str((p.get("args") or {}).get("command") or "")

hit = False
if tool == "read_file" and any(_is_secret(x) for x in paths):
    hit = True
elif tool == "run_command":
    low = cmd.lower()
    if any(w in low for w in ("cat ", "type ", "get-content", "more ")) and _is_secret(cmd):
        hit = True

if hit:
    print(json.dumps({"decision": "deny", "message": "reading secret files is blocked by a PreToolUse hook"}))
