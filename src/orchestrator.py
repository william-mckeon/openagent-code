"""
src/orchestrator.py

Deterministic map-reduce review — the fix for "review the whole project."

The recurring failure was structural, not a prompt bug: a single agent in a single
context window cannot hold a whole repo, and asking the MODEL to decompose (spawn one
child per folder, in the right order, without bloating itself first) failed a new way
every run — out of steps, half-done, then context overflow (>131k -> BadRequestError).

So the harness does the decomposition, not the model. `review_repo`:
  1. lists the top-level areas (each folder; loose root files as one bucket) — in CODE,
  2. spawns ONE bounded child agent per area (own clean context, own budget, scoped to
     just that area), capped at CODE_MAX_SUBAGENT_FANOUT,
  3. returns the small per-area summaries.

The lead model then writes the final synthesis over those summaries — never the raw
files — so its context stays bounded by construction. Decomposition can no longer be
skipped, misordered, or overflowed, because the model isn't the one doing it.
"""
import os

from . import config
# NOTE: do NOT import from .tools at module level — tools.py imports review_repo from here,
# so a module-level import would be a circular dependency sensitive to import order. ToolResult
# is imported lazily inside review_repo() instead.

# The dirs never walked / never their own review area now live in config.SKIP_DIRS — ONE
# source of truth shared with the search tools (grep/glob/tree), so the project MAP and the
# review PARTITION can't disagree (they drifted before: 5 names vs 13). config.looks_like_dep_cache
# additionally drops dependency stores (a committed Go module cache, ...) so they never
# become a review area.


def _areas(root):
    """Top-level review units under `root`: every visible subdirectory, plus a single
    bucket of the loose root files. Returns (dir_names, root_file_names)."""
    dirs, files = [], []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return [], []
    for e in entries:
        full = os.path.join(root, e)
        if os.path.isdir(full):
            # Skip noise/generated/vcs dirs, hidden dirs (.git, .claude, ...), egg-info, and
            # vendored dependency stores (a committed Go module cache, ...) — third-party, not
            # the project, so they must never become a review area.
            if (e not in config.SKIP_DIRS and not e.startswith(".")
                    and not e.endswith(".egg-info") and not config.looks_like_dep_cache(full)):
                dirs.append(e)
        elif os.path.isfile(full):
            files.append(e)
    return dirs, files


def _degenerate_scope(scope):
    """A scope that names the WHOLE repo rather than a part of it. Spawning a child for one of
    these defeats the partition — the child tries to review everything and (being unscoped) asks
    what to do. A real area names a concrete folder/file/concern."""
    s = scope.strip().strip("/").lower()
    return s in ("", ".", "..", "*", "**", "the whole project", "whole project", "the project",
                 "everything", "all", "all files", "the entire repo", "entire repo", "the repo",
                 "repo", "root", "the codebase", "codebase")


def _is_root_file(scope, root):
    """True if `scope` names a single file at the repo root (e.g. '.gitignore', 'LICENSE').
    These should NOT each get their own review child — collapsing them frees the fan-out
    budget for the folders (esp. src/)."""
    s = scope.strip().strip("/")
    return "/" not in s and os.path.isfile(os.path.join(root, s))


def _balance_plan(units, root, focus):
    """Make the model's `areas` plan safe to execute without dropping the actual code.

    Two guardrails, because 'review the source' is a must-not-fail, not a style preference:
      1. Collapse many individual ROOT-FILE areas into ONE 'root files' area — a plan that
         gives .dockerignore / LICENSE / pyproject.toml each their own child burns the
         fan-out cap on trivia and never reaches src/ (observed live).
      2. Ensure EVERY top-level folder is covered — append any directory the plan skipped, so
         the code can't be left unreviewed regardless of how the model partitioned.
    Folders sort ahead of the single root-files bucket, so if the cap still bites, the
    substance survives. The model keeps its agency (grouping + per-area focus); the harness
    just guarantees completeness."""
    file_units = [u for u in units if _is_root_file(u[0], root)]
    rest = [u for u in units if not _is_root_file(u[0], root)]
    if len(file_units) > 1:
        names = ", ".join(u[0] for u in file_units)
        rest.append(("the root-level files",
                     f"Review ONLY these root files (do not enter subfolders): {names}.", focus))
    else:
        rest += file_units  # 0 or 1 explicit root file — leave as the model asked
    dirs, _ = _areas(root)
    covered = " ".join(u[0].lower() for u in rest)
    for d in dirs:
        if d.lower() not in covered:
            rest.append((f"{d}/", f"Review ONLY the files under '{d}/'.", focus))
    rest.sort(key=lambda u: u[0] == "the root-level files")  # root-files bucket last
    return rest


def _child_task(area_label, scope_line, focus):
    focus_clause = f"Focus specifically on {focus}. " if focus else ""
    return (
        f"You are reviewing ONE part of a larger codebase, in isolation. {scope_line} "
        f"Do NOT read anything outside that scope. {focus_clause}"
        f"In UNDER 200 words, summarize: (1) the purpose of {area_label}, (2) how it is "
        f"structured, and (3) the top 2-3 concrete issues, risks, or improvements. Ground "
        f"every point in files you actually opened; if you couldn't read something, say so. "
        f"Return only the summary."
    )


def review_repo(args, ctx):
    """Deterministically fan a broad review out across the repo's top-level areas.

    Returns a digest of per-area summaries for the lead to synthesize. The lead must NOT
    read the files itself — that is exactly what overflows the context on a big repo.
    """
    from .tools import ToolResult  # lazy: avoids a tools<->orchestrator import cycle
    if ctx.spawn is None:
        return ToolResult(False, "Subagents are unavailable here, so review_repo cannot fan out. "
                                 "Review a single named folder directly instead.")
    # Guard against runaway nesting: this is a top-level orchestration tool. A child that
    # is already reviewing one area should review it directly, not fan out again.
    if ctx.depth >= 1:
        return ToolResult(False, "review_repo is for the top-level review only; you are already a "
                                 "scoped sub-review — read your assigned files directly.")

    rel = (args.get("path") or ".").strip() or "."
    focus = (args.get("focus") or "").strip() or None
    root = rel if os.path.isabs(rel) else os.path.normpath(os.path.join(ctx.cwd, rel))
    if not os.path.isdir(root):
        return ToolResult(False, f"Not a directory: {rel}")

    cap = config.MAX_REVIEW_AREAS
    # AGENTIC PATH: the model may propose its OWN partition via `areas` — which parts to review
    # and what each should focus on (by folder, by concern, grouping or skipping as it judges
    # best). The harness decides nothing about the carve-up; it only GUARANTEES safe execution:
    # one bounded child per area, capped fan-out, summaries collected. The model gets the agency
    # (the plan) without the risk (it never reads the files itself — that's what overflows).
    # Omit `areas` and the harness auto-splits by top-level folder as a sensible default.
    plan = args.get("areas")
    units = []  # (label, scope_line, area_focus)
    source = "auto-split by folder"
    if isinstance(plan, list) and plan:
        for a in plan:
            if isinstance(a, dict):
                scope = (a.get("scope") or a.get("path") or "").strip()
                area_focus = (a.get("focus") or "").strip() or focus
            else:
                scope, area_focus = str(a).strip(), focus
            # Drop degenerate "review everything" scopes ('.', '..', 'the whole project', ...):
            # they aren't a PART of the repo, so they make one child try to review all of it and
            # ask the user what to do. A real partition names concrete folders/areas.
            if scope and not _degenerate_scope(scope):
                units.append((scope, f"Review this part of the repo: {scope}. Do NOT read "
                                     f"anything outside that scope.", area_focus))
        if units:
            # Balance the plan: collapse root-file spam, guarantee every folder is covered.
            units = _balance_plan(units, root, focus)
            source = "your plan"

    if not units:
        # No usable plan (omitted, or only vague/whole-repo scopes) -> auto-split by folder.
        # Root files FIRST — the project's front door (README, pyproject, configs) gives the
        # orientation the synthesis hangs on, so it's never the unit the cap drops.
        dirs, root_files = _areas(root)
        base = "" if rel in (".", "") else rel.rstrip("/") + "/"
        if root_files:
            listed = ", ".join(root_files[:40])
            units.append(("the root-level files",
                          f"Review ONLY these root files (do not enter subfolders): {listed}.", focus))
        for d in dirs:
            units.append((f"{base}{d}/", f"Review ONLY the files under '{base}{d}/'.", focus))

    if not units:
        return ToolResult(False, f"No reviewable areas under {rel}.")

    # Bound the fan-out. Each unit is one bounded child agent.
    truncated = units[cap:]
    units = units[:cap]

    if ctx.verbose:
        print(f"  [review_repo] fanning out across {len(units)} area(s) ({source})"
              + (f", focus={focus}" if focus else ""))

    summaries = []
    for label, scope_line, area_focus in units:
        result = ctx.spawn(_child_task(label, scope_line, area_focus))
        summaries.append((label, (result or "").strip() or "(no summary returned)"))

    # Reduce: a compact digest the lead synthesizes from. Small by construction — N short
    # summaries, never the raw files.
    parts = [f"Deterministic review fan-out over {len(summaries)} area(s)"
             + (f" (focus: {focus})" if focus else "") + ":\n"]
    for label, summary in summaries:
        parts.append(f"### {label}\n{summary}\n")
    if truncated:
        names = ", ".join(lbl for lbl, *_ in truncated)
        parts.append(f"\n[NOTE] {len(truncated)} area(s) not reviewed (fan-out cap "
                     f"{cap}): {names}. Re-run review_repo scoped to those with `path`.")
    parts.append(f"\nYou now have what you need. Write the FINAL review for the user NOW by "
                 f"synthesizing ALL {len(summaries)} summaries above — do not let one area (e.g. "
                 f"src/) crowd out the rest. Give a one-line take on EACH area, then the overall "
                 f"architecture and the top cross-cutting findings. Do NOT call read_file / tree / "
                 f"grep / spawn_agent / review_repo again: the children already covered the files, "
                 f"and re-reading or re-delegating only wastes budget and overflows your context. "
                 f"This is a REVIEW — report findings only; do not edit, create, or run anything. "
                 f"Your next reply must be the finished review, as a clean report, with no tool calls.")
    return ToolResult(True, "\n".join(parts), {"areas": len(summaries)})
