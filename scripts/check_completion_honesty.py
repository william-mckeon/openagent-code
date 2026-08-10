"""
scripts/check_completion_honesty.py

Acceptance harness for specs/0026 - completion & manifest honesty. Dep-free: no model, no network. Proves
the three seams and the byte-identical-when-off invariant:

  * grounding.unbacked_mutation_claim: flags a completed file-mutation claim on an EMPTY ledger; returns []
    the moment any real mutation happened, and for a hedged / present-tense / file-ref-less sentence.
  * agent._unapplied_manifest: an APPROVED manifest item with no matching mutation is unapplied; err toward
    applied (path OR a move's from); [] with no approved manifest.
  * planner.Decision.dropped: a native turn that came back EMPTY is flagged (so the agent labels it no_output),
    but a real answer / a tool-call turn / a no-schemas turn is not.
  * trajectory.log_manifest writes `applied` ONLY when not None; convert drops an approved-but-partial apply;
    manifest_unapplied + no_output are honest gate outcomes.
  * Flag OFF (both CODE_VERIFY_* false) is byte-identical: the grounding net never runs.

Run:  python scripts/check_completion_honesty.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, grounding, outcomes  # noqa: E402
from src import agent as agent_mod  # noqa: E402
from src import tools as tools_mod  # noqa: E402
from src.permissions import Permissions  # noqa: E402
from src.planner import NativePlanner  # noqa: E402
from src.trajectory import Trajectory, _ts  # noqa: E402
from train import convert  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


# -- planner fakes (no litellm) ----------------------------------------------------------------------------
class _FakeFn:
    def __init__(self, name, arguments="{}"):
        self.name, self.arguments = name, arguments


class _FakeTC:
    def __init__(self, name):
        self.id, self.type, self.function = "1", "function", _FakeFn(name)


class _FakeMsg:
    def __init__(self, content="", tool_calls=None, reasoning=None):
        self.content, self.tool_calls, self.reasoning_content = content, tool_calls or [], reasoning


class _FakeModel:
    def __init__(self, msg):
        self._msg, self.effort = msg, None

    def complete(self, messages, schemas, step):
        return self._msg


# -- grounding-record builders (mirror check_propose) ------------------------------------------------------
def _ss():
    return {"type": "session_start", "session_id": "s", "schema_version": "0.13.0", "tool_schemas": []}


def _user(t):
    return {"type": "turn", "message": {"role": "user", "content": t}}


def _mc(content, calls=()):
    tcs = [{"id": str(i), "name": n, "arguments": "{}"} for i, n in enumerate(calls)]
    return {"type": "model_call", "step": 0, "request": {"messages": [], "tools": []},
            "response": {"content": content, "reasoning": None, "tool_calls": tcs}}


def _manifest(approved, applied=None):
    r = {"type": "manifest", "items": [{"action": "update", "path": "a.py", "why": "x"}],
         "approved": approved, "mode": "propose"}
    if applied is not None:
        r["applied"] = applied
    return r


def _tout(turn, outcome="completed"):
    return {"type": "turn_outcome", "turn": turn, "outcome": outcome, "terminated": "final", "tool_calls": 1}


def _end(outcome="completed"):
    return {"type": "session_end", "outcome": outcome, "tool_calls": 1}


class _CapTraj:
    """A minimal duck for Trajectory.log_manifest: it only reads self.session_id and self._write."""
    session_id = "s"

    def __init__(self):
        self.records = []

    def _write(self, rec):
        self.records.append(rec)


class _GCtx:
    """The fields grounding.problems() reads for the deterministic path (no spawn -> no Tier-2)."""
    depth, cwd, mutations, fetched, _verified_ok, spawn = 0, "", {}, {}, False, None


def main():
    ws = os.path.realpath(tempfile.mkdtemp(prefix="honesty-ws-"))
    _saved = {k: getattr(config, k) for k in ("VERIFY_MANIFEST", "VERIFY_MUTATION_CLAIMS", "ENABLE_WEB",
                                              "VERIFY_GROUNDING_SEMANTIC")}
    config.ENABLE_WEB = False
    config.VERIFY_GROUNDING_SEMANTIC = False

    # =====================================================================================================
    # 1. the grounding net: a completed mutation claim on an EMPTY ledger
    # =====================================================================================================
    umc = grounding.unbacked_mutation_claim
    check("net: 'Frontend folder copied ...' with an EMPTY ledger is flagged",
          umc("Frontend folder copied to the working directory as requested.", {}) != [])
    check("net: 'I created `src/app.js` ...' with an EMPTY ledger is flagged",
          umc("I created `src/app.js` with the UI.", {}) != [])
    check("net: the SAME claim is NOT flagged once a real mutation exists (partial apply is the manifest gate)",
          umc("I created `src/app.js`.", {"src/app.js": "write"}) == [])
    check("net: a HEDGED / future claim is not flagged ('I will create ...')",
          umc("I will create the file next.", {}) == [] and umc("You can copy the folder yourself.", {}) == [])
    check("net: a present-tense description of what code DOES is not a completion claim",
          umc("The Dockerfile creates the image on build.", {}) == [])
    check("net: a completion boast with NO file/folder reference is not flagged",
          umc("I saved you some time.", {}) == [])
    check("net: a NEGATED read-only statement is not flagged (regression: live-smoke-test false positive)",
          umc("No files were created, edited, or deleted during this run.", {}) == []
          and umc("I did not create any files.", {}) == []
          and umc("Nothing was written to the folder.", {}) == []
          and umc("The task completed without creating a file.", {}) == [])
    check("net: DESCRIPTIVE prose (no first-person, no directional) is not flagged - the smoke-test class",
          umc("Running `python main.py list` prints all saved notes.", {}) == []       # 'saved' + `python main.py`
          and umc("The file created earlier is closed automatically.", {}) == []       # 'created'+'file', descriptive
          and umc("The config copied at build time is cached.", {}) == [])
    check("net: a real completion claim STILL flags (first-person OR directional survive the tightening)",
          umc("I created `src/app.js` for the UI.", {}) != []                           # first-person
          and umc("Frontend folder copied to the working directory as requested.", {}) != [])  # directional

    # =====================================================================================================
    # 2. problems() wiring + byte-identical flag-off
    # =====================================================================================================
    claim = "Frontend folder copied to the working directory as requested."
    config.VERIFY_MUTATION_CLAIMS = True
    check("problems(): with the flag ON, the unbacked mutation claim surfaces",
          any("mutation ledger is empty" in p for p in grounding.problems(claim, _GCtx())))
    config.VERIFY_MUTATION_CLAIMS = False
    check("problems(): with the flag OFF, it is byte-identical (net never runs)",
          grounding.problems(claim, _GCtx()) == [])

    # =====================================================================================================
    # 3. agent._unapplied_manifest: an approved manifest reconciled against the mutation ledger
    # =====================================================================================================
    c = tools_mod.Context(ws, Permissions("propose", {}, []))
    c.manifest = {"approved": True, "items": [
        {"action": "add", "path": "src/a.py"}, {"action": "update", "path": "src/b.py"},
        {"action": "move", "path": "src/new.py", "from": "src/old.py"}]}
    c.mutations = {}
    check("manifest: nothing applied -> all three items unapplied", len(agent_mod._unapplied_manifest(c)) == 3)
    tools_mod._record_mutation(c, "src/a.py", "write")
    check("manifest: applying src/a.py drops it from the unapplied list", len(agent_mod._unapplied_manifest(c)) == 2)
    tools_mod._record_mutation(c, "src/old.py", "delete")
    check("manifest: a move counts applied if EITHER endpoint landed (its `from` was deleted)",
          len(agent_mod._unapplied_manifest(c)) == 1)   # only src/b.py remains
    c.manifest["approved"] = False
    check("manifest: an UNAPPROVED manifest yields [] (nothing to reconcile)",
          agent_mod._unapplied_manifest(c) == [])
    c.manifest = None
    check("manifest: no manifest at all yields []", agent_mod._unapplied_manifest(c) == [])

    # =====================================================================================================
    # 4. planner.Decision.dropped: an empty native turn is flagged
    # =====================================================================================================
    schemas = [{"type": "function", "function": {"name": "read_file"}}]
    d_empty = NativePlanner(_FakeModel(_FakeMsg("", [])), schemas).step([], 0)
    check("dropped: an EMPTY native response (no content, no tool calls) is flagged dropped, no calls",
          d_empty.dropped is True and not d_empty.calls)
    d_real = NativePlanner(_FakeModel(_FakeMsg("Here is the answer.", [])), schemas).step([], 0)
    check("dropped: a real final answer is NOT dropped", d_real.dropped is False)
    d_call = NativePlanner(_FakeModel(_FakeMsg("", [_FakeTC("read_file")])), schemas).step([], 0)
    check("dropped: a tool-call turn is NOT dropped (it has calls)",
          d_call.dropped is False and len(d_call.calls) == 1)
    d_nosch = NativePlanner(_FakeModel(_FakeMsg("", [])), []).step([], 0)
    check("dropped: with NO schemas (json-style) an empty turn is not flagged dropped", d_nosch.dropped is False)

    # =====================================================================================================
    # 5. trajectory.log_manifest: the OPTIONAL applied field
    # =====================================================================================================
    cap = _CapTraj()
    Trajectory.log_manifest(cap, [{"action": "add", "path": "a.py"}], True, mode="propose", applied=None)
    check("log_manifest: applied=None -> the field is ABSENT (byte-identical)", "applied" not in cap.records[-1])
    Trajectory.log_manifest(cap, [{"action": "add", "path": "a.py"}], True, mode="propose", applied=False)
    check("log_manifest: applied=False -> the field is written", cap.records[-1].get("applied") is False)
    check("schema: SCHEMA_VERSION bumped to 0.13.0", Trajectory.SCHEMA_VERSION == "0.13.0")

    # =====================================================================================================
    # 6. corpus: an approved-but-partially-applied manifest is dropped; a legacy/full one is kept
    # =====================================================================================================
    repl = [_ss(), _user("t1"), _mc("clean", calls=["read_file"]), _tout(1),
            _user("t2"), _mc("proposed", calls=["propose_changes"]), _manifest(True, applied=False), _tout(2), _end()]
    check("convert: _unapplied_manifest_turns pinpoints the PARTIAL-apply turn (approved but applied=False)",
          convert._unapplied_manifest_turns(repl) == {2})
    check("convert: a partial-apply turn doesn't drop the whole session (clean turn 1 survives)",
          convert.is_trainable(repl)[0])
    check("convert: only the good turn becomes a row (the partial-apply turn is dropped)",
          len(convert.to_rows(repl, "as_sent")) == 1)
    full = [_ss(), _user("t"), _mc("proposed", calls=["propose_changes"]), _manifest(True, applied=True),
            _mc("done", calls=["edit_file"]), _tout(1), _end()]
    check("convert: an approved + FULLY-applied manifest turn is kept",
          convert._unapplied_manifest_turns(full) == set() and convert.is_trainable(full)[0])
    legacy = [_ss(), _user("t"), _mc("proposed", calls=["propose_changes"]), _manifest(True),  # no applied field
              _mc("done", calls=["edit_file"]), _tout(1), _end()]
    check("convert: a LEGACY approved manifest (no applied field) is NOT flagged partial (byte-identical)",
          convert._unapplied_manifest_turns(legacy) == set())

    # one-shot honesty labels
    one_partial = [_ss(), _user("t"), _mc("proposed", calls=["propose_changes"]), _manifest(True, applied=False), _end()]
    check("convert: a one-shot APPROVED-but-PARTIAL manifest is dropped as manifest_unapplied",
          convert.is_trainable(one_partial) == (False, "manifest_unapplied"))
    one_declined = [_ss(), _user("t"), _mc("proposed", calls=["propose_changes"]), _manifest(False), _end()]
    check("convert: a one-shot DECLINED manifest is still dropped as manifest_declined",
          convert.is_trainable(one_declined) == (False, "manifest_declined"))

    # =====================================================================================================
    # 7. outcomes: the two new labels are honest (won't be washed to completed)
    # =====================================================================================================
    check("outcomes: manifest_unapplied + no_output are honest gate outcomes",
          "manifest_unapplied" in outcomes.GATE_OUTCOMES and "no_output" in outcomes.GATE_OUTCOMES
          and outcomes.classify("manifest_unapplied", 3) == "manifest_unapplied"
          and outcomes.classify("no_output", 5) == "no_output")   # tool_calls>0 must NOT wash it to completed

    # =====================================================================================================
    # 8. specs/0070: a REPL turn that CRASHED (cli.py's except branch now stamps it 'error') is dropped, never
    #    washed to 'completed'. Before the fix the crash logged NO turn_outcome, so the session had
    #    tool_calls>0 -> session_end 'completed' and the legacy one-shot branch trained the truncated partial
    #    turn as a success (corpus poison). The 'error' turn_outcome routes convert to the per-turn path.
    # =====================================================================================================
    crashed = [_ss(), _user("t1"), _mc("", calls=["read_file"]), _tout(1, "error"), _end("completed")]
    check("convert: a crashed REPL turn (turn_outcome='error') is dropped, not trained as 'completed'",
          convert.is_trainable(crashed) == (False, "no_trainable_turn"))
    good_then_crash = [_ss(), _user("t1"), _mc("done", calls=["edit_file"]), _tout(1, "completed"),
                       _user("t2"), _mc("", calls=["read_file"]), _tout(2, "error"), _end("completed")]
    check("convert: a good turn SURVIVES beside a later crashed turn (per-turn honesty, counter aligned)",
          convert.is_trainable(good_then_crash)[0]
          and len(convert.to_rows(good_then_crash, "as_sent")) == 1)

    for k, v in _saved.items():
        setattr(config, k, v)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
