"""
scripts/check_specs.py

Acceptance harness for specs/0025 - spec-first (author a design+acceptance spec, approve, build against it,
gate 'done' on the acceptance items). Dep-free: no model, no network (pure store + tool + gate + corpus
assertions), modeled on scripts/check_todos.py + scripts/check_propose.py.

Run:  python scripts/check_specs.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, specstore, outcomes  # noqa: E402
from src import tools as tools_mod  # noqa: E402
from src.agent import _unmet_acceptance  # noqa: E402
from src.toolset import active_tools  # noqa: E402
from src.prompts import build_system_prompt  # noqa: E402
from src.tools import TOOLS  # noqa: E402
from src.permissions import Permissions, MUTATING  # noqa: E402
from train import convert  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


class _Ctx:
    def __init__(self, cwd):
        self.cwd = cwd


def _tmp(prefix="specs-"):
    return os.path.realpath(tempfile.mkdtemp(prefix=prefix))


# -- trajectory-record builders (mirror check_propose) -----------------------------------------------------
def _ss():
    return {"type": "session_start", "session_id": "s", "schema_version": "0.11.0", "tool_schemas": []}


def _user(t):
    return {"type": "turn", "message": {"role": "user", "content": t}}


def _mc(content, calls=()):
    tcs = [{"id": str(i), "name": n, "arguments": "{}"} for i, n in enumerate(calls)]
    return {"type": "model_call", "step": 0, "request": {"messages": [], "tools": []},
            "response": {"content": content, "reasoning": None, "tool_calls": tcs}}


def _spec(approved, met):
    return {"type": "spec", "title": "S", "goal": "g", "acceptance": [{"content": "a", "done": met}],
            "non_goals": [], "approved": approved, "acceptance_met": met}


def _tout(turn, outcome="completed"):
    return {"type": "turn_outcome", "turn": turn, "outcome": outcome, "terminated": "final", "tool_calls": 1}


def _end(outcome="completed"):
    return {"type": "session_end", "outcome": outcome, "tool_calls": 1}


def main():
    _saved = {k: getattr(config, k) for k in ("SPEC_FIRST", "PROPOSE", "HOOKS", "GUARDIAN")}
    config.PROPOSE = config.HOOKS = config.GUARDIAN = False

    # -- the store: round-trip, numbering, active selection, acceptance ----------------------------------
    spec = {"number": 3, "title": "My Spec", "goal": "line1\nline2",
            "acceptance": [{"content": "a", "done": False}, {"content": "b", "done": True}], "non_goals": ["ng1"]}
    rt = specstore.parse(specstore.render(spec))
    check("specstore round-trips number/title/goal/acceptance(+done)/non_goals",
          rt["number"] == 3 and rt["title"] == "My Spec" and rt["goal"] == "line1\nline2"
          and rt["acceptance"] == spec["acceptance"] and rt["non_goals"] == ["ng1"])
    hand = ("# 2 - Hand Spec\n\n## Goal\n\nmy goal\n\n## Acceptance\n\n- [ ] todo\n- [X] done item\n"
            "* [~] in progress\n\n## Non-goals\n\n- later\n")
    hs = specstore.parse(hand)
    check("parse is lenient (hyphen H1, [X]/[~] marks, * bullets; only [x]/[X] = met)",
          hs["number"] == 2 and hs["title"] == "Hand Spec"
          and [it["done"] for it in hs["acceptance"]] == [False, True, False])

    wsp = _tmp()
    check("next_number is 1 on an empty/missing dir", specstore.next_number(wsp) == 1)
    p1 = specstore.save(wsp, {"title": "First!", "goal": "g", "acceptance": [{"content": "x", "done": False}], "non_goals": []})
    check("save writes NNNN-slug.md (slugified, zero-padded)", os.path.basename(p1) == "0001-first.md")
    check("next_number increments after a save", specstore.next_number(wsp) == 2)
    specstore.save(wsp, {"title": "Second", "goal": "g2", "acceptance": [{"content": "y", "done": False}], "non_goals": []})
    active = specstore.load_active(wsp)
    check("load_active returns the HIGHEST-numbered spec", active and active["title"] == "Second")
    check("load_active on an empty/missing dir is None (never raises)", specstore.load_active(_tmp()) is None)
    check("re-save reuses the number (idempotent, same file)",
          specstore.save(wsp, {**active, "goal": "edited"}) == os.path.join(specstore.specs_dir(wsp), "0002-second.md"))

    acc = [{"content": "a", "done": False}, {"content": "b", "done": False}]
    acc2, err = specstore.set_acceptance(acc, "1", True)
    check("set_acceptance by number flips done", err is None and acc2[0]["done"] is True)
    check("all_met False while an item is outstanding; outstanding lists it",
          not specstore.all_met(acc2) and len(specstore.outstanding(acc2)) == 1)
    acc3, _ = specstore.set_acceptance(acc2, "b", True)   # by exact text
    check("all_met True once every item is done", specstore.all_met(acc3))
    check("active_text is '' when there is no spec", specstore.active_text(_tmp()) == "")
    check("active_text renders the active spec markdown", "Second" in specstore.active_text(wsp))

    # -- the tool: validate / approve / decline / headless / top-level-only / mark -----------------------
    config.SPEC_FIRST = True

    def make_ctx(interactive, answer=None, depth=0):
        c = tools_mod.Context(_tmp("specws-"), Permissions("default", {}, []))
        c.depth, c.interactive, c.session_id = depth, interactive, "sess"
        c.ask = (lambda q: answer) if answer is not None else None
        return c

    cv = make_ctx(False)
    check("write_spec refuses a missing title", tools_mod.write_spec({"goal": "g", "acceptance": ["a"]}, cv).ok is False)
    check("write_spec refuses a missing goal", tools_mod.write_spec({"title": "t", "acceptance": ["a"]}, cv).ok is False)
    check("write_spec refuses an empty acceptance list",
          tools_mod.write_spec({"title": "t", "goal": "g", "acceptance": []}, cv).ok is False)
    check("write_spec is top-level only (depth>0 refused)",
          tools_mod.write_spec({"title": "t", "goal": "g", "acceptance": ["a"]}, make_ctx(False, depth=1)).ok is False)

    ch = make_ctx(False)
    rh = tools_mod.write_spec({"title": "My Feature", "goal": "do x", "acceptance": ["item one", "item two"]}, ch)
    check("write_spec headless: draft written, NOT approved, implementation should not begin",
          rh.ok is False and ch.spec and ch.spec["approved"] is False and os.path.isfile(ch.spec["path"]))

    cd = make_ctx(True, answer="n")
    rd = tools_mod.write_spec({"title": "F", "goal": "g", "acceptance": ["a"]}, cd)
    check("write_spec DECLINE: not approved, draft still on disk",
          rd.ok is False and cd.spec["approved"] is False and os.path.isfile(cd.spec["path"]))

    ca = make_ctx(True, answer="y")
    ra = tools_mod.write_spec({"title": "F", "goal": "g", "acceptance": ["build the thing", "test it"]}, ca)
    check("write_spec APPROVE: ctx.spec approved with the acceptance items",
          ra.ok and ca.spec["approved"] is True and len(ca.spec["acceptance"]) == 2)
    rm = tools_mod.write_spec({"action": "done", "item": "1"}, ca)
    check("write_spec done: marks the item met on ctx.spec + rewrites the file",
          rm.ok and ca.spec["acceptance"][0]["done"] is True
          and specstore.load_active(ca.cwd)["acceptance"][0]["done"] is True)
    check("write_spec done: one acceptance item still outstanding",
          len(specstore.outstanding(ca.spec["acceptance"])) == 1)

    # -- the acceptance gate logic (agent._unmet_acceptance) ---------------------------------------------
    class _G:
        pass
    g = _G(); g.spec = {"acceptance": [{"content": "a", "done": True}, {"content": "b", "done": False}]}
    check("_unmet_acceptance returns items not marked done", _unmet_acceptance(g) == ["b"])
    g.spec["acceptance"][1]["done"] = True
    check("_unmet_acceptance is empty once every item is done", _unmet_acceptance(g) == [])
    g.spec = None
    check("_unmet_acceptance is empty with no spec (nothing to hold)", _unmet_acceptance(g) == [])

    # -- flag gate + prompt injection + non-mutating -----------------------------------------------------
    config.SPEC_FIRST = True
    check("CODE_SPEC_FIRST on -> write_spec in active_tools()",
          any(t["name"] == "write_spec" for t in active_tools()))
    config.SPEC_FIRST = False
    check("CODE_SPEC_FIRST off -> write_spec absent (byte-identical toolset)",
          not any(t["name"] == "write_spec" for t in active_tools()))
    config.SPEC_FIRST = True

    with_spec = build_system_prompt("native", TOOLS, spec="SPECTOKEN-123")
    check("spec text is injected under an 'Active spec' heading",
          "SPECTOKEN-123" in with_spec and "Active spec" in with_spec)
    check("spec=None leaves the prompt spec-free", "Active spec" not in build_system_prompt("native", TOOLS, spec=None))
    check("an empty/whitespace spec injects NO block",
          "Active spec" not in build_system_prompt("native", TOOLS, spec="   "))

    check("write_spec is NOT in permissions.MUTATING", "write_spec" not in MUTATING)
    check("write_spec is permitted in PLAN mode (non-mutating)",
          Permissions("plan", {}, []).decide("write_spec", {"title": "t"}, _Ctx(wsp)).allowed)

    # -- corpus: a declined/unmet spec is dropped; approved+met is kept ----------------------------------
    check("outcomes: spec_declined + acceptance_unmet are honest gate outcomes",
          "spec_declined" in outcomes.GATE_OUTCOMES and "acceptance_unmet" in outcomes.GATE_OUTCOMES
          and outcomes.classify("spec_declined", 3) == "spec_declined"
          and outcomes.classify("acceptance_unmet", 3) == "acceptance_unmet")
    repl = [_ss(), _user("t1"), _mc("clean", calls=["read_file"]), _tout(1),
            _user("t2"), _mc("proposed", calls=["write_spec"]), _spec(False, False), _tout(2), _end()]
    check("convert._unmet_spec_turns pinpoints the declined-spec turn", convert._unmet_spec_turns(repl) == {2})
    keep, reason = convert.is_trainable(repl)
    check("a declined-spec turn doesn't drop the whole session (clean turn 1 survives)", keep and reason == "kept")
    check("only the good turn's step becomes a row (the declined-spec turn is dropped)",
          len(convert.to_rows(repl, "as_sent")) == 1)
    kept = [_ss(), _user("t"), _mc("proposed", calls=["write_spec"]), _spec(True, True),
            _mc("done", calls=["edit_file"]), _tout(1), _end()]
    check("an APPROVED + MET spec turn is kept",
          convert._unmet_spec_turns(kept) == set() and convert.is_trainable(kept)[0])
    check("a one-shot DECLINED spec is dropped as spec_declined",
          convert.is_trainable([_ss(), _user("t"), _mc("p", calls=["write_spec"]), _spec(False, False), _end()])
          == (False, "spec_declined"))
    check("a one-shot APPROVED-but-UNMET spec is dropped as acceptance_unmet",
          convert.is_trainable([_ss(), _user("t"), _mc("p", calls=["write_spec"]), _spec(True, False), _end()])
          == (False, "acceptance_unmet"))

    for k, v in _saved.items():
        setattr(config, k, v)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
