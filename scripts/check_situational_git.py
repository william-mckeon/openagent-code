"""
scripts/check_situational_git.py

Acceptance harness for specs/0012 (Phase 12, P2) — the per-turn git line, checked WITHOUT a model or a
network. `_format_git` is pure (canned porcelain); the git runner is injectable so no real git binary is
needed, and the real `_git_status` is exercised only against a temp NON-repo (tolerant of git being
absent). Run:

    python scripts/check_situational_git.py

Exits 0 only if every check holds.
"""
import os
import sys
import tempfile
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, envcontext  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def main():
    cap = config.MAX_MESSAGE_CHARS
    fixed = datetime(2026, 1, 15, tzinfo=timezone.utc)

    # 1. _format_git (pure): branch + total count + a file list capped to K with a '+N more' marker
    porcelain = "\n".join(f" M src/file{i}.py" for i in range(60))
    line = envcontext._format_git("main", porcelain)
    check("_format_git shows branch + total changed count",
          "branch main" in line and "60 changed" in line)
    check("_format_git caps the file list ('+N more') and stays bounded",
          f"+{60 - envcontext._MAX_GIT_FILES} more" in line
          and line.count("src/file") <= envcontext._MAX_GIT_FILES and len(line) <= cap)
    check("_format_git on a clean tree shows 0 changed, no file list",
          envcontext._format_git("dev", "") == "git: branch dev | 0 changed")
    check("_format_git with no branch marks detached",
          "branch (detached)" in envcontext._format_git("", " M x.py"))

    # 2. include_git=False emits no git line and NEVER invokes the runner
    called = []
    blk = envcontext.build_env_context("/w", include_git=False,
                                       git_status_fn=lambda c: called.append(c) or "git: X", now=fixed)
    check("include_git=False: no git line, runner never called",
          "git:" not in blk and not called)

    # 3. the REAL _git_status against a fresh non-repo returns None and does not raise
    tmp = tempfile.mkdtemp(prefix="sit_nogit_")
    check("real _git_status on a non-repo returns None (no raise)",
          envcontext._git_status(tmp) is None)

    # 4. a git_status_fn that raises is swallowed — no git line, block still returned
    def boom(_):
        raise RuntimeError("git blew up")
    blk = envcontext.build_env_context("/w", include_git=True, git_status_fn=boom, now=fixed)
    check("a raising git runner is swallowed (no git line, block still returned)",
          isinstance(blk, str) and "cwd:" in blk and "git:" not in blk)

    # 5. the branch line is present when the runner reports one
    blk = envcontext.build_env_context("/w", include_git=True,
                                       git_status_fn=lambda c: "git: branch feature/x | 2 changed",
                                       now=fixed)
    check("include_git=True surfaces the runner's branch line",
          "git: branch feature/x | 2 changed" in blk)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
