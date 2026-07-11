"""
scripts/check_read_tools.py

Acceptance harness for the read-side tools (tree / read_file), checked WITHOUT a model or a network.
Covers the two seam bugs a live review exposed:

  * tree measured `depth` from the WORKSPACE, not from the requested `path`, so tree('src/auth/cmd',
    depth=2) — where cmd is already deep — returned just "cmd/ (0 files)" and a reviewer read that as
    "the directory is empty / main.go is missing" (a false absence claim).
  * read_file's binary refusal read like "file absent"; it must say the file EXISTS.

Run:  python scripts/check_read_tools.py
Exits 0 only if every check holds.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.tools import tree, read_file, Context  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def main():
    d = tempfile.mkdtemp(prefix="readtools_")
    # a deep tree: <root>/src/auth/cmd/server/main.go  (cmd is 3 levels below the workspace root)
    deep = os.path.join(d, "src", "auth", "cmd", "server")
    os.makedirs(deep)
    open(os.path.join(deep, "main.go"), "w", encoding="utf-8").write("package main\n")
    ctx = Context(d, None)

    # depth is measured from the REQUESTED path: tree('src/auth/cmd', depth=2) must reach server/main.go.
    r = tree({"path": "src/auth/cmd", "depth": "2"}, ctx)
    check("tree(path=deep, depth=2) shows the subtree BELOW the path (server/ + main.go)",
          r.ok and "server" in r.content and "main.go" in r.content)
    # depth=0 from cmd shows only cmd itself, not its descendants — proving the bound is path-relative
    # (before the fix, cmd was already at depth 3 from cwd, so depth 2/3 showed NOTHING below it at all)
    r0 = tree({"path": "src/auth/cmd", "depth": "0"}, ctx)
    check("tree(path=deep, depth=0) shows only the path itself, not its descendants",
          r0.ok and "main.go" not in r0.content)
    # the whole point: the deep path is NOT reported empty
    check("a deep directory that contains files is never reported as empty by tree",
          "(0 files)" not in tree({"path": "src/auth/cmd/server", "depth": "1"}, ctx).content)

    # read_file on a BINARY file that EXISTS: refuse, but make clear it is PRESENT, not missing.
    bpath = os.path.join(d, "icon.png")
    with open(bpath, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR binary")
    rb = read_file({"path": "icon.png"}, ctx)
    check("read_file refuses a binary file but says it EXISTS / is NOT missing (not conflated with absent)",
          (not rb.ok) and "EXISTS" in rb.content and "missing" in rb.content.lower()
          and "binary" in rb.content.lower())
    # a genuinely-absent file still says 'File not found' (unchanged)
    check("read_file on a truly-absent file still reports not found",
          not read_file({"path": "nope.txt"}, ctx).ok)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
