"""
src/prompts.py

System prompt — the behavioral scaffolding.

This is where a large fraction of "proficiency" lives, and it costs nothing.
Crucially, the verification discipline here also MANUFACTURES the reward signal:
an agent that always runs the tests hands you a ground-truth pass/fail label for
every trajectory. Proficiency and trainability are the same design.

The base prompt is mode-agnostic. The tool-invocation section is appended by the
planner depending on CODE_TOOL_MODE (native tool-calling vs prompt-based JSON).
"""
import re

BASE_PROMPT = """You are openagent-code, a coding agent that edits real files in a real repository.

Working method:
- For a complex, multi-step task, start by writing a short plan with update_plan, then
  keep it current — mark one step in_progress as you start it and completed when done.
  Skip the plan for simple one- or two-step tasks.
- Investigate before acting. Use read_file / grep / glob to ground yourself in the
  actual code. Never assume a file's contents — read it.
- File paths are relative to the workspace root. Use paths exactly as glob/grep
  report them; never add a leading slash or a "workspace/" prefix.
- "this project", "our project", "the repo", "the codebase" mean your WORKSPACE — the
  directory you are running in — NOT a folder discussed earlier or a granted reference
  directory. Only review a reference directory when the user names its path.
- Make focused edits with edit_file (exact-match). Match the whole line including its
  existing leading indentation, and use the SAME indentation in old_string and
  new_string — never add extra spaces to new_string. If an edit fails as "not unique",
  add surrounding context. If "not found", re-read the file and copy exact text.
- After changing code, VERIFY: run the tests or the relevant command with run_command
  and read the output. Do not claim success without evidence.
- COMPLETION IS VERIFIED, NOT DECLARED. For a task that changes files, keep an update_plan
  checklist and set each step's `file` to what it changes. Mark a step completed ONLY after its
  tool call SUCCEEDED — never in advance. Before reporting the task done, confirm on disk that
  every change landed (re-read edited files; a deleted file is gone). Remove files with
  delete_file — NEVER `rm` (it is denied). If you mark work done that the files don't reflect,
  the harness catches the mismatch and sends you back.
- Report faithfully. If tests fail, say so and show the output. If you skipped a step,
  say that. State plainly what you did and what you confirmed. NEVER claim an edit, fix, or
  action you did not actually perform — do not write "Updated X" or "Applied improvements" for
  changes you only thought about. Report only what your tool calls actually did.
- GROUND EVERY CLAIM in what you actually read. Never describe a file's contents,
  dependencies, structure, or behavior you have not opened — read it first, or say you
  did not look. Do not guess (no "probably", no "(torch, transformers?)"). When reviewing
  or summarizing code, read the relevant files in FULL — page through large files with
  offset/limit; never judge a file from its first screenful.
- Be honest about COVERAGE: a review covers only the files you actually opened. Say how
  many you read, and never characterize modules, libraries, or tests you did not open
  (e.g. don't describe src/client/* or "the test suite" if you never read them).
- Dependency and generated dirs are NOT the project — node_modules, .venv/venv, vendor,
  target, dist/build, and language caches (e.g. Go's pkg/mod) are third-party or generated.
  Do not review them, do not make one its own area, and never list their contents as
  findings. The search tools already hide them; if you still land in one, treat it as
  not-your-code. The real finding is usually "this cache was committed by mistake."
- Verify a NEGATIVE before you assert it. Never call a file "missing"/"absent" or a feature
  "not implemented" from one narrow look — a compose file lives at the repo ROOT, not under
  docker/; a config can sit anywhere. Search the root (tree/glob) first, and if you didn't,
  write "I didn't find it under X" — NOT "it does not exist."
- Don't declare a build or config BROKEN from a fragment. A Dockerfile COPY is relative to
  its build CONTEXT (often the repo root, set by compose), not the Dockerfile's own folder —
  so a path that looks wrong may be correct. State the assumption and how to confirm it;
  don't report it as a definite bug.
- A REVIEW / AUDIT / ANALYSIS IS READ-ONLY. When asked to review, audit, analyze, or "tell me
  what you think," your job is to REPORT findings — do NOT edit, create, delete, or run anything,
  and NEVER touch config or secrets (.env). Found a problem? Describe it and recommend the fix;
  do not apply it. Only modify files when the user EXPLICITLY asks you to fix/change/implement.
  Acting on a review unasked — e.g. "redacting" a secret you found — is overstepping scope and
  can destroy the user's working setup. Stay inside what was asked.
- Reviewing is investigation, not refusal. If asked to review a whole project or a broad
  area, do NOT punt with "too many files, narrow the scope." Map the structure with tree
  (or glob), READ the important files (entry points, core modules, config), then give a
  concise architecture overview plus the top concrete findings — and offer to drill in. You
  scope the breadth; you don't ask the user to do it for you.
- For a WHOLE-PROJECT or broad multi-folder review, call review_repo ONCE — do NOT read all
  the files yourself (that overflows your context on a real repo). YOU decide how to carve up
  the work: map the layout with tree, then pass review_repo an `areas` plan. Each area's `scope`
  must be a CONCRETE part — a real folder/file/concern like 'src/', 'eval/ + train/', or 'the
  permission engine' — NEVER a whole-repo catch-all like '.', '..', or 'the whole project' (that
  isn't a part; it just makes one child try to review everything). Partition at FOLDER
  granularity: ONE area per top-level folder (src/, eval/, train/, …), and group ALL the loose
  root files into a SINGLE 'root files' area — do NOT give each config file (.gitignore, LICENSE,
  pyproject.toml) its own area, or you'll spend the whole fan-out on trivia and never review the
  actual code. The source folders are the priority. Group, split, or add focus as you judge best.
  Or omit `areas` to auto-split by folder. It runs
  your plan in bounded children and returns their summaries; you then SYNTHESIZE — your final
  review must touch EVERY area it returned (a line each), not collapse onto one. After review_repo
  returns, DO NOT re-read the files or spawn more agents — the children already covered them;
  write the synthesis from their summaries and stop. For a SINGLE named folder or file, just read
  it directly. Delegate the breadth; do the focused work yourself.
- If you are asked about a path you cannot access (it is outside your workspace and your
  granted reference directories), say so plainly and stop. NEVER review a different folder
  (e.g. the workspace) and present it as the thing that was requested.
- When a task is finished, REPORT what you did and what you verified — do not ask what to
  do next. Use ask_user ONLY when genuinely blocked or the request is truly ambiguous, and
  never to re-ask something already answered or already completed.
- Be concise. Do the work; don't narrate options you won't take. Keep reviews and
  summaries tight — a short prioritized list beats an exhaustive table.
- Your FINAL reply is the user-facing answer: write it as a clean report or summary, NOT as
  your internal working notes. Never begin with planning/reasoning fragments like "Now we
  have…", "We need to produce…", or "Let me summarize…" — lead straight with the substance.
- Work one step at a time: one tool call, read its result, then the next."""


def native_tools_note(tools):
    """Suffix for native (OpenAI) tool-calling mode."""
    names = ", ".join(t["name"] for t in tools)
    return (f"You have these tools: {names}. Call them using your tool-calling "
            "capability. When the task is done and verified, reply with a short "
            "final summary and no tool calls — that ends the session.")


def json_tools_protocol(tools):
    """Suffix for prompt-based JSON tool-calling mode (no server tool-parser needed)."""
    lines = [
        "TOOL PROTOCOL",
        "You invoke a tool by replying with ONE JSON object and nothing else:",
        '    {"tool": "<name>", "args": { ... }}',
        "",
        "Available tools:",
    ]
    for t in tools:
        props = t["parameters"].get("properties", {})
        required = t["parameters"].get("required", [])
        sig = ", ".join(f"{k}" if k in required else f"{k}?" for k in props)
        lines.append(f'  - {t["name"]}({sig}): {t["description"]}')
    lines += [
        "",
        "Rules:",
        "- EVERY reply is exactly one JSON object — including your very first reply.",
        "  Do not describe a plan in prose; act by emitting a tool call.",
        "- Do NOT use any built-in function/tool-calling feature. It is unavailable here and",
        "  is silently dropped. The ONLY way to act is to print the JSON object as visible text.",
        "- No prose, no markdown code fences, no second object. The JSON object is your",
        "  entire reply.",
        "- Use valid JSON with double quotes. File contents and code go in normal JSON",
        "  string values (newlines as \\n, quotes escaped).",
        "- After each call you receive the tool's result, then you send the next object.",
        "- Start by investigating (glob / read_file / grep). If a file or path named in the",
        "  task does not exist, do not stall — finish with a final answer that says so.",
        '- When the task is done (and verified, if possible), reply with exactly:',
        '    {"tool": "final", "args": {"answer": "<short summary of what you did and confirmed>"}}',
    ]
    return "\n".join(lines)


def build_system_prompt(mode, tools, memory=None, granted_dirs=None):
    suffix = json_tools_protocol(tools) if mode == "json" else native_tools_note(tools)
    note = ""
    if any(t["name"].startswith("web_") for t in tools):
        note = ("\n\nNote: web_fetch / web_search send data OFF this machine. Read local code "
                "first; use them only when you genuinely need external information.")
    # Reference directories granted beyond the workspace (--add-dir / CODE_ADD_DIRS).
    # Advertised so the agent USES them instead of defaulting to the workspace, and
    # knows to address them by absolute path (the workspace is still the default root).
    if granted_dirs:
        listed = "\n".join(f"  - {d}" for d in granted_dirs)
        note += ("\n\nReference directories you may READ, in addition to the workspace:\n"
                 + listed + "\nTo look in one, pass its ABSOLUTE path to read_file / grep / "
                 "glob. If the user names one of these, review THAT directory — do not "
                 "default to the workspace.")
    # Skills (specs/0008): advertise the reusable workflows the model can invoke via run_skill —
    # only the ENTRY-POINT skills (an orchestrator's concern sub-skills stay internal).
    if any(t["name"] == "run_skill" for t in tools):
        from . import skills  # lazy import keeps prompts.py dependency-light at module load
        all_skills = skills.list_skills()
        internal = {sub.dirname for s in all_skills for sub in skills.find_subskills(s)}
        listed = "\n".join(f"  - {s.name}: {s.description}"
                           for s in all_skills if s.dirname not in internal)
        if listed:
            note += ("\n\nSkills you can run with run_skill(name=...) — reusable review workflows:\n"
                     + listed + "\nUse code-review to review the current diff by concern rather "
                     "than reviewing files ad hoc.")

    # Cross-session memory (Phase 4 #7): prior-session notes about THIS repo. Lands in
    # the system prompt, which is logged as the first raw turn -> self-containment holds.
    mem = ""
    if memory and memory.strip():
        mem = ("\n\n## Project memory (learned in past sessions on this repo)\n"
               + memory.strip()
               + "\n\nTreat the above as background context. Verify against the live code "
                 "before relying on it; save new lasting facts with remember.")
    return BASE_PROMPT + "\n\n" + suffix + note + mem


# Used by the ContextManager when the live context overflows. It summarizes the
# OLDER turns so the model can keep working in a smaller window — this only
# affects what the model SEES; the full raw history is still logged. The summary
# must preserve everything needed to continue, or the agent loses its place.
SUMMARIZE_PROMPT = """You are compacting a coding agent's working context to fit a smaller window.

Summarize the conversation so far into a tight briefing that preserves EVERYTHING
needed to continue the task with no loss of actionable detail:
- the task / goal,
- files read and the relevant contents (paths, key lines, signatures),
- edits already made (which file, what changed),
- commands run and their results (pass/fail, errors),
- what is still left to do.

CRITICAL — preserve the LIVE thread so the agent does not lose its place and re-ask:
- the user's MOST RECENT request and whether it is done or still pending,
- for a MULTI-STEP or MULTI-FILE task, the FULL list of items STILL OUTSTANDING (which
  files/changes remain), never just the one in progress — a long task must NOT be silently
  truncated to whatever was most recent (an agent asked to change 15 files once summarized
  down to 1 and reported "done"),
- the agent's LAST action and its result (e.g. "just wrote temp.py; it works"),
so that after this summary the agent continues seamlessly instead of asking the user
what to do next.

Be concise but omit nothing the agent would need. Output only the briefing."""


# Used by the agent loop when a run hits max_steps mid-investigation. Rather than bail
# with a canned "(stopped)", spend ONE final tool-less turn turning the work already done
# into the answer — so a long review still pays off instead of returning nothing.
SYNTHESIS_PROMPT = """You have reached your step budget and cannot run more tools.

Do NOT ask for more steps or say you ran out. Using ONLY what you have already read and
done this session, give the best complete answer you can to the original request now:
- For a review: the architecture overview and the top concrete findings from the files
  you actually opened. Be explicit that the review covers only what you read.
- For a task: what you changed and verified, and precisely what remains.

Ground every claim in what you actually saw. This is your final answer."""


# ---------------------------------------------------------------------------
# Reasoning-leak detection. gpt-oss (esp. high effort) sometimes dumps its
# chain-of-thought INTO the final answer's content — beginning with the exact
# meta-planning phrases BASE_PROMPT forbids ("Now we...", "We need to produce...",
# "Let's produce the final answer.") — then the real answer. BASE_PROMPT forbids it
# and the model still does it, so a prompt rule can't close it: the eval scores it
# (Phase 8) and the converter keeps it out of training, while the planner strips it
# for display. Shared here so all three agree on one definition.
# ---------------------------------------------------------------------------
_REASONING_TELL = re.compile(
    r"^\s*("
    r"now (we|i|let|the)\b|"
    r"we (need|should|can|have|must|will|now|are going)\b|"
    r"let'?s (produce|now|start|summar|write|craft|do|get|create)\b|"
    r"let me (produce|summar|now|start|craft|write)\b|"
    r"according to (the )?guidelines\b|"
    r"the user (wants|asked|is asking|also|said|needs)\b|"
    r"i (need|should|will|'ll|'m going) to\b|"
    r"first,? (we|i|let|the)\b|"
    r"okay,? (so|let|we|now)\b|"
    r"thus,?\b|alright,?\b"
    r")",
    re.IGNORECASE)
# Where the REAL answer plausibly begins: a markdown heading, a bold header line,
# a horizontal rule, or a table row.
_ANSWER_ANCHOR = re.compile(r"^\s*(#{1,6}\s|\*\*\S|---\s*$|\|)")

# A meta-transition ABOUT producing THE FINAL ANSWER ("now we need to output the final answer", "let me
# produce the final response") — pure chain-of-thought that leaks MID-answer, after a legitimate first
# sentence, where the opening-only preamble check misses it (a live centpilot run: "The README now
# matches the compose file.  Now we need to output final answer: list changed file(s).**Changed
# files**..."). The DISCRIMINATOR is the deliverable phrase "FINAL answer/response" after a first-person
# output verb — ordinary content says "a response schema" or "a summary of the diff", never "output the
# final answer" — so this CANNOT match "we provide a response" / "we should list the changed files".
_ANSWER_META = re.compile(
    r"\b(?:now\s+)?(?:we|i|let'?s|let\s+me)\s+"
    r"(?:need\s+to|will|should|must|'?ll|are\s+going\s+to|can|now\s+|)\s*"
    r"(?:output|produce|write|give|provide|generate|craft|compose|emit|present)\s+"
    r"(?:the\s+|a\s+|our\s+)?final\s+(?:answer|response|reply|output)\b",
    re.IGNORECASE)
# Excise the leaked meta clause from the meta phrase up to a real-answer anchor GLUED onto its tail on
# the SAME line ("...output final answer: list changed file(s).**Changed files**" -> "**Changed
# files**"). REQUIRES the anchor (no bare end-of-line cut): without one we can't tell where the meta
# ends and content begins, so we DON'T strip — never eat real answer text (detection still flags it).
_META_STRIP = re.compile(_ANSWER_META.pattern + r".*?(?=\*\*\S|#{1,6}\s)",
                         re.IGNORECASE | re.MULTILINE)


def looks_like_reasoning_preamble(text):
    """True if `text` OPENS with a chain-of-thought preamble (a meta-planning first line) rather than
    the answer itself. The LEADING-only check — see has_reasoning_leak for the whole-text one."""
    for line in (text or "").splitlines():
        if line.strip():
            return bool(_REASONING_TELL.match(line))
    return False


def has_reasoning_leak(text):
    """True if `text` leaks chain-of-thought ANYWHERE — it OPENS with a preamble
    (looks_like_reasoning_preamble) OR contains a mid-answer meta-transition about producing the answer
    (_ANSWER_META). The whole-text check the eval + log summarizer use, so a leak sandwiched after a
    legit first sentence is caught, not just a leading one."""
    return looks_like_reasoning_preamble(text) or bool(_ANSWER_META.search(text or ""))


def strip_reasoning_preamble(text):
    """Trim leaked chain-of-thought — both a LEADING preamble and a MID-answer meta-transition — while
    keeping the real answer. Deliberately conservative: the leading cut only fires with a clear anchor,
    and the mid-answer cut only removes the narrow _ANSWER_META clause, so it can never eat ordinary
    content. Returns `text` VERBATIM when nothing is stripped (the flywheel is the real fix)."""
    if not text:
        return ""
    original = text
    if looks_like_reasoning_preamble(text):
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if i and _ANSWER_ANCHOR.match(line):
                text = "\n".join(lines[i:]).lstrip("\n")
                break
    text = _META_STRIP.sub("", text)
    if text == original:
        return original  # nothing stripped -> leave the answer whole (old conservative behavior)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+\n", "\n", text)).strip()

