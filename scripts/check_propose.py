"""
scripts/check_propose.py

Acceptance harness for specs/0022 - propose mode (a change manifest the user approves before any edit).
Dep-free: no model, no network. Proves the whole contract and the two invariants of every phase:

  * propose_changes validates the manifest, records ONE plan-level approval, and (headless) writes the plan
    out + STOPS instead of auto-approving; top-level only.
  * The permission engine is READ-ONLY during investigate and allows EXACTLY the approved manifest during
    execute - across ALL THREE ladders (decide, execpolicy _decide_command, decide_move).
  * Deny rules + the fence still win over an approved manifest (approve-once is under the hard rules).
  * The graduated off-plan net: an off-manifest DESTRUCTIVE op under an approved manifest is escalated to
    ask (never a silent allow in a permissive mode); a low-risk off-plan op keeps its allow.
  * Corpus: a DECLINED manifest turn is dropped from SFT (kept turns beside it survive); an APPROVED one is
    kept; manifest_declined is an honest gate outcome.
  * Flag OFF (CODE_PROPOSE false) is byte-identical: the tool isn't offered and every new branch is skipped.

Run:  python scripts/check_propose.py
"""
import os
import sys
import json
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, outcomes, toolset  # noqa: E402
from src import tools as tools_mod  # noqa: E402
from src.permissions import Permissions  # noqa: E402
from train import convert  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


class _Ctx:
    """A minimal context for decide() - the fields the gate reads (specs/0022 adds propose_phase/approved_paths)."""
    def __init__(self, cwd, perms=None, depth=0, interactive=False, propose_phase=None, approved=None,
                 ask=None, session_id="test", propose_graduated=False):
        self.cwd = cwd
        self.permissions = perms
        self.depth = depth
        self.interactive = interactive
        self.propose_phase = propose_phase
        self.approved_paths = set(approved or ())
        self.ask = ask
        self.session_id = session_id
        self.manifest = None
        self.propose_graduated = propose_graduated   # specs/0048


# -- tiny trajectory-record builders (mirror check_repl_outcomes) ------------------------------------------
def _ss():
    return {"type": "session_start", "session_id": "s", "schema_version": "0.10.0", "tool_schemas": []}


def _mc(content, calls=()):
    tcs = [{"id": str(i), "name": n, "arguments": "{}"} for i, n in enumerate(calls)]
    return {"type": "model_call", "step": 0, "request": {"messages": [], "tools": []},
            "response": {"content": content, "reasoning": None, "tool_calls": tcs}}


def _manifest(approved):
    return {"type": "manifest", "items": [{"action": "update", "path": "a.py", "why": "x"}],
            "approved": approved, "mode": "propose"}


def _tout(turn, outcome="completed"):
    return {"type": "turn_outcome", "turn": turn, "outcome": outcome, "terminated": "final", "tool_calls": 1}


def _end(outcome="completed"):
    return {"type": "session_end", "outcome": outcome, "tool_calls": 1}


def main():
    ws = os.path.realpath(tempfile.mkdtemp(prefix="propose-ws-"))
    _saved = {k: getattr(config, k) for k in ("PROPOSE", "EXECPOLICY", "HOOKS", "GUARDIAN",
              "PROPOSE_RUN_AFTER_APPROVAL", "PROPOSE_EXTEND_AFTER_APPROVAL", "PROPOSE_PERSIST_APPROVAL",
              "PROPOSE_AUTOPLAN")}
    config.HOOKS = config.GUARDIAN = False   # isolate the propose logic from hooks/guardian
    config.EXECPOLICY = False
    # isolate the specs/0048 relaxations + the specs/0052 autoplan from the live .env so the specs/0022
    # assertions stay deterministic (a live .env may enable them; an un-isolated read would flip a deny)
    config.PROPOSE_RUN_AFTER_APPROVAL = False
    config.PROPOSE_EXTEND_AFTER_APPROVAL = False
    config.PROPOSE_PERSIST_APPROVAL = False
    config.PROPOSE_AUTOPLAN = False

    # =====================================================================================================
    # 1. the tool: propose_changes validation + approval + headless
    # =====================================================================================================
    config.PROPOSE = True

    def make_ctx(interactive, answer=None, depth=0):
        c = tools_mod.Context(ws, Permissions("propose", {}, []))
        c.depth, c.interactive, c.session_id = depth, interactive, "sess"
        c.ask = (lambda q: answer) if answer is not None else None
        c.propose_phase = "investigate"
        return c

    good = [{"action": "update", "path": "src/a.py", "why": "fix"},
            {"action": "move", "path": "src/new.py", "from": "src/old.py", "why": "rename"}]

    c = make_ctx(interactive=False)
    check("tool: a bad manifest (not a list) is refused with a teaching message",
          tools_mod.propose_changes({"manifest": "nope"}, c).ok is False)
    check("tool: an empty manifest is refused", tools_mod.propose_changes({"manifest": []}, c).ok is False)
    check("tool: a bad action is refused",
          tools_mod.propose_changes({"manifest": [{"action": "zap", "path": "a"}]}, c).ok is False)
    check("tool: a missing path is refused",
          tools_mod.propose_changes({"manifest": [{"action": "update", "path": ""}]}, c).ok is False)
    check("tool: a move without 'from' is refused",
          tools_mod.propose_changes({"manifest": [{"action": "move", "path": "b"}]}, c).ok is False)
    check("tool: propose_changes is TOP-LEVEL only (a subagent can't collect approval)",
          tools_mod.propose_changes({"manifest": good}, make_ctx(False, depth=1)).ok is False)

    # headless: write the plan out + STOP, never auto-approve
    ch = make_ctx(interactive=False)
    rh = tools_mod.propose_changes({"manifest": good}, ch)
    check("tool: headless (no human) does NOT approve - returns ok=False and stays investigate",
          rh.ok is False and ch.propose_phase == "investigate" and ch.manifest["approved"] is False)
    check("tool: headless writes the proposed plan to .openagent/ for review",
          os.path.isfile(os.path.join(ws, ".openagent", "manifest-sess.json")))

    # interactive DECLINE
    cd = make_ctx(interactive=True, answer="n")
    rd = tools_mod.propose_changes({"manifest": good}, cd)
    check("tool: a DECLINE leaves the phase read-only and records approved=False",
          rd.ok is False and cd.propose_phase == "investigate" and cd.manifest["approved"] is False)

    # interactive APPROVE
    ca = make_ctx(interactive=True, answer="y")
    ra = tools_mod.propose_changes({"manifest": good}, ca)
    check("tool: an APPROVE flips propose_phase to 'approved' and records approved=True",
          ra.ok is True and ca.propose_phase == "approved" and ca.manifest["approved"] is True)
    check("tool: approved_paths holds the manifest paths (both endpoints of a move), normalized",
          ca.permissions.norm_path("src/a.py", ws) in ca.approved_paths
          and ca.permissions.norm_path("src/new.py", ws) in ca.approved_paths
          and ca.permissions.norm_path("src/old.py", ws) in ca.approved_paths)

    # =====================================================================================================
    # 2. decide(): investigate is read-only; approved allows exactly the manifest
    # =====================================================================================================
    p = Permissions("propose", {}, [])
    inv = _Ctx(ws, p, propose_phase="investigate")
    check("decide: investigate phase - read_file is allowed (investigation is read-only work)",
          p.decide("read_file", {"path": "src/a.py"}, inv).allowed)
    for tool, args in (("write_file", {"path": "src/a.py", "content": ""}),
                       ("edit_file", {"path": "src/a.py"}), ("delete_file", {"path": "src/a.py"}),
                       ("run_command", {"command": "echo hi"}),
                       ("apply_patch", {"patch": "*** Begin Patch\n*** Add File: src/a.py\n+x\n*** End Patch"})):
        d = p.decide(tool, args, inv)
        check(f"decide: investigate phase - {tool} is DENIED (read-only until approved)",
              (not d.allowed) and "read-only until the manifest is approved" in d.reason)

    appr = _Ctx(ws, p, propose_phase="approved", approved={p.norm_path("src/a.py", ws)})
    check("decide: approved phase - a write ON the manifest is allowed",
          p.decide("write_file", {"path": "src/a.py", "content": ""}, appr).allowed)
    check("decide: approved phase - a write OFF the manifest is not auto-allowed (needs approval, headless -> deny)",
          not p.decide("write_file", {"path": "src/b.py", "content": ""}, appr).allowed)
    check("decide: approved phase - apply_patch passes the envelope (each op is re-gated per file)",
          p.decide("apply_patch", {"patch": "*** Begin Patch\n*** Update File: src/a.py\n+x\n*** End Patch"}, appr).allowed)

    # missing propose_phase attr must read as investigate (the getattr default agrees with the field default)
    bare = _Ctx(ws, p)
    bare.propose_phase = None
    check("decide: propose mode with no approval yet is read-only (getattr default == investigate)",
          not p.decide("write_file", {"path": "src/a.py", "content": ""}, bare).allowed)

    # =====================================================================================================
    # 2b. propose PRE-VALIDATES the manifest against the hard rules (ride: the propose->approve->deny loop)
    # =====================================================================================================
    # Permissions.hard_block: the reason an op is HARD-blocked (deny rule / fence / PreToolUse deny hook).
    pdh = Permissions("propose", {"deny": ["write_file(secrets/**)"]}, [])
    check("hard_block: a deny-ruled write returns a reason",
          pdh.hard_block("write_file", {"path": "secrets/k.txt"}, _Ctx(ws, pdh)) is not None)
    check("hard_block: a normal in-fence write returns None",
          pdh.hard_block("write_file", {"path": "src/a.py"}, _Ctx(ws, pdh)) is None)
    check("hard_block: a path outside the fence returns a reason",
          pdh.hard_block("write_file", {"path": os.path.join(tempfile.gettempdir(), "hb-out.py")}, _Ctx(ws, pdh)) is not None)

    def _pctx(rules):
        c = tools_mod.Context(ws, Permissions("propose", rules, []))
        c.depth, c.interactive, c.session_id, c.ask, c.propose_phase = 0, True, "sess", (lambda q: "y"), "investigate"
        return c

    cblk = _pctx({"deny": ["write_file(secrets/**)"]})
    rblk = tools_mod.propose_changes({"manifest": [{"action": "update", "path": "secrets/keys.txt", "why": "x"}]}, cblk)
    check("tool: a plan with a HARD-blocked item is REFUSED before approval (no approve->deny loop)",
          rblk.ok is False and cblk.propose_phase == "investigate" and "hard rule" in rblk.content)
    cok = _pctx({"deny": ["write_file(secrets/**)"]})
    rok = tools_mod.propose_changes({"manifest": [{"action": "update", "path": "src/a.py", "why": "x"}]}, cok)
    check("tool: a plan whose items all clear the hard rules is approved normally",
          rok.ok is True and cok.propose_phase == "approved")
    # audit #1: the pre-check must probe edit_file too - an 'update' EXECUTES via edit_file, so an
    # edit_file-scoped deny rule has to be caught at propose time (not just write_file), or the loop reopens.
    cedit = _pctx({"deny": ["edit_file(secrets/**)"]})
    redit = tools_mod.propose_changes({"manifest": [{"action": "update", "path": "secrets/keys.txt", "why": "x"}]}, cedit)
    check("tool: an edit_file-scoped deny rule is caught by the pre-check (an update runs via edit_file)",
          redit.ok is False and "hard rule" in redit.content)
    cmv = _pctx({"deny": ["edit_file(secrets/**)"]})
    rmv = tools_mod.propose_changes({"manifest": [{"action": "move", "path": "secrets/new.txt", "from": "src/a.py", "why": "x"}]}, cmv)
    check("tool: a move is pre-checked on edit_file at BOTH endpoints (decide_move gates both)",
          rmv.ok is False and "hard rule" in rmv.content)

    # =====================================================================================================
    # 3. approve-once is UNDER the hard rules: deny + fence still win
    # =====================================================================================================
    p_deny = Permissions("propose", {"deny": ["edit_file(.env)"]}, [])
    a_env = _Ctx(ws, p_deny, propose_phase="approved", approved={p_deny.norm_path(".env", ws)})
    check("decide: an APPROVED op targeting a deny-ruled path is STILL denied (deny wins over the manifest)",
          not p_deny.decide("edit_file", {"path": ".env"}, a_env).allowed)
    outside = os.path.join(tempfile.gettempdir(), "propose-outside.py")
    a_out = _Ctx(ws, p, propose_phase="approved", approved={p.norm_path(outside, ws)})
    d = p.decide("write_file", {"path": outside, "content": ""}, a_out)
    check("decide: an APPROVED op outside the fence is STILL denied (the fence wins over the manifest)",
          (not d.allowed) and "outside" in d.reason)

    # =====================================================================================================
    # 4. the execpolicy command ladder mirrors the propose gate (the diverted-before-the-mode-branch trap)
    # =====================================================================================================
    config.EXECPOLICY = True
    d = p.decide("run_command", {"command": "rm -rf src"}, inv)
    check("decide(_decide_command): investigate - a MUTATING command is denied (execpolicy path)",
          (not d.allowed) and "read-only until the manifest is approved" in d.reason)
    check("decide(_decide_command): investigate - a READ-ONLY command (ls) is still allowed",
          p.decide("run_command", {"command": "ls"}, inv).allowed)
    config.EXECPOLICY = False

    # =====================================================================================================
    # 5. the Move ladder (decide_move) mirrors the propose gate
    # =====================================================================================================
    check("decide_move: investigate - a rename is denied (read-only)",
          not p.decide_move("src/old.py", "src/new.py", inv).allowed)
    mv_ok = _Ctx(ws, p, propose_phase="approved",
                 approved={p.norm_path("src/old.py", ws), p.norm_path("src/new.py", ws)})
    check("decide_move: approved - a move with BOTH endpoints on the manifest is allowed",
          p.decide_move("src/old.py", "src/new.py", mv_ok).allowed)
    mv_half = _Ctx(ws, p, propose_phase="approved", approved={p.norm_path("src/old.py", ws)})
    check("decide_move: approved - a move with only ONE endpoint on the manifest is not auto-allowed",
          not p.decide_move("src/old.py", "src/new.py", mv_half).allowed)

    # =====================================================================================================
    # 6. the graduated off-plan net (auto-propose in a permissive mode)
    # =====================================================================================================
    pb = Permissions("bypass", {}, [])
    off = _Ctx(ws, pb, propose_phase="approved", approved={pb.norm_path("src/a.py", ws)})
    check("off-plan net: bypass - an ON-manifest write stays allowed",
          pb.decide("write_file", {"path": "src/a.py", "content": ""}, off).allowed)
    check("off-plan net: bypass - an OFF-manifest LOW-RISK edit keeps its allow (low-risk -> allow+log)",
          pb.decide("edit_file", {"path": "src/b.py"}, off).allowed)
    d = pb.decide("delete_file", {"path": "src/b.py"}, off)
    check("off-plan net: bypass - an OFF-manifest DESTRUCTIVE delete is ESCALATED (headless -> not a silent allow)",
          (not d.allowed) and d.action == "ask")
    # audit #2: an OFF-manifest MOVE under an approved manifest is escalated too (not silently allowed), and
    # an ON-manifest move stays allowed; with NO approved manifest, bypass still auto-allows (byte-identical).
    check("off-plan net: bypass - an OFF-manifest move under an approved manifest is NOT silently allowed",
          not pb.decide_move("src/x.py", "src/y.py", off).allowed)
    on_mv = _Ctx(ws, pb, propose_phase="approved",
                 approved={pb.norm_path("src/x.py", ws), pb.norm_path("src/y.py", ws)})
    check("off-plan net: bypass - an ON-manifest move under an approved manifest IS allowed",
          pb.decide_move("src/x.py", "src/y.py", on_mv).allowed)
    check("decide_move: bypass with NO approved manifest auto-allows a move (byte-identical)",
          pb.decide_move("src/x.py", "src/y.py", _Ctx(ws, pb)).allowed)

    # =====================================================================================================
    # 7. flag OFF -> byte-identical
    # =====================================================================================================
    config.PROPOSE = False
    check("toolset: CODE_PROPOSE off -> propose_changes is NOT offered",
          not any(t["name"] == "propose_changes" for t in toolset.active_tools()))
    off_inv = _Ctx(ws, Permissions("propose", {}, []), propose_phase="investigate")
    # with the flag off, propose mode has no propose gate -> a mutating tool falls to the normal ladder
    # (propose isn't bypass/acceptEdits, so default-like -> headless deny, NOT the propose read-only reason)
    d = Permissions("propose", {}, []).decide("write_file", {"path": "src/a.py", "content": ""}, off_inv)
    check("decide: CODE_PROPOSE off -> the propose gate is skipped (no 'read-only until approved' branch)",
          "read-only until the manifest is approved" not in (d.reason or ""))
    config.PROPOSE = True
    check("toolset: CODE_PROPOSE on -> propose_changes IS offered",
          any(t["name"] == "propose_changes" for t in toolset.active_tools()))

    # =====================================================================================================
    # 8. corpus honesty: a declined plan is dropped; an approved plan is kept
    # =====================================================================================================
    check("outcomes: manifest_declined is an honest gate outcome (won't be washed to success)",
          "manifest_declined" in outcomes.GATE_OUTCOMES
          and outcomes.classify("manifest_declined", 3) == "manifest_declined")

    # REPL: turn 1 clean, turn 2 proposes a DECLINED plan -> keep turn 1, DROP turn 2
    repl = [_ss(),
            {"type": "turn", "message": {"role": "user", "content": "t1"}},
            _mc("clean answer", calls=["read_file"]), _tout(1),
            {"type": "turn", "message": {"role": "user", "content": "t2"}},
            _mc("proposed but declined", calls=["propose_changes"]), _manifest(False), _tout(2),
            _end("completed")]
    check("convert: _unapplied_manifest_turns pinpoints the DECLINED turn",
          convert._unapplied_manifest_turns(repl) == {2})
    keep, reason = convert.is_trainable(repl)
    check("convert: a declined-plan turn doesn't drop the whole session (clean turn 1 survives)",
          keep and reason == "kept")
    check("convert: only the good turn's step becomes a row (the declined turn is dropped)",
          len(convert.to_rows(repl, "as_sent")) == 1)

    # an APPROVED plan turn is KEPT (teaches propose->approve->execute)
    kept = [_ss(),
            {"type": "turn", "message": {"role": "user", "content": "t1"}},
            _mc("proposed", calls=["propose_changes"]), _manifest(True),
            _mc("done", calls=["write_file"]), _tout(1), _end("completed")]
    check("convert: an APPROVED plan turn is KEPT and not flagged unapplied",
          convert._unapplied_manifest_turns(kept) == set() and convert.is_trainable(kept)[0])

    # one-shot: a declined manifest drops the whole session
    one = [_ss(), {"type": "turn", "message": {"role": "user", "content": "t"}},
           _mc("proposed", calls=["propose_changes"]), _manifest(False), _end("completed")]
    check("convert: a one-shot run whose plan was DECLINED is dropped as manifest_declined",
          convert.is_trainable(one) == (False, "manifest_declined"))

    # =====================================================================================================
    # 9. propose follow-through (specs/0048): the per-turn reset + the three opt-in relaxations
    # =====================================================================================================
    config.PROPOSE = True
    from src.agent import _reset_propose_for_turn   # dep-free; drives the EXACT agent.py per-turn reset
    pp = Permissions("propose", {}, [])
    APPROVED = pp.norm_path("src/a.py", ws)

    # --- the per-turn reset (agent.py) — the cross-turn behavior the specs/0022 harness never exercised ---
    config.PROPOSE_PERSIST_APPROVAL = False
    g = _Ctx(ws, pp, propose_phase="approved", approved={APPROVED}, propose_graduated=True)
    _reset_propose_for_turn(g)
    check("reset default: an approved turn re-locks to investigate next turn (approval never leaks)",
          g.propose_phase == "investigate" and g.approved_paths == set())
    check("reset default: propose_graduated PERSISTS (session-scoped, not cleared by the reset)",
          g.propose_graduated is True)

    config.PROPOSE_PERSIST_APPROVAL = True
    g2 = _Ctx(ws, pp, propose_phase="approved", approved={APPROVED}, propose_graduated=True)
    _reset_propose_for_turn(g2)
    check("reset (a) PERSIST+graduated: approved phase + approved_paths survive to the next turn",
          g2.propose_phase == "approved" and APPROVED in g2.approved_paths)
    g3 = _Ctx(ws, pp, propose_phase="approved", approved={APPROVED}, propose_graduated=False)
    _reset_propose_for_turn(g3)
    check("reset (a): a NON-graduated (cold) session still re-locks to investigate (guard holds)",
          g3.propose_phase == "investigate" and g3.approved_paths == set())
    config.PROPOSE_PERSIST_APPROVAL = False

    grad = _Ctx(ws, pp, propose_phase="investigate", propose_graduated=True)   # a graduated later turn
    cold = _Ctx(ws, pp, propose_phase="investigate", propose_graduated=False)  # a cold session, no approval yet

    # --- (c) RUN_AFTER_APPROVAL: a graduated mutating command falls to ask (both EXECPOLICY paths) --------
    config.PROPOSE_RUN_AFTER_APPROVAL = True
    for label, ep in (("execpolicy path", True), ("generic path", False)):
        config.EXECPOLICY = ep
        d = pp.decide("run_command", {"command": "docker build ."}, grad)
        check(f"(c) graduated+RUN ({label}): mutating command NOT hard-denied read-only",
              "read-only until the manifest is approved" not in (d.reason or ""))
        dc = pp.decide("run_command", {"command": "docker build ."}, cold)
        check(f"(c) COLD ({label}): mutating command STILL read-only-denied (cold guard holds)",
              (not dc.allowed) and "read-only until the manifest is approved" in (dc.reason or ""))
    config.EXECPOLICY = False
    config.PROPOSE_RUN_AFTER_APPROVAL = False

    # --- (b) EXTEND_AFTER_APPROVAL: a graduated off-manifest write falls to ask; deny+fence still win -----
    config.PROPOSE_EXTEND_AFTER_APPROVAL = True
    dw = pp.decide("write_file", {"path": "src/new.py", "content": "x"}, grad)
    check("(b) graduated+EXTEND: off-manifest write NOT hard-denied read-only",
          "read-only until the manifest is approved" not in (dw.reason or ""))
    dwc = pp.decide("write_file", {"path": "src/new.py", "content": "x"}, cold)
    check("(b) COLD: off-manifest write STILL read-only-denied",
          (not dwc.allowed) and "read-only until the manifest is approved" in (dwc.reason or ""))
    pdeny = Permissions("propose", {"deny": ["write_file(.env)"]}, [])
    gdeny = _Ctx(ws, pdeny, propose_phase="investigate", propose_graduated=True)
    denv = pdeny.decide("write_file", {"path": ".env", "content": "x"}, gdeny)
    check("(b) deny rule STILL wins under graduated EXTEND (.env denied by rule, not relaxed)",
          (not denv.allowed) and "deny rule" in (denv.reason or ""))
    outside = os.path.join(os.path.dirname(ws), "outside.py")
    check("(b) fence STILL wins under graduated EXTEND (a path outside the workspace is denied)",
          not pp.decide("write_file", {"path": outside, "content": "x"}, grad).allowed)
    config.PROPOSE_EXTEND_AFTER_APPROVAL = False

    # --- flag-OFF byte-identity: a graduated session with ALL relaxations off is still read-only ---------
    off_cmd = pp.decide("run_command", {"command": "docker build ."}, grad)
    off_wr = pp.decide("write_file", {"path": "src/new.py", "content": "x"}, grad)
    check("flag OFF byte-identical: graduated but all relaxations off -> still read-only-denied",
          "read-only until the manifest is approved" in (off_cmd.reason or "")
          and "read-only until the manifest is approved" in (off_wr.reason or ""))

    for k, v in _saved.items():
        setattr(config, k, v)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
