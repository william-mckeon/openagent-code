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
from .tools import ToolResult
from .prompts import SYNTHESIS_PROMPT
from .logsetup import get_logger

log = get_logger("agent")


def _unverified_items(ctx):
    """Plan steps marked completed whose named file shows NO real change this run (or a delete
    whose file still exists / an edit whose file is gone). Returns human-readable problems; empty
    means completion is verified. Steps without a named file can't be checked, so they're trusted."""
    items = getattr(ctx, "plan_items", None) or []
    muts = getattr(ctx, "mutations", None) or {}
    problems = []
    for it in items:
        if it.get("status") != "completed" or not it.get("file"):
            continue
        rel = it["file"].replace("\\", "/").strip()
        rel = rel[2:] if rel.startswith("./") else rel
        rel = rel.strip("/")
        action = muts.get(rel)
        if action is None:
            problems.append(f"'{it['file']}' - marked done but nothing changed it this session")
            continue
        exists = os.path.exists(os.path.join(ctx.cwd, rel))
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
        self.terminated = terminated    # "final" | "nudge_exhausted" | "max_steps"
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
        consecutive_fail = {}  # tool name -> count of prior consecutive failures
        tool_calls = 0
        verify_retries = 0     # completion-gate re-prompts used this run (Phase 6)

        try:
            for step in range(self.max_steps):
                self.traj.steps = step + 1
                self.cm.set_pinned(ctx.plan)   # keep the current plan visible (Phase 4 planning)
                decision = self.planner.step(self.cm.context(), step)
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
                    return RunResult(decision.final,
                                     "unverified_completion" if unmet else "final", tool_calls)

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
