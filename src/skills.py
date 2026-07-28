"""
src/skills.py

Skills (specs/0008) — reusable, harness-orchestrated workflows defined as Markdown SKILL.md files.
A directory-per-skill Markdown convention, implemented as our own small Python.

A skill is a directory skills/<name>/SKILL.md = a `---` frontmatter block + a Markdown body. Two kinds:
  * LEAF (a concern): frontmatter name/description; body = a review rubric.
  * ORCHESTRATOR: adds `subskills: <glob>` over sibling dir names; body = the synthesis rubric.

`run_skill` (the tool) does the DECOMPOSITION IN THE HARNESS, not in the model — the same reason
src/orchestrator.py exists: model-driven fan-out fails a new way every run on a weak model. An
orchestrator skill fans out one CAPTURING subagent per concern over the current diff, then the lead
synthesizes their findings. review_repo partitions by FOLDER; run_skill partitions by CONCERN.

Import discipline mirrors orchestrator.py: only `config` at module top; ToolResult is imported
LAZILY inside run_skill to avoid the tools<->skills import cycle.
"""
import os
import fnmatch
import subprocess
from dataclasses import dataclass, field

from . import config

_DIFF_CAP = 20000   # max chars of diff embedded into each concern child's task


@dataclass
class Skill:
    name: str
    description: str
    body: str
    meta: dict = field(default_factory=dict)
    dirname: str = ""
    path: str = ""


def _parse_frontmatter(text):
    """Split a leading `---\\n...\\n---` block into (meta, body). Hand-rolled scalar `key: value`
    reader (no PyYAML) — enough for the scalar fields skills use, and it NEVER raises. Missing or
    malformed frontmatter -> ({}, whole text)."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text
    meta = {}
    for line in lines[1:end]:
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    return meta, body


def parse_skill(text, dirname, path):
    meta, body = _parse_frontmatter(text)
    return Skill(name=meta.get("name") or dirname, description=meta.get("description", ""),
                 body=body, meta=meta, dirname=dirname, path=path)


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def load_skill(name):
    """Load skills_dir()/<name>/SKILL.md, or None if absent. Never raises."""
    path = os.path.join(config.skills_dir(), name, "SKILL.md")
    text = _read(path)
    return parse_skill(text, name, path) if text is not None else None


def list_skills():
    """Every skill (an immediate subdir holding a SKILL.md). Never raises (missing dir -> [])."""
    root = config.skills_dir()
    out = []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return out
    for d in entries:
        path = os.path.join(root, d, "SKILL.md")
        if os.path.isfile(path):
            text = _read(path)
            if text is not None:
                out.append(parse_skill(text, d, path))
    return out


def find_subskills(skill):
    """Skills whose dirname matches the orchestrator's `subskills` glob, EXCLUDING the orchestrator
    itself — the Pythonic 'all code-review-* other than this one'."""
    pat = (skill.meta.get("subskills") or "").strip()
    if not pat:
        return []
    return [s for s in list_skills()
            if s.dirname != skill.dirname and fnmatch.fnmatch(s.dirname, pat)]


def bundled_scripts(skill):
    """Absolute paths of files under the skill's scripts/ dir — the 'skill + helper script'
    bundling pattern (C2). The model runs them via run_command / reads them via read_file. [] if there's no
    scripts/ dir. Never raises."""
    d = os.path.join(os.path.dirname(skill.path), "scripts")
    try:
        return [os.path.join(d, f) for f in sorted(os.listdir(d))
                if os.path.isfile(os.path.join(d, f))]
    except OSError:
        return []


def _current_diff(cwd, target=None):
    """(diff_text, changed_files) for the workspace, or (None, reason). Runs git in `cwd` via
    subprocess (NOT the run_command tool, so it isn't permission-gated and children stay pure-read).
    Never raises. `git diff HEAD` (all uncommitted vs the last commit), falling back to `git diff`
    when there is no HEAD yet. `target` scopes it with a pathspec."""
    ps = ["--", target] if target else []

    def _git(args):
        try:
            # encoding='utf-8', errors='replace' (matching _read): git emits UTF-8, but the default
            # text=True decodes with the PLATFORM encoding (cp1252 on Windows), which RAISES on any
            # byte undefined there — e.g. the UTF-8 of a curly quote / em-dash / emoji / CJK that
            # this repo actually contains — nulling stdout while returncode stays 0, so a real diff
            # would be silently reported as "no diff" and reviewed by nobody.
            p = subprocess.run(["git", *args, *ps], cwd=cwd, capture_output=True,
                               encoding="utf-8", errors="replace", timeout=30)
            return p.stdout if p.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    stat, diff = _git(["diff", "HEAD", "--stat"]), _git(["diff", "HEAD"])
    if diff is None:   # no HEAD (e.g. no commits yet) -> plain working diff
        stat, diff = _git(["diff", "--stat"]), _git(["diff"])
    if diff is None:
        return None, "this workspace is not a git repository (no diff to review)."
    if not diff.strip():
        return None, "the working tree is clean — there is no diff to review."
    if len(diff) > _DIFF_CAP:
        diff = diff[:_DIFF_CAP] + "\n... (diff truncated; read_file for the rest)"
    return diff, (stat or "").strip()


def _concern_task(concern, leaf_body, diff, changed):
    return (
        f"You are reviewing ONE concern of a code change, in isolation: {concern}.\n\n"
        f"{leaf_body.strip()}\n\n"
        f"Changed files:\n{changed}\n\n"
        f"The diff to review:\n```diff\n{diff}\n```\n\n"
        f"You MAY read_file for surrounding context, but the diff above is the change under review. "
        f"Report NUMBERED findings, each with a file path + line number; if there is nothing to flag "
        f"for this concern, say so briefly. This is a REVIEW — do NOT edit, create, or run anything. "
        f"Return only your findings."
    )


def run_skill(args, ctx):
    """Invoke a skill by name. LEAF skill -> return its body as guidance. ORCHESTRATOR skill (has
    `subskills`) -> deterministically fan out one CAPTURING subagent per concern over the current
    diff, then hand the lead a digest to synthesize. Harness-driven, like review_repo."""
    from .tools import ToolResult  # lazy: avoids the tools<->skills import cycle

    name = (args.get("name") or "").strip()

    def _avail():
        return ", ".join(s.name for s in list_skills()) or "(none found)"

    if not name:
        return ToolResult(False, f"run_skill needs a 'name'. Available skills: {_avail()}")
    skill = load_skill(name)
    if skill is None:
        return ToolResult(False, f"No skill named {name!r}. Available skills: {_avail()}")

    subs = find_subskills(skill)
    if not subs:
        # Leaf skill: inject its guidance. Expose any BUNDLED scripts by ABSOLUTE path (C2) so the
        # model can run them via run_command / read them via read_file, and pass the target through.
        body = skill.body
        scripts = bundled_scripts(skill)
        if scripts:
            body += ("\n\nBundled scripts for this skill (run with run_command, or read_file them):\n"
                     + "\n".join(f"  - {p}" for p in scripts))
        target = (args.get("target") or "").strip()
        if target:
            body += f"\n\ntarget: {target}"
        return ToolResult(True, body)

    # Orchestrator: harness-driven concern fan-out (guards mirror review_repo).
    if ctx.spawn is None:
        return ToolResult(False, "Subagents are unavailable here, so this skill cannot fan out.")
    if ctx.depth >= 1:
        return ToolResult(False, "This skill orchestrates a top-level review; you are already a "
                                 "scoped child — review your assigned concern directly.")

    diff, info = _current_diff(ctx.cwd, (args.get("target") or "").strip() or None)
    if diff is None:
        return ToolResult(True, f"Nothing to review: {info}")

    from .fanout import fanout  # local import keeps skills import-light (fanout is pure stdlib)
    subs = subs[:config.MAX_REVIEW_AREAS]
    tasks = [_concern_task(s.name, s.body, diff, info) for s in subs]   # pure builders -> prebuild is byte-safe
    results = fanout(ctx.spawn, tasks, config.WORKFLOW_CONCURRENCY)     # specs/0039: bounded parallel, read-only children
    findings = [(s.name, (r or "").strip() or "(no findings returned)") for s, r in zip(subs, results)]

    parts = [f"Reviewed the current diff across {len(findings)} concern(s):\n"]
    for concern, text in findings:
        parts.append(f"### {concern}\n{text}\n")
    synthesis = skill.body.strip() or ("Synthesize the findings above into ONE numbered review, "
                                       "each item with a file path + line.")
    from . import prompts   # specs/0041: reply_shape_caveat() (prompts imports config only -> no cycle)
    parts.append("\n" + synthesis + "\n\nWrite that final review NOW from the findings above. Do "
                 "NOT re-read files or call more tools — the concern reviews already covered the "
                 "diff. This is a REVIEW: report only; do not edit or run anything." + prompts.reply_shape_caveat())
    return ToolResult(True, "\n".join(parts), {"concerns": len(findings)})
