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
single item can grow unbounded and blow the model's window (our "no unbounded model-visible
items" rule). The full text is always kept raw in the trajectory.
"""
import json

from . import config
from .logsetup import get_logger

log = get_logger("context")


def estimate_tokens(messages):
    """Cheap, dependency-free token estimate (~4 chars/token over the JSON)."""
    return sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) // 4


# -- bounded summarize (specs/0034): a single summarize call must never exceed the model window ------------
# These are PURE (no model, no litellm) so model.py delegates to them and the harness tests them offline.

def _msg_render_len(m):
    return len(f"[{m.get('role')}] {m.get('content') or ''}") + 2


def chunk_messages(messages, max_chars):
    """Greedily group messages so each group's rendered length stays <= max_chars (specs/0034) - bounds the
    input to one summarize call. A per-message cap (MAX_MESSAGE_CHARS) keeps any single message well under
    max_chars, so no group ever holds a lone oversized message."""
    chunks, cur, size = [], [], 0
    for m in messages:
        s = _msg_render_len(m)
        if cur and size + s > max_chars:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(m)
        size += s
    if cur:
        chunks.append(cur)
    return chunks


def chunk_text(text, max_chars):
    """Split text into <= max_chars slices (for folding partial summaries)."""
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)] or [""]


def bounded_summary(messages, budget_chars, summarize_once, render):
    """Map-reduce summarize that never hands `summarize_once` more than budget_chars (specs/0034) - so a
    resumed session's whole history can be summarized without a single call overflowing the model window.
    PURE: `summarize_once(text)->str` and `render(messages)->str` are injected, so this is unit-testable with
    NO model. The FAST PATH (input already fits) calls summarize_once ONCE on the full render - byte-identical
    to the old single-shot summarize."""
    rendered = render(messages)
    if len(rendered) <= budget_chars:
        return summarize_once(rendered)
    parts = [summarize_once(render(c)) for c in chunk_messages(messages, budget_chars)]
    if len(parts) == 1:
        return parts[0]
    text = "\n\n".join(parts)
    guard = 0
    while len(text) > budget_chars and guard < 20:
        guard += 1
        text = "\n\n".join(summarize_once(t) for t in chunk_text(text, budget_chars))
    return summarize_once(text)


def sanitize_tail(working):
    """Snap a rehydrated history to a VALID tool-pairing EVERYWHERE (specs/0034, generalized specs/0074), not
    just the tail. A prior turn that died mid-flight — or logged only SOME of a parallel call's results — left
    an assistant tool_call with no matching tool result; Bedrock's Converse API rejects an unpaired
    tool_use/tool_result on the NEXT step, permanently poisoning a resumed session (the tail-only scan missed a
    MID-list dangle and a PARTIAL-results group). The scan: drop a LEADING orphan tool result; then for every
    assistant-with-tool_calls, pair each call id against the immediately-following tool results — synthesize a
    stub result for a missing id MID-history (so the surrounding turns stay valid) and DROP a TRAILING
    incomplete group (nothing follows it to keep). A strict no-op on an already-clean history."""
    w = list(working)
    while w and w[0].get("role") == "tool":          # leading orphan tool result
        w.pop(0)
    out, i, n = [], 0, len(w)
    while i < n:
        m = w[i]
        calls = m.get("tool_calls") if m.get("role") == "assistant" else None
        if not calls:
            out.append(m); i += 1; continue
        j = i + 1
        results = []
        while j < n and w[j].get("role") == "tool":  # the results that immediately follow this turn
            results.append(w[j]); j += 1
        answered = {r.get("tool_call_id") for r in results}
        missing = [c.get("id") for c in calls if c.get("id") not in answered]
        if not missing:
            out.extend([m, *results]); i = j; continue
        if j >= n:                                    # TRAILING incomplete group -> drop it (nothing after)
            break
        out.extend([m, *results])                     # MID-history gap -> keep the turn, stub the missing ids
        for cid in missing:
            out.append({"role": "tool", "tool_call_id": cid,
                        "content": "(interrupted — no result was recorded for this call)"})
        i = j
    return out


class ContextManager:
    def __init__(self, system_prompt, model, trajectory,
                 compact_at_tokens=None, keep_recent=None, verbose=False,
                 initial_working=None):
        self.model = model
        self.traj = trajectory
        self.compact_at = config.COMPACT_AT_TOKENS if compact_at_tokens is None else compact_at_tokens
        self.keep_recent = config.COMPACT_KEEP_RECENT if keep_recent is None else keep_recent
        self.hard_cap = config.COMPACT_HARD_AT_TOKENS   # specs/0034: the SENT context must never exceed this
        self.verbose = verbose

        self.system = {"role": "system", "content": system_prompt}
        self.pinned = None       # always-visible, never-compacted message (e.g. the plan)
        self.pinned_task = None   # the current user request, pinned so compaction can't lose it
        self.pinned_review = None  # a completed review_repo digest, pinned so compaction can't drop it
        self.pinned_goal = None    # the pursued objective + bar (specs/0020), pinned across a long loop
        self.pinned_env = None    # per-turn environment block (specs/0012), refreshed each turn
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
        for review rather than silent (oversized model-visible fragments are flagged, not hidden)."""
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
        """Snapshot the live working set so a failed turn can roll back to its EXACT pre-turn state.

        Returns a COPY of the list, not its length. _compact() can REASSIGN self.working mid-turn
        (summarizing older turns into a shorter list), which invalidates a bare length index: a later
        rollback would then either no-op (the old length now exceeds the shorter list) or `del` at the
        wrong post-compaction boundary — slicing an assistant tool_call away from its tool result and
        leaving a DANGLING call that Bedrock's Converse API rejects on the NEXT turn (session poisoned).
        A snapshot is compaction-invariant. Only the model's live view is marked — capture is untouched."""
        return list(self.working)

    def rollback(self, mark):
        """Restore the live working set to the snapshot mark() took — discarding everything appended
        during a turn that died mid-flight (a Bedrock 503 after some tool results were appended, a raised
        tool), so the live context never ends on an orphaned tool-result or a dangling assistant tool_call
        (the consecutive user/tool blocks Bedrock's Converse API rejects). If compaction ran during the
        failed turn it is rolled back with it — the next turn re-compacts as needed. The trajectory keeps
        the full raw record (capture vs. context): only what the model SEES is trimmed."""
        if isinstance(mark, list):
            self.working = list(mark)
        elif isinstance(mark, int) and 0 <= mark < len(self.working):
            del self.working[mark:]   # back-compat: a legacy integer mark

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
        self.pinned_review = None  # a new task invalidates any prior turn's review digest
        self.pinned_goal = None    # ...and any prior turn's goal/bar (the cross-turn hijack class)
        # Reply-shape precedence (specs/0041): when on, the pin asserts that THIS request is the only
        # instruction in force and an earlier turn's format/length constraint does not carry over. Off ->
        # the neutral "answer THIS directly" lead, byte-identical to before.
        lead = (("The user's current request - this is the ONLY instruction in force this turn; answer THIS, "
                 "in the shape they ask. A format or length constraint from an EARLIER turn does NOT apply "
                 "now unless repeated here:\n") if config.REPLY_SHAPE
                else "The user's current request (answer THIS directly):\n")
        self.pinned_task = (self._capped({"role": "user", "content": lead + text}) if text else None)

    def set_review_digest(self, text):
        """Pin the digest a review_repo fan-out returned — always sent, never compacted.

        The digest carries BOTH the per-area findings the lead must write its final review from AND a
        trailer telling it to synthesize now and not re-run review_repo (orchestrator.py). But it enters
        as an ordinary working message, and the review's own token weight trips compaction on the very
        next step — lossy-summarizing the findings away. A live run then re-ran review_repo twice and,
        having lost the child's read of the auth service, declared it 'empty'. Pinning the digest keeps
        the completed review's evidence (and its stop-trailer) intact through compaction, so the lead
        synthesizes from real per-area findings instead of re-deriving them wrong. A new task clears it
        (see set_task). Bounded like the other pins (specs/0009).

        This is a CONTEXT device only: the digest is already in the raw trajectory as the review_repo
        tool result, so pinning a copy never adds to the captured turn stream.
        """
        self.pinned_review = (self._capped({"role": "user",
                                            "content": "Your COMPLETED review_repo fan-out (write the final "
                                            "review by synthesizing THIS; do not re-run review_repo):\n" + text})
                              if text else None)

    def set_goal(self, text):
        """Pin the pursued objective + its BAR — always sent, never compacted (specs/0020).

        A goal loop is long by construction (N iterations x many steps each), so it WILL compact. The bar
        arrives once, as the `pursue` tool result, and would be summarized away mid-loop — leaving the
        agent grinding toward a target it can no longer state. The re-prompt after a failing bar restates
        it, but only at the END of an iteration; the WORK phase in between is exactly where drift happens.
        Pinning it keeps "what am I converging on, and what decides it" visible throughout. Cleared when
        the loop resolves and by a new task (see set_task). Bounded like the other pins (specs/0009).

        A CONTEXT device only: the goal is already in the raw trajectory (the pursue tool call + the `goal`
        record), so pinning a copy never adds to the captured turn stream."""
        self.pinned_goal = (self._capped({"role": "user", "content": text}) if text else None)

    def set_env_context(self, text):
        """Pin the per-turn environment block (cwd/OS/shell/date/git — see envcontext.py) just before
        the live working messages. It is DYNAMIC state, so unlike the system prompt it must REFRESH each
        turn: this REPLACES the slot on every call (never appends), and being in _base() it is always
        sent and never compacted (it can't go stale from summarization, only from being re-set). Bounded
        like the other pins (specs/0009). A context device only — agent.py also logs a copy as a normal
        turn, so capture is unaffected."""
        self.pinned_env = (self._capped({"role": "user", "content": text}) if text else None)

    def log_env_capture(self, text):
        """Capture the per-turn environment block to the TRAJECTORY, WITHOUT sending it again (specs/0035
        fix C). The block is already SENT once via set_env_context's pin (pinned_env, in _base); this only
        records it in the raw history so the flywheel still captures it — but as role:'system' (auto-
        generated state), NOT the role:'user' turn agent.py used to add. That old add did double duty: it
        logged the capture AND put a second copy in `working` ADJACENT to the task, where the model treated
        it as user input and bled it into a typed path. This records the capture with no second sent copy
        and no touch to the pin. A no-op on empty text."""
        if text:
            self.traj.log_turn({"role": "system", "content": text})

    def _base(self):
        return ([self.system]
                + ([self.pinned_task] if self.pinned_task else [])   # the request first — the anchor
                + ([self.pinned_goal] if self.pinned_goal else [])   # then the bar that decides "done"
                + ([self.pinned] if self.pinned else [])             # then the working plan
                + ([self.pinned_review] if self.pinned_review else [])   # then a completed review digest
                + ([self.pinned_env] if self.pinned_env else []))    # then the live environment block

    def context(self):
        """The message list to send the model this step — compacting first if needed."""
        if self.compact_at and estimate_tokens(self._base() + self.working) > self.compact_at:
            self._compact()
        # Hard model-window ceiling (specs/0034): the soft pass above can still leave the context over the
        # model's TRUE window — the worst case is a resumed session's ENTIRE raw history, or a keep_recent
        # tail that alone exceeds the budget. Guarantee the SENT context fits so a turn can never overflow.
        # A NO-OP for a normal session already under the ceiling (byte-identical).
        self._enforce_hard_cap()
        return self._base() + self.working

    def _enforce_hard_cap(self):
        """Compact/trim in a LOOP until the sent context is under the hard model-window ceiling (specs/0034).
        When compaction can no longer shrink it (the summary isn't smaller, or the kept tail alone exceeds the
        cap), fall back to dropping the OLDEST working message — so this always converges and the context
        provably fits the window. Does nothing when already under the cap."""
        if not self.hard_cap:
            return
        guard = 0
        while self.working and estimate_tokens(self._base() + self.working) > self.hard_cap and guard < 500:
            guard += 1
            if self._compact():
                continue                 # a summarization shrank it; re-measure
            self._trim_oldest()          # no shrink possible -> drop the oldest message and retry

    def _trim_oldest(self):
        """Last-resort hard-cap trim (specs/0034): drop the OLDEST working message, then snap off a now-leading
        orphan 'tool' result so the kept head never begins with a tool result the model would reject."""
        if not self.working:
            return
        del self.working[0]
        while self.working and self.working[0].get("role") == "tool":
            del self.working[0]

    def _safe_cut(self):
        """Largest cut index such that working[cut:] starts at a clean group boundary.

        A 'tool' message depends on the assistant (with tool_calls) before it, so we
        never let the kept tail begin with one — that would orphan it and break the
        next API call. Snap the boundary back to the owning assistant / a user turn.
        """
        cut = len(self.working) - self.keep_recent
        if cut <= 0:
            return 0
        # keep_recent == 0 makes cut == len(working); `< len` guards the index so this doesn't
        # IndexError while snapping the boundary back off a tool message.
        while 0 < cut < len(self.working) and self.working[cut].get("role") == "tool":
            cut -= 1
        return cut

    def _compact(self):
        cut = self._safe_cut()
        if cut <= 0:
            return False  # nothing safe to summarize yet (the hard-cap loop falls back to trimming)
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
        # would make things worse, so keep the raw turns instead. When even this can't
        # help (the kept tail alone exceeds the ceiling), _enforce_hard_cap (specs/0034)
        # falls back to trimming the oldest message; return False to signal no progress.
        if after >= before:
            if self.verbose:
                print(f"  [compact skipped] summary not smaller (~{before} -> ~{after})")
            return False

        self.working = candidate
        self.traj.log_compaction(len(old), summary, before, after)
        log.info("compacted %d msgs  ~%d->%d tok", len(old), before, after)
        if self.verbose:
            print(f"  [compact] summarized {len(old)} msgs  ~{before}->{after} tok")
        return True
