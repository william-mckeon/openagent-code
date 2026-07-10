"""
src/patch.py

apply_patch (specs/0013, sub-phases B + C) — an ATOMIC, grammar-validated multi-file edit tool.

The model emits ONE envelope describing several file operations (Add / Update / Delete / Move). The
harness parses + validates the WHOLE thing and resolves every Update hunk in memory FIRST, then applies
all-or-nothing. On ANY parse or hunk error, zero files are touched — there is never a partial multi-file
write. Every touched path is recorded on the mutation ledger (Move = delete(old) + write(new)) so the
completion gate (specs/0007) and grounding gate (specs/0010) already cover it, and the ToolResult
carries a "Touched paths:" manifest (sub-phase C) so the closing answer can cite each path without a
phantom-citation false flag.

Grammar (our own; ASCII markers):

    *** Begin Patch
    *** Add File: <relpath>
    +<line>                      full new-file content, one '+'-prefixed line each
    *** Update File: <relpath>
    <<<<<<< SEARCH               one or more search/replace hunks
    <old text>
    =======
    <new text>
    >>>>>>> REPLACE
    *** Delete File: <relpath>
    *** Move File: <oldrel> -> <newrel>
    *** End Patch

Import discipline: imports only stdlib + editmatch + config at module top; ToolResult / _record_mutation
/ _abs are imported LAZILY inside apply_patch (mirrors skills.py) so tools.py can import apply_patch with
no cycle. apply_patch NEVER raises — every failure is a teaching ToolResult(False, ...).

Known v1 limit: a SEARCH/REPLACE or Add-content line that literally begins with '*** Add|Update|Delete|
Move File:' is mistaken for a new op header and the patch fails to PARSE (safe — it never mis-applies).
"""
import os

from . import editmatch
from . import config

_BEGIN = "*** Begin Patch"
_END = "*** End Patch"
_MARKERS = (("add", "*** Add File:"), ("update", "*** Update File:"),
            ("delete", "*** Delete File:"), ("move", "*** Move File:"))
_SEARCH = "<<<<<<< SEARCH"
_DIVIDER = "======="
_REPLACE = ">>>>>>> REPLACE"


class PatchError(Exception):
    """Internal — a parse/validation failure carrying a teaching message. Never escapes apply_patch."""


def _op_header(line):
    """If `line` starts a new file op, return (kind, header_remainder); else None."""
    for kind, marker in _MARKERS:
        if line.startswith(marker):
            return kind, line[len(marker):].strip()
    return None


def parse_patch(text):
    """Parse the envelope into typed ops. Raises PatchError (caught by apply_patch) on any malformed
    structure. Ops: {'op':'add','path','content'} | {'op':'update','path','hunks':[(old,new)]} |
    {'op':'delete','path'} | {'op':'move','old','new'}."""
    lines = (text or "").splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == _BEGIN)
    except StopIteration:
        raise PatchError(f"missing '{_BEGIN}' header")
    try:
        end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == _END)
    except StopIteration:
        raise PatchError(f"missing '{_END}' trailer")
    body = lines[start + 1:end]
    ops, i = [], 0
    while i < len(body):
        if not body[i].strip():
            i += 1
            continue
        hdr = _op_header(body[i])
        if hdr is None:
            raise PatchError(f"expected a '*** <Add|Update|Delete|Move> File:' line, got: {body[i]!r}")
        kind, rest = hdr
        i += 1
        op_body = []
        while i < len(body) and _op_header(body[i]) is None:
            op_body.append(body[i])
            i += 1
        ops.append(_build_op(kind, rest, op_body))
    if not ops:
        raise PatchError("empty patch (no file operations between Begin/End)")
    return ops


def _build_op(kind, rest, op_body):
    if kind == "add":
        if not rest:
            raise PatchError("Add File: missing path")
        content_lines = []
        for bl in op_body:
            if bl.startswith("+"):
                content_lines.append(bl[1:])
            elif not bl.strip():
                continue  # tolerate blank separators around the content
            else:
                raise PatchError(f"Add File '{rest}': content lines must start with '+', got: {bl!r}")
        return {"op": "add", "path": rest,
                "content": ("\n".join(content_lines) + "\n") if content_lines else ""}
    if kind == "delete":
        if not rest:
            raise PatchError("Delete File: missing path")
        if any(bl.strip() for bl in op_body):
            raise PatchError(f"Delete File '{rest}' takes no body")
        return {"op": "delete", "path": rest}
    if kind == "move":
        if "->" not in rest:
            raise PatchError(f"Move File requires 'old -> new', got: {rest!r}")
        old, new = (s.strip() for s in rest.split("->", 1))
        if not old or not new:
            raise PatchError(f"Move File requires 'old -> new', got: {rest!r}")
        if any(bl.strip() for bl in op_body):
            raise PatchError(f"Move File '{rest}' takes no body")
        return {"op": "move", "old": old, "new": new}
    # update
    if not rest:
        raise PatchError("Update File: missing path")
    return {"op": "update", "path": rest, "hunks": _parse_hunks(rest, op_body)}


def _parse_hunks(path, op_body):
    hunks, i = [], 0
    while i < len(op_body):
        if not op_body[i].strip():
            i += 1
            continue
        if op_body[i].strip() != _SEARCH:
            raise PatchError(f"Update File '{path}': expected '{_SEARCH}', got: {op_body[i]!r}")
        i += 1
        old_lines = []
        while i < len(op_body) and op_body[i].strip() != _DIVIDER:
            old_lines.append(op_body[i])
            i += 1
        if i >= len(op_body):
            raise PatchError(f"Update File '{path}': a hunk is missing '{_DIVIDER}'")
        i += 1
        new_lines = []
        while i < len(op_body) and op_body[i].strip() != _REPLACE:
            new_lines.append(op_body[i])
            i += 1
        if i >= len(op_body):
            raise PatchError(f"Update File '{path}': a hunk is missing '{_REPLACE}'")
        i += 1
        hunks.append(("\n".join(old_lines), "\n".join(new_lines)))
    if not hunks:
        raise PatchError(f"Update File '{path}': no SEARCH/REPLACE hunk")
    return hunks


def _apply_hunk(path, content, old, new):
    """Resolve `old` in `content` (exact-first, then the safe fuzzy cascade) and return `content` with
    that span replaced by `new`. Raises PatchError if not found or ambiguous — never guesses."""
    if not old:
        raise PatchError(f"Update File '{path}': a hunk has an empty SEARCH block")
    res = editmatch.resolve(content, old, config.EDIT_FUZZY_THRESHOLD)
    if res.status == editmatch.MATCH:
        return content[:res.start] + new + content[res.end:]
    if res.status == editmatch.AMBIGUOUS:
        raise PatchError(f"Update File '{path}': a SEARCH block matches more than one place — add "
                         f"surrounding context so it's unique")
    raise PatchError(f"Update File '{path}': a SEARCH block wasn't found in the file")


def _restore(path, data):
    """Rewrite a file's exact original bytes — the undo for an Update/Delete during a rolled-back apply."""
    with open(path, "wb") as f:
        f.write(data)


def _gate(ctx, tool, raw):
    """Route ONE patch op through the SAME permission engine as its single-file equivalent, so
    apply_patch inherits the workspace fence + deny/ask rules + plan-mode block PER operation — it
    can't be fenced as a single path at dispatch because one envelope carries many. Denied -> PatchError
    (an atomic refusal, raised during validation, before any file is touched). A ctx with no engine
    (a direct unit-test Context(cwd, None)) has nothing to gate against, so it passes through."""
    perms = getattr(ctx, "permissions", None)
    if perms is None:
        return
    d = perms.decide(tool, {"path": raw}, ctx)
    if not d.allowed:
        raise PatchError(f"'{raw}' blocked by permissions - {d.reason}")


def _read_text(rel, path):
    """Read a file as text for patching. On a BINARY / undecodable file raise PatchError (a clean
    teaching refusal), NOT a UnicodeDecodeError - so apply_patch never crashes on a PNG the way a naive
    utf-8 read would (read_file has the same guard on the read side)."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError:
        raise PatchError(f"Update File '{rel}': looks like a BINARY file - apply_patch edits text, not binary")
    except OSError as e:
        raise PatchError(f"Update File '{rel}': can't read ({e})")


def apply_patch(args, ctx):
    """Tool entry: parse + validate + resolve the whole patch in memory, then apply ALL-OR-NOTHING."""
    from .tools import ToolResult, _record_mutation, _abs  # lazy — avoid the tools<->patch cycle
    try:
        ops = parse_patch(args.get("patch") or "")
    except PatchError as e:
        return ToolResult(False, f"apply_patch: could not parse the patch - {e}. No files were changed.")

    plan, touched = [], []   # plan: (action, path, content|None, old_abs|None); touched: (rel, action)
    try:
        for op in ops:
            if op["op"] == "add":
                _gate(ctx, "write_file", op["path"])   # fence + deny/ask + plan-mode, per op
                path = _abs(ctx, op["path"])
                if os.path.exists(path):
                    raise PatchError(f"Add File '{op['path']}': already exists")
                plan.append(("write", path, op["content"], None))
                touched.append((op["path"], "write"))
            elif op["op"] == "delete":
                _gate(ctx, "delete_file", op["path"])
                path = _abs(ctx, op["path"])
                if not os.path.isfile(path):
                    raise PatchError(f"Delete File '{op['path']}': not found")
                plan.append(("delete", path, None, None))
                touched.append((op["path"], "delete"))
            elif op["op"] == "move":
                _gate(ctx, "delete_file", op["old"])   # a Move removes old + writes new: gate both
                _gate(ctx, "write_file", op["new"])
                old_p, new_p = _abs(ctx, op["old"]), _abs(ctx, op["new"])
                if not os.path.isfile(old_p):
                    raise PatchError(f"Move File '{op['old']}': source not found")
                if os.path.exists(new_p):
                    raise PatchError(f"Move File: destination '{op['new']}' already exists")
                # A Move is a byte-preserving RENAME - never read the content as text (a binary file
                # like a PNG would crash a utf-8 read; this is the android-chrome-*.png case that failed).
                plan.append(("rename", new_p, None, old_p))
                touched.append((op["old"], "delete"))
                touched.append((op["new"], "write"))
            else:  # update
                _gate(ctx, "edit_file", op["path"])
                path = _abs(ctx, op["path"])
                if not os.path.isfile(path):
                    raise PatchError(f"Update File '{op['path']}': not found")
                content = _read_text(op["path"], path)   # PatchError (not a crash) on a binary target
                for old, new in op["hunks"]:
                    content = _apply_hunk(op["path"], content, old, new)
                plan.append(("write", path, content, None))
                touched.append((op["path"], "edit"))
    except PatchError as e:
        return ToolResult(False, f"apply_patch: {e}. No files were changed (atomic).")

    # Every op validated in memory — now apply, TRANSACTIONALLY. Each op records how to UNDO itself
    # (restore original bytes / delete the new file / rename back), so a failure part-way through rolls
    # every already-applied op back and the tool keeps its "on ANY error, no file is changed" contract
    # instead of leaving a half-written multi-file patch. Validation above catches the common errors;
    # this covers a disk/permission race and broader-than-OSError faults (a NUL path -> ValueError, a
    # lone surrogate in Add content -> UnicodeError), so apply_patch still NEVER raises.
    undo = []
    try:
        for action, path, content, old_p in plan:
            if action == "delete":
                with open(path, "rb") as f:
                    saved = f.read()
                os.remove(path)
                undo.append(lambda p=path, b=saved: _restore(p, b))
            elif action == "rename":   # Move: byte-preserving, so it works on binary files too
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                os.rename(old_p, path)
                undo.append(lambda a=old_p, b=path: os.rename(b, a))
            else:  # write (Add / Update)
                # Register the undo BEFORE the write: open(path,"w") truncates/creates the file before
                # f.write can fail, so a failed Add must still be cleaned up (remove the empty file) and a
                # failed Update must still restore the original.
                if os.path.exists(path):                    # Update: save + restore the original bytes
                    with open(path, "rb") as f:
                        saved = f.read()
                    undo.append(lambda p=path, b=saved: _restore(p, b))
                else:                                        # Add: undo removes the (maybe half-written) file
                    undo.append(lambda p=path: os.path.exists(p) and os.remove(p))
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                # newline="" writes '\n' verbatim (LF) — no Windows LF->CRLF rewrite of the whole file.
                with open(path, "w", encoding="utf-8", newline="") as f:
                    f.write(content)
    except (OSError, ValueError, UnicodeError) as e:
        for fn in reversed(undo):
            try:
                fn()
            except OSError:
                pass   # best-effort restore; nothing else can be done and we must not raise
        return ToolResult(False, f"apply_patch: failed while applying ({e}); rolled back the partial "
                                 "changes — no files were left modified.")
    for rel, act in touched:
        _record_mutation(ctx, rel, act)

    manifest = "\n".join(f"  {act:<6} {rel}" for rel, act in touched)
    return ToolResult(True,
                      f"apply_patch: applied {len(ops)} operation(s) atomically.\nTouched paths:\n{manifest}",
                      {"touched_paths": [rel for rel, _ in touched], "ops": len(ops)})
