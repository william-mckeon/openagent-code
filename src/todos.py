"""
src/todos.py

Project todos (Phase 23 / specs/0023) — a persistent, per-project BACKLOG the agent maintains.

Where memory.py is the agent's append-only notebook of FACTS about a repo, this is its structured CHECKLIST
of outstanding WORK: a human-editable markdown file (`<workspace>/.openagent/todos.md` by default) of
`- [ ]` / `- [~]` / `- [x]` items with a status, loaded into the system prompt and shown at startup each
session so a later run picks up the backlog instead of rediscovering it. The agent records work it finds and
checks items off with the `project_todos` tool; you can edit the file directly.

This module is the STORE only — parse / render a checklist and pure list transforms (add / set status /
clear done). It differs from memory in ONE load-bearing way: memory is append-only text, but a checklist
must be READ-MODIFY-WRITTEN to flip an item's status, so save() rewrites the whole file, and the prompt
injection is bounded by WHOLE LINES (never a byte tail that would slice a checkbox). Opt-in via
CODE_PROJECT_TODOS; OFF for eval so the harness stays isolated. See specs/0023-project-todos.md.
"""
import os
import re

from . import config

# The three states an item can be in and their checkbox markers. NOT tools._PLAN_MARKS: that is
# {pending/in_progress/completed} with bare '[ ]' marks and no leading '- ' — the wrong status name AND the
# wrong line form for a hand-editable checkbox file. This module is the ONE source of the todos format.
STATUSES = ("pending", "in_progress", "done")
_MARKS = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
# Lenient parse: accept '-' or '*' bullets, any leading whitespace, and the common hand-typed marker
# variants ('x'/'X' done, '~' or '/' in-progress, ' ' pending) so a file a human edited still loads.
_LINE_RE = re.compile(r"^\s*[-*]\s+\[([ xX~/])\]\s+(.+?)\s*$")
_MARK_TO_STATUS = {" ": "pending", "~": "in_progress", "/": "in_progress", "x": "done", "X": "done"}
_HEADER = "# Project todos (openagent-code)"


def path(workspace):
    """Absolute path to this workspace's todos file (mirrors memory.path)."""
    return config.todos_file(workspace)


def parse(text):
    """A checklist markdown string -> [{content, status}]. Lenient: skips the header, blank lines, and any
    prose so a hand-edited file still loads; an empty-content checkbox is skipped. Never raises."""
    items = []
    for line in (text or "").splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        content = m.group(2).strip()
        if content:
            items.append({"content": content, "status": _MARK_TO_STATUS.get(m.group(1), "pending")})
    return items


def render(items):
    """[{content, status}] -> the human-editable checklist markdown for the FILE (header + one line per
    item). Number-LESS on purpose: a number prefix isn't a valid `- [ ]` checklist line and would break
    parse()'s round-trip. The model/user-facing NUMBERED view is display() below."""
    lines = [_HEADER, ""]
    for it in items:
        mark = _MARKS.get(it.get("status", "pending"), _MARKS["pending"])
        lines.append(f"- {mark} {(it.get('content') or '').strip()}")
    return "\n".join(lines) + "\n"


def display(items, outstanding_only=False):
    """A NUMBERED, model/user-facing view of the backlog. The number is the item's 1-based position in the
    FULL list, so a number shown here is EXACTLY what _find/set_status resolves (they index the full list),
    whether or not done items are shown. `outstanding_only` hides done items (the prompt/startup backlog)
    while keeping their full-list numbers stable. '' when nothing to show."""
    lines = []
    for i, it in enumerate(items):
        if outstanding_only and it.get("status") == "done":
            continue
        mark = _MARKS.get(it.get("status", "pending"), _MARKS["pending"])
        lines.append(f"{i + 1}. {mark} {(it.get('content') or '').strip()}")
    return "\n".join(lines)


def load(workspace):
    """This workspace's todo items (parsed). Missing file / unreadable -> [] (never raises)."""
    fp = path(workspace)
    if not os.path.isfile(fp):
        return []
    try:
        with open(fp, encoding="utf-8", errors="replace") as f:
            return parse(f.read())
    except OSError:
        return []


def save(workspace, items):
    """Rewrite the todos file from `items` — a READ-MODIFY-WRITE, unlike memory's append (a checklist can't
    flip an item's status by appending). Returns the path. newline='' writes the checklist verbatim (LF), the
    same guard write_file uses so Windows doesn't silently rewrite the whole file to CRLF."""
    fp = path(workspace)
    os.makedirs(os.path.dirname(fp) or ".", exist_ok=True)
    with open(fp, "w", encoding="utf-8", newline="") as f:
        f.write(render(items))
    return fp


def backlog_text(workspace, max_chars=None):
    """The OUTSTANDING backlog as markdown for the system-prompt injection and the startup display: pending
    + in_progress items only (done items are history, not backlog), bounded to max_chars by WHOLE LINES — a
    byte tail (as memory uses) would slice a `- [ ]` line and corrupt the checklist. '' when nothing is
    outstanding."""
    items = load(workspace)
    if not outstanding(items):
        return ""
    text = display(items, outstanding_only=True)   # NUMBERED (full-list index) so a number matches _find
    cap = config.PROJECT_TODOS_MAX_CHARS if max_chars is None else max_chars
    if cap and len(text) > cap:
        kept, total = [], 0
        for line in text.splitlines():
            if total + len(line) + 1 > cap:
                kept.append("...(more todos elided; see .openagent/todos.md)")
                break
            kept.append(line)
            total += len(line) + 1
        text = "\n".join(kept)
    return text.strip()


# -- pure list transforms the project_todos tool composes with load + save ----

def add(items, content, status="pending"):
    """Append a new item. De-dupes on content (case-insensitive) — re-adding an existing item just updates
    its status rather than creating a duplicate."""
    content = (content or "").strip()
    status = status if status in STATUSES else "pending"
    items = [dict(it) for it in items]
    for it in items:
        if it["content"].lower() == content.lower():
            it["status"] = status
            return items
    items.append({"content": content, "status": status})
    return items


def _find(items, selector):
    """Resolve a selector to an index: a 1-based number (as shown in the rendered list) OR an exact, UNIQUE
    content match. Returns (index, error): index is None with a reason on no / ambiguous / out-of-range."""
    if selector is None or str(selector).strip() == "":
        return None, "no item given"
    s = str(selector).strip()
    if s.isdigit():
        i = int(s) - 1
        if 0 <= i < len(items):
            return i, None
        return None, f"item {s} is out of range (1..{len(items)})"
    hits = [i for i, it in enumerate(items) if it["content"].lower() == s.lower()]
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        return None, f"no item matches {selector!r}"
    return None, f"{selector!r} matches {len(hits)} items - use the number shown in the list"


def set_status(items, selector, status):
    """Set one item's status (by number or exact text). Returns (items, error)."""
    items = [dict(it) for it in items]
    i, err = _find(items, selector)
    if err:
        return items, err
    items[i]["status"] = status if status in STATUSES else "pending"
    return items, None


def clear_done(items):
    """Drop every done item — the backlog's 'archive completed'. The STRUCTURED cap (never a byte tail)."""
    return [dict(it) for it in items if it.get("status") != "done"]


def outstanding(items):
    """Items not yet done (pending + in_progress) — what the startup section and prompt injection show."""
    return [dict(it) for it in items if it.get("status") != "done"]
