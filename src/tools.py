"""
src/tools.py

The tool boundary.

Tool ERGONOMICS are the single most underrated lever for agent proficiency —
more than the model in many cases. The choices here are deliberate:

  * read_file returns LINE NUMBERS -> enables precise edits and references.
  * edit_file is EXACT-MATCH-OR-FAIL and requires a UNIQUE match -> forces the
    model to ground every edit in text it actually read, fails loudly instead of
    silently corrupting, and the error message TEACHES the next attempt.
  * grep/glob are dedicated structured tools, not raw shell -> clean output,
    less token waste, no quoting hell.
  * Permissions are enforced at DISPATCH (src/agent.py calls permissions.decide
    before running the tool), not inside each tool — so the gate is in one place
    and the decision is captured once. Tools here assume they're cleared to run.

Every failure returns ok=False with a message designed to fix the next try.
That same ok/fail + retry count is the cheapest training signal.
"""
import os
import re
import glob as globlib
import fnmatch
import subprocess
from dataclasses import dataclass, field

from . import config


@dataclass
class ToolResult:
    ok: bool
    content: str
    meta: dict = field(default_factory=dict)


class Context:
    """Carried into every tool call: working dir + permission gate (+ subagent wiring)."""
    def __init__(self, cwd, permissions):
        self.cwd = cwd
        self.permissions = permissions
        self.verbose = False
        # Subagent support — wired by subagent.make_context (None at the tool layer
        # keeps tools.py free of any agent/runtime import).
        self.spawn = None              # callable(task) -> final text
        self.depth = 0                 # this agent's nesting depth (0 = top-level)
        self.session_id = None         # this agent's trajectory id (parent link for children)
        self.plan = None               # current plan text (set by update_plan; pinned by the loop)
        self.ask = None                # callable(question) -> answer; wired by make_context
        self.interactive = False       # True only when a human is present to answer


def _abs(ctx, path):
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(ctx.cwd, path))


def _rel(ctx, path):
    """Path relative to the workspace root, with forward slashes.

    glob/grep emit results through this so the model sees `foo.py`, not the
    absolute container path `/workspace/foo.py` — feeding an absolute path back
    led the model to mis-relativize it to `workspace/foo.py` and double-prefix
    (`/workspace/workspace/foo.py`), wasting a failed read every run.
    """
    try:
        rel = os.path.relpath(path, ctx.cwd)
    except ValueError:  # different drive on Windows — fall back to the original
        rel = path
    return rel.replace(os.sep, "/")


_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "trajectories"}


# ---------------------------------------------------------------- read-only

def read_file(args, ctx):
    path = _abs(ctx, args["path"])
    offset = int(args.get("offset", 0))
    limit = int(args.get("limit", 2000))
    if not os.path.isfile(path):
        return ToolResult(False, f"File not found: {path}")
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    chunk = lines[offset:offset + limit]
    numbered = "".join(f"{i + offset + 1}\t{ln}" for i, ln in enumerate(chunk))
    return ToolResult(True, numbered or "(empty file)", {"total_lines": len(lines)})


def grep(args, ctx):
    try:
        pattern = re.compile(args["pattern"])
    except re.error as e:
        return ToolResult(False, f"Invalid regex: {e}")
    root = _abs(ctx, args.get("path", "."))
    glob_filter = args.get("glob")
    matches = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if glob_filter and not fnmatch.fnmatch(fn, glob_filter):
                continue
            fp = os.path.join(dirpath, fn)
            try:
                with open(fp, encoding="utf-8") as f:
                    for i, line in enumerate(f, 1):
                        if pattern.search(line):
                            matches.append(f"{_rel(ctx, fp)}:{i}:{line.rstrip()}")
                            if len(matches) >= 200:
                                break
            except (UnicodeDecodeError, OSError):
                continue
        if len(matches) >= 200:
            break
    return ToolResult(True, "\n".join(matches) or "(no matches)", {"count": len(matches)})


def glob_tool(args, ctx):
    root = _abs(ctx, args.get("path", "."))
    hits = []
    for h in globlib.glob(os.path.join(root, args["pattern"]), recursive=True):
        rel = _rel(ctx, h)
        # Skip heavy/noise dirs (same set grep uses) so a broad pattern like '**/*'
        # doesn't return the whole repo (trajectories/, .venv, __pycache__, ...) and
        # blow the model's context window — which is how a glob 500'd the 8k worker.
        if any(part in _SKIP_DIRS for part in rel.split("/")):
            continue
        hits.append(rel)
    hits = sorted(hits)
    cap = 200
    body = "\n".join(hits[:cap]) or "(no matches)"
    if len(hits) > cap:
        body += f"\n... ({len(hits)} matches; showing first {cap} — narrow the pattern)"
    return ToolResult(True, body, {"count": len(hits)})


# ---------------------------------------------------------------- mutating

def write_file(args, ctx):
    path = _abs(ctx, args["path"])
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    content = args["content"]
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return ToolResult(True, f"Wrote {len(content)} bytes to {path}")


def edit_file(args, ctx):
    path = _abs(ctx, args["path"])
    old, new = args["old_string"], args["new_string"]
    replace_all = bool(args.get("replace_all", False))
    if not os.path.isfile(path):
        return ToolResult(False, f"File not found: {path}")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    count = text.count(old)
    if count == 0:
        return ToolResult(False, "old_string not found. Read the file and copy the exact "
                                 "text including whitespace and indentation.")
    if count > 1 and not replace_all:
        return ToolResult(False, f"old_string is not unique ({count} matches). Add "
                                 f"surrounding context to make it unique, or set replace_all=true.")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.replace(old, new))
    return ToolResult(True, f"Edited {path} ({count} replacement(s))")


def run_command(args, ctx):
    cmd = args["command"]
    shell_cmd = (["powershell", "-NoProfile", "-Command", cmd] if os.name == "nt"
                 else ["bash", "-lc", cmd])
    try:
        p = subprocess.run(shell_cmd, cwd=ctx.cwd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return ToolResult(False, "Command timed out after 120s")
    out = (p.stdout or "")
    if p.stderr:
        out += "\n[stderr]\n" + p.stderr
    return ToolResult(p.returncode == 0, f"(exit {p.returncode})\n{out[:5000]}",
                      {"returncode": p.returncode})


_PLAN_MARKS = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}


def update_plan(args, ctx):
    """Record/replace the agent's plan — a tracked checklist (Phase 4 planning).

    Stored on ctx.plan; the loop pins it into the live context each turn so it
    stays visible (and survives compaction). Captured in the trajectory as this
    tool call's args, so decomposition is a learnable signal.
    """
    steps = args.get("steps") or []
    if not steps:
        ctx.plan = None
        return ToolResult(True, "Plan cleared.")
    lines = []
    for s in steps:
        if isinstance(s, dict):
            content, status = s.get("content", ""), s.get("status", "pending")
        else:
            content, status = str(s), "pending"
        lines.append(f"{_PLAN_MARKS.get(status, '[ ]')} {content}")
    ctx.plan = "\n".join(lines)
    return ToolResult(True, "Plan updated:\n" + ctx.plan)


def ask_user(args, ctx):
    """Ask the human a clarifying question (Phase 4 interactivity).

    Degrades safely when no human is present (eval / one-shot / Docker): it returns
    a 'proceed on your own judgment' note instead of blocking, so non-interactive
    runs stay deterministic. The question + answer are captured in the trajectory.
    """
    question = (args.get("question") or "").strip()
    if not question:
        return ToolResult(False, "ask_user requires a non-empty 'question'.")
    if ctx.ask is None or not ctx.interactive:
        return ToolResult(True, "(No user is available to answer. Proceed with your "
                                "best judgment and state any assumption you made.)")
    return ToolResult(True, ctx.ask(question))


def spawn_agent(args, ctx):
    """Delegate a self-contained subtask to a fresh subagent (Phase 4).

    Depth is enforced HERE (uniform toolset everywhere; the limit is a call-time
    check, not a per-depth tool list). The child runs in isolation and its full
    work is captured as its own trajectory; only its final answer comes back.
    """
    if ctx.spawn is None:
        return ToolResult(False, "Subagents are not available in this context.")
    if ctx.depth >= config.MAX_SUBAGENT_DEPTH:
        return ToolResult(False, f"Max subagent depth ({config.MAX_SUBAGENT_DEPTH}) reached "
                                 "- do this subtask yourself.")
    task = (args.get("task") or "").strip()
    if not task:
        return ToolResult(False, "spawn_agent requires a non-empty 'task'.")
    final = ctx.spawn(task)
    return ToolResult(True, final or "(subagent returned no answer)")


def request_dir(args, ctx):
    """Request READ access to a directory outside the workspace (Phase 4 host access).

    The agent CANNOT widen its own fence — this ASKS the human. On approval the dir is
    added to the live permission roots (so subsequent reads succeed); on refusal, or when
    no human is present, access stays denied. Use it when a task needs a folder you can't
    currently read, instead of giving up or reviewing the wrong folder.
    """
    path = (args.get("path") or "").strip().strip('"')
    if not path:
        return ToolResult(False, "request_dir requires a 'path'.")
    ap = os.path.abspath(path)
    if not os.path.isdir(ap):
        return ToolResult(False, f"Not a directory: {ap}")
    real = os.path.realpath(ap)
    if ctx.permissions._within_roots(real, ctx.cwd):
        return ToolResult(True, f"Already accessible: {ap}")
    if ctx.ask is None or not ctx.interactive:
        return ToolResult(False, f"Cannot grant access to {ap}: no human is present to approve. "
                                 "Ask the user to restart with --add-dir, or proceed without it.")
    why = (args.get("why") or "").strip()
    question = (f"The agent requests READ access to: {ap}"
                + (f"\n  reason: {why}" if why else "") + "\nGrant access? [y/N]")
    ans = (ctx.ask(question) or "").strip().lower()
    if ans in ("y", "yes", "ok", "sure", "allow", "approve"):
        if real not in ctx.permissions.extra_roots:
            ctx.permissions.extra_roots.append(real)
        return ToolResult(True, f"Access granted to {ap}. You may now read files there with absolute paths.")
    return ToolResult(False, f"The user denied access to {ap}. Do not try to read it.")


# ---------------------------------------------------------------- memory (opt-in)

def remember(args, ctx):
    """Save a durable note to PROJECT memory (Phase 4 #7).

    Appends to <workspace>/.openagent/memory.md, which is reloaded into context in
    future sessions. The agent's own notebook for lasting facts about THIS repo
    (conventions, where things live, build/test quirks) - not transient task state.
    """
    from . import memory
    note = (args.get("note") or "").strip()
    if not note:
        return ToolResult(False, "remember requires a non-empty 'note'.")
    fp = memory.remember(ctx.cwd, note)
    return ToolResult(True, f"Saved to project memory: {_rel(ctx, fp)}")


# ---------------------------------------------------------------- web (opt-in)

def web_fetch(args, ctx):
    """Fetch a URL and return its text. OPT-IN (CODE_ENABLE_WEB): sends the URL off-machine."""
    if not config.ENABLE_WEB:
        return ToolResult(False, "Web tools are disabled. Set CODE_ENABLE_WEB=true to allow them.")
    url = (args.get("url") or "").strip()
    if not url:
        return ToolResult(False, "web_fetch requires a 'url'.")
    try:
        import httpx
        r = httpx.get(url, timeout=30, follow_redirects=True,
                      headers={"User-Agent": "openagent-code"})
    except Exception as e:
        return ToolResult(False, f"fetch error: {type(e).__name__}: {e}")
    if r.status_code != 200:
        return ToolResult(False, f"HTTP {r.status_code} fetching {url}")
    text = r.text
    if "html" in r.headers.get("content-type", "").lower():
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
    return ToolResult(True, text[:8000], {"url": url, "bytes": len(r.text)})


def web_search(args, ctx):
    """Search the web via a configured BYO endpoint. OPT-IN: sends the query off-machine."""
    if not config.ENABLE_WEB:
        return ToolResult(False, "Web tools are disabled. Set CODE_ENABLE_WEB=true to allow them.")
    if not config.SEARCH_URL:
        return ToolResult(False, "web_search is not configured. Set CODE_SEARCH_URL.")
    query = (args.get("query") or "").strip()
    if not query:
        return ToolResult(False, "web_search requires a 'query'.")
    headers = {"Content-Type": "application/json"}
    if config.SEARCH_KEY:
        headers["Authorization"] = f"Bearer {config.SEARCH_KEY}"
    try:
        import httpx
        r = httpx.post(config.SEARCH_URL, json={"query": query}, headers=headers, timeout=30)
    except Exception as e:
        return ToolResult(False, f"search error: {type(e).__name__}: {e}")
    if r.status_code != 200:
        return ToolResult(False, f"search HTTP {r.status_code}")
    return ToolResult(True, r.text[:6000], {"query": query})


# ---------------------------------------------------------------- registry

TOOLS = [
    {
        "name": "read_file", "fn": read_file,
        "description": "Read a file. Returns content with line numbers. Use offset/limit for large files.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer", "description": "0-based start line"},
            "limit": {"type": "integer", "description": "max lines to return"},
        }, "required": ["path"]},
    },
    {
        "name": "grep", "fn": grep,
        "description": "Search file contents by regex. Optional glob filter (e.g. '*.py').",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
            "glob": {"type": "string"},
        }, "required": ["pattern"]},
    },
    {
        "name": "glob", "fn": glob_tool,
        "description": "Find files by glob pattern, e.g. '**/*.py'.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
        }, "required": ["pattern"]},
    },
    {
        "name": "write_file", "fn": write_file,
        "description": "Create or overwrite a file with the given content.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        }, "required": ["path", "content"]},
    },
    {
        "name": "edit_file", "fn": edit_file,
        "description": ("Replace an exact string in a file. old_string must match exactly "
                        "(including whitespace/indentation) and be unique unless replace_all=true. "
                        "Include the line's existing leading indentation in BOTH old_string and "
                        "new_string; do not add extra indentation to new_string."),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean"},
        }, "required": ["path", "old_string", "new_string"]},
    },
    {
        "name": "run_command", "fn": run_command,
        "description": "Run a shell command (PowerShell on Windows, bash elsewhere). Use for tests, build, git.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"},
        }, "required": ["command"]},
    },
    {
        "name": "ask_user", "fn": ask_user,
        "description": ("Ask the human a brief clarifying question when you are genuinely "
                        "blocked or the task is ambiguous. Do NOT use it for anything you can "
                        "find yourself by reading the code. If no human is available it returns "
                        "a note telling you to proceed; act on your best judgment then."),
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string"},
        }, "required": ["question"]},
    },
    {
        "name": "update_plan", "fn": update_plan,
        "description": ("Record or update your plan as a tracked checklist for a multi-step task. "
                        "Call it first to lay out the steps, then again to mark progress. Statuses: "
                        "pending, in_progress, completed. Keep exactly one step in_progress at a time."),
        "parameters": {"type": "object", "properties": {
            "steps": {"type": "array", "items": {"type": "object", "properties": {
                "content": {"type": "string"},
                "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
            }, "required": ["content", "status"]}},
        }, "required": ["steps"]},
    },
    {
        "name": "spawn_agent", "fn": spawn_agent,
        "description": ("Delegate a self-contained subtask to a fresh subagent that has its own "
                        "clean context. Use it to offload a big search or an independent subtask so "
                        "your own context stays focused. The subagent CANNOT see this conversation — "
                        "give it a complete, standalone instruction. It returns only its final answer."),
        "parameters": {"type": "object", "properties": {
            "task": {"type": "string", "description": "A complete, standalone instruction for the subagent."},
        }, "required": ["task"]},
    },
    {
        "name": "request_dir", "fn": request_dir,
        "description": ("Request READ access to a directory OUTSIDE your workspace when a task "
                        "needs it. This ASKS the user to approve (you cannot grant it yourself). "
                        "On approval you can read files there with absolute paths. If denied or no "
                        "human is present, do not try to read it. Don't review a folder you can't access."),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Absolute path of the directory to access."},
            "why": {"type": "string", "description": "Brief reason you need it (shown to the user)."},
        }, "required": ["path"]},
    },
]


# Opt-in web tools — added to the active toolset by src/toolset.py only when
# CODE_ENABLE_WEB is on, so the model isn't offered them when egress is disabled.
WEB_TOOLS = [
    {
        "name": "web_fetch", "fn": web_fetch,
        "description": ("Fetch a URL and return its text. Sends the URL OFF this machine - use "
                        "only for genuinely external information (docs, references)."),
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"},
        }, "required": ["url"]},
    },
    {
        "name": "web_search", "fn": web_search,
        "description": ("Search the web for a query and return results. Sends the query OFF this "
                        "machine. Read local code first; use this only for external information."),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
        }, "required": ["query"]},
    },
]


# Opt-in memory tool — added to the active toolset by src/toolset.py only when
# CODE_MEMORY is on. Non-mutating for permission gating (the agent's notebook, not a
# project edit), so it works even in plan mode; still inside the workspace fence.
MEMORY_TOOLS = [
    {
        "name": "remember", "fn": remember,
        "description": ("Save a durable note to PROJECT memory that is reloaded in future "
                        "sessions. Use it for lasting facts about THIS repo - conventions, "
                        "where key things live, build/test quirks, decisions - NOT transient "
                        "task state (use update_plan for that)."),
        "parameters": {"type": "object", "properties": {
            "note": {"type": "string"},
        }, "required": ["note"]},
    },
]


class Registry:
    def __init__(self, tools):
        self.tools = {t["name"]: t for t in tools}

    def run(self, name, args, ctx):
        t = self.tools.get(name)
        if not t:
            return ToolResult(False, f"Unknown tool: {name}")
        try:
            return t["fn"](args, ctx)
        except Exception as e:  # never let a tool crash the loop
            return ToolResult(False, f"Tool error: {type(e).__name__}: {e}")


def openai_schemas(tools):
    """Convert TOOLS into the OpenAI/LiteLLM 'tools' format."""
    return [{"type": "function", "function": {
        "name": t["name"], "description": t["description"], "parameters": t["parameters"],
    }} for t in tools]
