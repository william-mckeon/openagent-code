"""
scripts/check_skills.py

Acceptance harness for specs/0008-skills.md — the skills system, checked WITHOUT a model or a
network. Loading + the harness-driven concern fan-out are deterministic, so a stub `ctx.spawn`
exercises the whole orchestrator path. Run:

    python scripts/check_skills.py

Exits 0 only if every check holds.
"""
import os
import sys
import tempfile
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["CODE_SKILLS"] = "true"  # set BEFORE importing config so config.SKILLS is on

from src import config, skills  # noqa: E402
from src.tools import Context  # noqa: E402
from src.toolset import active_tools  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def main():
    # -- loading + format ----------------------------------------------------
    cr = skills.load_skill("code-review")
    check("code-review skill loads", cr is not None and cr.name == "code-review")
    check("orchestrator carries the subskills glob", cr and cr.meta.get("subskills") == "code-review-*")
    leaf = skills.load_skill("code-review-correctness")
    check("a leaf skill loads with a body", leaf is not None and bool(leaf.body.strip()))
    check("unknown skill loads as None", skills.load_skill("does-not-exist") is None)

    subs = sorted(s.dirname for s in skills.find_subskills(cr))
    check("find_subskills = the 3 concerns, orchestrator EXCLUDED",
          subs == ["code-review-breaking-changes", "code-review-correctness", "code-review-tests"])

    # -- self-location -------------------------------------------------------
    check("skills_dir() resolves against INSTALL_ROOT",
          os.path.normpath(config.skills_dir()) == os.path.normpath(os.path.join(config.INSTALL_ROOT, "skills")))

    # -- gating --------------------------------------------------------------
    names = {t["name"] for t in active_tools()}
    check("run_skill is offered when CODE_SKILLS is on", "run_skill" in names)

    # -- run_skill: errors + leaf --------------------------------------------
    ctx = Context(ROOT, None)
    ctx.spawn, ctx.depth = None, 0
    r = skills.run_skill({"name": ""}, ctx)
    check("empty name -> teaching error", (not r.ok) and "Available skills" in r.content)
    r = skills.run_skill({"name": "nope"}, ctx)
    check("unknown name -> teaching error listing skills", (not r.ok) and "code-review" in r.content)
    r = skills.run_skill({"name": "code-review-correctness"}, ctx)
    check("leaf skill -> returns its body (no subagent)", r.ok and "correctness" in r.content.lower())
    r = skills.run_skill({"name": "code-review"}, ctx)  # orchestrator, spawn is None
    check("orchestrator without spawn -> clean refusal", (not r.ok) and "fan out" in r.content)
    ctx.spawn, ctx.depth = (lambda t: "x"), 1
    r = skills.run_skill({"name": "code-review"}, ctx)  # orchestrator as a child
    check("orchestrator at depth>=1 -> refuses to re-fan", not r.ok)

    # -- full orchestrator fan-out in a temp git repo (stub spawn) -----------
    tmp = tempfile.mkdtemp(prefix="skill_diff_")
    _git(tmp, "init")
    _git(tmp, "config", "user.email", "t@t")
    _git(tmp, "config", "user.name", "t")
    open(os.path.join(tmp, "a.py"), "w", encoding="utf-8").write("def f():\n    return 1\n")
    _git(tmp, "add", "."); _git(tmp, "commit", "-m", "init")
    # The changed line carries U+201D (right double quote): its UTF-8 bytes (E2 80 9D) contain
    # 0x9D, which is UNDEFINED in cp1252 — so this catches the Windows bug where git's UTF-8 diff
    # was decoded with the platform encoding and silently nulled (a real diff going unreviewed).
    # chr() keeps this test file ASCII while a.py still gets the real UTF-8 byte.
    _q = chr(0x201D)
    open(os.path.join(tmp, "a.py"), "w", encoding="utf-8").write(
        "def f():\n    return 2  # changed " + _q + "\n")
    calls = []

    def stub_spawn(task):
        calls.append(task)
        return "1. example finding (a.py:1)"

    ctx2 = Context(tmp, None)
    ctx2.spawn, ctx2.depth = stub_spawn, 0
    r = skills.run_skill({"name": "code-review"}, ctx2)
    check("fans out exactly one child per concern (3)", len(calls) == 3)
    check("each concern child received the diff + changed files",
          all("```diff" in t and "Changed files" in t and "return 2" in t for t in calls))
    check("digest contains all 3 concern sections + a synthesis footer",
          r.ok
          and all(c in r.content for c in ("code-review-correctness", "code-review-tests", "code-review-breaking-changes"))
          and "final review" in r.content.lower())
    check("meta reports the concern count", (r.meta or {}).get("concerns") == 3)

    # -- no-diff / non-git workspace -> nothing to review, no spawn ----------
    tmp2 = tempfile.mkdtemp(prefix="skill_nogit_")
    before = len(calls)
    ctx3 = Context(tmp2, None)
    ctx3.spawn, ctx3.depth = stub_spawn, 0
    r = skills.run_skill({"name": "code-review"}, ctx3)
    check("non-git workspace -> 'Nothing to review', spawns nothing",
          r.ok and "Nothing to review" in r.content and len(calls) == before)

    # -- C2: review-log leaf skill + bundled helper script -------------------
    rl = skills.load_skill("review-log")
    check("review-log skill loads as a leaf (no subskills)",
          rl is not None and not skills.find_subskills(rl))
    scr = skills.bundled_scripts(rl)
    check("bundled_scripts finds summarize_log.py",
          any(os.path.basename(p) == "summarize_log.py" for p in scr))
    ctxL = Context(ROOT, None)
    ctxL.spawn, ctxL.depth = None, 0
    r = skills.run_skill({"name": "review-log", "target": "logs/x.log"}, ctxL)
    check("run_skill(review-log) returns body + bundled script path + target",
          r.ok and "summarize_log.py" in r.content and "logs/x.log" in r.content)

    # -- C2: summarize_log.py must not MISPARSE (regression guards for the review findings) -------
    import subprocess as _sp
    summ = os.path.join(ROOT, "skills", "review-log", "scripts", "summarize_log.py")

    def _digest(loglines):
        p = os.path.join(tempfile.mkdtemp(prefix="skill_log_"), "s.log")
        open(p, "w", encoding="utf-8").write("\n".join(loglines) + "\n")
        return _sp.run([sys.executable, summ, p], capture_output=True,
                       encoding="utf-8", errors="replace").stdout

    dg = _digest([
        "12:00:01 INFO    [openagent_code.cli] REPL start | model=m mode=bypass workspace=/x",
        "12:00:02 INFO    [openagent_code.cli] turn 1 | you> do a thing",
        # a step whose RESULT snippet contains 'retrying' -> counts as a tool call, NOT a retry
        "12:00:03 INFO    [openagent_code.agent] step 1 [FAIL] run_command(cmd='pytest') -> ConnectionError: retrying...",
        # a read whose RESULT contains '.env' and a second ') ->' -> must NOT false-flag .env-touch
        "12:00:04 INFO    [openagent_code.agent] step 2 [ok] read_file(path='src/config.py') -> import os  # .env parse; def load() -> dict:",
        # the genuine model retry (a non-step line)
        "12:00:05 WARNING [openagent_code.model] model call TimeoutError (attempt 1/6) - retrying",
        # a clean 'According to the spec' answer -> must NOT flag reasoning-leak
        "12:00:06 INFO    [openagent_code.cli] result (terminated=False): According to the spec, the loader works.",
        # a prefix-less CONTINUATION line quoting a log -> must be SKIPPED (no phantom edit_file)
        "step 3 [FAIL] edit_file(path='x.py') -> phantom, retrying",
        "12:00:07 INFO    [openagent_code.cli] REPL end | 1 turn(s) tool_calls=2",
    ])
    check("step w/ 'retrying' in its result counts as a tool call, not a model retry",
          "tool calls: 2 |" in dg and "model retries: 1 |" in dg and "run_command [FAIL] x1" in dg)
    check("read whose result contains '.env' is NOT false-flagged .env-touch", "[.ENV-TOUCH]" not in dg)
    check("'According to the spec' is NOT flagged reasoning-leak", "[REASONING-LEAK]" not in dg)
    check("a prefix-less continuation line makes no phantom tool call", "edit_file" not in dg)

    dg2 = _digest(["12:00:01 INFO    [openagent_code.cli] result (terminated=False): Now we need to produce a review."])
    check("a real CoT-opening answer IS flagged reasoning-leak", "[REASONING-LEAK]" in dg2)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
