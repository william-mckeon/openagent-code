"""
src/outcomes.py

The ONE place that maps an agent.run() terminated label to an honest training outcome.

Every capture path classifies the same way through classify(): the one-shot CLI, each REPL turn,
the eval verify/agentic harnesses, and subagents. So a degenerate / ungrounded / unverified /
verify-failed run is labeled identically no matter which path recorded it, and train/convert.py's
KEEP_OUTCOMES drops it everywhere. Before this, each path had its own mapping and the REPL / run_task
paths silently relabeled honest failures as 'completed' / 'success' -> the exact trajectories the gates
exist to reject became SFT targets (corpus poison).
"""

# The honest gate outcomes (Phases 6 / 10 / 13 / 14). A run that ended in one of these is NOT trainable,
# and the label MUST survive to train/convert.py. They take precedence over any success relabel a caller
# applies (a verify command that happens to pass must never mask an ungrounded/unverified answer).
GATE_OUTCOMES = ("unverified_completion", "ungrounded_completion", "degenerate", "verify_failed_edits",
                 "goal_unmet", "manifest_declined", "spec_declined", "acceptance_unmet",
                 "manifest_unapplied", "no_output")


def classify(terminated, tool_calls):
    """terminated (agent.RunResult.terminated) + that run's own tool-call count -> honest outcome.

    Only a plain 'completed' is eligible for a caller's success/verify relabel; every gate outcome and
    'no_action' / 'protocol_stalled' / 'max_steps' is returned as-is so it can't be washed to 'success'.
    Order mirrors the original one-shot mapping: protocol first, then the gates, then no-action (a
    zero-tool-call run — including one that also hit max_steps), then max_steps, then a clean finish.
    """
    if terminated == "nudge_exhausted":
        return "protocol_stalled"
    if terminated in GATE_OUTCOMES:
        return terminated
    if (tool_calls or 0) == 0:
        return "no_action"
    if terminated == "max_steps":
        return "max_steps"
    return "completed"
