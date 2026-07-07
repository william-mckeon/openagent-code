"""
scripts/check_curate.py

Acceptance harness for specs/0011 — the offline corpus curation (train/curate.py), the convert.py
flag/exclude wiring, and the grounded_claims rubric check, on SYNTHETIC trajectory records, WITHOUT a
model or a network. Run:  python scripts/check_curate.py
Exits 0 only if every check holds.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, grounding  # noqa: E402
from train import curate, convert  # noqa: E402
from eval import rubric  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def _tc(tool, path, ok=True, result=""):
    return {"type": "tool_call", "tool": tool, "args": {"path": path}, "ok": ok, "result": result}


def _session(final, tcs):
    return ([{"type": "session_start", "session_id": "s", "cwd": "/w"}]
            + tcs + [{"type": "session_end", "final_text": final}])


def _session_full(final, tcs, outcome="completed"):
    """A session that also passes is_trainable's outcome/tool_calls gate + has a convertible model_call."""
    return ([{"type": "session_start", "session_id": "s", "cwd": "/w"},
             {"type": "model_call", "step": 0, "request": {"messages": []}, "response": {"content": final}}]
            + tcs
            + [{"type": "session_end", "final_text": final, "outcome": outcome, "tool_calls": len(tcs)}])


def main():
    # -- the shared existence oracle -----------------------------------------
    check("touched_paths reconstructs ENGAGED files (read/write), not listings",
          grounding.touched_paths([_tc("read_file", "a.py"),
                                   _tc("glob", ".", result="b.py"),
                                   _tc("write_file", "c.py")]) == {"a.py", "c.py"})

    # -- curation_verdict: grounded vs phantom -------------------------------
    ok, ung = curate.curation_verdict(_session("Fixed the bug in `app/main.py`.",
                                               [_tc("read_file", "app/main.py")]))
    check("grounded: a cited file that was read -> grounded", ok and ung == [])

    ok, ung = curate.curation_verdict(_session("See `app/ghost.py` for the issue.",
                                               [_tc("read_file", "app/main.py")]))
    check("ungrounded: a cited file never opened -> flagged", (not ok) and any("ghost" in u for u in ung))

    ok, ung = curate.curation_verdict(_session("The config is in `config/settings.py`.",
                                               [_tc("glob", ".", result="app/main.py\nconfig/settings.py\n")]))
    check("conservative: a path seen only in a tool listing is not flagged", ok and ung == [])

    check("normalization: a ./-prefixed citation matches the read path",
          curate.curation_verdict(_session("Updated `./docker/README.md`.",
                                           [_tc("read_file", "docker/README.md")]))[0])
    check("no cited paths -> grounded",
          curate.curation_verdict(_session("All good, nothing to cite.", []))[0])
    check("basename: a file read at src/config.py, cited as `config.py`, is grounded",
          curate.curation_verdict(_session("Raised the timeout in `config.py`.",
                                           [_tc("read_file", "src/config.py")]))[0])
    # A grounding VERIFIER subagent (depth>0) cites ABSENT paths by design — it must be skipped, not
    # flagged for doing its job (mirrors the runtime gate's depth-0-only rule).
    verifier = ([{"type": "session_start", "session_id": "s", "cwd": "/w", "depth": 1},
                 {"type": "session_end",
                  "final_text": "UNGROUNDED: answer claims `docker/auth/init.sql` -> compose mounts "
                                "`docker/database/init.sql`."}])
    verifier[1:1] = [_tc("read_file", "docker/database/init.sql"), _tc("read_file", "docker-compose.yml")]
    check("a depth>0 verifier trajectory is skipped (not flagged for citing absent paths)",
          curate.curation_verdict(verifier)[0])

    # -- convert.is_trainable EXCLUDE vs FLAG + the row tag ------------------
    config.CURATE = True
    ungrounded_sess = _session_full("See `app/ghost.py`.", [_tc("read_file", "app/main.py")])
    grounded_sess = _session_full("Fixed `app/main.py`.", [_tc("read_file", "app/main.py")])

    config.CURATE_MODE = "exclude"
    keep, reason = convert.is_trainable(ungrounded_sess)
    check("EXCLUDE mode drops an ungrounded session with a named reason",
          (not keep) and reason == "ungrounded_answer")
    check("EXCLUDE mode keeps a grounded session", convert.is_trainable(grounded_sess)[0])

    config.CURATE_MODE = "flag"
    check("FLAG mode keeps an ungrounded session (tag, don't drop)",
          convert.is_trainable(ungrounded_sess)[0])
    rows = convert.to_rows(ungrounded_sess, "raw")
    check("FLAG mode stamps the curation verdict on every row",
          bool(rows) and rows[0]["meta"].get("curation", {}).get("grounded") is False)

    # -- grounded_claims rubric check ----------------------------------------
    sc = rubric.score_turn(_session("Issue in `app/ghost.py`.", [_tc("read_file", "app/main.py")]),
                           {"grounded_claims": True})
    check("rubric grounded_claims FAILS a phantom citation",
          sc["checks"].get("grounded_claims") is False and bool(sc["ungrounded_claims"]))
    sc = rubric.score_turn(_session("Issue in `app/main.py`.", [_tc("read_file", "app/main.py")]),
                           {"grounded_claims": True})
    check("rubric grounded_claims PASSES a grounded citation",
          sc["checks"].get("grounded_claims") is True and not sc["ungrounded_claims"])
    sc = rubric.score_turn(_session("Issue in `main.py`.", [_tc("read_file", "app/main.py")]),
                           {"grounded_claims": True})
    check("rubric grounded_claims PASSES a basename citation of an engaged file",
          sc["checks"].get("grounded_claims") is True)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
