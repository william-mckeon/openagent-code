"""
src/subagent.py

Subagents (Phase 4). `spawn_agent` delegates a self-contained subtask to a fresh
agent that has its OWN clean ContextManager and its OWN trajectory (linked to the
spawning agent by `parent_session_id` + `depth`). The child runs in isolation —
it never sees the parent's conversation — and returns only its final answer, which
re-enters the parent as a tool result. So the parent's context stays focused while
the child's full work is captured separately as training data (subagents *multiply*
the dataset).

Depth is capped by CODE_MAX_SUBAGENT_DEPTH, enforced at the spawn_agent tool.

Import direction is one-way (subagent -> runtime), so wiring `ctx.spawn` here keeps
tools.py free of any agent/runtime import and avoids a cycle.
"""
from . import config
from . import outcomes
from .tools import Context
from .trajectory import Trajectory
from .runtime import build_agent


def _terminal_ask(question):
    print(f"\n[agent asks] {question}")
    try:
        ans = input("> ").strip()
    except EOFError:
        ans = ""
    return ans or "(no answer given)"


def make_context(cwd, permissions, session_id, depth=0, verbose=False, interactive=False,
                 traj_dir=None):
    """A Context with `ctx.spawn` and `ctx.ask` wired.

    `session_id` is THIS agent's trajectory id — a spawned child records it as its
    `parent_session_id`, which is how nested runs link together. `interactive`
    enables ask_user to actually prompt a human (else it degrades). `traj_dir` is where
    spawned children write their trajectory: None -> the corpus (config.trajectory_dir());
    an eval run passes trajectories/eval/ so children (e.g. the Phase-10 grounding verifier)
    stay behind the train/eval firewall (specs/0005) instead of leaking held-out eval
    content into the SFT corpus.
    """
    ctx = Context(cwd, permissions)
    ctx.verbose = verbose
    ctx.depth = depth
    ctx.session_id = session_id
    ctx.interactive = interactive
    ctx.traj_dir = traj_dir
    # effort is optional so a caller (the grounding verifier) can run the child at its own reasoning
    # effort; a bare spawn(task) keeps the parent's/global effort.
    ctx.spawn = lambda task, effort=None, label=None, read_only=False: run_subagent(
        task, ctx, effort=effort, label=label, read_only=read_only)
    ctx.ask = _terminal_ask if interactive else None
    return ctx


def _classify(result, tool_calls):
    """Honest outcome for a subagent — DELEGATES to the ONE shared mapping (src/outcomes.classify).

    This module used to keep a hand-copied duplicate of that mapping. AUDIT-FINDINGS row 3 fixed exactly
    this class in eval/harness.py ("collapsed honest labels to success on the corpus path" -> one shared
    outcomes.classify) and left this copy behind. It is a corpus-poison trap: a subagent (spawn_agent /
    review_repo child) whose run ends on a NEW gate outcome the copy doesn't know falls through to
    "completed" — a KEEP_OUTCOMES label — so a thrashing run becomes a positive SFT target. Children write
    to the same corpus, so the leak is live. Delegating means a new gate outcome can never be added in one
    place and silently mislabeled here."""
    return outcomes.classify(result.terminated, tool_calls)


def _child_permissions(parent_permissions, read_only):
    """The Permissions a spawned child runs under. A parallel/read-only fan-out child (specs/0039) gets the
    plan-mode read-only projection so concurrent children can't race the filesystem. specs/0084: a serial child
    that would otherwise INHERIT propose mode ALSO gets that projection — a depth>0 child can neither mutate
    (read-only until the manifest is approved) NOR approve (propose_changes is top-level-only), a guaranteed
    deadlock that burned whole turns live; plan mode gives it the honest 'stop and report up' terminal instead.
    Any other serial child inherits the parent's Permissions unchanged (CODE_SUBAGENT_NO_PROPOSE off ->
    byte-identical)."""
    if read_only:
        return parent_permissions.readonly_view()
    if config.SUBAGENT_NO_PROPOSE and getattr(parent_permissions, "mode", None) == "propose":
        return parent_permissions.readonly_view()
    return parent_permissions


def run_subagent(task, parent_ctx, effort=None, label=None, read_only=False):
    """Build a child agent for `task`, run it in isolation, return its final text. `effort` overrides
    the child's reasoning effort (None = the global) — e.g. a grounding verifier at CODE_GROUNDING_EFFORT.
    `label` is a short human tag for the console line (a guardian/grounding review) instead of dumping the
    raw injected prompt; it does NOT change what the child runs. `read_only` (specs/0039): a PARALLEL
    fan-out child runs under a read-only Permissions projection so concurrent children can't race the FS."""
    child_depth = parent_ctx.depth + 1
    # specs/0091: a spawned child runs at CODE_SUBAGENT_EFFORT (cheap) UNLESS its caller pinned an effort — a
    # grounding verifier (GROUNDING_EFFORT) / guardian (GUARDIAN_EFFORT) pass their own and still win. So the
    # review/spawn children go budget while the main agent keeps its premium pin. Empty -> None -> global pin.
    if effort is None and config.SUBAGENT_EFFORT:
        effort = config.SUBAGENT_EFFORT
    perms = _child_permissions(parent_ctx.permissions, read_only)
    # Children write to the parent's trajectory dir (None -> the corpus). This keeps subagents spawned
    # INSIDE an eval — e.g. the Phase-10 grounding verifier — under trajectories/eval/ (the firewall),
    # not in the training corpus (specs/0005).
    traj_dir = getattr(parent_ctx, "traj_dir", None) or config.trajectory_dir()
    traj = None
    try:
        traj = Trajectory(
            traj_dir, task, config.MODEL, parent_ctx.cwd,
            parent_session_id=parent_ctx.session_id,
            depth=child_depth,   # tool_schemas defaults to the active toolset
            safety=config.safety_fingerprint(perms),   # specs/0033: the child's ACTUAL guards (read-only when parallel)
        )
        # Children are ALWAYS non-interactive, even from a REPL: a spawned worker reviewing one
        # folder must never prompt the human (ask_user degrades to "no human - proceed"). Inheriting
        # the parent's interactive flag let a child hijack the REPL with "Could you specify the path?"
        # mid-review. The human talks to the lead; children just do their bounded task and report.
        child_ctx = make_context(parent_ctx.cwd, perms, traj.session_id,
                                 depth=child_depth, verbose=parent_ctx.verbose,
                                 interactive=False, traj_dir=getattr(parent_ctx, "traj_dir", None))
        if parent_ctx.verbose:
            print(f"  [subagent depth={child_depth}] {(label or task[:70])[:80]}")

        # specs/0027: advertise the granted reference dirs (--add-dir / request_dir) to the child's prompt so a
        # grounding verifier / reviewer can read a cited granted-dir file by ABSOLUTE path (its inherited fence
        # already permits it). run_subagent was the sole build_agent caller omitting granted_dirs. Gated on
        # CODE_VERIFY_GROUNDING_PATHS so flag-off is byte-identical (the child's prompt is unchanged).
        granted = getattr(parent_ctx.permissions, "extra_roots", None) if config.VERIFY_GROUNDING_PATHS else None
        # specs/0030: a child shares the parent's cwd (the workspace); pin it durably so a spawned worker knows
        # where "here" is too. Gated in build_system_prompt on CODE_WORKDIR_PROMPT (byte-identical off).
        agent = build_agent(traj, effort=effort, granted_dirs=granted, cwd=parent_ctx.cwd,
                            max_steps=(config.SUBAGENT_MAX_STEPS or None),   # specs/0091: smaller child step budget
                            user_facing=False)   # specs/0092: a subagent returns structured text to its PARENT,
                                                  # never the user -> keep the plain prompt + terse-verdict contract
        result = agent.run(task, child_ctx)
        traj.end(_classify(result, traj.tool_calls), result.final, terminated=result.terminated)
        return result.final or ""
    except Exception as e:
        # specs/0076: the try now wraps Trajectory()/make_context()/build_agent() too, so a failure in
        # CHILD CONSTRUCTION becomes the '(subagent error: ...)' string instead of escaping run_subagent
        # and crashing the whole fan-out. traj may be None if Trajectory() itself raised.
        if traj is not None:
            try:
                traj.end("error", None, terminated="exception")
            except Exception:  # noqa: BLE001 - closing a trajectory must never mask the real error
                pass
        return f"(subagent error: {type(e).__name__}: {e})"
