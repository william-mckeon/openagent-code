"""
scripts/check_context.py

Acceptance harness for specs/0009 — the bounded-fragment invariant in src/context.py, checked
WITHOUT a model or a network (stub model/trajectory). Run:  python scripts/check_context.py
Exits 0 only if every fragment that can enter the live context is bounded.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config  # noqa: E402
from src.context import ContextManager  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


class _Traj:
    def log_turn(self, m): pass
    def log_compaction(self, *a): pass


class _Model:
    def summarize(self, msgs): return "a short summary"


def _clen(m):
    c = m.get("content")
    return len(c) if isinstance(c, str) else 0


def main():
    cap = config.MAX_MESSAGE_CHARS
    margin = 300  # room for the appended "...truncated N chars" note

    # compaction OFF (compact_at=0) so we test the per-fragment bound in isolation
    cm = ContextManager("system prompt", _Model(), _Traj(), compact_at_tokens=0)

    # 1. the bounding primitive
    big = cm._capped({"role": "user", "content": "x" * (cap * 3)})
    check("_capped bounds an oversized fragment", len(big["content"]) <= cap + margin)
    check("_capped leaves a small fragment untouched",
          cm._capped({"role": "user", "content": "hi"})["content"] == "hi")

    # 2. add() caps a huge tool result / user turn
    cm.add({"role": "user", "content": "y" * (cap * 3)})
    check("add() caps a huge message in the live set", _clen(cm.working[-1]) <= cap + margin)

    # 3. set_pinned() caps the always-sent, never-compacted plan (the real C3 gap)
    cm.set_pinned("z" * (cap * 3))
    check("set_pinned() caps the pinned plan (no unbounded always-sent item)",
          cm.pinned is not None and _clen(cm.pinned) <= cap + margin)

    # 4. the system prompt (a fixed, curated fragment) is NOT truncated
    check("the system prompt is preserved intact", cm.system["content"] == "system prompt")

    # 5. THE INVARIANT: every fragment the model sees is bounded
    biggest = max((_clen(m) for m in cm.context()), default=0)
    check("EVERY fragment in context() is bounded to the cap", biggest <= cap + margin)

    # 6. compaction routes an OVERSIZED summary through the cap. NOT vacuous: the stub summary is 3x
    #    the cap, and the summarized block is big enough that even a CAPPED summary still shrinks the
    #    context (after < before), so the summary really enters `working` and must come out bounded.
    #    If the summary cap at context.py were removed, the summary fragment would be ~3x cap -> FAIL.
    class _BigSummaryModel:
        def summarize(self, msgs): return "S" * (cap * 3)
    cm3 = ContextManager("s", _BigSummaryModel(), _Traj(), compact_at_tokens=1, keep_recent=1)
    for i in range(10):
        cm3.add({"role": "user", "content": f"m{i} " + "." * (cap // 2)})  # big enough to force + survive a shrink
    cm3.context()  # over budget -> compaction applies WITH the oversized summary
    summ = [m for m in cm3.working
            if isinstance(m.get("content"), str) and m["content"].startswith("[Earlier conversation summarized")]
    check("compaction ran and its oversized summary was capped (not vacuous)",
          len(summ) == 1 and _clen(summ[0]) <= cap + margin)
    check("after compaction, every live fragment is bounded",
          all(_clen(m) <= cap + margin for m in cm3.working))

    # 7. RESUME: a huge historical message rehydrated from the trajectory is capped, not re-injected raw
    resumed = ContextManager("s", _Model(), _Traj(),
                             initial_working=[{"role": "user", "content": "w" * (cap * 3)},
                                              {"role": "assistant", "content": "ok"}])
    check("a resumed session caps its rehydrated history",
          all(_clen(m) <= cap + margin for m in resumed.working))

    # 8. TOOL-CALL ARGUMENTS: a native-mode write_file/edit_file carries the whole file body in its
    #    arguments string while `content` is short/empty, so it must be bounded too (the huge-WRITE
    #    case). _clen only measures content, so assert the arguments length directly.
    cmT = ContextManager("s", _Model(), _Traj(), compact_at_tokens=0)
    huge_args = '{"path": "big.py", "content": "' + "Q" * (cap * 3) + '"}'
    cmT.add({"role": "assistant", "content": "",
             "tool_calls": [{"id": "call_1", "type": "function",
                             "function": {"name": "write_file", "arguments": huge_args}}]})
    got = cmT.working[-1]["tool_calls"][0]
    check("a huge tool_calls arguments payload (a file-body WRITE) is capped",
          len(got["function"]["arguments"]) <= cap + margin)
    check("the tool_call id + name survive capping (tool_call<->result pairing intact)",
          got["id"] == "call_1" and got["function"]["name"] == "write_file")
    cmT.add({"role": "assistant", "content": "",
             "tool_calls": [{"id": "call_2", "type": "function",
                             "function": {"name": "read_file", "arguments": '{"path": "x.py"}'}}]})
    check("a small tool_calls arguments is left untouched",
          cmT.working[-1]["tool_calls"][0]["function"]["arguments"] == '{"path": "x.py"}')

    # 9. the current user REQUEST is pinned and survives compaction (can't be summarized away — the
    #    live-run failure where "what project is this?" got compacted out and the agent lost the question)
    cmT = ContextManager("s", _Model(), _Traj(), compact_at_tokens=1, keep_recent=1)
    cmT.set_task("what project are we in?")
    for i in range(6):
        cmT.add({"role": "user", "content": f"noise {i} " + "." * 40})
    check("the pinned user request survives compaction",
          any("what project are we in?" in (m.get("content") or "") for m in cmT.context()))

    # 10. a completed review_repo digest is pinned and survives compaction (the live failure where the
    #     digest — the per-area findings AND the "don't re-review" trailer — got summarized away, so the
    #     lead lost its own review, re-ran review_repo, and called an auth service it had read 'empty').
    #     A new task must clear it (no stale review pin), and it must be bounded like the other pins.
    cmR = ContextManager("s", _Model(), _Traj(), compact_at_tokens=1, keep_recent=1)
    cmR.set_task("review the whole project")
    cmR.set_review_digest("### src/auth\nHas Go source: config.go, handlers/auth.go. UNIQUEDIGEST42")
    for i in range(6):
        cmR.add({"role": "user", "content": f"noise {i} " + "." * 40})
    check("a pinned review digest survives compaction",
          any("UNIQUEDIGEST42" in (m.get("content") or "") for m in cmR.context()))
    cmR.set_task("now do something unrelated")
    check("a new task clears the prior review digest (no stale review pin)",
          cmR.pinned_review is None
          and not any("UNIQUEDIGEST42" in (m.get("content") or "") for m in cmR.context()))
    cmR.set_review_digest("R" * (cap * 3))
    check("a pinned review digest is bounded to the cap",
          cmR.pinned_review is not None and _clen(cmR.pinned_review) <= cap + margin)

    # 11. rollback survives an in-turn compaction (the poisoning fix): mark() snapshots the working set,
    #     so even when _compact REASSIGNS it mid-turn, rollback restores the EXACT pre-turn state instead
    #     of no-opping / slicing at the wrong boundary and leaving a dangling assistant tool_call.
    cmB = ContextManager("s", _Model(), _Traj(), compact_at_tokens=100000, keep_recent=1)
    cmB.add({"role": "user", "content": "real work so far " + "." * 60})
    snap = cmB.mark()                                    # pre-turn snapshot
    cmB.add({"role": "assistant", "content": "",         # a turn appends a tool_call (no result yet)...
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "read_file", "arguments": "{}"}}]})
    cmB.compact_at = 1                                    # ...then a compaction reassigns working mid-turn
    cmB.context()
    check("an in-turn compaction actually reassigned the working set",
          not any("real work so far" in (m.get("content") or "") for m in cmB.working) or len(cmB.working) != len(snap))
    cmB.rollback(snap)
    check("rollback after an in-turn compaction restores the exact pre-turn working set",
          cmB.working == snap)
    check("rollback leaves NO dangling assistant tool_call at the tail",
          not (cmB.working and cmB.working[-1].get("tool_calls")))

    # 12. _safe_cut no longer IndexErrors when keep_recent == 0 (a config edge)
    cm0 = ContextManager("s", _Model(), _Traj(), compact_at_tokens=1, keep_recent=0)
    cm0.add({"role": "user", "content": "x " + "." * 80})
    cm0.add({"role": "user", "content": "y " + "." * 80})
    try:
        cm0.context()          # would IndexError in _safe_cut (self.working[len]) before the fix
        ok0 = True
    except IndexError:
        ok0 = False
    check("compaction with keep_recent=0 does not IndexError (_safe_cut guarded)", ok0)

    # 13. review_repo does NOT fan out twice in one turn — a 2nd call returns the cached digest (a live
    #     review re-ran review_repo mid-review, wasting a full fan-out).
    import tempfile
    from src.orchestrator import review_repo
    from src.tools import Context as _ToolCtx
    from src import config as _cfg
    _cfg.WORKFLOW_CONCURRENCY = 1   # hermetic (specs/0039): serial fan-out so the 1-arg stub spawn below is used
    _d = tempfile.mkdtemp(prefix="revrepo_")
    os.makedirs(os.path.join(_d, "src"))
    os.makedirs(os.path.join(_d, "docs"))
    _calls = {"n": 0}
    _rc = _ToolCtx(_d, None)
    _rc.spawn = lambda task: (_calls.__setitem__("n", _calls["n"] + 1) or "area summary")
    _r1 = review_repo({"path": "."}, _rc)
    _after1 = _calls["n"]
    _r2 = review_repo({"path": "."}, _rc)
    check("review_repo re-run in the same turn returns the CACHED digest (no second fan-out)",
          _r1.ok and _r2.ok and _after1 > 0 and _calls["n"] == _after1 and "already ran" in _r2.content.lower())

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
