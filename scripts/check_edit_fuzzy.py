"""
scripts/check_edit_fuzzy.py

Acceptance harness for specs/0013 sub-phase A — the SAFE fuzzy fallback under exact-match edit_file,
checked WITHOUT a model or a network. editmatch.resolve is pure; edit_file is exercised on temp files
with the CODE_EDIT_FUZZY flag toggled directly on the config module. Run:

    python scripts/check_edit_fuzzy.py

Exits 0 only if every check holds — including that flag-off behavior is today's exact-or-fail verbatim.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, editmatch  # noqa: E402
from src.tools import edit_file, write_file, Context  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def _write(d, name, content):
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return p


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def main():
    M, A, NF = editmatch.MATCH, editmatch.AMBIGUOUS, editmatch.NOT_FOUND

    # -- editmatch.resolve() cascade (pure, no files) -------------------------
    text = "def f():\n    return 1\n\ndef g():\n    return 2\n"
    r = editmatch.resolve(text, "    return 1\n")
    check("exact unique -> MATCH (strategy=exact)", r.status == M and r.strategy == "exact")
    check("exact multiple -> AMBIGUOUS (never guess)",
          editmatch.resolve("a = 1\na = 1\n", "a = 1\n").status == A)

    wtext = "def f():\n    if x:\n        return 1\n    return 0\n"
    r = editmatch.resolve(wtext, "  if x:\n      return 1\n")  # 2-sp / 6-sp vs real 4-sp / 8-sp
    check("whitespace/indentation-only miss -> MATCH (strategy=whitespace) at the real span",
          r.status == M and r.strategy == "whitespace"
          and wtext[r.start:r.end] == "    if x:\n        return 1\n")

    stext = "def f():\n    total = compute_sum(items)\n    return total\n"
    r = editmatch.resolve(stext, "    total = compute_sam(items)\n")  # 'sam' typo vs 'sum'
    check("near-identical content -> MATCH (strategy=similar) at the real span",
          r.status == M and r.strategy == "similar"
          and stext[r.start:r.end] == "    total = compute_sum(items)\n")

    check("two equally-good spots -> AMBIGUOUS (refuse)",
          editmatch.resolve("x = 1\n    x = 1\n", "\tx = 1\n").status == A)
    check("garbage below threshold -> NOT_FOUND",
          editmatch.resolve(stext, "completely unrelated zzzzz qqqqq\n").status == NF)
    check("empty old -> NOT_FOUND", editmatch.resolve(stext, "").status == NF)

    # -- edit_file integration (temp files, flag toggled) ---------------------
    d = tempfile.mkdtemp(prefix="editfuzzy_")
    ctx = Context(d, None)
    old_ws, new_ws = "  if x:\n      return 1\n", "  if x:\n      return 42\n"

    # flag OFF -> exact-or-fail verbatim: refuse, NO write
    config.EDIT_FUZZY = False
    p = _write(d, "off.py", wtext)
    r = edit_file({"path": "off.py", "old_string": old_ws, "new_string": new_ws}, ctx)
    check("flag OFF: a whitespace-drift edit is refused with the teaching error, file unchanged",
          (not r.ok) and "not found" in r.content and _read(p) == wtext)

    # flag ON -> fuzzy whitespace edit applies, strategy tagged
    config.EDIT_FUZZY = True
    p = _write(d, "on.py", wtext)
    r = edit_file({"path": "on.py", "old_string": old_ws, "new_string": new_ws}, ctx)
    check("flag ON: the whitespace-drift edit applies, strategy tagged, file changed",
          r.ok and "fuzzy match: whitespace" in r.content
          and (r.meta or {}).get("edit_strategy") == "whitespace" and "return 42" in _read(p))

    # flag ON, ambiguous -> refuse, NO write
    atext = "x = 1\n    x = 1\n"
    p = _write(d, "amb.py", atext)
    r = edit_file({"path": "amb.py", "old_string": "\tx = 1\n", "new_string": "x = 9\n"}, ctx)
    check("flag ON: an ambiguous fuzzy target REFUSES with no write",
          (not r.ok) and _read(p) == atext)

    # flag ON, EXACT still wins first (normal path, not fuzzy)
    p = _write(d, "exact.py", wtext)
    r = edit_file({"path": "exact.py", "old_string": "    return 0\n", "new_string": "    return 7\n"}, ctx)
    check("flag ON: an EXACT match still applies via the normal path (no fuzzy strategy tagged)",
          r.ok and "replacement(s)" in r.content
          and (r.meta or {}).get("edit_strategy") is None and "return 7" in _read(p))

    # -- line-ending preservation (the Windows default-newline LF->CRLF rewrite bug) ----------
    #    Fixtures are written with newline="" so the OS can't corrupt the setup; bytes are read raw.
    write_file({"path": "wf.py", "content": "x = 1\ny = 2\n"}, ctx)
    with open(os.path.join(d, "wf.py"), "rb") as f:
        check("write_file writes '\\n' verbatim (LF, no CRLF rewrite)", b"\r\n" not in f.read())

    lf = os.path.join(d, "lf.py")
    with open(lf, "w", encoding="utf-8", newline="") as f:
        f.write("a = 1\nb = 2\n")
    edit_file({"path": "lf.py", "old_string": "a = 1", "new_string": "a = 11"}, ctx)
    with open(lf, "rb") as f:
        raw = f.read()
    check("edit_file preserves LF endings (no whole-file CRLF rewrite)", b"\r\n" not in raw and b"a = 11" in raw)

    crlf = os.path.join(d, "crlf.py")
    with open(crlf, "w", encoding="utf-8", newline="") as f:
        f.write("a = 1\r\nb = 2\r\n")
    edit_file({"path": "crlf.py", "old_string": "a = 1", "new_string": "a = 11"}, ctx)
    with open(crlf, "rb") as f:
        raw = f.read()
    check("edit_file preserves CRLF endings (detect-and-restore, not forced to LF)",
          raw.count(b"\r\n") == 2 and b"a = 11\r\n" in raw)

    # hermetic default-off (independent of this repo's own .env)
    _saved = os.environ.pop("CODE_EDIT_FUZZY", None)
    default_off = config._as_bool(os.environ.get("CODE_EDIT_FUZZY", "false")) is False
    if _saved is not None:
        os.environ["CODE_EDIT_FUZZY"] = _saved
    check("CODE_EDIT_FUZZY defaults False when unset (opt-in)", default_off)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
