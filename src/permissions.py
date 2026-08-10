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


# Self-preservation (specs/0050, hardened specs/0071): a kill verb AND a `python` token in the SAME statement,
# in EITHER order. The agent runs as `python -m src`, so any NAME-based python kill takes it down. A STATEMENT
# separator (; & newline && ||) ends a statement, so `Stop-Process -Name foo; python x` is two statements and
# NOT flagged — but a PIPE stays within one statement, so the idiomatic PowerShell form
# `Get-Process python | Stop-Process` IS flagged (the live bypass: the old regex required the verb BEFORE
# python and stopped matching at `|`). kill-by-PID carries no `python` token and is intentionally not matched.
_KILL_VERB = re.compile(r"\b(?:stop-process|spps|taskkill|pkill|killall|kill)\b", re.I)
_PYTHON_TOK = re.compile(r"\bpython[3w]?\b", re.I)   # python / python3 / python3.x / pythonw / python.exe
_STMT_SEP = re.compile(r"&&|\|\||[;&\n]")            # NOT a single '|': a pipe is one statement


def _is_self_kill(command):
    """True when `command` has a name-based process kill that would terminate the agent's own interpreter
    (Stop-Process -Name python / `Get-Process python | Stop-Process` / taskkill /IM python.exe / pkill python).
    Checked per STATEMENT so a kill of some OTHER process next to an unrelated `python` call isn't flagged."""
    return any(_KILL_VERB.search(seg) and _PYTHON_TOK.search(seg)
               for seg in _STMT_SEP.split(command or ""))


def _canonical_tool(tool):
    """specs/0071: resolve a tool alias (e.g. print_tree -> tree) to its REAL name so the permission gate
    classifies and fences it correctly. Registry.run resolves the alias AFTER the gate, so without this a
    read-only alias (`print_tree`) was treated as an unknown tool and got a fence-free read-only ALLOW —
    letting it enumerate any directory on the host, outside the workspace fence. Lazy import avoids the
    tools<->permissions module cycle."""
    from .tools import _TOOL_ALIASES
    return _TOOL_ALIASES.get(tool, tool)


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
# `pursue` (Phase 20) is MUTATING even though it only registers a goal: it hands the harness a
# MODEL-PROPOSED command to run repeatedly, unattended. Omit it here and decide() would fall to the
# "read-only tool" allow at step 3 and the bar would face NO gate at all.
MUTATING = {"write_file", "edit_file", "delete_file", "run_command", "apply_patch", "pursue"}

# Delete/remove verbs — a run_command is DESTRUCTIVE (counts toward the mass-destruction cap) if it's
# dangerous per execpolicy OR removes files. A routine edit / install / build is NOT destructive.
_DESTRUCTIVE_CMD = re.compile(r"\b(rm|rmdir|rd|del|erase|unlink|remove-item|ri|shred)\b", re.IGNORECASE)


def _is_destructive(tool, shown):
    """Is this ask-tier op DESTRUCTIVE — an irreversible delete / move / dangerous command that the
    per-turn mass-destruction cap should count? delete_file and an apply_patch Move always are; an
    apply_patch whose summary deletes/moves is; a run_command that's dangerous or removes files is. A
    plain edit / write / install / read is NOT (the cap must not throttle ordinary coding)."""
    if tool in ("delete_file", "apply_patch move"):
        return True
    s = (shown or "").lower()
    if tool == "apply_patch":
        return "delete " in s or "move " in s
    if tool == "run_command":
        try:
            if execpolicy.assess(shown).worst == execpolicy.DANGEROUS:
                return True
        except Exception:  # noqa: BLE001 - classification must never break the gate
            pass
        return bool(_DESTRUCTIVE_CMD.search(s))
    return False
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
        # READ-only reference roots (specs/0035), populated at runtime by a trusted-user-dir grant (cli.py
        # fix A / request_dir fix B, both under CODE_TRUST_USER_DIRS). Unlike extra_roots these widen READS
        # ONLY: _within_roots consults them just for non-mutating path tools, so a write/edit/delete/rename
        # can never reach a read-granted dir. Empty until a grant lands -> byte-identical to today.
        self.read_only_roots = []

    @classmethod
    def from_config(cls, mode_override=None, extra_dirs=None):
        """Build from CODE_* config, with optional CLI overrides (--mode / --add-dir)."""
        mode = mode_override or config.resolved_permission_mode()
        roots = config.permission_extra_roots()
        for d in (extra_dirs or []):
            if d:
                roots.append(os.path.realpath(d))
        return cls(mode, config.load_permission_rules(), roots)

    def readonly_view(self):
        """A READ-ONLY projection of this Permissions for a PARALLEL fan-out child (specs/0039): a FRESH
        object at mode 'plan' (the ladder denies every MUTATING tool at the plan step) carrying this
        object's deny/ask/allow rules + the fence (extra_roots + read_only_roots). So a concurrent child can
        read its scope but can never write/edit/delete/run — parallel children can't race the filesystem.
        This object is left untouched (the SERIAL fan-out path uses it directly and must stay byte-identical)."""
        p = Permissions("plan", None, list(self.extra_roots))
        p.deny, p.ask, p.allow = list(self.deny), list(self.ask), list(self.allow)
        p.read_only_roots = list(self.read_only_roots)
        return p

    # -- the gate -------------------------------------------------------------

    def decide(self, tool, args, ctx):
        tool = _canonical_tool(tool)   # specs/0071: alias -> real name BEFORE the fence (print_tree -> tree)
        t = self._target(tool, args, ctx)
        # PreToolUse hooks (Phase 15): an explicit hook DENY hard-blocks ANY tool BEFORE the engine runs,
        # so a policy can be about the EFFECT (a path / content pattern), not the tool NAME — this is what
        # closes "deny is only tool-scoped." Fail-open + flag-off -> None, a no-op (byte-identical).
        h = self._hooks_pretool(tool, t, args, ctx)
        if h is not None and getattr(h, "decision", "deny") in ("deny", "block"):
            return Decision(False, tool, (t.rel if t.kind == "path" else t.raw), "deny",
                            f"PreToolUse hook: {h.message or 'blocked'}", None, self.mode)
        # A hook ASK (specs/0022) is NOT acted on here: it's captured and applied only AFTER the engine's
        # normal decision resolves to an allow, so it can only DOWNGRADE an allow to an ask — never override
        # a deny rule / fence / plan-mode / propose-investigate block that runs first below.
        hook_ask = h if (h is not None and getattr(h, "decision", None) == "ask") else None
        dec = self._decide_core(tool, t, ctx)
        # Escalation net (specs/0022): a hook-ask, or an off-plan DESTRUCTIVE op under an approved manifest,
        # turns an ALLOW into an ASK (guardian headless / prompt interactive / block). Only ever runs on an
        # allow (never a deny/block), and only when a trigger is live -> flag-off + no hook-ask leaves the
        # decision untouched (byte-identical).
        if dec.allowed and dec.action == "allow" and (hook_ask is not None or config.PROPOSE):
            esc = self._escalate_allow(tool, t, ctx, hook_ask)
            if esc is not None:
                return esc
        return dec

    def _decide_core(self, tool, t, ctx):
        """The permission ladder itself (deny -> fence -> read-only -> propose -> plan -> ask -> allow ->
        baseline). Split out of decide() so the PreToolUse hook-ask / off-plan escalation can post-process
        its result uniformly, whether it came from here or the execpolicy command path."""
        # Self-preservation (specs/0050): a name-based process-kill that would catch the agent's OWN
        # interpreter (Stop-Process -Name python / taskkill /IM python* / pkill / killall python) is
        # HARD-DENIED in EVERY mode — including bypass — because killing yourself is never a legitimate agent
        # action and it aborts the run mid-task (a live run self-terminated this way stopping a test server).
        # Runs before the mode ladder + execpolicy routing, so it wins like a deny rule. Kill-by-PID is
        # untouched. Flag-gated -> OFF (default) never runs -> byte-identical.
        if config.GUARD_SELF_KILL and tool == "run_command" and _is_self_kill(t.raw):
            return Decision(False, "run_command", t.raw, "deny",
                            "self-preservation: refusing a name-based kill that would terminate the agent's own process",
                            None, self.mode)
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

        # 2. fence — file tools may not resolve outside the workspace + CODE_ADD_DIRS. A READ tool (specs/0035)
        # additionally reaches a trusted-user-dir READ grant (read_only_roots); a MUTATING path tool never
        # does, so a read-granted dir can be read but not written/edited/deleted. read_only_roots is empty
        # until such a grant lands -> byte-identical when the feature is off.
        if t.kind == "path" and not self._within_roots(t.abs, ctx.cwd, include_read_only=not mutating):
            return D(False, "deny", "path is outside your allowed directories — call request_dir "
                                    "to ask the user for access, or have them restart with --add-dir")

        # 3. read-only tools are allowed once past deny + fence.
        if not mutating:
            return D(True, "allow", "read-only tool")

        # 3b. propose mode (specs/0022): read-only until the manifest is approved, then allow exactly the
        # approved plan. UNDER deny + fence (an approved edit still can't touch .env / escape the fence).
        if config.PROPOSE and self.mode == "propose":
            pd = self._propose_gate(tool, t, ctx, D)
            if pd is not None:
                return pd

        # 4. plan mode is read-only — no mutating tools at all.
        if self.mode == "plan":
            return D(False, "deny", "plan mode is read-only")

        # 5. ask — guardian reviews (fail-closed), else prompt a human, else block.
        r = self._match(self.ask, tool, t)
        if r:
            a = self._ask_approver(tool, t, f"ask rule {r!r}", ctx)
            if a is not None:
                return D(a[0], "ask", f"ask rule {r!r} -> {a[1]}", r)
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
        # default mode (and acceptEdits for run_command): auto-approver (hook/guardian), else prompt/block.
        a = self._ask_approver(tool, t, f"{self.mode} mode", ctx)
        if a is not None:
            return D(a[0], "ask", f"{self.mode} mode -> {a[1]}")
        if getattr(ctx, "interactive", False):
            ok = self._prompt(tool, t)
            return D(ok, "ask", f"{self.mode} mode -> {'allowed' if ok else 'denied'} by user")
        return D(False, "deny", f"{self.mode} mode needs approval, but no human is present")

    # -- propose mode (specs/0022) --------------------------------------------

    def norm_path(self, raw, cwd):
        """The canonical key for a path target — workspace-relative, forward-slashed, case-normalized: the
        SAME form decide() matches an approved manifest against. propose_changes FILLS ctx.approved_paths
        through this and decide()/decide_move() TEST it through this, so the two can never disagree (an
        edit made via an absolute path or different Windows casing still matches its approved entry)."""
        return os.path.normcase(_rel(_resolve(cwd, raw), cwd))

    def hard_block(self, tool, args, ctx):
        """The reason this op would be HARD-blocked no matter what — a deny rule, the workspace fence, or a
        PreToolUse DENY hook — the rules an approved manifest can NEVER override (specs/0022). Returns the
        reason string, or None if the op would clear the hard rules. propose_changes calls this per manifest
        item so it refuses a plan that could be approved but never EXECUTED (a live run looped
        propose -> approve -> hook-deny on a docs/ write). Does NOT consult the propose phase, ask rules, or
        the mode baseline — only the immovable rules."""
        t = self._target(tool, args, ctx)
        h = self._hooks_pretool(tool, t, args, ctx)
        if h is not None and getattr(h, "decision", "deny") in ("deny", "block"):
            return f"a PreToolUse hook blocks it: {h.message or 'blocked'}"
        r = self._match(self.deny, tool, t)
        if r:
            return f"a deny rule blocks it: {r}"
        if t.kind == "path" and not self._within_roots(t.abs, ctx.cwd):
            return "it is outside your workspace fence"
        return None

    def _on_manifest(self, ctx, t):
        approved = getattr(ctx, "approved_paths", None)
        return bool(approved) and os.path.normcase(t.rel or "") in approved

    def _on_manifest_move(self, ctx, old_path, new_path):
        approved = getattr(ctx, "approved_paths", None)
        if not approved:
            return False
        return (self.norm_path(old_path, ctx.cwd) in approved
                and self.norm_path(new_path, ctx.cwd) in approved)

    def _propose_ro_msg(self, ctx):
        """specs/0072: propose-mode read-only deny text, DEPTH-AWARE. A subagent (depth>0) inherits propose
        mode (subagent.py) but propose_changes / manifest approval are top-level-only, so a child can NEITHER
        mutate NOR approve — the plain 'read-only until the manifest is approved' text is unsatisfiable for it
        and drove dozens of retry steps (seen live). Tell a child the truth: stop and report up."""
        if getattr(ctx, "depth", 0) > 0:
            return ("propose mode is read-only and a SUBAGENT cannot approve a change-list — do NOT retry this "
                    "edit/command. Finish investigating and RETURN your findings and proposed plan to the "
                    "top-level agent, which will propose and apply the changes.")
        return "propose mode is read-only until the manifest is approved"

    def _propose_gate(self, tool, t, ctx, D):
        """Propose mode's read-only-until-approved gate, consulted for a MUTATING tool (called after the
        read-only allow). Investigate phase -> DENY (nothing is edited before the user approves). Approved
        phase -> ALLOW an op that's on the approved manifest (apply_patch is allowed at the envelope and
        re-gated per file by patch.py); an OFF-manifest op returns None to fall through to the normal ask
        baseline. Only called when config.PROPOSE and self.mode == 'propose'."""
        if getattr(ctx, "propose_phase", "investigate") != "approved":
            # specs/0048: after a manifest was approved this session, opt-in relaxations let a mutating op
            # fall to the ASK ladder instead of hard-deny (deny-rules + the fence already ran above). A
            # COMMAND rides RUN_AFTER_APPROVAL (c); an off-manifest FILE mutation rides EXTEND_AFTER_APPROVAL
            # (b). approved_paths is still reset each turn, so nothing is AUTO-allowed here. Off -> the deny.
            if getattr(ctx, "propose_graduated", False):
                if t.kind != "path" and config.PROPOSE_RUN_AFTER_APPROVAL:
                    return None
                if t.kind == "path" and config.PROPOSE_EXTEND_AFTER_APPROVAL:
                    return None
                if config.PROPOSE_AUTOPLAN:   # specs/0052: an autoplan-unlocked session relaxes every op
                    return None
            # specs/0052: turn the read-only dead-end into an approvable one-item plan (a yes graduates +
            # allows). Returns None when autoplan can't apply -> the byte-identical deny below stands.
            unlocked = self._propose_autoplan(tool, t, ctx, D)
            if unlocked is not None:
                return unlocked
            return D(False, "deny", self._propose_ro_msg(ctx))
        if tool == "apply_patch":
            return D(True, "allow", "propose mode: approved (each patch op is re-gated per file)")
        if t.kind == "path" and self._on_manifest(ctx, t):
            return D(True, "allow", "on the approved manifest")
        return None

    def _propose_autoplan(self, tool, t, ctx, D):
        """specs/0052: turn the propose investigate-phase read-only DENY into an approvable one-item plan.
        The first graduation can otherwise ONLY come from the model calling propose_changes; a weak model may
        never do so, dead-ending the user with nothing to approve. When CODE_PROPOSE_AUTOPLAN is on AND a
        human is present, prompt to approve THIS op and UNLOCK the session — a yes sets propose_graduated
        (so the gate relaxations apply to every further op) and ALLOWS this op; a no denies it. Returns None
        when autoplan cannot apply (flag OFF / already graduated / headless / no ask channel) so the caller's
        normal read-only deny stands. OFF (default) -> returns None immediately -> byte-identical. The op has
        already cleared the deny-rules + fence (steps 1-2) before any propose gate runs, so an autoplan allow
        can never bypass a hard rule."""
        if not config.PROPOSE_AUTOPLAN or getattr(ctx, "propose_graduated", False):
            return None
        ask = getattr(ctx, "ask", None)
        if ask is None or not getattr(ctx, "interactive", False):
            return None
        shown = t.rel if getattr(t, "kind", None) == "path" else (t.raw or tool)
        try:
            ans = ask(f"  [propose] approve this action and unlock the session? {tool}: {shown} [y/N] ")
        except Exception:  # noqa: BLE001 - a broken ask channel leaves the read-only deny in place
            return None
        if str(ans).strip().lower() in ("y", "yes"):
            try:
                ctx.propose_graduated = True
            except Exception:  # noqa: BLE001 - a ctx that can't record graduation can't be unlocked
                return None
            return D(True, "allow", "propose auto-plan: approved by user (session unlocked)")
        return D(False, "deny", "propose auto-plan: declined by user (still read-only)")

    def _offplan(self, tool, t, ctx):
        """True if a manifest was APPROVED this turn AND this mutating op is NOT on it — the graduated
        off-plan net's trigger. Live only once a plan is approved (propose mode, or auto-propose in another
        mode), so a normal run with no manifest never reaches it. apply_patch is judged per file (its ops
        re-enter decide()), so the envelope itself is never off-plan."""
        if getattr(ctx, "propose_phase", None) != "approved":
            return False
        if tool == "apply_patch":
            return False
        if t.kind == "path":
            return not self._on_manifest(ctx, t)
        return True   # a command / pursue is never ON a file manifest -> off-plan

    def _escalate(self, tool, t, reason, ctx, D):
        """Route an allow that a hook-ask / off-plan check wants to second-guess through the SAME ask ladder
        as an ask rule: the mass-destruction cap + PermissionRequest hook + guardian (headless), else the
        human prompt (interactive), else a headless block. Only ever downgrades an allow to an ask."""
        a = self._ask_approver(tool, t, reason, ctx)
        if a is not None:
            return D(a[0], "ask", f"{reason} -> {a[1]}")
        if getattr(ctx, "interactive", False):
            ok = self._prompt(tool, t)
            return D(ok, "ask", f"{reason} -> {'allowed' if ok else 'denied'} by user")
        return D(False, "ask", f"{reason}, but no human is present to confirm")

    def _escalate_allow(self, tool, t, ctx, hook_ask):
        """Post-process an ALLOW (specs/0022). (a) a PreToolUse hook that asked to confirm this call turns
        ANY allow into an ask; (b) an off-plan DESTRUCTIVE op under an approved manifest turns its allow
        into an ask (a low-risk off-plan op keeps its original, already-logged allow — only irreversible
        deviations are second-guessed). Returns the new ask Decision, or None to leave the allow as-is."""
        def D(allowed, action, reason, rule=None):
            return Decision(allowed, tool, (t.rel if t.kind == "path" else t.raw), action, reason, rule, self.mode)
        if hook_ask is not None:
            return self._escalate(tool, t, f"PreToolUse hook: {hook_ask.message or 'ask'}", ctx, D)
        if config.PROPOSE and tool in MUTATING and self._offplan(tool, t, ctx):
            shown = t.rel if t.kind == "path" else (t.raw or tool)
            if _is_destructive(tool, shown):
                return self._escalate(tool, t, "off-plan change not on the approved manifest", ctx, D)
        return None

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
        if tool == "apply_patch":
            # apply_patch hides its targets INSIDE the patch body — summarize what it DOES so the log
            # line, the label, and the guardian's review all see "delete CONTRIBUTING.md", not "apply_patch".
            try:
                from . import patch
                return _Target("other", patch.patch_summary(args.get("patch", "")) or "apply_patch")
            except Exception:  # noqa: BLE001 - a bad patch still gets a (useless-but-safe) generic target
                return _Target("other", "apply_patch")
        if tool == "pursue":
            # Same class as apply_patch: `pursue` hides its BAR in an args field, so without this the
            # target is '' — the log line and the guardian's review would see a bare 'pursue' with the
            # command invisible, and a bar-scoped rule (`pursue(pytest:*)`) could never match. kind
            # "command" routes it through the command matcher so such rules are real.
            try:
                from . import goal
                return _Target("command", goal.render(args.get("bar")) or "pursue")
            except Exception:  # noqa: BLE001
                return _Target("command", "pursue")
        if tool == "web_fetch":
            return _Target("other", args.get("url", ""))
        if tool == "web_search":
            return _Target("other", args.get("query", ""))
        return _Target("none", "")

    def _within_roots(self, abs_path, cwd, include_read_only=False):
        """Is abs_path inside the workspace fence? `include_read_only` (specs/0035) additionally admits the
        READ-only reference roots — passed True by the step-2 fence ONLY for non-mutating path tools, so a
        read reaches a read-granted dir while a write/edit/delete does not. read_only_roots is empty until a
        trusted-user-dir grant lands, so with the flag off this is byte-identical however it is called."""
        roots = [os.path.realpath(cwd)] + self.extra_roots
        if include_read_only:
            roots = roots + self.read_only_roots
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
        """The guardian's Verdict(approved, reason) for an ask-tier decision, or None to fall through to
        the human prompt / headless block (unchanged). Fires ONLY when the flag is on, at the top level
        (depth 0), and HEADLESS (no human present) — when a human IS present they get the [y/N] prompt as
        before. OFF / interactive -> None, so every ask/prompt site is byte-identical to today. Identical
        (tool, target) calls are reviewed once per turn (a cache on ctx), so a repeated command isn't
        re-litigated. Governs the ASK tier ONLY."""
        # specs/0057: with CODE_GUARDIAN_INTERACTIVE the guardian ALSO adjudicates when a human is present
        # (the REPL) — it auto-approves the clearly-safe, on-request calls; _ask_approver defers anything it
        # won't approve to the human [y/N]. Default off -> headless-only, byte-identical to specs/0019.
        interactive = getattr(ctx, "interactive", False)
        if not (config.GUARDIAN and getattr(ctx, "depth", 0) == 0
                and (not interactive or config.GUARDIAN_INTERACTIVE)):
            return None
        from . import guardian   # lazy: only needed when the flag is on; keeps permissions low-level
        shown = t.rel if getattr(t, "kind", None) == "path" else (t.raw or tool)
        cache = getattr(ctx, "_guardian_cache", None)
        if cache is None:
            cache = {}
            try:
                ctx._guardian_cache = cache
            except Exception:  # noqa: BLE001 - a ctx that can't hold the cache just skips memoization
                pass
        key = (tool, shown)
        if key not in cache:
            cache[key] = guardian.review(tool, shown, reason, ctx)
        return cache[key]

    def _ask_approver(self, tool, t, reason, ctx):
        """Auto-decide an ASK-tier call when a human isn't present: first the deterministic
        MASS-DESTRUCTION cap (ride-5), then a PermissionRequest hook, then the guardian (breadth-aware).
        Returns (approved, why) or None to fall through to the human prompt / headless block.

        The cap is a HARD ceiling on DISTINCT destructive ops (delete / move / dangerous command) APPROVED
        this turn: the reviewer is aggregate-blind (one call at a time), so without this a decomposed bulk
        delete is rubber-stamped file-by-file. Past N (CODE_GUARDIAN_MAX_DESTRUCTIVE) a new destructive
        target is DENIED regardless of the verdict — no enumeration bypass; raise the flag to go further."""
        shown = t.rel if getattr(t, "kind", None) == "path" else (t.raw or tool)
        destructive = _is_destructive(tool, shown)
        seen = getattr(ctx, "_destructive_targets", None)
        if seen is None:
            seen = set()
            try:
                ctx._destructive_targets = seen
            except Exception:  # noqa: BLE001 - a ctx that can't hold the ledger just skips the cap
                pass
        cap = config.GUARDIAN_MAX_DESTRUCTIVE
        key = (tool, shown)
        if destructive and cap and key not in seen and len(seen) >= cap:
            return (False, f"mass-destruction budget exceeded ({cap} destructive ops this turn) - escalate to a human")

        hv = self._hooks_permreq(tool, t, ctx)
        if hv is not None:
            verdict = (hv.approved, f"PermissionRequest hook {'approved' if hv.approved else 'denied'}: {hv.reason}")
        else:
            gv = self._guardian(tool, t, reason, ctx)   # reads len(ctx._destructive_targets) for breadth
            if gv is None:
                return None
            # specs/0057: an INTERACTIVE guardian only AUTO-APPROVES; anything it will not clear falls through
            # to the human [y/N] (return None) rather than a hard deny — the human stays the backstop. Headless
            # keeps the fail-closed deny (an unattended run must not proceed on an unreviewed action).
            if not gv.approved and getattr(ctx, "interactive", False):
                return None
            verdict = (gv.approved, f"guardian {'approved' if gv.approved else 'denied'}: {gv.reason}")
        if destructive and verdict[0]:                  # a destructive op was APPROVED -> it counts toward the cap
            seen.add(key)
        return verdict

    def _hooks_pretool(self, tool, t, args, ctx):
        """A PreToolUse hook's DENY (or None). Fires whenever CODE_HOOKS is on — at EVERY depth, since a
        hook is an external subprocess (no re-entrancy) and policy should apply to subagents too. OFF ->
        None, byte-identical. Never raises (the runner is fail-open)."""
        if not config.HOOKS:
            return None
        from . import hooks   # lazy: only when the flag is on; keeps permissions low-level
        shown = t.rel if getattr(t, "kind", None) == "path" else (t.raw or tool)
        return hooks.pretool(tool, shown, args, ctx)

    def _hooks_permreq(self, tool, t, ctx):
        """A PermissionRequest hook's verdict for an ask-tier call, or None. Headless-only + top-level,
        like the guardian (a present human decides). OFF -> None. Never raises (fail-open)."""
        if not (config.HOOKS and getattr(ctx, "depth", 0) == 0 and not getattr(ctx, "interactive", False)):
            return None
        from . import hooks
        shown = t.rel if getattr(t, "kind", None) == "path" else (t.raw or tool)
        return hooks.permission_request(tool, shown, ctx)

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
        # propose mode (specs/0022): a MUTATING/dangerous command is read-only until the manifest is
        # approved. Mirror of the main-ladder guard — decide() diverts here BEFORE that guard, so without
        # this a mutating command would slip through the investigate phase. (A read-only command already
        # returned above; in the approved phase a command is inherently off-manifest -> falls to ask below.)
        if config.PROPOSE and self.mode == "propose" and getattr(ctx, "propose_phase", "investigate") != "approved":
            # specs/0048 (c): after a manifest was approved this session, a mutating command falls to the ask
            # ladder below instead of hard-deny (a command is never ON a file manifest, so gating run/test
            # behind file approval was a category error). specs/0052: an autoplan-unlocked session (graduated
            # via /approve or an autoplan yes) relaxes too. Off by default -> the original deny.
            graduated = getattr(ctx, "propose_graduated", False)
            relaxed = graduated and (config.PROPOSE_RUN_AFTER_APPROVAL or config.PROPOSE_AUTOPLAN)
            if not relaxed:
                # specs/0052: offer to unlock instead of a bare dead-end (a yes graduates + allows this op).
                unlocked = self._propose_autoplan("run_command", t, ctx, D)
                if unlocked is not None:
                    return unlocked
                return D(False, "deny", self._propose_ro_msg(ctx))
        for seg in candidates:                                   # ask — matches ANY segment
            r = self._match_command_rules(self.ask, seg)
            if r:
                a = self._ask_approver("run_command", t, f"ask rule {r!r}", ctx)
                if a is not None:
                    return D(a[0], "ask", f"ask rule {r!r} -> {a[1]}", r)
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
        a = self._ask_approver("run_command", t, f"{self.mode} mode", ctx)
        if a is not None:
            return D(a[0], "ask", f"{self.mode} mode -> {a[1]}")
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
        # propose mode (specs/0022): the Move ladder, mirroring the main gate. Read-only until approved;
        # then allow a move whose BOTH endpoints are on the approved manifest (an off-manifest move falls
        # through to the default ask below). UNDER the deny + fence checks above.
        if config.PROPOSE and self.mode == "propose":
            if getattr(ctx, "propose_phase", "investigate") != "approved":
                # specs/0048 (b): a graduated off-manifest move falls to the ask ladder instead of hard-deny.
                # specs/0052: an autoplan-unlocked session relaxes moves too.
                graduated = getattr(ctx, "propose_graduated", False)
                if not (graduated and (config.PROPOSE_EXTEND_AFTER_APPROVAL or config.PROPOSE_AUTOPLAN)):
                    return D(False, "deny", self._propose_ro_msg(ctx), target=old_path)
            elif self._on_manifest_move(ctx, old_path, new_path):
                return D(True, "allow", "move on the approved manifest", target=old_path)
        if self.mode == "plan":
            return D(False, "deny", "plan mode is read-only", target=old_path)
        # Off-plan net (specs/0022): under an APPROVED manifest, an OFF-manifest move is a deviation - don't
        # auto-allow it in a permissive mode; let it fall to the ask ladder below, mirroring decide()'s
        # off-plan escalation for a delete (an ON-manifest move already returned above). Off by default:
        # with no approved manifest, off_plan is False and the permissive-mode allow is byte-identical.
        off_plan = (config.PROPOSE and getattr(ctx, "propose_phase", None) == "approved"
                    and not self._on_manifest_move(ctx, old_path, new_path))
        if self.mode in ("bypass", "acceptEdits") and not off_plan:
            return D(True, "allow", f"{self.mode} mode (a rename is edit-level)", target=old_path)
        _wt = self._target("write_file", {"path": new_path}, ctx)
        a = self._ask_approver("apply_patch move", _wt, "a Move in default mode", ctx)
        if a is not None:
            return D(a[0], "ask", f"default mode -> {a[1]}", target=old_path)
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
