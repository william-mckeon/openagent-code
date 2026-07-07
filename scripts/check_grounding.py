"""
scripts/check_grounding.py

Acceptance harness for specs/0010 — the grounding gate in src/grounding.py, checked WITHOUT a model
or a network. Tier 1 is pure/deterministic; Tier 2 is exercised with a STUB spawn (the real
run_subagent path is covered elsewhere). Run:  python scripts/check_grounding.py
Exits 0 only if every check holds.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["CODE_VERIFY_GROUNDING_SEMANTIC"] = "true"  # set BEFORE importing config

from src import config, grounding  # noqa: E402
from src.tools import Context  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def _ctx(cwd, mutations=None, depth=0, spawn=None):
    ctx = Context(cwd, None)
    ctx.mutations = mutations or {}
    ctx.depth = depth
    ctx.spawn = spawn
    return ctx


def main():
    # -- cited_paths: BROAD (verifier) vs STRICT (deterministic) --------------
    cp = grounding.cited_paths("See `docker/README.md` and `src/auth/init.sql`; `config` is not a path.")
    check("cited_paths pulls quoted local paths, ignores a non-path word",
          cp == {"docker/README.md", "src/auth/init.sql"})
    # BROAD (default) MUST include directories + non-listed extensions + dotted dirs, else the verifier
    # never spawns for those (the honest-but-wrong hole the redesign accidentally re-opened).
    broad = grounding.cited_paths(
        "wired via `docker/auth`, `infra/main.tf`, `schema.proto`, `docs.internal/x.md`")
    check("BROAD extraction includes directories + .tf/.proto + dotted dirs (verifier authority)",
          {"docker/auth", "infra/main.tf", "schema.proto", "docs.internal/x.md"} <= broad)
    # STRICT (deterministic) MUST exclude import/URL/date/scoped/absolute look-alikes (a hard existence
    # check would wrongly fail a correct answer that quotes them).
    strict = grounding.cited_paths(
        "imports `github.com/gorilla/mux`, `lodash/fp`, `@scope/pkg`; date `2024/01/15`; "
        "url `https://example.com/x`; abs `/etc/nginx/nginx.conf`; but real `docker/README.md`.",
        strict=True)
    check("STRICT extraction excludes imports/URLs/dates/scoped/absolute (only the real path survives)",
          strict == {"docker/README.md"})
    check("cited_paths on empty text is empty", grounding.cited_paths("") == set())

    # -- Tier 1 deterministic -------------------------------------------------
    tmp = tempfile.mkdtemp(prefix="ground_")
    os.makedirs(os.path.join(tmp, "docker"), exist_ok=True)
    open(os.path.join(tmp, "docker", "README.md"), "w", encoding="utf-8").write("# real\n")

    def exists(p):
        return os.path.exists(os.path.join(tmp, p))
    probs = grounding.deterministic_problems({"docker/README.md", "docker/ghost.md"}, exists)
    check("deterministic flags the phantom path only", len(probs) == 1 and "ghost.md" in probs[0])

    # -- problems() is TOP-LEVEL ONLY: a depth>0 agent is skipped entirely ----
    deep_calls = []
    ctx_deep = _ctx(tmp, depth=1, spawn=lambda t, effort=None: deep_calls.append(t) or "UNGROUNDED: x -> y")
    check("depth>0 skips grounding entirely (verifier can't self-ground)",
          grounding.problems("I updated `docker/ghost.md`.", ctx_deep) == [] and not deep_calls)

    # -- semantic OFF: deterministic fallback flags a phantom, clears a real one ----
    config.VERIFY_GROUNDING_SEMANTIC = False
    check("semantic OFF flags a phantom cited path",
          any("ghost.md" in x for x in grounding.problems("Made `docker/ghost.md`.", _ctx(tmp))))
    check("semantic OFF clears an existing cited path",
          grounding.problems("Updated `docker/README.md`.", _ctx(tmp)) == [])
    ctx_del = _ctx(tmp, mutations={"docker/old.md": "delete"})
    check("semantic OFF: a deleted (mutated) path is not flagged phantom",
          grounding.problems("Removed `docker/old.md`.", ctx_del) == [])
    config.VERIFY_GROUNDING_SEMANTIC = True

    # -- semantic ON: the verifier subagent is the AUTHORITY (not path-existence) ----
    check("semantic ON: verifier is the authority (a phantom path it clears -> clean)",
          grounding.problems("I made `docker/ghost.md`.", _ctx(tmp, spawn=lambda t, effort=None: "GROUNDED")) == [])
    dir_calls = []
    grounding.problems("Auth is wired via `docker/auth` per the compose.",
                       _ctx(tmp, spawn=lambda t, effort=None: dir_calls.append(t) or "GROUNDED"))
    check("semantic ON: a DIRECTORY citation still spawns the verifier (no false-negative)",
          len(dir_calls) == 1)
    calls = []

    def spawn_flag(task, effort=None):
        calls.append(task)
        return "UNGROUNDED: init.sql is at docker/database -> compose mounts docker/auth/init.sql"
    out = grounding.problems("Init lives in `docker/README.md`.", _ctx(tmp, spawn=spawn_flag))
    check("semantic ON: verifier UNGROUNDED verdict is surfaced",
          len(calls) == 1 and len(out) == 1 and "docker/auth" in out[0])
    check("the verifier task carries the answer + the cited files",
          "GROUNDING VERIFIER" in calls[0] and "docker/README.md" in calls[0])
    check("Tier 2 empty/error verdict -> fail-open",
          grounding.semantic_problems("x", {"a.md"}, lambda t, effort=None: "") == []
          and grounding.semantic_problems("x", {"a.md"}, lambda t, effort=None: "(subagent error: boom)") == [])

    # -- _parse_verdict tolerates markdown BUT preserves the claim body -------
    check("_parse_verdict handles a plain UNGROUNDED line",
          grounding._parse_verdict("UNGROUNDED: a -> b") == ["a -> b"])
    check("_parse_verdict handles **bold** and bullet-wrapped labels",
          grounding._parse_verdict("**UNGROUNDED**: a -> b") == ["a -> b"]
          and grounding._parse_verdict("- **UNGROUNDED**: c -> d") == ["c -> d"])
    check("_parse_verdict preserves claim decoration (no __init__.py / glob mangling)",
          grounding._parse_verdict("**UNGROUNDED**: uses `__init__.py` and globs `src/**/*.py`")
          == ["uses `__init__.py` and globs `src/**/*.py`"])
    check("_parse_verdict does NOT flag a GROUNDED line mentioning 'ungrounded' mid-sentence",
          grounding._parse_verdict("GROUNDED - no ungrounded claims found") == [])

    # -- challenge() + empty cases -------------------------------------------
    ch = grounding.challenge(["'x.md' - cited in the answer but not found in the workspace"])
    check("challenge() names the problem and says not-done", "x.md" in ch and "Do NOT report" in ch)
    check("an answer with no cited paths -> clean",
          grounding.problems("All good, nothing to cite here.", _ctx(tmp)) == [])

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
