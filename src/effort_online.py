"""
src/effort_online.py

Phase 21 (specs/0021) — the OPT-IN online effort learner. This is the "self-learning" policy an operator
switches in with CODE_EFFORT_POLICY=online; it is imported ONLY when selected, so the default path
(src/effort.py's reactive policy) stays deterministic and dependency-light.

It closes the loop online: whenever a turn ESCALATED, update() records whether that turn then SUCCEEDED,
keyed by a coarse task signature. decide() then PRE-escalates on a signature that has historically needed
it — so over time the agent thinks harder from the START on task shapes that used to make it struggle,
instead of only reacting after it burns steps. State persists to CODE_EFFORT_STATE (JSON) between sessions,
so it adapts within a project without a retrain. It never raises: a bad state file just starts empty.

This is deliberately a SIMPLE reference learner (a per-signature win-rate), not an RL system — the real
policy is still meant to be learned by the distilled model via the flywheel (the `effort_change` records).
This gives faster, per-project feedback for operators who want it, behind a switch, without compromising
the pinnable default.
"""
import json
import os

from . import config
from .effort import Policy, _higher, _bump, cap
from .logsetup import get_logger

log = get_logger("effort")

_KEYWORDS = ("refactor", "migrate", "debug", "fix", "test", "review", "implement", "optimize",
             "port", "rewrite", "design", "audit")
_MIN_OBSERVATIONS = 2      # don't act on a signature until it's been seen at least this many times
_WIN_RATE = 0.5            # ...and escalation helped at least this often


def _sig(request):
    """A COARSE, stable task signature so similar tasks share learned stats: the difficulty keywords
    present, plus a length bucket. Deliberately lossy — the point is 'tasks shaped like this', not an
    exact match. Deterministic, so the learner's decisions are reproducible for testing."""
    t = (request or "").lower()
    kws = [k for k in _KEYWORDS if k in t]
    bucket = "s" if len(t) < 60 else ("m" if len(t) < 200 else "l")
    return (",".join(kws) or "generic") + "#" + bucket


class OnlinePolicy(Policy):
    name = "online"

    def __init__(self):
        self.threshold = config.EFFORT_THRESHOLD
        self.path = config.EFFORT_STATE
        self.state = self._load()

    def decide(self, baseline, requested, struggle, cap_level, signature=None):
        target = baseline
        if requested:
            target = _higher(target, requested)
        if struggle >= self.threshold:
            target = _higher(target, _bump(baseline))
        # LEARNED pre-escalation: a signature that escalated-and-succeeded often before -> think harder NOW.
        st = self.state.get(_sig(signature))
        if st and st.get("n", 0) >= _MIN_OBSERVATIONS and st.get("wins", 0) / max(1, st["n"]) >= _WIN_RATE:
            target = _higher(target, _bump(baseline))
        return cap(target, cap_level)

    def update(self, signature, escalated, success):
        """Record the outcome of an ESCALATED turn (a non-escalated turn teaches nothing about escalation)."""
        if not escalated:
            return
        st = self.state.setdefault(_sig(signature), {"n": 0, "wins": 0})
        st["n"] += 1
        if success:
            st["wins"] += 1
        self._save()

    # -- persistence (never raises) ----------------------------------------------------------------------
    def _load(self):
        if not self.path or not os.path.isfile(self.path):
            return {}
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self):
        if not self.path:
            return
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.state, f)
        except OSError as e:  # noqa: BLE001 - persistence is best-effort; a full disk must not break the run
            log.warning("effort: could not persist learner state (%s)", e)
