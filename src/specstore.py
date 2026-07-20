"""
src/specstore.py

Spec-first artifact store (Phase 25 / specs/0025) — the persistent design+acceptance spec the agent authors
before a substantive change. The memory/todos store analog, with two deliberate departures: it's a NUMBERED
DIRECTORY of specs (`<workspace>/.openagent/specs/NNNN-slug.md`), not one flat file, and its acceptance
checklist is BINARY (`- [ ]` / `- [x]`), not project-todos' tri-state — so it owns its OWN format constants,
NOT tools._PLAN_MARKS or todos._MARKS. A spec has Goal / Acceptance (the checklist that defines done) /
Non-goals, the same shape as this repo's own specs/ folder, but kept DISTINCT: the agent's specs live
per-repo under .openagent/specs/ (co-located with memory.md and todos.md), never in the maintainers' specs/.

Pure store: parse/render/save/load + acceptance helpers. Every path is fail-safe — a missing/malformed dir
loads as None, never raises (mirror memory.load / todos.load). Opt-in via CODE_SPEC_FIRST; OFF for eval so
the harness stays isolated. See specs/0025-spec-first.md.
"""
import os
import re

from . import config

# Binary acceptance markers (an item is met or not) - NOT todos' tri-state. This module is the ONE source
# of the spec-file format. parse is lenient (a hand-typed [X]/[~]/[/] still loads) but only [x]/[X] count
# as met, so a human-edited spec round-trips and an in-progress mark collapses to not-met.
_MARKS = {False: "[ ]", True: "[x]"}
_ACCEPT_RE = re.compile(r"^\s*[-*]\s+\[([ xX~/])\]\s+(.+?)\s*$")
_H1_RE = re.compile(r"^#\s+(\d+)\s*[—:-]\s*(.+?)\s*$")   # "# 0025 — title" (em-dash / hyphen / colon)
_FILE_RE = re.compile(r"^(\d+)-.*\.md$")
_GOAL, _ACCEPT, _NONGOALS = "## Goal", "## Acceptance", "## Non-goals"


def specs_dir(workspace):
    """The .openagent/specs/ DIRECTORY for this workspace (mirrors config.specs_dir)."""
    return config.specs_dir(workspace)


def slugify(title):
    """A filesystem-safe slug from a title: lowercase, non-alphanumerics -> single '-', trimmed, bounded.
    Empty/garbage -> 'spec'."""
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:50].strip("-")
    return s or "spec"


def path(workspace, number, slug):
    """Absolute path to a spec file: <specs_dir>/NNNN-slug.md (4-digit zero-padded number)."""
    return os.path.join(specs_dir(workspace), f"{int(number):04d}-{slug}.md")


def next_number(workspace):
    """The next spec number: 1 if the dir is missing/empty, else max existing NNNN + 1. Ignores files that
    don't match NNNN-*.md (a human's stray file). Never raises."""
    d = specs_dir(workspace)
    if not os.path.isdir(d):
        return 1
    top = 0
    try:
        for name in os.listdir(d):
            m = _FILE_RE.match(name)
            if m:
                top = max(top, int(m.group(1)))
    except OSError:
        return 1
    return top + 1


def render(spec):
    """A spec dict -> the human-editable markdown (H1 + Goal / Acceptance checklist / Non-goals)."""
    num = int(spec.get("number") or 0)
    lines = [f"# {num:04d} — {(spec.get('title') or '').strip()}", "",
             _GOAL, "", (spec.get("goal") or "").strip(), "", _ACCEPT, ""]
    for it in spec.get("acceptance") or []:
        lines.append(f"- {_MARKS[bool(it.get('done'))]} {(it.get('content') or '').strip()}")
    lines += ["", _NONGOALS, ""]
    for ng in spec.get("non_goals") or []:
        lines.append(f"- {str(ng).strip()}")
    return "\n".join(lines).rstrip() + "\n"


def parse(text):
    """Spec markdown -> {number, title, goal, acceptance:[{content,done}], non_goals:[...]}. Lenient +
    never raises: a hand-edited file with `-`/`:` H1 separators, `[X]`/`[~]` marks, or extra prose still
    loads. Section-cursored so a multi-line goal (blanks included) is captured until the next '## ' header."""
    number, title, goal, acceptance, non_goals = 0, "", [], [], []
    section = None
    for line in (text or "").splitlines():
        m = _H1_RE.match(line)
        if m:
            number, title = int(m.group(1)), m.group(2).strip()
            continue
        stripped = line.strip()
        if stripped.startswith("## "):
            section = stripped.lower()
            continue
        if section == _GOAL.lower():
            goal.append(line)
        elif section == _ACCEPT.lower():
            am = _ACCEPT_RE.match(line)
            if am and am.group(2).strip():
                acceptance.append({"content": am.group(2).strip(), "done": am.group(1) in ("x", "X")})
        elif section == _NONGOALS.lower():
            if stripped[:1] in ("-", "*") and stripped[1:].strip():
                non_goals.append(stripped[1:].strip())
    return {"number": number, "title": title, "goal": "\n".join(goal).strip(),
            "acceptance": acceptance, "non_goals": non_goals}


def save(workspace, spec):
    """Write the spec to .openagent/specs/NNNN-slug.md and return its path. The number is IDENTITY: reuse
    spec['number'] on a re-save (idempotent, same file), else mint next_number ONCE with a collision guard
    (a same-instant second spec lands on N+1, never clobbers). newline='' preserves LF (the Windows-CRLF
    guard write_file uses)."""
    d = specs_dir(workspace)
    os.makedirs(d, exist_ok=True)
    slug = spec.get("slug") or slugify(spec.get("title"))
    if spec.get("number"):
        num, fp = int(spec["number"]), path(workspace, spec["number"], slug)
    else:
        num = next_number(workspace)
        while os.path.exists(path(workspace, num, slug)):
            num += 1
        fp = path(workspace, num, slug)
    spec["number"], spec["slug"] = num, slug
    with open(fp, "w", encoding="utf-8", newline="") as f:
        f.write(render(spec))
    return fp


def _active_file(workspace):
    """The highest-numbered spec file's path (the ACTIVE spec), or None. No separate pointer to desync."""
    d = specs_dir(workspace)
    if not os.path.isdir(d):
        return None
    best = None
    try:
        for name in os.listdir(d):
            m = _FILE_RE.match(name)
            if m and (best is None or int(m.group(1)) > best[0]):
                best = (int(m.group(1)), os.path.join(d, name))
    except OSError:
        return None
    return best[1] if best else None


def load_active(workspace):
    """The ACTIVE (highest-numbered) spec as a dict, or None. Missing/malformed dir / unreadable -> None,
    never raises (mirror todos.load / memory.load)."""
    fp = _active_file(workspace)
    if not fp:
        return None
    try:
        with open(fp, encoding="utf-8", errors="replace") as f:
            spec = parse(f.read())
    except OSError:
        return None
    spec["slug"] = os.path.basename(fp).split("-", 1)[-1].rsplit(".md", 1)[0]
    return spec


def active_text(workspace, max_chars=None):
    """The active spec rendered as markdown for the prompt injection, bounded by WHOLE LINES (never a byte
    tail that would slice an acceptance checkbox). '' when there's no active spec."""
    spec = load_active(workspace)
    if not spec or not (spec.get("goal") or spec.get("acceptance")):
        return ""
    text = render(spec)
    cap = config.SPECS_MAX_CHARS if max_chars is None else max_chars
    if cap and len(text) > cap:
        kept, total = [], 0
        for line in text.splitlines():
            if total + len(line) + 1 > cap:
                kept.append("...(spec truncated; see .openagent/specs/)")
                break
            kept.append(line)
            total += len(line) + 1
        text = "\n".join(kept)
    return text.strip()


# -- acceptance helpers (the tool + the gate compose with these) --------------

def _find(acceptance, selector):
    """Resolve a selector to an acceptance index: a 1-based number OR an exact, UNIQUE content match.
    Returns (index, error) — index None with a reason on no / ambiguous / out-of-range."""
    if selector is None or str(selector).strip() == "":
        return None, "no acceptance item given"
    s = str(selector).strip()
    if s.isdigit():
        i = int(s) - 1
        return (i, None) if 0 <= i < len(acceptance) else (None, f"item {s} is out of range (1..{len(acceptance)})")
    hits = [i for i, it in enumerate(acceptance) if it["content"].lower() == s.lower()]
    if len(hits) == 1:
        return hits[0], None
    return (None, f"no acceptance item matches {selector!r}") if not hits else \
        (None, f"{selector!r} matches {len(hits)} items - use the number shown")


def set_acceptance(acceptance, selector, done=True):
    """Flip one acceptance item's done flag (by number or exact text). Returns (acceptance, error)."""
    items = [dict(it) for it in acceptance]
    i, err = _find(items, selector)
    if err:
        return items, err
    items[i]["done"] = bool(done)
    return items, None


def outstanding(acceptance):
    """Acceptance items not yet met."""
    return [it for it in acceptance if not it.get("done")]


def all_met(acceptance):
    """True if there is at least one acceptance item and every item is done."""
    return bool(acceptance) and all(it.get("done") for it in acceptance)
