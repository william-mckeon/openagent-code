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

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
