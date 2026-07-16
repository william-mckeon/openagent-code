"""
src/effort.py

Phase 21 (specs/0021) — adaptive reasoning effort: match how hard the model thinks to how hard the task
is, instead of a fixed level. Effort is a single mutable attribute (model.Model.effort) re-read on every
call, so the harness sets it per step. Two triggers feed ONE decision:

  * the AGENT self-escalates via the `escalate_effort` tool (model proposes) — a sticky per-turn request;
  * the HARNESS auto-escalates when the run is STRUGGLING (consecutive tool failures, gate re-prompts, a
    goal bar that keeps failing) — deterministic, and where the run is already burning steps.

Both go through a PLUGGABLE Policy (CODE_EFFORT_POLICY): the deterministic `reactive` default ships here,
`off` is an explicit no-op, and an OPT-IN online learner lives in its OWN module (effort_online), imported
only when selected — so the default path stays deterministic and pinnable, and "self-learning" is a switch
each operator flips. A bad/missing custom policy FALLS BACK to reactive; nothing here ever raises.

Escalate-only by construction (decide() only RAISES from the baseline floor), capped, and the agent
restores the baseline per task so an escalation never leaks into the next turn. Imports only config +
logsetup — no cycle with agent/model.
"""
import importlib

from . import config
from .logsetup import get_logger

log = get_logger("effort")

LADDER = ("low", "medium", "high")   # ORDERED (config._EFFORTS is an unordered set — membership only)


def rank(level):
    try:
        return LADDER.index(level)
    except ValueError:
        return -1


def _higher(a, b):
    """The higher-rank of two levels (an unknown level never wins)."""
    return a if rank(a) >= rank(b) else b


def _bump(level, n=1):
    i = rank(level)
    return level if i < 0 else LADDER[min(i + n, len(LADDER) - 1)]


def cap(level, max_level):
    """Never above the cap (by rank). An unknown cap or an already-in-bounds level passes through."""
    m = rank(max_level)
    return max_level if (m >= 0 and rank(level) > m) else level


def resolve_baseline(effort):
    """A CONCRETE floor rung to ladder from. A real rung passes through; None/'' (the send-nothing default)
    maps to the configured floor — turning ON adaptive effort establishes a floor, by design."""
    if effort in LADDER:
        return effort
    return config.EFFORT_FLOOR if config.EFFORT_FLOOR in LADDER else "medium"


def struggle_score(consec=0, retries=0, goal_fails=0):
    """How stuck the run is, from signals the agent loop already tracks: the worst run of consecutive tool
    failures, the sum of gate re-prompts (completion / auto-verify / grounding), and a goal bar that keeps
    failing. A crude sum on purpose — the THRESHOLD (config.EFFORT_THRESHOLD) is the tunable knob."""
    return int(consec) + int(retries) + int(goal_fails)


class Policy:
    """Decide the effort for the NEXT model call. decide(baseline, requested, struggle, cap_level) is
    called each step; update(signature, escalated, success) feeds an outcome back (a no-op for the
    deterministic policies; the online learner uses it). Escalate-only: decide() only RAISES from the
    baseline floor, so effort never drops below the operator's floor."""
    name = "base"

    def decide(self, baseline, requested, struggle, cap_level, signature=None):
        return baseline

    def update(self, signature, escalated, success):
        pass


class OffPolicy(Policy):
    """An explicit no-op — never escalates. The safe, auditable choice for 'I want it fixed'."""
    name = "off"


class ReactivePolicy(Policy):
    """The deterministic default: honor the model's sticky tool request, and auto-bump ONE rung once the
    struggle score reaches the threshold. Capped, escalate-only, stateless (so it is fully pinnable)."""
    name = "reactive"

    def __init__(self, threshold=None):
        self.threshold = config.EFFORT_THRESHOLD if threshold is None else threshold

    def decide(self, baseline, requested, struggle, cap_level, signature=None):
        target = baseline
        if requested:
            target = _higher(target, requested)                  # the model asked (sticky this turn)
        if struggle >= self.threshold:
            target = _higher(target, _bump(baseline))            # auto-escalate on struggle
        return cap(target, cap_level)


def load_policy():
    """The active effort policy (CODE_EFFORT_POLICY): 'off' | 'reactive' | 'online' | a dotted
    'module:Class' an operator wrote. The online learner is imported ONLY when selected. A bad / missing /
    erroring choice FALLS BACK to reactive — a policy must never crash the run."""
    choice = (config.EFFORT_POLICY or "reactive").strip()
    low = choice.lower()
    if low in ("off", "none"):
        return OffPolicy()
    if low in ("", "reactive", "default"):
        return ReactivePolicy()
    if low == "online":
        try:
            from . import effort_online
            return effort_online.OnlinePolicy()
        except Exception as e:  # noqa: BLE001 - a missing/broken learner degrades to the safe default
            log.warning("effort: online policy unavailable (%s) - using reactive", e)
            return ReactivePolicy()
    try:                                                         # a user's own 'pkg.mod:Class'
        mod_name, _, cls_name = choice.partition(":")
        mod = importlib.import_module(mod_name)
        return getattr(mod, cls_name or "Policy")()
    except Exception as e:  # noqa: BLE001
        log.warning("effort: custom policy %r failed to load (%s) - using reactive", choice, e)
        return ReactivePolicy()
