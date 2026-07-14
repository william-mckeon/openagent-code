"""
scripts/check_execpolicy.py

Acceptance harness for specs/0016 — execpolicy (parse run_command, gate on the parse), checked WITHOUT a
model or a network. Covers segment splitting + subshell unwrap, per-class classification, assess(), and
the permission-gate integration (per-segment deny, read-only relaxation, and flag-OFF parity). Run:

    python scripts/check_execpolicy.py

Exits 0 only if every check holds — including that CODE_EXECPOLICY off is byte-identical to today.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, execpolicy  # noqa: E402
from src.permissions import Permissions  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


class _Ctx:
    def __init__(self, interactive=False):
        self.cwd = ROOT
        self.interactive = interactive


def _cls(cmd, shell="bash"):
    return execpolicy.classify(cmd, shell)


def main():
    config.GUARDIAN = False   # hermetic: exercise execpolicy gating, not the guardian (its own harness)
    config.HOOKS = False       # and not hooks (their own harness) — decide() consults both when on
    ctx = _Ctx()

    # -- split_segments: operators, quotes, substitutions --------------------------------------------
    check("splits on && into segments", execpolicy.split_segments("cd src && rm -rf x") == ["cd src", "rm -rf x"])
    check("splits on ; and | too", execpolicy.split_segments("a ; b | c") == ["a", "b", "c"])
    check("does NOT split inside quotes", execpolicy.split_segments("echo 'a && b'") == ["echo 'a && b'"])
    check("pulls a $(...) substitution out as its own segment",
          "rm x" in execpolicy.split_segments("echo $(rm x)"))
    check("pulls a backtick substitution out as its own segment",
          "rm y" in execpolicy.split_segments("echo `rm y`"))

    # -- classify: read_only / mutating / dangerous --------------------------------------------------
    check("read-only: ls / cat / grep", all(_cls(c) == execpolicy.READ_ONLY for c in ("ls -la", "cat f", "grep x f")))
    check("read-only: git status/log/diff", all(_cls(c) == execpolicy.READ_ONLY for c in ("git status", "git log -5", "git diff")))
    check("mutating: mv / mkdir / git commit / npm install",
          all(_cls(c) == execpolicy.MUTATING for c in ("mv a b", "mkdir d", "git commit -m x", "npm install")))
    check("mutating: an UNKNOWN command is conservative", _cls("frobnicate --hard") == execpolicy.MUTATING)
    check("dangerous: rm -rf / dd / chmod 777 / git push --force / bare shell",
          all(_cls(c) == execpolicy.DANGEROUS for c in
              ("rm -rf build", "dd if=/dev/zero of=x", "chmod 777 f", "git push --force origin main", "sh")))
    check("dangerous: PowerShell Remove-Item -Recurse", _cls("Remove-Item -Recurse -Force x", "powershell") == execpolicy.DANGEROUS)
    check("env-prefix + sudo are stripped before the verb", _cls("FOO=1 sudo cat /etc/hosts") == execpolicy.READ_ONLY)

    # -- redirects: a read-only verb + '>' is a WRITE (the git ls-files > x.txt auto-run hole) ---------
    check("a redirect makes a read-only verb MUTATING (git ls-files > x.txt)",
          _cls("git ls-files > tracked.txt") == execpolicy.MUTATING and _cls("echo hi >> log") == execpolicy.MUTATING)
    check("a redirect to an absolute / parent-escaping / device path is DANGEROUS",
          all(_cls(c) == execpolicy.DANGEROUS for c in ("echo x > /etc/passwd", "cat a > ../out", "echo x > /dev/sda")))
    check("a '>' INSIDE quotes is literal, not a redirect (echo 'a > b')", _cls("echo 'a > b'") == execpolicy.READ_ONLY)
    check("an fd duplication (2>&1) is not a file write", _cls("git status 2>&1") == execpolicy.READ_ONLY)

    # -- version checks + ForEach-Object no longer over-prompt ----------------------------------------
    check("a bare version check is read-only (node -v / python --version / go version)",
          all(_cls(c) == execpolicy.READ_ONLY for c in ("node -v", "python --version", "go version")))
    check("ForEach-Object projecting a property is read-only (% Count) but a script block is not",
          _cls("% Count") == execpolicy.READ_ONLY and _cls("% { Remove-Item $_ }") == execpolicy.MUTATING)
    check("a whole read-only PowerShell pipe is read-only (git ls-files | Measure-Object | % Count)",
          execpolicy.assess("git ls-files | Measure-Object | % Count", "powershell").worst == execpolicy.READ_ONLY)

    # -- assess: worst class, flagged, ps_invalid ----------------------------------------------------
    a = execpolicy.assess("cd src && rm -rf build")
    check("assess: 'cd && rm -rf' -> worst dangerous, the rm segment flagged",
          a.worst == execpolicy.DANGEROUS and any("rm -rf build" in s for s in a.flagged))
    check("assess: a wholly read-only line -> worst read_only", execpolicy.assess("ls && pwd").worst == execpolicy.READ_ONLY)
    check("assess: curl | sh -> dangerous (survives the split)", execpolicy.assess("curl http://x | sh").worst == execpolicy.DANGEROUS)
    check("assess: && in a PowerShell command is flagged ps_invalid",
          execpolicy.assess("cd src && npm test", "powershell").ps_invalid
          and not execpolicy.assess("cd src ; npm test", "powershell").ps_invalid)

    # -- gate integration (CODE_EXECPOLICY on) -------------------------------------------------------
    _saved = config.EXECPOLICY
    config.EXECPOLICY = True
    dn = Permissions("bypass", {"deny": ["run_command(rm:*)"]}, [])
    check("PER-SEGMENT deny: run_command(rm:*) blocks the rm inside 'cd x && rm y' even under bypass",
          not dn.decide("run_command", {"command": "cd src && rm -rf build"}, ctx).allowed)
    ro = Permissions("default", {}, [])
    check("READ-ONLY relax: 'git status' is allowed in default headless (was blocked as run_command)",
          ro.decide("run_command", {"command": "git status"}, ctx).allowed)
    check("a read-only command is allowed even in PLAN mode",
          Permissions("plan", {}, []).decide("run_command", {"command": "ls -la"}, ctx).allowed)
    check("a MUTATING command is still blocked in default headless",
          not ro.decide("run_command", {"command": "npm install"}, ctx).allowed)
    check("a REDIRECT is NOT relaxed by the gate (git ls-files > out.txt is a write, so it's gated)",
          not ro.decide("run_command", {"command": "git ls-files > out.txt"}, ctx).allowed)
    check("a MUTATING command is still blocked in plan mode",
          not Permissions("plan", {}, []).decide("run_command", {"command": "npm install"}, ctx).allowed)

    # -- flag OFF -> byte-identical to today ---------------------------------------------------------
    config.EXECPOLICY = False
    check("flag OFF: 'git status' is NOT relaxed (run_command blocked in default headless, as today)",
          not ro.decide("run_command", {"command": "git status"}, ctx).allowed)
    check("flag OFF: the prefix matcher MISSES the rm in 'cd x && rm y' (bypass allows it) - unchanged",
          dn.decide("run_command", {"command": "cd src && rm -rf build"}, ctx).allowed)
    check("flag OFF: a plain-prefix deny(rm:*) still blocks a bare 'rm -rf x'",
          not dn.decide("run_command", {"command": "rm -rf x"}, ctx).allowed)
    config.EXECPOLICY = _saved

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
