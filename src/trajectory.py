"""
src/trajectory.py

Trajectory logging — the flywheel fuel.

We log at two STABLE boundaries (the model gateway and the tool boundary) rather
than scattered through the harness, so the agent can be refactored freely without
breaking the dataset. One session == one schema-versioned JSONL file.

Records emitted:
  session_start  — task, model, cwd
  model_call     — exact prompt sent, raw model output (incl. reasoning), usage, latency
  tool_call      — tool, args, result, ok/fail, retry_index  (cheapest reward signal)
  verification   — test/lint command + pass/fail            (objective reward signal)
  session_end    — outcome, totals, user_label (filled in later from accept/reject UI)

Bump SCHEMA_VERSION whenever a record shape changes so old data stays interpretable.
"""
import os
import json
import uuid
import datetime

from .toolset import active_schemas


def _ts():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class Trajectory:
    # 0.5.0: interactivity (Phase 4). A `session_resume` record marks where a stopped
    #        session was reopened and continued (see Trajectory.resume). The session
    #        keeps its original id; multi-turn/resumed sessions are one growing file.
    # 0.4.0: subagents (Phase 4). session_start carries `parent_session_id` + `depth`
    #        so nested subagent runs link to their parent. Top-level = (null, 0).
    # 0.3.0: capture vs. context. With compaction, what the model SEES diverges from
    #        the raw history, so we log BOTH:
    #   - `turn`        : raw per-turn messages, never compacted — the full history.
    #   - `model_call`  : marked `as_sent` — the (possibly compacted) context sent.
    #   - `compaction`  : emitted when older turns are summarized away.
    # 0.2.0: session_start carries full tool_schemas (Phase-3 self-containment gate).
    # 0.6.0: `permission` record per gated tool call (Phase 4 #6) — the decision
    #        (allow/ask/deny + which rule/mode decided it), captured before the call.
    # Older data stays usable — the converter falls back to as-sent / reattachment.
    SCHEMA_VERSION = "0.6.0"

    @classmethod
    def resume(cls, path):
        """Reopen an existing trajectory to continue it (append mode).

        Rehydrates the running counters from the file and logs a `session_resume`
        marker. Does NOT write a new session_start — the session keeps its original
        id and schema. Used by src/session.py to continue a stopped session.
        """
        recs = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
        ss = next((r for r in recs if r.get("type") == "session_start"), {})
        self = cls.__new__(cls)
        self.session_id = ss.get("session_id") or os.path.basename(path).split(".")[0]
        self.path = path
        self.steps = 0
        self.tool_calls = sum(1 for r in recs if r.get("type") == "tool_call")
        self.completion_tokens = sum((r.get("usage") or {}).get("completion_tokens") or 0
                                     for r in recs if r.get("type") == "model_call")
        self.tool_schemas = ss.get("tool_schemas")
        self.f = open(path, "a", encoding="utf-8")
        self._write({"type": "session_resume", "session_id": self.session_id, "ts": _ts()})
        return self

    def __init__(self, traj_dir, task, model, cwd, tool_schemas=None,
                 parent_session_id=None, depth=0):
        os.makedirs(traj_dir, exist_ok=True)
        self.session_id = uuid.uuid4().hex[:12]
        self.path = os.path.join(traj_dir, f"{self.session_id}.jsonl")
        self.f = open(self.path, "w", encoding="utf-8")
        self.steps = 0
        self.tool_calls = 0
        self.completion_tokens = 0
        # Default to the ACTIVE toolset (base + web + MCP) so EVERY trajectory is
        # self-contained and records exactly what was offered this run.
        self.tool_schemas = tool_schemas if tool_schemas is not None else active_schemas()
        self._write({
            "type": "session_start",
            "schema_version": self.SCHEMA_VERSION,
            "session_id": self.session_id,
            "ts": _ts(),
            "task": task,
            "model": model,
            "cwd": cwd,
            "tool_schemas": self.tool_schemas,
            "parent_session_id": parent_session_id,   # None for a top-level run
            "depth": depth,                            # 0 top-level, 1+ subagent
        })

    def _write(self, rec):
        self.f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.f.flush()

    def log_model_call(self, step, messages, tool_names, msg, usage, latency_ms):
        tool_calls = [
            {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
            for tc in (msg.tool_calls or [])
        ]
        u = {}
        if usage is not None:
            u = {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
            }
            self.completion_tokens += (u.get("completion_tokens") or 0)
        self._write({
            "type": "model_call",
            "session_id": self.session_id,
            "ts": _ts(),
            "step": step,
            # The exact input the model saw this step. With compaction this is the
            # AS-SENT (possibly summarized) view, not the raw history — the raw
            # history lives in the `turn` records. as_sent=True marks that.
            "request": {"messages": messages, "tools": tool_names, "as_sent": True},
            "response": {
                "content": msg.content,
                # gpt-oss / reasoning models surface a separate reasoning channel.
                "reasoning": getattr(msg, "reasoning_content", None),
                "tool_calls": tool_calls,
            },
            "usage": u,
            "latency_ms": round(latency_ms),
        })

    def log_turn(self, message):
        """One raw message added to the conversation — the lossless history stream.

        Logged for EVERY message regardless of compaction, so the full raw
        conversation is always reconstructable by concatenating `turn` records.
        Never summarized; decoupled from the live (compactable) context.
        """
        self._write({
            "type": "turn",
            "session_id": self.session_id,
            "ts": _ts(),
            "message": message,
        })

    def log_compaction(self, summarized_count, summary, before_tokens, after_tokens):
        """Emitted when the ContextManager summarizes older turns away.

        Records what was compacted out of the LIVE context (the raw turns are
        untouched in the `turn` stream). The summary itself is produced by a model
        call that is deliberately NOT logged as a `model_call`, so it doesn't look
        like an agent step.
        """
        self._write({
            "type": "compaction",
            "session_id": self.session_id,
            "ts": _ts(),
            "summarized_messages": summarized_count,
            "summary": summary,
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
        })

    def log_permission(self, step, name, decision):
        """Record the permission decision for a tool call (Phase 4 #6), written just
        BEFORE the call runs. Captures WHY a tool was permitted or refused — training
        signal (the model learns the boundary) and an audit trail of what was allowed."""
        self._write({
            "type": "permission",
            "session_id": self.session_id,
            "ts": _ts(),
            "step": step,
            "tool": name,
            "target": decision.target,
            "allowed": decision.allowed,
            "action": decision.action,        # allow | ask | deny
            "reason": decision.reason,        # which step/rule/mode decided it
            "rule": decision.rule,            # the matched rule string, if any
            "mode": decision.mode,            # active permission mode
        })

    def log_tool_call(self, step, name, args, result, retry_index):
        self.tool_calls += 1
        self._write({
            "type": "tool_call",
            "session_id": self.session_id,
            "ts": _ts(),
            "step": step,
            "tool": name,
            "args": args,
            "ok": result.ok,
            "retry_index": retry_index,      # >0 means the model fumbled this tool before
            "result": result.content[:4000],
            "meta": result.meta,
        })

    def log_verification(self, command, ok, output):
        self._write({
            "type": "verification",
            "session_id": self.session_id,
            "ts": _ts(),
            "command": command,
            "ok": ok,
            "output": output[:4000],
        })

    def end(self, outcome, final_text=None, terminated=None):
        self._write({
            "type": "session_end",
            "session_id": self.session_id,
            "ts": _ts(),
            # success | completed | verify_failed | no_action | protocol_stalled |
            # max_steps | error
            "outcome": outcome,
            "terminated": terminated,                 # how the loop ended (agent.RunResult)
            "steps": self.steps,
            "tool_calls": self.tool_calls,            # 0 == the agent did nothing
            "completion_tokens_total": self.completion_tokens,
            "final_text": (final_text or "")[:2000],
            "user_label": None,                       # fill from accept/reject UI later
        })
        self.f.close()
