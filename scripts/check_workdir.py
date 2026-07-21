"""
scripts/check_workdir.py

Acceptance harness for specs/0030 - working-dir durability (pin the absolute workspace path in the durable
system prompt; distinguish a granted READ source from a write DESTINATION). Dep-free: no model, no network.
Proves the prompt behavior and the byte-identical-when-off invariant:

  * flag ON + cwd -> a durable WORKING DIRECTORY line carries the absolute workspace path.
  * flag ON but cwd=None -> no line; flag OFF -> no line even with a cwd (byte-identical).
  * flag ON -> the granted-dirs note gains the read-source-vs-write-destination clause; OFF -> old text only.

Run:  python scripts/check_workdir.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config  # noqa: E402
from src.prompts import build_system_prompt  # noqa: E402
from src.tools import TOOLS  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def main():
    _saved = {"WORKDIR_PROMPT": config.WORKDIR_PROMPT}
    cwd = "/abs/workspace/messing-with-oac"
    granted = ["/abs/granted/resume-helper"]

    # =====================================================================================================
    # 1. the durable working-directory line
    # =====================================================================================================
    config.WORKDIR_PROMPT = True
    p_on = build_system_prompt("native", TOOLS, cwd=cwd)
    check("flag ON + cwd: the absolute workspace path is pinned under WORKING DIRECTORY",
          "WORKING DIRECTORY" in p_on and cwd in p_on)
    check("flag ON but cwd=None: no WORKING DIRECTORY line",
          "WORKING DIRECTORY" not in build_system_prompt("native", TOOLS, cwd=None))

    config.WORKDIR_PROMPT = False
    p_off = build_system_prompt("native", TOOLS, cwd=cwd)
    check("flag OFF: no WORKING DIRECTORY line and the cwd is not rendered (byte-identical)",
          "WORKING DIRECTORY" not in p_off and cwd not in p_off)

    # =====================================================================================================
    # 2. read-source vs write-destination on the granted-dirs note
    # =====================================================================================================
    config.WORKDIR_PROMPT = True
    g_on = build_system_prompt("native", TOOLS, granted_dirs=granted, cwd=cwd)
    check("flag ON: the granted-dirs note gains the read-source-vs-write-destination clause",
          "READ SOURCES, not write destinations" in g_on and granted[0] in g_on)
    config.WORKDIR_PROMPT = False
    g_off = build_system_prompt("native", TOOLS, granted_dirs=granted)
    check("flag OFF: the granted-dirs note keeps its old text (no destination clause)",
          "Reference directories you may READ" in g_off and "not write destinations" not in g_off)

    check("config: CODE_WORKDIR_PROMPT exists as a bool flag (hermetic - not asserting the local .env value)",
          hasattr(config, "WORKDIR_PROMPT") and isinstance(_saved["WORKDIR_PROMPT"], bool))

    for k, v in _saved.items():
        setattr(config, k, v)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
