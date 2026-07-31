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
from . import effort
from . import goal
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


def _unapplied_manifest(ctx):
    """Approved propose-mode manifest items (specs/0022) whose target shows NO real change this run - the
    manifest mirror of _unverified_items (which sees only update_plan steps). An item counts as APPLIED if its
    target `path` OR (for a move) its `from` appears in the mutation ledger; err toward applied so a correct
    apply is never spuriously challenged. [] when there is no APPROVED manifest or every item landed (specs/0026)."""
    m = getattr(ctx, "manifest", None)
    if not (m and m.get("approved")):
        return []
    muts = getattr(ctx, "mutations", None) or {}
    muts_ci = {os.path.normcase(k) for k in muts}

    def _applied(p):
        return bool(p) and os.path.normcase(_rel(ctx, _abs(ctx, p))) in muts_ci

    out = []
    for it in (m.get("items") or []):
        if _applied(it.get("path")) or _applied(it.get("from")):
            continue
        out.append(f"{it.get('action', 'change')} {it.get('path', '?')} - approved but never applied this run")
    return out


def _unmet_acceptance(ctx):
    """Acceptance items on the approved spec (specs/0025) NOT yet marked done - the deterministic, mark-based
    mirror of the completion gate (_unverified_items): the agent marks each item with write_spec(action=
    'done') as it satisfies it, and this returns the ones still outstanding. [] means acceptance is met (or
    there is no spec / no acceptance items - nothing to hold)."""
    spec = getattr(ctx, "spec", None)
    if not spec:
        return []
    return [it.get("content", "") for it in (spec.get("acceptance") or []) if not it.get("done")]


def _acceptance_challenge(unmet):
    return ("Do NOT report the task done yet - these ACCEPTANCE items from the approved spec are not met:\n"
            + "\n".join(f"- {p}" for p in unmet)
            + "\nFinish each one, then mark it with write_spec(action='done', item=<its number>). Only report "
            "the task done once EVERY acceptance item is checked off.")


def _reset_propose_for_turn(ctx):
    """Per-turn propose-mode reset (specs/0022 + 0048). DEFAULT: re-lock read-only and clear approved_paths,
    so an approval NEVER leaks past the turn it was granted (the cross-turn WRITE-leak guard). With
    CODE_PROPOSE_PERSIST_APPROVAL and a session that has already had a manifest approved
    (ctx.propose_graduated), KEEP the approved phase + approved paths across turns instead, so the signed-off
    files stay writable (scoped bypass; the deny-rules + the fence still gate every op). propose_graduated is
    session-scoped and is deliberately NOT reset here. Off by default -> byte-identical to specs/0022."""
    ctx.manifest = None
    in_propose = getattr(ctx.permissions, "mode", None) == "propose"
    if config.PROPOSE_PERSIST_APPROVAL and in_propose and getattr(ctx, "propose_graduated", False):
        ctx.propose_phase = "approved"           # (a) a prior approval keeps the approved phase + paths live
    else:
        ctx.approved_paths = set()               # default / (b)(c): re-lock read-only, nothing auto-allowed
        ctx.propose_phase = "investigate" if in_propose else None


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
        # Adaptive effort (specs/0021): snapshot the model's AS-BUILT effort so a per-turn escalation can be
        # restored (a subagent is built with its own GUARDIAN_/GROUNDING_EFFORT — restore to THIS, never
        # config.REASONING_EFFORT). Policy loaded lazily on first use; None planner-model (test stubs) opts out.
        self._baseline_effort = getattr(getattr(planner, "model", None), "effort", None)
        self._effort_policy = None
        self._escalated = False

    def _finish(self, ctx, final, terminated, tool_calls):
        """Feed the effort policy this turn's OUTCOME (specs/0021), then return the RunResult. Only fires
        when the turn escalated and a policy is active: the deterministic policies ignore it; the online
        learner learns from it ('a task shaped like THIS, escalated -> {succeeded|not}'). Every run() exit
        routes through here so the learner sees failures as well as successes."""
        # Propose mode (specs/0022): log the resolved change-list ONCE, at turn end (every run() exit routes
        # here). approved=False marks a DECLINED plan that convert.py drops so a not-applied change never
        # trains as completed. No manifest -> nothing logged (byte-identical).
        m = getattr(ctx, "manifest", None)
        if m is not None:
            try:
                # specs/0026: when CODE_VERIFY_MANIFEST is on and the plan was approved, record whether
                # every item actually LANDED in the mutation ledger, so convert.py can drop a partial apply
                # instead of training it as a completed change. applied=None (flag off / unapproved) keeps
                # the record byte-identical.
                applied = None
                if config.VERIFY_MANIFEST and m.get("approved"):
                    applied = not _unapplied_manifest(ctx)
                self.traj.log_manifest(m.get("items", []), bool(m.get("approved")),
                                       mode=getattr(ctx.permissions, "mode", None), applied=applied)
            except Exception:  # noqa: BLE001 - logging a manifest must never break the run
                pass
        # Spec-first (specs/0025): log the resolved design contract ONCE at turn end - the acceptance items,
        # whether approved, and whether they were all met. A declined/unmet spec -> dropped from SFT by
        # convert. No spec (flag-off or a spec-less turn) -> nothing logged (byte-identical).
        s = getattr(ctx, "spec", None)
        if s is not None:
            try:
                self.traj.log_spec(s.get("title", ""), s.get("goal", ""), s.get("acceptance", []),
                                   s.get("non_goals", []), bool(s.get("approved")),
                                   not _unmet_acceptance(ctx))
            except Exception:  # noqa: BLE001 - logging a spec must never break the run
                pass
        if self._effort_policy is not None and self._escalated:
            try:
                self._effort_policy.update(getattr(ctx, "request", "") or "", True, terminated == "final")
            except Exception:  # noqa: BLE001 - a learner must never break the run
                pass
        return RunResult(final, terminated, tool_calls)

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
        ctx.fetched = {}              # web read-ledger (specs/0024): a page fetched last turn must not ground a citation on this one
        # The subagent fan-out breadth cap is a per-TASK budget too: without this reset a long REPL
        # session's earlier spawns accumulate on the reused ctx and permanently block spawn_agent on
        # later, unrelated turns (the same cross-turn-leak class as the plan/mutations reset above).
        ctx.spawn_count = 0
        ctx._reviewed_digest = None   # a new task may run review_repo fresh (the per-turn re-run guard)
        ctx._workflow_digest = None   # same, for run_workflow (specs/0038); inert unless CODE_WORKFLOWS offers it
        ctx._guardian_cache = {}      # per-turn guardian verdicts (specs/0019): a repeated command isn't re-reviewed
        ctx._destructive_targets = set()  # per-turn mass-destruction ledger (ride-5): distinct approved delete/move/dangerous ops
        ctx._turn_id = getattr(ctx, "_turn_id", 0) + 1   # a stable per-turn id a hook can key its own budget on
        ctx.goal = None               # a PREVIOUS task's bar must never be pursued on this one (specs/0020)
        ctx._verified_ok = False      # did a CHECK actually confirm success this turn? (grounding's unverified-success net)
        ctx.effort = None             # a prior turn's effort request must not carry over (specs/0021)
        # Propose mode (specs/0022): a change-list approved for one task must NEVER authorize edits on the
        # next (the same cross-turn-leak class the plan/goal resets above fix — and worse here, because it
        # governs WRITES). Reset the manifest + approval every task; start propose mode read-only.
        _reset_propose_for_turn(ctx)   # specs/0022 + 0048: re-lock read-only each turn, unless PERSIST_APPROVAL keeps a graduated approval live
        # Spec-first (specs/0025): a spec approved-but-unfinished on a prior task must never keep the
        # acceptance gate armed on an unrelated later turn (the stale-plan hijack class). Reset per task.
        ctx.spec = None
        self._escalated = False
        _emdl = getattr(self.planner, "model", None)
        if _emdl is not None:         # restore the AS-BUILT effort so a prior turn's escalation never leaks
            _emdl.effort = self._baseline_effort
        ctx.request = task            # pin the user's request so the guardian can weigh "is this what was asked"
        # Situational context (specs/0012): inject the agent's real environment (cwd / OS / shell / date
        # / granted dirs, + git branch when enabled) once per turn as a refreshed pin, so it conditions on
        # live state instead of confabulating it. The pin (set_env_context) is the SINGLE SENT copy; the raw
        # capture goes through log_env_capture as role:'system' (specs/0035 fix C) — NOT a second role:'user'
        # turn ADJACENT to the task, which a live run treated as user input and bled ("Environment") into a
        # user-typed path ("...\\OpenCode" -> "...\\OpenCodeEnvironment"). Off by default => today's behavior.
        if config.SITUATIONAL_CONTEXT:
            env = envcontext.build_env_context(
                ctx.cwd, getattr(ctx.permissions, "extra_roots", None),
                include_git=config.SITUATIONAL_GIT, shell_hints=config.SHELL_HINTS)
            self.cm.set_env_context(env)
            self.cm.log_env_capture(env)
        consecutive_fail = {}  # tool name -> count of prior consecutive failures
        tool_calls = 0
        verify_retries = 0     # completion-gate re-prompts used this run (Phase 6)
        edit_verify_retries = 0  # auto-verify-gate re-prompts used this run (Phase 14)
        ground_retries = 0     # grounding-gate re-prompts used this run (Phase 10)
        accept_retries = 0     # acceptance-gate re-prompts used this run (Phase 25)

        try:
            for step in range(self.max_steps):
                self.traj.steps = step + 1
                self.cm.set_pinned(ctx.plan)   # keep the current plan visible (Phase 4 planning)
                # Keep the pursued bar visible too (specs/0020) — a goal loop is long by construction, so
                # the bar WOULD be compacted away mid-loop. Mirrors the plan pin: set while active, cleared
                # the moment the loop resolves (ctx.goal = None), so a met goal can't linger in context.
                _g = getattr(ctx, "goal", None)
                self.cm.set_goal(
                    f"You are pursuing this goal. The BAR decides when it is done - not you:\n"
                    f"  GOAL: {_g['objective']}\n  BAR:  {goal.render(_g['bar'])}" if _g else None)
                # Adaptive reasoning effort (specs/0021): set the effort for THIS call from the task's
                # difficulty. Depth-0 ONLY (a subagent's own effort must stay untouched) + flag-gated +
                # only with a real planner model (test stubs opt out). The pluggable policy raises from the
                # baseline floor on the model's sticky request (ctx.effort) or the accumulated struggle
                # (signals tracked below); escalate-only, capped, reset to baseline each task.
                _mdl = getattr(self.planner, "model", None)
                if config.ADAPTIVE_EFFORT and _mdl is not None and getattr(ctx, "depth", 0) == 0:
                    if self._effort_policy is None:
                        self._effort_policy = effort.load_policy()
                    _base = effort.resolve_baseline(self._baseline_effort)
                    _score = effort.struggle_score(
                        consec=max(consecutive_fail.values(), default=0),
                        retries=verify_retries + edit_verify_retries + ground_retries,
                        goal_fails=(_g or {}).get("used", 0))
                    _new = self._effort_policy.decide(_base, getattr(ctx, "effort", None), _score,
                                                      config.EFFORT_MAX, getattr(ctx, "request", ""))
                    # Escalate-only + MONOTONIC within the turn: only RAISE above the current level and the
                    # floor. A non-escalating turn leaves the as-built effort untouched (byte-identical); a
                    # bump can't be undone mid-turn (once it's thinking hard it stays), and the per-task
                    # reset restores the baseline so nothing leaks to the next turn.
                    if effort.rank(_new) > max(effort.rank(_mdl.effort), effort.rank(_base)):
                        self.traj.log_effort_change(_mdl.effort, _new, _score, getattr(ctx, "request", ""))
                        _mdl.effort = _new
                        self._escalated = True
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
                    return self._finish(ctx, decision.final or "(the model produced a repetition loop; the turn "
                                        "was ended before it could finish)", "degenerate", tool_calls)

                self.cm.add(decision.assistant)

                # Model never produced a usable action (json protocol exhausted).
                if decision.gave_up:
                    return self._finish(ctx, decision.final, "nudge_exhausted", tool_calls)

                # Model broke protocol once — re-prompt instead of ending.
                if decision.nudge:
                    if ctx.verbose:
                        print("  [nudge] model did not emit a JSON action; re-prompting")
                    self.cm.add({"role": "user", "content": decision.nudge})
                    continue

                if not decision.calls:
                    # Dropped tool call (specs/0026): a native turn that came back EMPTY (no content, no
                    # tool calls) is an infra glitch model.py already retried - not a deliberate finish.
                    # Label it honestly so it isn't washed to 'completed' and can't stamp a manifest
                    # approved off a glitch. Gated so flag-off returns 'final' as before (byte-identical).
                    if config.VERIFY_MANIFEST and getattr(decision, "dropped", False):
                        return self._finish(ctx, decision.final or "", "no_output", tool_calls)

                    # Verified completion (Phase 6 / specs/0007): don't accept "done" when the
                    # agent marked plan steps complete that its actual file changes don't back up.
                    # Re-prompt with the discrepancy (bounded), else return an HONEST outcome.
                    unmet = _unverified_items(ctx) if config.VERIFY_COMPLETION else []
                    # Manifest reconciliation (specs/0026): an APPROVED manifest whose items didn't all land
                    # is an unbacked completion too - the manifest mirror of the plan-step check. Merged so
                    # the same bounded re-prompt / honest 'unverified_completion' handles it. Gated so
                    # flag-off adds nothing (byte-identical).
                    if config.VERIFY_MANIFEST:
                        unmet = unmet + _unapplied_manifest(ctx)
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
                        return self._finish(ctx, decision.final, "unverified_completion", tool_calls)

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
                            return self._finish(ctx, decision.final, "verify_failed_edits", tool_calls)
                        if vres:
                            ctx._verified_ok = True   # the touched files compiled/passed -> a real check ran

                    # Goal gate (Phase 20 / specs/0020): the model says done — but if it declared a BAR
                    # via `pursue`, the bar decides, not the model. Run it; a failure re-prompts with the
                    # REAL output and loops, else an honest 'goal_unmet'. Sits AFTER completion/auto-verify
                    # (so each iteration is honest) and BEFORE grounding (so grounding judges the REAL
                    # final answer, not an interim one). The whole loop stays inside THIS run(), which is
                    # what makes the per-turn guarantees — above all the mass-destruction cap — span it.
                    g = getattr(ctx, "goal", None)
                    if config.GOAL_LOOP and g:
                        bar_ok, bar_out = goal.run_bar(g["bar"], ctx.cwd)
                        # Steps run out -> run() falls THROUGH this chain to the synthesis path and returns
                        # 'max_steps', so 'goal_unmet' would be unreachable. Stop re-prompting while there's
                        # still headroom and own the honest label here.
                        room = (self.max_steps - step) > config.GOAL_STEP_HEADROOM
                        if not bar_ok and g["used"] + 1 < g["max_iterations"] and room:
                            g["used"] += 1
                            if ctx.verbose:
                                print(f"  [goal] bar failed ({g['used']}/{g['max_iterations']}): "
                                      f"{goal.render(g['bar'])}")
                            log.info("goal challenge (attempt %d/%d): %s", g["used"], g["max_iterations"],
                                     bar_out[:200].replace("\n", " "))
                            self.cm.add({"role": "user", "content": goal.challenge(
                                g["objective"], g["bar"], bar_out, g["used"], g["max_iterations"])})
                            continue
                        # RESOLVED (passed, or budget/steps exhausted): log ONLY this final result as the
                        # reward — logging each failing attempt would make verif_ok False and drop every
                        # successfully-converged loop from the corpus (the 0014 lesson).
                        self.traj.log_goal(g["objective"], g["bar"], g["used"] + (0 if bar_ok else 1),
                                           g["max_iterations"], bar_ok)
                        self.traj.log_verification(goal.render(g["bar"]), bar_ok, bar_out)
                        if bar_ok:
                            ctx._verified_ok = True   # the bar ran and passed -> a success claim IS backed
                        ctx.goal = None          # met-or-spent: never re-run it on a later re-prompt
                        if ctx.verbose:
                            print(f"  [goal] bar {'PASSED' if bar_ok else 'NOT met'}: {goal.render(g['bar'])}")
                        if not bar_ok:
                            return self._finish(ctx, decision.final, "goal_unmet", tool_calls)

                    # Acceptance gate (Phase 25 / specs/0025): when an APPROVED spec is active, every
                    # acceptance item must be marked done — the deterministic, mark-based mirror of the
                    # completion gate — before 'done' is accepted. Re-prompt with the unmet items (bounded),
                    # else record an honest 'acceptance_unmet'. TRIPLE-gated (flag + a spec + approved) so a
                    # spec-less / flag-off run never reaches it (byte-identical), and reads ctx.spec (reset
                    # per task) NOT the on-disk file, so it can't hijack an unrelated turn.
                    if config.SPEC_FIRST and getattr(ctx, "spec", None) and ctx.spec.get("approved"):
                        unmet_acc = _unmet_acceptance(ctx)
                        if unmet_acc and accept_retries < config.SPEC_FIRST_RETRIES:
                            accept_retries += 1
                            if ctx.verbose:
                                print(f"  [spec] {len(unmet_acc)} acceptance item(s) not yet met")
                            log.info("acceptance challenge (retry %d): %s", accept_retries,
                                     "; ".join(unmet_acc))
                            self.cm.add({"role": "user", "content": _acceptance_challenge(unmet_acc)})
                            continue
                        if unmet_acc:
                            return self._finish(ctx, decision.final, "acceptance_unmet", tool_calls)

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
                    return self._finish(ctx, decision.final,
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

                    # A run_command that IS a check (test / build / lint) and SUCCEEDED is real evidence a
                    # "the tests pass" claim can rest on — so the grounding unverified-success net won't
                    # flag it even if the model verified manually instead of via `pursue`.
                    if name == "run_command" and result.ok and grounding.ran_check(args.get("command", "")):
                        ctx._verified_ok = True

                    # PostToolUse hooks (Phase 15): observe the executed call (side effects / telemetry /
                    # trajectory annotation). Observe-only + fail-open — never alters `result`, never
                    # raises. Flag-off -> skipped entirely (byte-identical).
                    if config.HOOKS:
                        from . import hooks
                        hooks.posttool(name, args, result, ctx)

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
                        # Show the decision reason when a human/guardian was in the loop (action "ask") or
                        # the call was denied — so the run log explains WHY, e.g. a guardian verdict.
                        why = f"  -- {pd.reason}" if (not pd.allowed or pd.action == "ask") else ""
                        print(f"  [{flag}] {name}({_short(args)}){why}")
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
        return self._finish(ctx, final, "max_steps", tool_calls)


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
