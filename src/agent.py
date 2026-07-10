"""
src/agent.py

The agent loop.

state -> planner.step (model decides) -> run tool(s) -> observe -> repeat, until
the planner reports a final answer, gives up (json protocol never satisfied), or we
hit max_steps.

The conversation lives in a ContextManager, not a raw list. The agent APPENDS every
turn to it (which logs the raw history) and sends it `context()` (the live,
possibly-compacted view) to the planner. Capture and context are decoupled: the
manager may summarize what the model sees, but every turn is still logged raw.

run() returns a RunResult so the caller can label the outcome HONESTLY: a run that
made no tool calls, or stalled on the protocol, is not a success.
"""
import os

from . import config
from . import grounding
from . import envcontext
from . import verify_edits
from .tools import ToolResult, _abs, _rel
from .prompts import SYNTHESIS_PROMPT, looks_degenerate
from .logsetup import get_logger

log = get_logger("agent")


def _unverified_items(ctx):
    """Plan steps marked completed whose named file shows NO real change this run (or a delete
    whose file still exists / an edit whose file is gone). Returns human-readable problems; empty
    means completion is verified. Steps without a named file can't be checked, so they're trusted."""
    items = getattr(ctx, "plan_items", None) or []
    muts = getattr(ctx, "mutations", None) or {}
    # Case-insensitive ledger view for the lookup: Windows paths are case-insensitive (and the
    # os.path.exists check below agrees), so a plan step that names a file with different casing than the
    # edit call still matches its real change. os.path.normcase is identity on POSIX — correctly
    # case-SENSITIVE on the Linux training substrate.
    muts_ci = {os.path.normcase(k): v for k, v in muts.items()}
    problems = []
    for it in items:
        if it.get("status") != "completed" or not it.get("file"):
            continue
        # Normalize the step's file through the SAME _abs->_rel the mutation ledger keys on, so a step
        # matches its change no matter how the path was written - relative, absolute, or inside a granted
        # reference dir. (Hand-relativizing here missed edits made via an ABSOLUTE path: every real change
        # read as "not backed" and the gate fired spuriously, seen live editing centpilot via abs paths.)
        rel = _rel(ctx, _abs(ctx, it["file"]))
        action = muts_ci.get(os.path.normcase(rel))
        if action is None:
            problems.append(f"'{it['file']}' - marked done but nothing changed it this session")
            continue
        exists = os.path.exists(_abs(ctx, it["file"]))
        if action == "delete" and exists:
            problems.append(f"'{it['file']}' - marked deleted but the file still exists")
        elif action in ("write", "edit") and not exists:
            problems.append(f"'{it['file']}' - marked edited but the file is missing")
    return problems


def _completion_challenge(problems):
    return ("Do NOT report the task done yet - these plan steps are marked complete but the "
            "filesystem doesn't back them up:\n" + "\n".join(f"- {p}" for p in problems)
            + "\nActually make each change with edit_file / write_file / delete_file (never rm), "
            "then re-verify. Only mark a step completed AFTER its tool call succeeds.")


class RunResult:
    def __init__(self, final, terminated, tool_calls):
        self.final = final              # the model's closing text (may be empty)
        self.terminated = terminated    # "final" | "nudge_exhausted" | "max_steps" | "degenerate" | gate outcomes
                                        # | "unverified_completion" | "ungrounded_completion"
        self.tool_calls = tool_calls    # how many tool calls actually executed


class Agent:
    def __init__(self, planner, registry, trajectory, max_steps, context_manager):
        self.planner = planner
        self.registry = registry
        self.traj = trajectory
        self.max_steps = max_steps
        self.cm = context_manager       # owns the system prompt + the live context

    def run(self, task, ctx):
        # Snapshot the live context BEFORE this turn. If a model call dies mid-turn (a
        # Bedrock 503 after tool results were appended), we roll the live view back to
        # here so it never ends in orphaned tool-results — otherwise the next user turn
        # produces the consecutive user/tool blocks Bedrock's Converse API rejects,
        # poisoning the session. The trajectory still captured every raw turn.
        mark = self.cm.mark()
        self.cm.add({"role": "user", "content": task})
        self.cm.set_task(task)   # pin the request so compaction can't summarize away what was asked
        # Per-task reset (fixes the cross-turn completion-gate hijack): the plan and this run's mutation
        # ledger are per-TASK artifacts. A new user turn starts CLEAN so a PREVIOUS task's completed-but-
        # unbacked steps can't keep firing the completion gate and hijack an unrelated question - seen live
        # where "what project is this?" got answered with a stale favicon status + "I exhausted my budget",
        # and the same stale plan then blocked the grounding gate from ever running on the real answer.
        ctx.plan = None
        ctx.plan_items = []
        ctx.mutations = {}
        # The subagent fan-out breadth cap is a per-TASK budget too: without this reset a long REPL
        # session's earlier spawns accumulate on the reused ctx and permanently block spawn_agent on
        # later, unrelated turns (the same cross-turn-leak class as the plan/mutations reset above).
        ctx.spawn_count = 0
        # Situational context (specs/0012): inject the agent's real environment (cwd / OS / shell / date
        # / granted dirs, + git branch when enabled) once per turn as a refreshed pin, so it conditions
        # on live state instead of confabulating it. Pinned (survives compaction) AND logged as a turn
        # (raw capture) — the same dual the task itself uses. Off by default => today's behavior verbatim.
        if config.SITUATIONAL_CONTEXT:
            env = envcontext.build_env_context(
                ctx.cwd, getattr(ctx.permissions, "extra_roots", None),
                include_git=config.SITUATIONAL_GIT)
            self.cm.set_env_context(env)
            self.cm.add({"role": "user", "content": env})
        consecutive_fail = {}  # tool name -> count of prior consecutive failures
        tool_calls = 0
        verify_retries = 0     # completion-gate re-prompts used this run (Phase 6)
        edit_verify_retries = 0  # auto-verify-gate re-prompts used this run (Phase 14)
        ground_retries = 0     # grounding-gate re-prompts used this run (Phase 10)

        try:
            for step in range(self.max_steps):
                self.traj.steps = step + 1
                self.cm.set_pinned(ctx.plan)   # keep the current plan visible (Phase 4 planning)
                decision = self.planner.step(self.cm.context(), step)

                # Degeneracy guard (repetition loop): a weak model can get stuck emitting the same line
                # over and over (seen live: "...rename the comment at line 578? Already done." x
                # hundreds). Left unchecked it never finishes, bloats the window into a forced compaction
                # next turn, and poisons the corpus. Detect it, SUPPRESS the raw garbage (log a short
                # marker so it enters neither the live context nor the trajectory), end the turn honestly.
                _out = (decision.assistant or {}).get("content") or decision.final or ""
                if looks_degenerate(_out):
                    log.info("degenerate repetition loop at step %d - ending turn (outcome=degenerate)", step)
                    self.cm.add({"role": "assistant",
                                 "content": "[degenerate repetition output detected and suppressed]"})
                    return RunResult(decision.final or "(the model produced a repetition loop; the turn "
                                     "was ended before it could finish)", "degenerate", tool_calls)

                self.cm.add(decision.assistant)

                # Model never produced a usable action (json protocol exhausted).
                if decision.gave_up:
                    return RunResult(decision.final, "nudge_exhausted", tool_calls)

                # Model broke protocol once — re-prompt instead of ending.
                if decision.nudge:
                    if ctx.verbose:
                        print("  [nudge] model did not emit a JSON action; re-prompting")
                    self.cm.add({"role": "user", "content": decision.nudge})
                    continue

                if not decision.calls:
                    # Verified completion (Phase 6 / specs/0007): don't accept "done" when the
                    # agent marked plan steps complete that its actual file changes don't back up.
                    # Re-prompt with the discrepancy (bounded), else return an HONEST outcome.
                    unmet = _unverified_items(ctx) if config.VERIFY_COMPLETION else []
                    if unmet and verify_retries < config.VERIFY_COMPLETION_RETRIES:
                        verify_retries += 1
                        if ctx.verbose:
                            print(f"  [verify] completion challenged — {len(unmet)} item(s) not "
                                  f"backed by a real change")
                        log.info("completion challenge (retry %d): %s", verify_retries,
                                 "; ".join(unmet))
                        self.cm.add({"role": "user", "content": _completion_challenge(unmet)})
                        continue
                    if unmet:
                        return RunResult(decision.final, "unverified_completion", tool_calls)

                    # Auto-verify gate (Phase 14 / specs/0014): completion proved the changes are REAL —
                    # now run a configured check (default py_compile) on just the TOUCHED files. A failure
                    # re-prompts to fix (bounded), else records an honest 'verify_failed_edits'. Each check
                    # result is logged as an objective reward (sub-phase C). Composes with the other gates
                    # (own counter); OFF by default, so today's branch is byte-identical.
                    if config.VERIFY_TOUCHED:
                        vres = verify_edits.results(ctx)
                        failing = verify_edits.problems_from(vres)
                        if failing and edit_verify_retries < config.VERIFY_TOUCHED_RETRIES:
                            edit_verify_retries += 1
                            if ctx.verbose:
                                print(f"  [verify] {len(failing)} touched file(s) failed the check")
                            log.info("verify challenge (retry %d): %s", edit_verify_retries,
                                     "; ".join(failing))
                            self.cm.add({"role": "user", "content": verify_edits.challenge(failing)})
                            continue
                        # Resolved (passed, or retries exhausted): record the FINAL result as the reward
                        # label — NOT the intermediate attempts, so a failed-then-fixed run logs only the
                        # passing result and stays trainable (is_trainable drops any run whose verification
                        # records show a failure).
                        if config.VERIFY_TOUCHED_LABEL:
                            for r in vres:
                                self.traj.log_verification(r["cmd"], r["ok"], r["output"])
                        if failing:
                            return RunResult(decision.final, "verify_failed_edits", tool_calls)

                    # Grounding gate (Phase 10 / specs/0010): completion is verified — the plan's
                    # changes are real. Now check the closing answer's CLAIMS are grounded in the
                    # sources it cited/touched (catches honest-but-wrong: a real file, wrong facts).
                    # Re-prompt with the discrepancy (bounded), else return an HONEST outcome.
                    ungrounded = grounding.problems(decision.final, ctx) if config.VERIFY_GROUNDING else []
                    if ungrounded and ground_retries < config.VERIFY_GROUNDING_RETRIES:
                        ground_retries += 1
                        if ctx.verbose:
                            print(f"  [grounding] {len(ungrounded)} claim(s) not backed by the "
                                  f"cited sources")
                        log.info("grounding challenge (retry %d): %s", ground_retries,
                                 "; ".join(ungrounded))
                        self.cm.add({"role": "user", "content": grounding.challenge(ungrounded)})
                        continue
                    return RunResult(decision.final,
                                     "ungrounded_completion" if ungrounded else "final", tool_calls)

                for call in decision.calls:
                    name, args = call["name"], call["args"]

                    # Permission gate (Phase 4 #6): decide BEFORE running, capture the
                    # decision, and substitute a denial result if blocked. One gate for
                    # every tool, logged against THIS agent's trajectory (subagent-safe).
                    pd = ctx.permissions.decide(name, args, ctx)
                    self.traj.log_permission(step, name, pd)
                    if pd.allowed:
                        result = self.registry.run(name, args, ctx)
                    else:
                        result = ToolResult(False, f"Permission denied: {pd.reason}")
                    tool_calls += 1

                    retry_index = consecutive_fail.get(name, 0)
                    self.traj.log_tool_call(step, name, args, result, retry_index)
                    consecutive_fail[name] = 0 if result.ok else retry_index + 1

                    # A completed review_repo digest carries the per-area findings the lead must
                    # synthesize from PLUS a "synthesize now, don't re-review" trailer. The review's own
                    # token weight trips compaction on the next step, which would lossy-summarize both
                    # away (a live run then re-reviewed twice and called an auth service it had read
                    # 'empty'). Pin a COPY so it survives compaction; the normal tool-result is still
                    # added below, keeping the assistant/tool_result pairing Bedrock requires intact.
                    if name == "review_repo" and result.ok:
                        self.cm.set_review_digest(result.content)

                    flag = "deny" if not pd.allowed else ("ok" if result.ok else "FAIL")
                    if ctx.verbose:
                        print(f"  [{flag}] {name}({_short(args)})")
                    # Richer than the console line: include a result snippet — this is the
                    # detail that makes the run log reviewable for bugs.
                    log.info("step %d [%s] %s(%s) -> %s", step, flag, name, _short(args),
                             str(result.content)[:200].replace("\n", " "))

                    self.cm.add(self.planner.format_result(call, result))
        except Exception:
            # The live context must never be left ending in dangling tool-results. Roll
            # back this whole turn (capture is untouched) and re-raise so the caller
            # labels the outcome — the REPL keeps the session alive on CLEAN history.
            log.warning("turn raised at step %d — rolling back the turn", step)
            self.cm.rollback(mark)
            raise

        # Out of step budget. Don't bail with a canned "(stopped)" — a long investigation
        # would return nothing. Spend ONE final tool-less turn turning the work already done
        # into the answer (the review, or what got changed + what remains). Best-effort: if
        # this synthesis call fails, fall back to the plain max_steps marker.
        final = "(stopped: reached max_steps)"
        try:
            self.cm.add({"role": "user", "content": SYNTHESIS_PROMPT})
            msg = self.planner.model.complete(self.cm.context(), None, self.max_steps)
            text = (getattr(msg, "content", "") or "").strip()
            if text:
                final = text
                self.cm.add({"role": "assistant", "content": text})
        except Exception:
            pass
        return RunResult(final, "max_steps", tool_calls)


def _short(args):
    """One-line arg preview for the console and the run log. Path-like values KEEP their
    basename — a blind mid-name cut made logs unreadable ('Button.test.tsx' -> 'Button.tes',
    'crypto' -> 'cryp'), which defeats reviewing a run from its log afterwards."""
    def fmt(k, v):
        s = str(v)
        if len(s) > 60:
            flat = s.replace("\\", "/")
            if "/" in flat:
                tail = flat.rsplit("/", 1)[-1]
                s = (s[:20] + "..." + tail) if len(tail) <= 38 else ("..." + s[-50:])
            else:
                s = s[:57] + "..."
        return f"{k}={s!r}"
    return ", ".join(fmt(k, v) for k, v in args.items())
