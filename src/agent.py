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
from .tools import ToolResult
from .prompts import SYNTHESIS_PROMPT
from .logsetup import get_logger

log = get_logger("agent")


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
                    return RunResult(decision.final, "final", tool_calls)

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
