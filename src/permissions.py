"""
src/permissions.py

The permission engine — Claude Code's model, ported (Phase 4 #6).

Every tool call is gated by `decide(tool, args, ctx)` BEFORE it runs (the gate
lives at dispatch in src/agent.py, so the decision is captured once and bound to
the right trajectory — important for subagents). The engine combines three things:

  * a MODE       — default / acceptEdits / plan / bypass (how much to auto-approve)
  * RULES        — allow / ask / deny, matched by `tool_name(pattern)`
  * a FENCE      — confine file tools to the agent's cwd + CODE_ADD_DIRS

Precedence (first match wins), per specs/0001-permissions.md:
  1. deny rule        -> BLOCK   (wins over everything, including bypass)
  2. outside fence    -> BLOCK   (file tools only)
  3. read-only tool   -> ALLOW
  4. plan mode        -> BLOCK   (mutating tools are read-only-mode forbidden)
  5. ask rule         -> PROMPT if interactive, else BLOCK
  6. allow rule       -> ALLOW
  7. mode baseline    -> bypass=ALLOW; acceptEdits=ALLOW for write/edit else prompt/block;
                         default=prompt if interactive else BLOCK

Headless-safe by construction: deny wins and ask/default BLOCK (never allow) when no
human is present, so an unattended run can only ever be more restrictive, not less.
"""
import os
import re
import sys

from . import config
from . import execpolicy


def _shell_name():
    """The shell run_command actually uses (tools.run_command): PowerShell on Windows, else bash."""
    return "powershell" if os.name == "nt" else "bash"


def _flush_stdin():
    """Drain any typed-ahead input so a permission prompt raised mid-turn doesn't swallow the user's NEXT
    query as its y/N answer (seen live: 'allow delete_file...? [y/N] what project is this?y' — the query
    got eaten and the non-'y' string denied the op)."""
    try:
        if os.name == "nt":
            import msvcrt
            while msvcrt.kbhit():
                msvcrt.getwch()
        else:
            import termios
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:  # noqa: BLE001 - best-effort; a flush failure must never break the prompt
        pass

# Tools that change files or run commands. Everything else is read-only for gating.
# apply_patch mutates too, but ONE envelope carries MANY paths, so decide()'s single-path fence can't
# cover it here — patch.py re-gates each op through decide() with its single-file equivalent. Listing it
# here still makes the OUTER gate treat it as a mutation (blocked in plan mode / headless default).
MUTATING = {"write_file", "edit_file", "delete_file", "run_command", "apply_patch"}
# Tools whose target is a filesystem path (fence-checked, glob-matched in rules).
PATH_TOOLS = {"read_file", "write_file", "edit_file", "delete_file", "grep", "glob", "tree"}


class Decision:
    """The outcome of a permission check — logged to the trajectory verbatim."""
    def __init__(self, allowed, tool, target, action, reason, rule=None, mode=None):
        self.allowed = allowed      # bool — did the call pass the gate?
        self.tool = tool            # tool name
        self.target = target        # the gated target (path or command), for the record
        self.action = action        # "allow" | "deny" | "ask"
        self.reason = reason        # human-readable why (which step decided)
        self.rule = rule            # the matched rule string, if any
        self.mode = mode            # the active mode


class _Target:
    __slots__ = ("kind", "raw", "rel", "abs")

    def __init__(self, kind, raw, rel=None, abspath=None):
        self.kind = kind            # "path" | "command" | "other" | "none"
        self.raw = raw              # the literal arg (command string / url / query)
        self.rel = rel              # workspace-relative path (for path glob matching)
        self.abs = abspath          # resolved absolute path (for the fence)


class Permissions:
    def __init__(self, mode, rules, extra_roots):
        self.mode = mode
        self.deny = list((rules or {}).get("deny") or [])
        self.ask = list((rules or {}).get("ask") or [])
        self.allow = list((rules or {}).get("allow") or [])
        self.extra_roots = list(extra_roots or [])

    @classmethod
    def from_config(cls, mode_override=None, extra_dirs=None):
        """Build from CODE_* config, with optional CLI overrides (--mode / --add-dir)."""
        mode = mode_override or config.resolved_permission_mode()
        roots = config.permission_extra_roots()
        for d in (extra_dirs or []):
            if d:
                roots.append(os.path.realpath(d))
        return cls(mode, config.load_permission_rules(), roots)

    # -- the gate -------------------------------------------------------------

    def decide(self, tool, args, ctx):
        t = self._target(tool, args, ctx)
        # run_command gated on the PARSED command (execpolicy, Phase 16): deny/ask/allow rules match ANY
        # segment (the `rm` inside `cd x && rm y`) and a wholly read-only command is allowed like a read
        # tool. OFF (default) -> decide() never consults execpolicy and the prefix path below is unchanged.
        if tool == "run_command" and config.EXECPOLICY:
            return self._decide_command(t, ctx)
        mutating = tool in MUTATING

        def D(allowed, action, reason, rule=None):
            return Decision(allowed, tool, (t.rel if t.kind == "path" else t.raw),
                            action, reason, rule, self.mode)

        # 1. deny — overrides everything, even bypass.
        r = self._match(self.deny, tool, t)
        if r:
            return D(False, "deny", f"deny rule {r!r}", r)

        # 2. fence — file tools may not resolve outside the workspace + CODE_ADD_DIRS.
        if t.kind == "path" and not self._within_roots(t.abs, ctx.cwd):
            return D(False, "deny", "path is outside your allowed directories — call request_dir "
                                    "to ask the user for access, or have them restart with --add-dir")

        # 3. read-only tools are allowed once past deny + fence.
        if not mutating:
            return D(True, "allow", "read-only tool")

        # 4. plan mode is read-only — no mutating tools at all.
        if self.mode == "plan":
            return D(False, "deny", "plan mode is read-only")

        # 5. ask — guardian reviews (fail-closed), else prompt a human, else block.
        r = self._match(self.ask, tool, t)
        if r:
            g = self._guardian(tool, t, f"ask rule {r!r}", ctx)
            if g is not None:
                return D(g, "ask", f"ask rule {r!r} -> guardian {'approved' if g else 'denied'}", r)
            if getattr(ctx, "interactive", False):
                ok = self._prompt(tool, t)
                return D(ok, "ask", f"ask rule {r!r} -> {'allowed' if ok else 'denied'} by user", r)
            return D(False, "ask", f"ask rule {r!r}, but no human is present to confirm", r)

        # 6. allow.
        r = self._match(self.allow, tool, t)
        if r:
            return D(True, "allow", f"allow rule {r!r}", r)

        # 7. mode baseline.
        if self.mode == "bypass":
            return D(True, "allow", "bypass mode")
        if self.mode == "acceptEdits" and tool in ("write_file", "edit_file", "apply_patch"):
            # apply_patch passes the OUTER gate here; patch.py still fences each op and blocks a
            # Delete/Move op in acceptEdits (delete_file isn't auto-approved), same as delete_file itself.
            return D(True, "allow", "acceptEdits mode")
        # default mode (and acceptEdits for run_command): guardian reviews, else prompt or block.
        g = self._guardian(tool, t, f"{self.mode} mode", ctx)
        if g is not None:
            return D(g, "ask", f"{self.mode} mode -> guardian {'approved' if g else 'denied'}")
        if getattr(ctx, "interactive", False):
            ok = self._prompt(tool, t)
            return D(ok, "ask", f"{self.mode} mode -> {'allowed' if ok else 'denied'} by user")
        return D(False, "deny", f"{self.mode} mode needs approval, but no human is present")

    # -- helpers --------------------------------------------------------------

    def _target(self, tool, args, ctx):
        if tool in ("read_file", "write_file", "edit_file", "delete_file"):
            raw = args.get("path", "")
            ap = _resolve(ctx.cwd, raw)
            return _Target("path", raw, _rel(ap, ctx.cwd), ap)
        if tool in ("grep", "glob", "tree"):
            raw = args.get("path", ".")
            ap = _resolve(ctx.cwd, raw)
            return _Target("path", raw, _rel(ap, ctx.cwd), ap)
        if tool == "run_command":
            return _Target("command", args.get("command", ""))
        if tool == "web_fetch":
            return _Target("other", args.get("url", ""))
        if tool == "web_search":
            return _Target("other", args.get("query", ""))
        return _Target("none", "")

    def _within_roots(self, abs_path, cwd):
        roots = [os.path.realpath(cwd)] + self.extra_roots
        return any(abs_path == r or abs_path.startswith(r + os.sep) for r in roots)

    def _match(self, rules, tool, target):
        """Return the first rule string that matches this tool+target, else None."""
        for rule in rules:
            rtool, pat = _parse_rule(rule)
            if rtool not in (tool, "*"):
                continue
            if target.kind == "command":
                if _match_command(pat, target.raw):
                    return rule
            elif target.kind == "path":
                if _match_glob(pat, target.rel):
                    return rule
            else:  # other / none
                if pat == "*" or pat == target.raw:
                    return rule
        return None

    def _prompt(self, tool, target):
        shown = target.rel if target.kind == "path" else (target.raw or tool)
        _flush_stdin()   # so a query typed while the turn was working isn't consumed as the y/N answer
        try:
            ans = input(f"  [permission] allow {tool} on {shown!r}? [y/N] ").strip().lower()
        except EOFError:
            return False
        return ans == "y"

    def _guardian(self, tool, t, reason, ctx):
        """The guardian's verdict for an ask-tier decision, or None to fall through to the human prompt /
        headless block (unchanged). ON (top level only) -> a captured fail-closed reviewer decides. OFF
        -> None, so every ask/prompt site below is byte-identical to today. Governs the ASK tier ONLY."""
        if config.GUARDIAN and getattr(ctx, "depth", 0) == 0:
            from . import guardian   # lazy: only needed when the flag is on; keeps permissions low-level
            shown = t.rel if getattr(t, "kind", None) == "path" else (t.raw or tool)
            return guardian.review(tool, shown, reason, ctx)
        return None

    def _decide_command(self, t, ctx):
        """Gate run_command on execpolicy's parsed segments: deny/ask/allow rules match ANY segment (so
        run_command(rm:*) catches `cd x && rm y`), and a wholly READ-ONLY command (ls, git status) is
        allowed like a read tool — even in plan / default mode. A `dangerous` command is NOT relaxed; it
        stays on the mutating path (its elevation to a fail-closed review is the guardian's job, 0019)."""
        a = execpolicy.assess(t.raw, _shell_name())
        candidates = [t.raw] + [s for s, _ in a.segments]   # the whole line too, so a plain prefix still fires

        def D(allowed, action, reason, rule=None):
            return Decision(allowed, "run_command", t.raw, action, reason, rule, self.mode)

        for seg in candidates:                                   # deny — matches ANY segment
            r = self._match_command_rules(self.deny, seg)
            if r:
                return D(False, "deny", f"deny rule {r!r}", r)
        if a.worst == execpolicy.READ_ONLY:                      # a read-only command is safe anywhere
            return D(True, "allow", "read-only command (execpolicy)")
        if self.mode == "plan":
            return D(False, "deny", "plan mode is read-only")
        for seg in candidates:                                   # ask — matches ANY segment
            r = self._match_command_rules(self.ask, seg)
            if r:
                g = self._guardian("run_command", t, f"ask rule {r!r}", ctx)
                if g is not None:
                    return D(g, "ask", f"ask rule {r!r} -> guardian {'approved' if g else 'denied'}", r)
                if getattr(ctx, "interactive", False):
                    ok = self._prompt("run_command", t)
                    return D(ok, "ask", f"ask rule {r!r} -> {'allowed' if ok else 'denied'} by user", r)
                return D(False, "ask", f"ask rule {r!r}, but no human is present to confirm", r)
        for seg in candidates:                                   # allow — matches ANY segment
            r = self._match_command_rules(self.allow, seg)
            if r:
                return D(True, "allow", f"allow rule {r!r}", r)
        if self.mode == "bypass":
            return D(True, "allow", "bypass mode")
        g = self._guardian("run_command", t, f"{self.mode} mode", ctx)
        if g is not None:
            return D(g, "ask", f"{self.mode} mode -> guardian {'approved' if g else 'denied'}")
        if getattr(ctx, "interactive", False):
            ok = self._prompt("run_command", t)
            return D(ok, "ask", f"{self.mode} mode -> {'allowed' if ok else 'denied'} by user")
        return D(False, "deny", f"{self.mode} mode needs approval, but no human is present")

    def _match_command_rules(self, rules, cmd):
        """Match run_command deny/ask/allow rules against a SINGLE command string (one parsed segment)."""
        for rule in rules:
            rtool, pat = _parse_rule(rule)
            if rtool in ("run_command", "*") and _match_command(pat, cmd):
                return rule
        return None

    def decide_move(self, old_path, new_path, ctx):
        """Gate an apply_patch Move (rename). A rename is RECOVERABLE (content preserved), so its mode
        baseline follows edit_file — auto-allowed in acceptEdits — rather than delete_file's prompt, which
        was disrupting a routine multi-file rename. But the DENY rules and the fence still apply to BOTH
        endpoints, so a move can't bypass delete_file(.env) / edit_file(.git/**) or escape the workspace."""
        def D(allowed, action, reason, rule=None, target=""):
            return Decision(allowed, "apply_patch", target, action, reason, rule, self.mode)
        # deny + fence on both endpoints: check the delete/edit rules on the OLD path (it's removed) and
        # the write/edit rules on the NEW path (it's created), so .env / .git denies fire either way.
        for tool, p in (("delete_file", old_path), ("edit_file", old_path),
                        ("write_file", new_path), ("edit_file", new_path)):
            t = self._target(tool, {"path": p}, ctx)
            r = self._match(self.deny, tool, t)
            if r:
                return D(False, "deny", f"deny rule {r!r}", r, t.rel)
            if not self._within_roots(t.abs, ctx.cwd):
                return D(False, "deny", "a moved path is outside your allowed directories", target=t.rel)
        if self.mode == "plan":
            return D(False, "deny", "plan mode is read-only", target=old_path)
        if self.mode in ("bypass", "acceptEdits"):
            return D(True, "allow", f"{self.mode} mode (a rename is edit-level)", target=old_path)
        _wt = self._target("write_file", {"path": new_path}, ctx)
        g = self._guardian("apply_patch move", _wt, "a Move in default mode", ctx)
        if g is not None:
            return D(g, "ask", f"default mode -> guardian {'approved' if g else 'denied'}", target=old_path)
        if getattr(ctx, "interactive", False):
            ok = self._prompt("apply_patch move", _wt)
            return D(ok, "ask", f"default mode -> {'allowed' if ok else 'denied'} by user", target=old_path)
        return D(False, "deny", "default mode needs approval, but no human is present", target=old_path)


# -- module-level matchers (pure functions, easy to unit-test) ----------------

def _resolve(cwd, path):
    return os.path.realpath(path if os.path.isabs(path) else os.path.join(cwd, path))


def _rel(abs_path, cwd):
    try:
        return os.path.relpath(abs_path, cwd).replace(os.sep, "/")
    except ValueError:
        return abs_path.replace(os.sep, "/")


def _parse_rule(rule):
    """'run_command(rm:*)' -> ('run_command', 'rm:*'); 'web_fetch' -> ('web_fetch', '*')."""
    rule = rule.strip()
    if "(" in rule and rule.endswith(")"):
        name, pat = rule.split("(", 1)
        return name.strip(), pat[:-1].strip()
    return rule, "*"


def _match_command(pat, cmd):
    cmd = (cmd or "").strip()
    if pat == "*":
        return True
    if pat.endswith(":*"):
        prefix = pat[:-2].strip()
        return cmd == prefix or cmd.startswith(prefix + " ")
    return cmd == pat


def _match_glob(pat, path):
    return re.fullmatch(_glob_to_regex(pat.replace("\\", "/")), (path or "").replace("\\", "/")) is not None


def _glob_to_regex(pat):
    out, i = "", 0
    while i < len(pat):
        c = pat[i]
        if c == "*":
            if pat[i:i + 2] == "**":
                out += ".*"
                i += 2
            else:
                out += "[^/]*"
                i += 1
        elif c == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(c)
            i += 1
    return out
