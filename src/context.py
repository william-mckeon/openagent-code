"""
src/context.py

ContextManager — owns the LIVE working context (Phase 4 compaction).

The locked "capture vs. context" decision (ROADMAP.md): what the model SEES and
what we LOG are two different things once compaction exists.

  - This object owns the live context the model sees. When it overflows the
    token budget, older turns are summarized into a short briefing so the agent
    keeps working in a smaller window.
  - It does NOT own the training record. Every message added is logged RAW via
    `trajectory.log_turn` — the full history, never compacted — independent of
    whatever this object trims. A `compaction` event is logged when it summarizes.

So compaction shrinks the model's context but never what we capture.

Bounded-fragment invariant (specs/0009): every DYNAMIC fragment that enters the live context —
tool results, user turns, the pinned plan, a compaction summary — passes through `_capped`, so no
single item can grow unbounded and blow the model's window (Codex's "no unbounded model-visible
items", adapted). The full text is always kept raw in the trajectory.
"""
import json

from . import config
from .logsetup import get_logger

log = get_logger("context")


def estimate_tokens(messages):
    """Cheap, dependency-free token estimate (~4 chars/token over the JSON)."""
    return sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) // 4


class ContextManager:
    def __init__(self, system_prompt, model, trajectory,
                 compact_at_tokens=None, keep_recent=None, verbose=False,
                 initial_working=None):
        self.model = model
        self.traj = trajectory
        self.compact_at = config.COMPACT_AT_TOKENS if compact_at_tokens is None else compact_at_tokens
        self.keep_recent = config.COMPACT_KEEP_RECENT if keep_recent is None else keep_recent
        self.verbose = verbose

        self.system = {"role": "system", "content": system_prompt}
        self.pinned = None       # always-visible, never-compacted message (e.g. the plan)
        self.pinned_task = None   # the current user request, pinned so compaction can't lose it
        if initial_working is None:
            # Fresh session: empty working set; the system prompt is the first raw turn.
            self.working = []
            self.traj.log_turn(self.system)
        else:
            # Resumed session (src/session.py): pre-populate from the rehydrated raw
            # history. These messages are ALREADY in the trajectory file, so do NOT
            # re-log them — only new turns get logged from here on. Cap each so the
            # bounded-fragment invariant (specs/0009) holds on resume too: a huge
            # historical message must not re-enter the live context uncapped.
            self.working = [self._capped(m) for m in initial_working]

    def add(self, message):
        """Append one message. Logged raw (never compacted) and added to the live set.

        The TRAJECTORY gets the full raw message (capture is lossless); the LIVE working set
        gets a size-capped copy, so no single tool result — a huge file read, a long subagent
        return — can dominate the window and defeat compaction (which keeps recent messages
        verbatim). This is the per-message half of staying under the model's hard limit; the
        review_repo orchestrator handles the whole-repo case at the source."""
        self.traj.log_turn(message)
        self.working.append(self._capped(message))

    def _capped(self, message):
        """Bound ONE fragment to the live-context cap — the primitive behind the bounded-fragment
        invariant (specs/0009). EVERY dynamic fragment (tool results, user turns, the pinned plan, a
        compaction summary, resumed history) passes through here, so no single item grows unbounded.
        BOTH places a huge string can hide are bounded: the message `content` (a big file READ, a long
        subagent return) AND a native-mode tool call's `arguments` (a big file WRITE/edit — the whole
        file body the model emits, with only short reasoning in `content`). The full text is always
        preserved raw in the trajectory; a truncation is LOGGED so an oversized fragment is visible
        for review rather than silent (Codex flags large model-visible items)."""
        limit = config.MAX_MESSAGE_CHARS
        if not limit:
            return message
        role = message.get("role", "?")
        trimmed = None  # copy lazily — only if something actually needs capping

        content = message.get("content")
        if isinstance(content, str) and len(content) > limit:
            trimmed = dict(message)
            trimmed["content"] = self._truncate(content, limit, role, "content")

        # Native-mode tool calls carry the model's raw arguments string; for a write_file/edit_file
        # that is the ENTIRE file body while `content` is only short reasoning — so the symmetric
        # huge-WRITE case would slip the cap the huge-READ case (a capped tool RESULT) is caught by.
        # The id/type are preserved, so the tool_call<->result pairing the API requires is intact.
        calls = message.get("tool_calls")
        if isinstance(calls, list):
            new_calls, changed = [], False
            for tc in calls:
                fn = tc.get("function") if isinstance(tc, dict) else None
                argstr = fn.get("arguments") if isinstance(fn, dict) else None
                if isinstance(argstr, str) and len(argstr) > limit:
                    label = f"tool-call args ({fn.get('name', '?')})"
                    new_calls.append({**tc, "function": {**fn, "arguments": self._truncate(argstr, limit, role, label)}})
                    changed = True
                else:
                    new_calls.append(tc)
            if changed:
                if trimmed is None:
                    trimmed = dict(message)
                trimmed["tool_calls"] = new_calls

        return trimmed if trimmed is not None else message

    def _truncate(self, text, limit, role, what):
        over = len(text) - limit
        log.info("capped a %s %s fragment to fit the live context: %d -> %d chars (-%d)",
                 role, what, len(text), limit, over)
        return (text[:limit]
                + f"\n...[truncated {over} chars to fit the live context; "
                  "the full text is preserved in the trajectory]")

    def mark(self):
        """Snapshot the live working-set length so a failed turn can be rolled back to a
        clean state. See rollback(). Only the model's live view is marked — capture is
        untouched."""
        return len(self.working)

    def rollback(self, mark):
        """Drop working-set messages appended since `mark`. Used when a model call dies
        mid-turn (e.g. a Bedrock 503 after some tool results were already appended): it
        keeps the LIVE context from ending in orphaned tool-results, which the next user
        turn would otherwise turn into the consecutive user/tool blocks Bedrock's Converse
        API rejects — poisoning the whole session. The trajectory keeps the full raw
        record (capture vs. context): only what the model SEES is trimmed."""
        if 0 <= mark < len(self.working):
            del self.working[mark:]

    def set_pinned(self, text):
        """Pin a message just after the system prompt — always sent, never compacted.

        Used for the plan (Phase 4). It is a CONTEXT device only: the plan's content
        is already in the raw history as the update_plan tool call, so pinning never
        adds to the captured `turn` stream.
        """
        # Bound the pinned fragment too (specs/0009): it is always sent AND never compacted, so an
        # unbounded plan would silently eat the window every single turn.
        self.pinned = (self._capped({"role": "user",
                                     "content": "Current plan (keep it updated as you work):\n" + text})
                       if text else None)

    def set_task(self, text):
        """Pin the CURRENT user request just after the system prompt — always sent, never compacted.

        A live run turned "what project is this?" into a whole-repo audit: after 80+ tool calls the
        turn compacted, the original question was summarized away, and the agent ended up answering the
        grounding gate instead of the user. Pinning the request keeps it visible through compaction so
        the agent stays on what was actually asked. Bounded like the plan pin (specs/0009).
        """
        self.pinned_task = (self._capped({"role": "user",
                                          "content": "The user's current request (answer THIS directly):\n" + text})
                            if text else None)

    def _base(self):
        return ([self.system]
                + ([self.pinned_task] if self.pinned_task else [])   # the request first — the anchor
                + ([self.pinned] if self.pinned else []))            # then the working plan

    def context(self):
        """The message list to send the model this step — compacting first if needed."""
        if self.compact_at and estimate_tokens(self._base() + self.working) > self.compact_at:
            self._compact()
        return self._base() + self.working

    def _safe_cut(self):
        """Largest cut index such that working[cut:] starts at a clean group boundary.

        A 'tool' message depends on the assistant (with tool_calls) before it, so we
        never let the kept tail begin with one — that would orphan it and break the
        next API call. Snap the boundary back to the owning assistant / a user turn.
        """
        cut = len(self.working) - self.keep_recent
        if cut <= 0:
            return 0
        while cut > 0 and self.working[cut].get("role") == "tool":
            cut -= 1
        return cut

    def _compact(self):
        cut = self._safe_cut()
        if cut <= 0:
            return  # nothing safe to summarize yet
        old, keep = self.working[:cut], self.working[cut:]
        before = estimate_tokens(self._base() + self.working)

        summary = self.model.summarize(old)
        summary_msg = self._capped({
            "role": "user",
            "content": "[Earlier conversation summarized to save context]\n" + summary,
        })
        candidate = [summary_msg] + keep
        after = estimate_tokens(self._base() + candidate)

        # Only apply if it actually SHRINKS the context. Summarizing a few small
        # messages can yield a summary longer than what it replaced — applying that
        # would make things worse, so keep the raw turns instead. (Skipping the
        # wasted summarize() attempt when the kept tail alone already exceeds the
        # budget is a deeper tuning step — see ROADMAP Phase-4 follow-ups.)
        if after >= before:
            if self.verbose:
                print(f"  [compact skipped] summary not smaller (~{before} -> ~{after})")
            return

        self.working = candidate
        self.traj.log_compaction(len(old), summary, before, after)
        log.info("compacted %d msgs  ~%d->%d tok", len(old), before, after)
        if self.verbose:
            print(f"  [compact] summarized {len(old)} msgs  ~{before}->{after} tok")
