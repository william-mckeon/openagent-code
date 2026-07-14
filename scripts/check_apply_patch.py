"""
scripts/check_apply_patch.py

Acceptance harness for specs/0013 sub-phase B — the atomic, grammar-validated apply_patch tool, checked
WITHOUT a model or a network. apply_patch is exercised on temp workspaces; the toolset gate is toggled
on the config module. Run:

    python scripts/check_apply_patch.py

Exits 0 only if every check holds — including ATOMICITY (one bad op touches zero files).
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, patch  # noqa: E402
from src.tools import Context  # noqa: E402
from src.toolset import active_tools  # noqa: E402
from src.permissions import Permissions  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def _w(d, name, content):
    p = os.path.join(d, name)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return p


def _r(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _env(*lines):
    return "\n".join(lines) + "\n"


def main():
    config.GUARDIAN = False   # hermetic: decide_move here tests the mode logic, not the guardian (own harness)
    config.HOOKS = False       # and not hooks (their own harness)

    # patch_paths: the best-effort target extractor a permission hook uses (specs/0015 apply_patch hole).
    _pp = patch.patch_paths("*** Begin Patch\n*** Update File: docs/x.md\ngarbage\n"
                            "*** Delete File: a/b.txt\n*** Move File: c.py -> d.py\n*** End Patch")
    check("patch_paths: every touched path incl. move endpoints, despite a malformed hunk body",
          _pp == ["docs/x.md", "a/b.txt", "c.py", "d.py"])
    # -- 1. a valid multi-op patch: Add / Update / Delete / Move all land ------
    d = tempfile.mkdtemp(prefix="applypatch_")
    _w(d, "upd.py", "def f():\n    return 1\n")
    _w(d, "del.py", "obsolete\n")
    _w(d, "mov.py", "movable\n")
    r = patch.apply_patch({"patch": _env(
        "*** Begin Patch",
        "*** Add File: new.py", "+def g():", "+    return 2",
        "*** Update File: upd.py",
        "<<<<<<< SEARCH", "    return 1", "=======", "    return 42", ">>>>>>> REPLACE",
        "*** Delete File: del.py",
        "*** Move File: mov.py -> moved.py",
        "*** End Patch")}, Context(d, None))
    check("valid multi-op patch applies (ok)", r.ok)
    check("Add created the file with its content",
          os.path.isfile(os.path.join(d, "new.py")) and "return 2" in _r(os.path.join(d, "new.py")))
    check("Update edited in place", "return 42" in _r(os.path.join(d, "upd.py")))
    check("Delete removed the file", not os.path.exists(os.path.join(d, "del.py")))
    check("Move renamed (old gone, new carries the content)",
          (not os.path.exists(os.path.join(d, "mov.py"))) and "movable" in _r(os.path.join(d, "moved.py")))

    # -- 2. a malformed envelope -> refuse, write nothing ---------------------
    d2 = tempfile.mkdtemp(prefix="applypatch_")
    _w(d2, "keep.py", "orig\n")
    r = patch.apply_patch({"patch": "*** Add File: y.py\n+hi\n"}, Context(d2, None))  # no Begin/End
    check("malformed (no Begin Patch) -> refused, nothing written",
          (not r.ok) and (not os.path.exists(os.path.join(d2, "y.py")))
          and _r(os.path.join(d2, "keep.py")) == "orig\n")

    # -- 3. ATOMICITY: one bad op in the middle -> zero files touched ---------
    d3 = tempfile.mkdtemp(prefix="applypatch_")
    _w(d3, "a.py", "aaa\n")
    _w(d3, "b.py", "bbb\n")
    r = patch.apply_patch({"patch": _env(
        "*** Begin Patch",
        "*** Update File: a.py",
        "<<<<<<< SEARCH", "aaa", "=======", "AAA", ">>>>>>> REPLACE",
        "*** Delete File: b.py",
        "*** Update File: c.py",   # c.py does not exist -> validation fails during planning
        "<<<<<<< SEARCH", "ccc", "=======", "CCC", ">>>>>>> REPLACE",
        "*** End Patch")}, Context(d3, None))
    check("ATOMIC: a later bad op -> refused and NO earlier op applied",
          (not r.ok) and _r(os.path.join(d3, "a.py")) == "aaa\n"
          and os.path.isfile(os.path.join(d3, "b.py")) and _r(os.path.join(d3, "b.py")) == "bbb\n")

    # -- 4. an ambiguous hunk refuses the WHOLE patch -------------------------
    d4 = tempfile.mkdtemp(prefix="applypatch_")
    _w(d4, "amb.py", "x = 1\nx = 1\n")
    r = patch.apply_patch({"patch": _env(
        "*** Begin Patch", "*** Update File: amb.py",
        "<<<<<<< SEARCH", "x = 1", "=======", "x = 9", ">>>>>>> REPLACE",
        "*** End Patch")}, Context(d4, None))
    check("an ambiguous SEARCH hunk -> whole patch refused, file unchanged",
          (not r.ok) and _r(os.path.join(d4, "amb.py")) == "x = 1\nx = 1\n")

    # -- 5. toolset gating ----------------------------------------------------
    config.APPLY_PATCH = False
    check("apply_patch ABSENT from active_tools() when CODE_APPLY_PATCH off",
          "apply_patch" not in {t["name"] for t in active_tools()})
    config.APPLY_PATCH = True
    check("apply_patch PRESENT in active_tools() when CODE_APPLY_PATCH on",
          "apply_patch" in {t["name"] for t in active_tools()})

    # -- 6. binary files: Move renames byte-for-byte; Update refuses cleanly (the live PNG crash) ---
    d5 = tempfile.mkdtemp(prefix="applypatch_")
    raw = b"\x89PNG\r\n\x1a\n\x00\x01\x02 binary image bytes"   # 0x89 -> a naive utf-8 read would crash
    with open(os.path.join(d5, "icon.png"), "wb") as f:
        f.write(raw)
    ctx5 = Context(d5, None)
    r = patch.apply_patch({"patch": _env(
        "*** Begin Patch", "*** Move File: icon.png -> logo.png", "*** End Patch")}, ctx5)
    with open(os.path.join(d5, "logo.png"), "rb") as f:
        moved = f.read()
    check("Move of a BINARY file renames it byte-for-byte (no utf-8 crash)",
          r.ok and (not os.path.exists(os.path.join(d5, "icon.png"))) and moved == raw)
    r = patch.apply_patch({"patch": _env(
        "*** Begin Patch", "*** Update File: logo.png",
        "<<<<<<< SEARCH", "x", "=======", "y", ">>>>>>> REPLACE", "*** End Patch")}, ctx5)
    with open(os.path.join(d5, "logo.png"), "rb") as f:
        after = f.read()
    check("Update of a BINARY file refuses cleanly (no crash, file unchanged)",
          (not r.ok) and "binary" in r.content.lower() and after == raw)

    # -- 7. PERMISSION FENCE: apply_patch must inherit the fence + deny rules + plan mode, PER op ------
    #    (the critical audit finding: apply_patch was auto-allowed as 'read-only' and bypassed all of it).
    #    A Context carrying a real Permissions engine now re-gates every op inside patch.py.
    d6 = tempfile.mkdtemp(prefix="applypatch_")
    _w(d6, ".env", "SECRET=1\n")
    _w(d6, "keep.py", "orig\n")
    esc = os.path.realpath(os.path.join(d6, "..", "escape.txt"))

    ctx_bypass = Context(d6, Permissions("bypass", {}, []))
    r = patch.apply_patch({"patch": _env(
        "*** Begin Patch", "*** Add File: ../escape.txt", "+pwned", "*** End Patch")}, ctx_bypass)
    check("FENCE: apply_patch Add resolving outside the workspace -> refused, nothing written",
          (not r.ok) and (not os.path.exists(esc)))

    ctx_deny = Context(d6, Permissions("bypass", {"deny": ["delete_file(.env)"]}, []))
    r = patch.apply_patch({"patch": _env(
        "*** Begin Patch", "*** Delete File: .env", "*** End Patch")}, ctx_deny)
    check("DENY: apply_patch Delete .env -> refused even under bypass, .env untouched",
          (not r.ok) and os.path.isfile(os.path.join(d6, ".env")))

    ctx_plan = Context(d6, Permissions("plan", {}, []))
    r = patch.apply_patch({"patch": _env(
        "*** Begin Patch", "*** Update File: keep.py",
        "<<<<<<< SEARCH", "orig", "=======", "new", ">>>>>>> REPLACE", "*** End Patch")}, ctx_plan)
    check("PLAN: apply_patch (all-mutating) -> refused in read-only plan mode, file unchanged",
          (not r.ok) and _r(os.path.join(d6, "keep.py")) == "orig\n")

    r = patch.apply_patch({"patch": _env(
        "*** Begin Patch", "*** Add File: ok.py", "+x = 1", "*** End Patch")}, ctx_bypass)
    check("in-fence Add under bypass STILL applies (the gate doesn't over-block legitimate patches)",
          r.ok and os.path.isfile(os.path.join(d6, "ok.py")))

    # -- 8. APPLY-PHASE ATOMICITY: an op that fails DURING apply rolls back the ops already applied ----
    #    Op 1 updates a.py (applies); Op 2 adds b.py whose content is a lone surrogate that can't be
    #    UTF-8 encoded, so the write raises mid-apply. a.py must be restored and b.py must not survive.
    d7 = tempfile.mkdtemp(prefix="applypatch_")
    _w(d7, "a.py", "AAA-original\n")
    bad = _env("*** Begin Patch",
               "*** Update File: a.py",
               "<<<<<<< SEARCH", "AAA-original", "=======", "AAA-new", ">>>>>>> REPLACE",
               "*** Add File: b.py", "+\udc80bad", "*** End Patch")
    r = patch.apply_patch({"patch": bad}, Context(d7, None))
    check("apply-phase failure -> ROLLED BACK: refused, earlier Update restored, new Add file gone",
          (not r.ok) and "rolled back" in r.content.lower()
          and _r(os.path.join(d7, "a.py")) == "AAA-original\n"
          and not os.path.exists(os.path.join(d7, "b.py")))

    # -- 9. Move is EDIT-LEVEL: a rename applies in acceptEdits (no delete prompt/block), but a deny on
    #    the moved path still blocks it (the ride showed a routine favicon rename prompting per-file).
    d8 = tempfile.mkdtemp(prefix="applypatch_")
    _w(d8, "old.txt", "keep\n")
    ctx_ae = Context(d8, Permissions("acceptEdits", {}, []))
    r = patch.apply_patch({"patch": _env(
        "*** Begin Patch", "*** Move File: old.txt -> new.txt", "*** End Patch")}, ctx_ae)
    check("Move applies in acceptEdits headless (rename is edit-level, no delete prompt)",
          r.ok and os.path.exists(os.path.join(d8, "new.txt")) and not os.path.exists(os.path.join(d8, "old.txt")))
    _w(d8, ".env", "SECRET\n")
    ctx_dn = Context(d8, Permissions("acceptEdits", {"deny": ["delete_file(.env)"]}, []))
    r = patch.apply_patch({"patch": _env(
        "*** Begin Patch", "*** Move File: .env -> public.env", "*** End Patch")}, ctx_dn)
    check("Move of .env is still blocked by the delete_file(.env) deny (no rename-bypass)",
          (not r.ok) and os.path.isfile(os.path.join(d8, ".env")) and not os.path.exists(os.path.join(d8, "public.env")))

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
