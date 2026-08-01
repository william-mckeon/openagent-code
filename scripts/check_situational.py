"""
scripts/check_situational.py

Acceptance harness for specs/0012 (Phase 12, P1) — situational-context injection, checked WITHOUT a
model or a network. build_env_context is pure (injected `now`); the refreshed pin is exercised on a
stub ContextManager (mirrors scripts/check_context.py). Run:

    python scripts/check_situational.py

Exits 0 only if every check holds.
"""
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, envcontext, prompts  # noqa: E402
from src.context import ContextManager  # noqa: E402
from src.toolset import active_tools  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


class _Traj:
    def __init__(self):
        self.turns = []

    def log_turn(self, m): self.turns.append(m)
    def log_compaction(self, *a): pass


class _Model:
    def summarize(self, msgs): return "a short summary"


def _clen(m):
    c = m.get("content")
    return len(c) if isinstance(c, str) else 0


def main():
    cap = config.MAX_MESSAGE_CHARS
    margin = 300
    fixed = datetime(2026, 1, 15, tzinfo=timezone.utc)  # injected clock -> deterministic date

    # 1. the builder carries the real environment, deterministically
    blk = envcontext.build_env_context("/work/repo", ["/ref/a", "/ref/b"], now=fixed)
    check("build_env_context carries cwd / os / shell / date / granted dirs",
          "/work/repo" in blk and "os:" in blk and "shell:" in blk
          and "2026-01-15" in blk and "/ref/a" in blk and "/ref/b" in blk)

    # 2. a huge granted-dir list is bounded (capped count + overflow marker)
    many = [f"/ref/dir{i}" for i in range(200)]
    big = envcontext.build_env_context("/w", many, now=fixed)
    check("a huge granted-dir list is bounded (capped + '+N more')",
          f"+{200 - envcontext._MAX_DIRS} more" in big
          and big.count("/ref/dir") <= envcontext._MAX_DIRS)

    # 3. set_env_context pins the block; it SURVIVES a forced compaction
    cm = ContextManager("s", _Model(), _Traj(), compact_at_tokens=1, keep_recent=1)
    cm.set_task("do a thing")
    cm.set_env_context(envcontext.build_env_context("/w", now=fixed) + "\nMARKER_ENV_1")
    for i in range(6):
        cm.add({"role": "user", "content": f"noise {i} " + "." * 40})
    check("the pinned env block survives compaction",
          any("MARKER_ENV_1" in (m.get("content") or "") for m in cm.context()))

    # 4. a second set REPLACES it (per-turn refresh, not pin-stale)
    cm.set_env_context(envcontext.build_env_context("/w", now=fixed) + "\nMARKER_ENV_2")
    ctx = cm.context()
    check("set_env_context REPLACES the block (refresh, not stale)",
          any("MARKER_ENV_2" in (m.get("content") or "") for m in ctx)
          and not any("MARKER_ENV_1" in (m.get("content") or "") for m in ctx))

    # 5. an oversized block is capped (specs/0009 bounded-fragment invariant)
    cm.set_env_context("E" * (cap * 3))
    check("an oversized env block is capped to the cap",
          cm.pinned_env is not None and _clen(cm.pinned_env) <= cap + margin)

    # 6. the flag is OFF BY DEFAULT (opt-in). Tested against the FALLBACK, independent of this repo's
    #    own .env — which a live ride may have turned ON (config loads .env at import, so reading
    #    config.SITUATIONAL_CONTEXT directly would reflect that, not the default).
    _saved = os.environ.pop("CODE_SITUATIONAL_CONTEXT", None)
    default_off = config._as_bool(os.environ.get("CODE_SITUATIONAL_CONTEXT", "false")) is False
    if _saved is not None:
        os.environ["CODE_SITUATIONAL_CONTEXT"] = _saved
    check("CODE_SITUATIONAL_CONTEXT defaults False when unset (opt-in)", default_off)

    # 7. dynamic state stays OUT of the cached (static) system prompt
    sp = prompts.build_system_prompt("native", active_tools())
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    check("dynamic state stays OUT of the static system prompt (no date / git / env block)",
          today not in sp and "git: branch" not in sp and "environment state" not in sp)

    # 8. Fix C (specs/0035): the env block is CAPTURED to the trajectory as role:'system' (not the old
    #    role:'user' turn), is NOT re-appended to the SENT working set, and the pin stays role:'user'
    #    (a mid-array system message risks a Bedrock Converse rejection, so only the CAPTURE role changed).
    tr = _Traj()
    cm2 = ContextManager("s", _Model(), tr)
    before_working = list(cm2.working)
    cm2.set_env_context("PINNED_ENV_BLOCK")
    cm2.log_env_capture("CAPTURED_ENV_BLOCK")
    captured = [m for m in tr.turns
                if m.get("role") == "system" and "CAPTURED_ENV_BLOCK" in (m.get("content") or "")]
    check("log_env_capture records the env block to the trajectory as role:'system'", len(captured) == 1)
    check("log_env_capture does NOT add the env block to the sent context",
          cm2.working == before_working
          and not any("CAPTURED_ENV_BLOCK" in (m.get("content") or "") for m in cm2.context()))
    check("the env PIN stays role:'user' and carries the block (Bedrock-safe; only the capture role changed)",
          cm2.pinned_env is not None and cm2.pinned_env.get("role") == "user"
          and "PINNED_ENV_BLOCK" in (cm2.pinned_env.get("content") or ""))
    # the header now self-identifies as system state (attribution fix)
    check("the env-block header self-identifies as auto-generated system state, not user input",
          "NOT a message from the user" in envcontext.build_env_context("/w", now=fixed))

    # 9. shell hints (specs/0046): OFF -> byte-identical block; ON -> PowerShell 5.1 rules, Windows-only.
    off = envcontext.build_env_context("/w", now=fixed)
    check("shell_hints OFF (default): no shell-rules line (byte-identical block)", "shell rules" not in off)
    on = envcontext.build_env_context("/w", now=fixed, shell_hints=True)
    if os.name == "nt":
        check("shell_hints ON + PowerShell: the PS 5.1 rules line is present + covers the Unix->PS mappings",
              "shell rules (PowerShell 5.1)" in on and "New-Item" in on and "&&" in on and "/dev/null" in on
              and "Get-ChildItem" in on and "Select-String" in on
              and "Stop-Process -Name python" in on)   # self-kill warning (specs/0050 pairing)
    else:
        check("shell_hints ON + non-Windows shell: no PS rules (PowerShell-specific)", "shell rules" not in on)
    _s = os.environ.pop("CODE_SHELL_HINTS", None)
    hints_default_off = config._as_bool(os.environ.get("CODE_SHELL_HINTS", "false")) is False
    if _s is not None:
        os.environ["CODE_SHELL_HINTS"] = _s
    check("CODE_SHELL_HINTS defaults False when unset (opt-in)", hints_default_off)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
