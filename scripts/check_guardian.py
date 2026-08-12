"""
scripts/check_guardian.py

Acceptance harness for specs/0019 — the fail-CLOSED guardian, checked WITHOUT a model or a network (the
reviewer subagent is a stub). The crux is the inversion from grounding: any failure DENIES. It fires ONLY
headless (a human present gets the [y/N] prompt), reviews an identical (tool, target) once per turn, and
surfaces a reason. Run:

    python scripts/check_guardian.py

Exits 0 only if every check holds — including that CODE_GUARDIAN off is byte-identical to today.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, guardian  # noqa: E402
from src.permissions import Permissions  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


class _Ctx:
    def __init__(self, spawn=None, depth=0, interactive=False):
        self.cwd = ROOT
        self.spawn = spawn
        self.depth = depth
        self.interactive = interactive


def _spawn(verdict, calls, raises=False):
    # Mirrors the real ctx.spawn signature (task, effort=None, label=None, **_k) so a label= call doesn't blow up.
    def s(task, effort=None, label=None, **_k):
        calls.append(label or task)
        if raises:
            raise RuntimeError("reviewer exploded")
        return verdict
    return s


def main():
    # -- review(): APPROVE allows, everything else DENIES (fail-closed); a Verdict carries a reason -------
    v = guardian.review("edit_file", "a.py", "ask", _Ctx(_spawn("APPROVE: safe config edit", [])))
    check("review: a clean APPROVE -> approved, with the reason carried", v.approved is True and "safe" in v.reason)
    check("review: a DENY -> deny", guardian.review("edit_file", "a.py", "ask", _Ctx(_spawn("DENY: risky", []))).approved is False)
    check("review: NO spawn available -> DENY (fail-closed)", guardian.review("edit_file", "a.py", "ask", _Ctx(spawn=None)).approved is False)
    check("review: the reviewer RAISES -> DENY (fail-closed)",
          guardian.review("edit_file", "a.py", "ask", _Ctx(_spawn("APPROVE", [], raises=True))).approved is False)
    check("review: an EMPTY / subagent-error verdict -> DENY (fail-closed)",
          guardian.review("x", "y", "z", _Ctx(_spawn("", []))).approved is False
          and guardian.review("x", "y", "z", _Ctx(_spawn("(subagent error: boom)", []))).approved is False)

    # -- _parse_verdict: markdown tolerated; an AMBIGUOUS verdict denies; a reason is extracted -----------
    check("_parse_verdict: **APPROVE** (markdown) -> True", guardian._parse_verdict("**APPROVE**: looks fine").approved is True)
    check("_parse_verdict: a bullet-wrapped DENY -> False", guardian._parse_verdict("- DENY: nope").approved is False)
    check("_parse_verdict: BOTH APPROVE and DENY present -> DENY (ambiguous, fail-closed)",
          guardian._parse_verdict("APPROVE the read but DENY the write").approved is False)
    check("_parse_verdict: prose with no verdict -> DENY", guardian._parse_verdict("I think it is probably fine").approved is False)
    check("_parse_verdict: reason is the tail after the verdict word",
          guardian._parse_verdict("APPROVE: routine npm install").reason == "routine npm install")

    # -- calibration: routine installs are APPROVE-able + a REQUESTED destructive op is presented -----
    _prompt = guardian._review_task("run_command", "cd src/homepage && npm install", "acceptEdits mode")
    check("prompt: routine installs (npm/pip/go) are called out as safe to APPROVE",
          "npm" in _prompt.lower() and "APPROVE" in _prompt and "arbitrary network" in _prompt.lower())
    # ride-5: the guardian gets the USER'S REQUEST, so it can approve a destructive-but-REQUESTED op
    _req_prompt = guardian._review_task("delete_file", "CONTRIBUTING.md", "default mode",
                                        "delete the file CONTRIBUTING.md")
    check("prompt: the user's request is threaded in, and requested-destructive is APPROVE-able",
          "delete the file CONTRIBUTING.md" in _req_prompt and "destructive-but-REQUESTED" in _req_prompt)
    check("prompt: with no request, the field degrades safely (not provided) and destructive still denies",
          "(not provided)" in guardian._review_task("delete_file", "x", "default mode")
          and "canNOT tie it to the user's request, DENY" in _req_prompt)
    # review() reads ctx.request and passes it to the reviewer
    _seen = []
    def _cap(task, effort=None, label=None, **_k):
        _seen.append(task)
        return "APPROVE: does exactly what the user asked"
    _cx = _Ctx(_cap, depth=0)
    _cx.request = "delete the file CONTRIBUTING.md"
    _rv = guardian.review("delete_file", "CONTRIBUTING.md", "default mode", _cx)
    check("review threads ctx.request into the reviewer's prompt",
          _rv.approved is True and any("delete the file CONTRIBUTING.md" in t for t in _seen))

    # -- permissions integration: the guardian decides the ASK tier, HEADLESS ----------------------------
    _saved = config.GUARDIAN
    _saved_gi = config.GUARDIAN_INTERACTIVE
    config.HOOKS = False   # hermetic: no PermissionRequest hook shadowing the guardian in the ask chain
    config.GUARDIAN = True
    config.GUARDIAN_INTERACTIVE = False   # isolate specs/0057 from the live .env for the headless-only assertions
    p = Permissions("default", {"ask": ["edit_file(.env)"]}, [])
    calls = []
    d = p.decide("edit_file", {"path": ".env"}, _Ctx(_spawn("APPROVE: safe config edit", calls), depth=0))
    check("GUARDIAN on + headless: an APPROVE lets an ask-tier call through", d.allowed and len(calls) == 1)
    check("GUARDIAN on + headless: the decision reason carries the guardian's verdict text",
          "guardian approved" in d.reason and "safe" in d.reason)
    check("GUARDIAN on + headless: a DENY blocks the ask-tier call",
          not p.decide("edit_file", {"path": ".env"}, _Ctx(_spawn("DENY: no", []), depth=0)).allowed)

    # headless-only: when a human IS present, the guardian returns None (falls through to the [y/N]
    # prompt). Probe _guardian directly so the test never blocks on the real input() prompt.
    calls = []
    tgt = p._target("edit_file", {"path": ".env"}, _Ctx())
    g_int = p._guardian("edit_file", tgt, "ask rule", _Ctx(_spawn("APPROVE", calls), depth=0, interactive=True))
    check("headless-only (GUARDIAN_INTERACTIVE off): interactive -> guardian returns None (not consulted), no spawn",
          g_int is None and calls == [])

    # -- specs/0057: interactive guardian — auto-approve the safe, DEFER the rest to the human ------------
    config.GUARDIAN_INTERACTIVE = True
    calls = []
    g_ap = p._guardian("edit_file", tgt, "ask rule", _Ctx(_spawn("APPROVE: safe", calls), depth=0, interactive=True))
    check("GUARDIAN_INTERACTIVE on: the guardian IS consulted when interactive (spawns + returns a verdict)",
          g_ap is not None and g_ap.approved is True and len(calls) == 1)
    ap = p._ask_approver("edit_file", tgt, "ask rule", _Ctx(_spawn("APPROVE: safe", []), depth=0, interactive=True))
    check("GUARDIAN_INTERACTIVE on + APPROVE: _ask_approver auto-approves (no human prompt needed)",
          ap is not None and ap[0] is True)
    dn = p._ask_approver("edit_file", tgt, "ask rule", _Ctx(_spawn("DENY: risky", []), depth=0, interactive=True))
    check("GUARDIAN_INTERACTIVE on + DENY: _ask_approver returns None -> DEFERS to the human [y/N] (not a hard deny)",
          dn is None)
    config.GUARDIAN_INTERACTIVE = False

    # per-turn cache: the SAME (tool, target) is reviewed once, even across two decide() calls
    calls = []
    ctx_cached = _Ctx(_spawn("APPROVE: ok", calls), depth=0)
    a1 = p.decide("edit_file", {"path": ".env"}, ctx_cached)
    a2 = p.decide("edit_file", {"path": ".env"}, ctx_cached)
    check("cache: an identical ask-tier call is reviewed ONCE per turn (spawn called once)",
          a1.allowed and a2.allowed and len(calls) == 1)

    # recursion gate: the reviewer's OWN ask-tier call (depth>0) never re-enters the guardian
    calls = []
    p.decide("edit_file", {"path": ".env"}, _Ctx(_spawn("APPROVE", calls), depth=1))
    check("recursion gate: at depth>0 the guardian is NOT consulted (no spawn)", calls == [])

    # the guardian governs the ASK tier ONLY — it can't turn a DENY-rule into an allow
    pdeny = Permissions("default", {"deny": ["edit_file(.env)"]}, [])
    check("GUARDIAN only touches ask: a DENY rule still blocks even with an APPROVE verdict",
          not pdeny.decide("edit_file", {"path": ".env"}, _Ctx(_spawn("APPROVE", []), depth=0)).allowed)

    # -- ride-5: the mass-destruction cap (deterministic hard ceiling on decomposed bulk destruction) ----
    from src.permissions import _is_destructive
    check("_is_destructive: delete / apply_patch move / delete-patch / rm command ARE destructive",
          _is_destructive("delete_file", "x") and _is_destructive("apply_patch move", "x")
          and _is_destructive("apply_patch", "delete a; update b") and _is_destructive("run_command", "rm foo.txt"))
    check("_is_destructive: edit / write / install / update-patch are NOT destructive",
          not _is_destructive("edit_file", "x") and not _is_destructive("run_command", "npm install")
          and not _is_destructive("apply_patch", "update a.py") and not _is_destructive("write_file", "x"))

    _savedcap = config.GUARDIAN_MAX_DESTRUCTIVE
    config.GUARDIAN_MAX_DESTRUCTIVE = 3
    pcap = Permissions("default", {}, [])

    def _ctx_req(verdict):
        c = _Ctx(_spawn(verdict, []), depth=0)
        c.request = "cleaning up"
        c._destructive_targets = set()
        return c

    cx = _ctx_req("APPROVE: ok")
    outs = [pcap.decide("delete_file", {"path": f"f{i}.md"}, cx).allowed for i in range(5)]
    check("cap: the first N distinct destructive ops pass, the rest DENY", outs == [True, True, True, False, False])
    check("cap: the deny reason names the exceeded budget",
          "mass-destruction budget" in pcap.decide("delete_file", {"path": "z.md"}, cx).reason)
    cxd = _ctx_req("DENY: no")
    for i in range(4):
        pcap.decide("delete_file", {"path": f"g{i}.md"}, cxd)
    check("cap: guardian-DENIED destructive ops don't consume the budget", len(cxd._destructive_targets) == 0)
    cxe = _ctx_req("APPROVE: ok")
    cxe._destructive_targets = {("delete_file", f"d{i}") for i in range(3)}   # already at the cap
    check("cap: a non-destructive edit is still allowed at the cap (cap targets destruction only)",
          pcap.decide("edit_file", {"path": "a.py", "old_string": "a", "new_string": "b"}, cxe).allowed)
    config.GUARDIAN_MAX_DESTRUCTIVE = 0
    cx0 = _ctx_req("APPROVE: ok")
    check("cap=0 disables the ceiling (all destructive ops approved)",
          all(pcap.decide("delete_file", {"path": f"h{i}.md"}, cx0).allowed for i in range(8)))
    check("breadth: the reviewer prompt surfaces the running destructive count",
          "already approved 2 destructive" in guardian._review_task("delete_file", "x", "default mode", "req", 2))
    config.GUARDIAN_MAX_DESTRUCTIVE = _savedcap

    # -- flag OFF -> byte-identical (guardian never consulted) --------------------------------------------
    config.GUARDIAN = False
    calls = []
    d = p.decide("edit_file", {"path": ".env"}, _Ctx(_spawn("APPROVE", calls), depth=0))
    check("GUARDIAN off: the reviewer is never consulted; a headless ask-tier call blocks (unchanged)",
          (not d.allowed) and calls == [])
    config.GUARDIAN = _saved
    config.GUARDIAN_INTERACTIVE = _saved_gi

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
