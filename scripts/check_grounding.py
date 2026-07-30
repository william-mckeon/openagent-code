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
    # Pin CODE_ENABLE_WEB OFF for the existing cases so they're byte-identical regardless of the local .env
    # (the web-source section below flips it on explicitly). specs/0024's web-citation branch is gated on it.
    _saved_web = config.ENABLE_WEB
    # specs/0026 + 0027 add flag-gated nets; pin them OFF so these baseline cases are hermetic against a local
    # .env that ENABLES them (each phase's own harness exercises the ON behavior). Without this, a .env with
    # CODE_VERIFY_GROUNDING_PATHS=true makes the deterministic present-path check also fire in semantic mode.
    _saved_gp, _saved_mc = config.VERIFY_GROUNDING_PATHS, config.VERIFY_MUTATION_CLAIMS
    _saved_gf = config.GROUND_SKIP_GREENFIELD
    config.ENABLE_WEB = False
    config.VERIFY_GROUNDING_PATHS = False
    config.VERIFY_MUTATION_CLAIMS = False
    config.GROUND_SKIP_GREENFIELD = False   # isolate from live .env: these tests use a small workspace + the verifier

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
    ctx_deep = _ctx(tmp, depth=1, spawn=lambda t, effort=None, label=None: deep_calls.append(t) or "UNGROUNDED: x -> y")
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
    check("semantic OFF: a bare basename (config.py, actually at src/config.py) is NOT false-flagged",
          grounding.problems("The config lives in `config.py`.", _ctx(tmp)) == [])
    config.VERIFY_GROUNDING_SEMANTIC = True

    # -- semantic ON: the verifier subagent is the AUTHORITY (not path-existence) ----
    check("semantic ON: verifier is the authority (a phantom path it clears -> clean)",
          grounding.problems("I made `docker/ghost.md`.",
                             _ctx(tmp, spawn=lambda t, effort=None, label=None: "GROUNDED")) == [])
    dir_calls = []
    grounding.problems("Auth is wired via `docker/auth` per the compose.",
                       _ctx(tmp, spawn=lambda t, effort=None, label=None: dir_calls.append(t) or "GROUNDED"))
    check("semantic ON: a DIRECTORY citation still spawns the verifier (no false-negative)",
          len(dir_calls) == 1)
    calls = []

    def spawn_flag(task, effort=None, label=None):
        calls.append(task)
        return "UNGROUNDED: init.sql is at docker/database -> compose mounts docker/auth/init.sql"
    out = grounding.problems("Init lives in `docker/README.md`.", _ctx(tmp, spawn=spawn_flag))
    check("semantic ON: verifier UNGROUNDED verdict is surfaced",
          len(calls) == 1 and len(out) == 1 and "docker/auth" in out[0])
    check("the verifier task carries the answer + the cited files",
          "GROUNDING VERIFIER" in calls[0] and "docker/README.md" in calls[0])
    check("the verifier task instructs it to check ABSENCE/emptiness claims by looking (S3 fix)",
          "ABSENCE" in calls[0] and "empty" in calls[0].lower())
    # PROPORTIONALITY is the verifier's LENIENCY + a non-hijacking challenge, NOT skipping the check: a
    # READ-ONLY run STILL fires the verifier (so a read-only review's wrong claim is caught), but a
    # GROUNDED verdict clears a fair overview — it is not turned into a repo audit.
    ro_calls = []
    check("read-only run fires the verifier; a GROUNDED verdict clears it (no over-flag)",
          grounding.problems("This project is documented in `docker/README.md`.",
                             _ctx(tmp, spawn=lambda t, effort=None, label=None: ro_calls.append(t) or "GROUNDED")) == []
          and len(ro_calls) == 1)
    check("Tier 2 empty/error verdict -> fail-open",
          grounding.semantic_problems("x", {"a.md"}, lambda t, effort=None, label=None: "") == []
          and grounding.semantic_problems("x", {"a.md"}, lambda t, effort=None, label=None: "(subagent error: boom)") == [])

    # -- ABSENCE claim spawns Tier 2 even with NO cited path (the live "auth has no Go source" miss) --
    check("absence_claim fires on 'has no Go source' / 'are empty' / 'is not implemented'",
          grounding.absence_claim("The auth service has no Go source, just docs and config.")
          and grounding.absence_claim("cmd/, internal/ are empty; there are no .go files.")
          and grounding.absence_claim("The endpoint is not implemented.")
          and grounding.absence_claim("The service cannot be built."))
    check("absence_claim does NOT fire on a normal factual answer",
          not grounding.absence_claim("This is a Next.js app; the homepage renders the marketing site.")
          and not grounding.absence_claim("Everything looks consistent and well-organized."))
    abs_calls = []
    out = grounding.problems(
        "The auth service has no Go source - it is just docs and config.",   # NO backticked path
        _ctx(tmp, spawn=lambda t, effort=None, label=None: abs_calls.append(t) or
             "UNGROUNDED: 'no Go source' -> src/auth holds 14 .go files"))
    check("semantic ON: an absence claim with NO cited path STILL spawns the verifier and surfaces it",
          len(abs_calls) == 1 and len(out) == 1 and ".go" in out[0])
    check("the verifier task handles the no-explicit-path case (find the target from the prose)",
          "no explicit file path" in abs_calls[0])
    noabs_calls = []
    grounding.problems("Everything looks consistent and well-organized.",   # no path, no absence claim
                       _ctx(tmp, spawn=lambda t, effort=None, label=None: noabs_calls.append(t) or "GROUNDED"))
    check("semantic ON: a normal no-path answer does NOT spawn (no proportionality regression)",
          len(noabs_calls) == 0)

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
    check("challenge() names the problem, stays targeted + points at the CURRENT request (not 'original')",
          "x.md" in ch and "whole repo" in ch and "current" in ch.lower() and "original request" not in ch.lower())
    # CAP: 20 flagged claims must not all be dumped on a weak model (that induced a repetition loop live) -
    # surface a handful, note the rest.
    _big = grounding.challenge([f"claim {i} not backed" for i in range(20)])
    check("challenge() caps the list (<= 6 bullets shown) and notes the remainder",
          _big.count("\n- ") <= 7 and "more unbacked" in _big and "claim 0" in _big and "claim 19" not in _big)
    check("an answer with no cited paths -> clean",
          grounding.problems("All good, nothing to cite here.", _ctx(tmp)) == [])

    # -- DETERMINISTIC absence contradiction (the src/auth/cmd main.go false-empty review) -------------
    adir = tempfile.mkdtemp(prefix="grd_abs_")
    os.makedirs(os.path.join(adir, "src", "auth", "cmd", "server"))
    open(os.path.join(adir, "src", "auth", "cmd", "server", "main.go"), "w", encoding="utf-8").write("package main\n")
    fc = grounding.absence_contradictions(
        "The entry point `src/auth/cmd/server/main.go` is missing — the cmd directory is empty.", adir)
    check("absence_contradictions flags a 'missing' file that EXISTS on disk",
          any("main.go" in m and "EXISTS" in m for m in fc))
    dc = grounding.absence_contradictions("The `src/auth/cmd` directory is empty.", adir)
    check("absence_contradictions flags an 'empty' directory that CONTAINS files",
          any("src/auth/cmd" in m for m in dc))
    check("absence_contradictions does NOT flag a genuinely-absent path",
          grounding.absence_contradictions("There is no `src/auth/ghost.go` in the repo.", adir) == [])
    check("absence_contradictions is sentence-scoped (a path outside the absence claim is safe)",
          grounding.absence_contradictions(
              "I edited `src/auth/cmd/server/main.go`. Separately, the docs folder is empty.", adir) == [])
    ac = grounding.problems("The `src/auth/cmd/server/main.go` file is missing.",
                            _ctx(adir, spawn=lambda t, effort=None, label=None: "GROUNDED"))
    check("problems() surfaces the deterministic contradiction even when the verifier says GROUNDED",
          any("main.go" in m for m in ac))

    # -- UNVERIFIED SUCCESS claim (specs/0020 net): "the tests now pass" with nothing that confirmed it --
    _RIDE = ("These class names are present in Button.tsx, matching the test suite's expectations, "
             "so the homepage tests now pass.")
    check("a 'tests now pass' claim with NO check this turn is flagged unverified",
          grounding.unverified_success_claim(_RIDE, verified=False))
    check("the SAME claim is clean once a check actually passed (verified=True)",
          grounding.unverified_success_claim(_RIDE, verified=True) == [])
    check("a HEDGED mention is not flagged ('run npm test to confirm', 'should pass', 'could not run')",
          grounding.unverified_success_claim("Run npm test to confirm the styles.", False) == []
          and grounding.unverified_success_claim("The tests should now pass.", False) == []
          and grounding.unverified_success_claim("I could not run the tests, so I have not verified this.", False) == [])
    check("a NEGATED / descriptive answer is not flagged",
          grounding.unverified_success_claim("The tests still fail on the icon case.", False) == []
          and grounding.unverified_success_claim("I added the secondary variant styles to Button.tsx.", False) == [])
    check("ran_check: a test/build command counts as verification, a plain command does not",
          grounding.ran_check("cd src/homepage && npm test") and grounding.ran_check("python -m pytest")
          and not grounding.ran_check("npm install") and not grounding.ran_check("git status"))
    # end-to-end through problems(): flagged when ctx says nothing was verified, clean when it was
    _c = _ctx(tmp)
    check("problems() flags the unverified success claim (ctx._verified_ok False)",
          any("PASSES" in m for m in grounding.problems(_RIDE, _c)))
    _c2 = _ctx(tmp)
    _c2._verified_ok = True
    check("problems() clears it once a check confirmed success (ctx._verified_ok True)",
          not any("PASSES" in m for m in grounding.problems(_RIDE, _c2)))

    # -- web sources (specs/0024): a cited URL is grounded by the ctx.fetched read-ledger ----------------
    config.ENABLE_WEB = True
    wf = _ctx(tmp)
    wf.fetched = {"https://docs.python.org/3/x": "print() writes to the text stream."}
    check("problems() does NOT flag a cited URL that WAS fetched",
          not any("never fetched" in m
                  for m in grounding.problems("Per https://docs.python.org/3/x, print writes to stdout.", wf)))
    wu = _ctx(tmp)
    check("problems() flags a cited URL that was NEVER fetched (phantom web citation)",
          any("never fetched" in m
              for m in grounding.problems("Per https://made-up.example/api it works.", wu)))
    # audit #6: a URL that legitimately contains parens (a Wikipedia _(programming_language) page) must not
    # be truncated on the citation side and then false-flagged against the full URL stored in ctx.fetched.
    _paren = "https://en.wikipedia.org/wiki/Python_(programming_language)"
    wp = _ctx(tmp)
    wp.fetched = {_paren: "Python is a programming language."}
    check("a cited+fetched PARENTHESIZED URL is grounded, not a false phantom",
          not any("never fetched" in m
                  for m in grounding.problems(f"Per {_paren}, Python is a language.", wp)))
    config.ENABLE_WEB = False
    wo = _ctx(tmp)
    check("with CODE_ENABLE_WEB off, a cited URL produces NO web-citation problem (byte-identical)",
          not any("never fetched" in m
                  for m in grounding.problems("Per https://made-up.example/api it works.", wo)))

    # the Tier-2 verifier task carries the fetched content (it has no on-disk copy) under the untrusted fence
    task = grounding._verifier_task("see https://a.com/p", set(), {"https://a.com/p": "print writes to stdout"})
    check("verifier task embeds fetched web content under the untrusted boundary",
          "FETCHED WEB SOURCES" in task and "https://a.com/p" in task
          and "print writes to stdout" in task and "untrusted data" in task.lower())
    check("verifier task with no fetched sources is unchanged (no web block)",
          "FETCHED WEB SOURCES" not in grounding._verifier_task("x", {"a.py"}))
    cf = grounding._cited_fetched("cites https://a.com/p only",
                                  {"https://a.com/p": "Y" * 9000, "https://b.com/q": "Z" * 9000})
    check("_cited_fetched keeps only cited+fetched URLs, each per-source bounded (tiered value)",
          set(cf) == {"https://a.com/p"} and len(cf["https://a.com/p"]["content"]) == grounding._WEB_SRC_CAP)

    config.ENABLE_WEB = _saved_web
    config.VERIFY_GROUNDING_PATHS, config.VERIFY_MUTATION_CLAIMS = _saved_gp, _saved_mc
    config.GROUND_SKIP_GREENFIELD = _saved_gf
    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
