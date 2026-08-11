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
    # 0.13.0: safety-config provenance (Phase 33 / specs/0033). session_start gains an OPTIONAL `safety` block -
    #        a human-readable snapshot of which safety/verification guards were ACTIVE at launch (permission
    #        mode/rules/fence, guardian/sandbox/execpolicy/hooks, the verify family, propose/spec/goal, web/MCP
    #        reach) - so a clean guardian-ON run is never indistinguishable from a guardian-OFF one. Written
    #        only when the construction site supplies it (it holds the Permissions); ABSENT on legacy/test
    #        records, keeping those byte-identical. It is a LAUNCH-time snapshot (a --resume or a mid-session
    #        /mode / /add-dir does not re-stamp it).
    # 0.12.0: completion & manifest honesty (Phase 26 / specs/0026). The `manifest` record gains an OPTIONAL
    #        `applied` field - whether every APPROVED item actually landed in the mutation ledger - written
    #        only when CODE_VERIFY_MANIFEST is on and the plan was approved. A partial apply (applied=False)
    #        is dropped from SFT (train/convert) so it can't train as a completed change; the field is ABSENT
    #        on a flag-off / legacy record, keeping those byte-identical.
    # 0.11.0: spec-first (Phase 25 / specs/0025). A `spec` record captures an AUTHORED design+acceptance
    #        spec (title/goal/acceptance/non_goals), whether the user APPROVED it, and whether its ACCEPTANCE
    #        items were all met - so a spec->build->met run is a first-class 'contract before acting' signal,
    #        and a DECLINED or UNMET spec is dropped from SFT (train/convert._unmet_spec_turns) so an
    #        undelivered change can't train as completed. Logged ONCE, when the spec resolves.
    # 0.10.0: propose mode (Phase 22 / specs/0022). A `manifest` record captures a proposed change-list
    #        (add/move/update/delete + why), whether the user APPROVED the whole plan, and the mode - so a
    #        propose->approve->execute run is a first-class 'plan before acting' signal, and a DECLINED plan
    #        (approved=False) is dropped from SFT (train/convert._unapplied_manifest_turns) so a change that
    #        never happened can't train as completed. Logged ONCE, when the manifest resolves.
    # 0.9.0: adaptive effort (Phase 21 / specs/0021). `model_call` gains an `effort` field (the reasoning
    #        level that call ran at), and an `effort_change` record marks each escalation (old->new + the
    #        struggle that drove it) - the metacognitive signal the flywheel learns an effort policy from.
    # 0.8.0: goal loops (Phase 20 / specs/0020). A `goal` record marks a pursued objective + the
    #        MODEL-PROPOSED bar (argv), attempts burned, and whether the bar finally passed. The bar's
    #        pass/fail rides the existing `verification` record — logged ONCE, when the loop resolves, so
    #        a converged loop stays trainable (an intermediate failure would drop it).
    # 0.7.0: per-turn honesty (corpus integrity). A REPL session is ONE trajectory with many turns,
    #        so a `turn_outcome` record now marks each turn's honest outcome (via src/outcomes.classify).
    #        train/convert.py drops exactly the degenerate/ungrounded/unverified turns and keeps the good
    #        ones, instead of keeping-or-dropping the whole file by its single session_end label.
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
    # note (specs/0035 fix C): the per-turn situational-context env block is now captured via
    #        ContextManager.log_env_capture as a `turn` with role:'system' (was role:'user'), so the raw SFT
    #        view no longer trains it as something the USER said. This is NOT a schema change — `role` is a
    #        value inside the opaque message dict a `turn` already carries, not a record field — so
    #        SCHEMA_VERSION is unchanged and old data stays byte-identical. Only emitted under
    #        CODE_SITUATIONAL_CONTEXT (off by default).
    SCHEMA_VERSION = "0.13.0"

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
                 parent_session_id=None, depth=0, safety=None):
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
        rec = {
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
        }
        # specs/0033: the safety/verification config fingerprint (which guards were on at launch), written
        # ONLY when the construction site supplies it (it holds the Permissions -> config.safety_fingerprint).
        # Omitted when None, so a legacy / test construction stays byte-identical and old trajectories convert.
        if safety is not None:
            rec["safety"] = safety
        self._write(rec)

    def _write(self, rec):
        # specs/0059: scrub secrets / PII from the PERSISTED record before it hits disk, so the flywheel never
        # ingests a pasted token or budget. Gated + lazy -> OFF is byte-identical (scrub is never imported).
        from . import config
        if config.SCRUB_TRAJECTORY:
            from . import scrub
            rec = scrub.scrub_record(rec)
        self.f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.f.flush()

    def log_model_call(self, step, messages, tool_names, msg, usage, latency_ms, effort=None):
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
            # The reasoning effort this call actually ran at (specs/0021) — a per-step field so a
            # step-level / DPO filter can weight by effort. None when unset (inherit the provider default).
            "effort": effort or None,
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

    def log_turn_outcome(self, turn, outcome, terminated, tool_calls):
        """Record one REPL turn's honest outcome (0.7.0). A multi-turn session is ONE trajectory, so
        without this the converter saw only the single session_end label and had to keep or drop the
        WHOLE file — silently training on a degenerate/ungrounded/unverified turn, or discarding a good
        turn beside a bad one. With a per-turn outcome, train/convert.py drops exactly the bad turns and
        keeps the good ones. The one-shot path is a single turn == the session, so it uses session_end."""
        self._write({
            "type": "turn_outcome",
            "session_id": self.session_id,
            "ts": _ts(),
            "turn": turn,
            "outcome": outcome,          # src/outcomes.classify — 'completed'/'success' train; the rest don't
            "terminated": terminated,    # the raw agent.RunResult.terminated
            "tool_calls": tool_calls,    # this turn's own tool-call count (RunResult.tool_calls)
        })

    def log_dir_grant(self, path, tier):
        """specs/0074: a mid-session directory grant (/add-dir or a trusted-user-dir auto-grant), typed so
        session.resume can re-apply it by TIER — instead of re-parsing the model-visible '(system) Read access
        granted to: X' prose (fragile; the trusted-dir variant prints the user-typed path, not the realpath).
        Additive record: convert.py ignores unknown types, so no SCHEMA_VERSION bump and old files are unchanged."""
        self._write({
            "type": "dir_grant",
            "session_id": self.session_id,
            "ts": _ts(),
            "path": path,                # realpath of the granted directory
            "tier": tier,                # "read_only" | "extra" (write-capable)
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

    def log_effort_change(self, old, new, struggle, request):
        """Record an adaptive-effort escalation (specs/0021): the level before/after, the struggle score
        that (with any tool request) drove it, and the task it happened on. The trainable metacognitive
        signal - 'a task shaped like THIS needed more thinking' - the flywheel learns an effort policy
        from, and the same signal the online learner feeds on. Logged only when the level actually moves."""
        self._write({
            "type": "effort_change",
            "session_id": self.session_id,
            "ts": _ts(),
            "old": old or "",
            "new": new or "",
            "struggle": struggle,
            "request": (request or "")[:400],
        })

    def log_goal(self, objective, bar, iterations_used, max_iterations, met):
        """Record a goal loop's OUTCOME once, when it resolves (specs/0020) — the objective, the bar the
        model proposed, how many attempts it burned, and whether the bar ultimately passed. An audit trail
        of what the agent was told to converge on, and a first-class signal for the flywheel (did declaring
        a bar lead anywhere?). The bar's pass/fail reward itself rides the `verification` record."""
        self._write({
            "type": "goal",
            "session_id": self.session_id,
            "ts": _ts(),
            "objective": objective,
            "bar": list(bar or []),
            "iterations_used": iterations_used,
            "max_iterations": max_iterations,
            "met": bool(met),
        })

    def log_manifest(self, items, approved, mode=None, applied=None):
        """Record a proposed change-list's resolution once (specs/0022 propose mode): the proposed items
        (add/move/update/delete + why), whether the user APPROVED the whole plan, and the mode it was
        proposed in. A propose->approve->execute run is a first-class 'plan before acting' signal for the
        flywheel; a DECLINED plan (approved=False) marks a turn train/convert.py must NOT keep as a
        completed change. Logged ONCE, when the manifest resolves — never per revision or while awaiting."""
        # specs/0026: `applied` (whether every APPROVED item landed in the mutation ledger) is written ONLY
        # when computed (CODE_VERIFY_MANIFEST on + approved), so a flag-off / legacy record is byte-identical
        # and convert.py's `applied is False` partial-apply drop never matches it.
        rec = {
            "type": "manifest",
            "session_id": self.session_id,
            "ts": _ts(),
            "items": list(items or []),
            "approved": bool(approved),
            "mode": mode,
        }
        if applied is not None:
            rec["applied"] = bool(applied)
        self._write(rec)

    def log_spec(self, title, goal, acceptance, non_goals, approved, acceptance_met):
        """Record a spec-first design contract's resolution once (specs/0025 spec-first): the authored spec
        (title/goal/acceptance/non_goals), whether the user APPROVED it, and whether every ACCEPTANCE item
        was met. A spec->build->met run is a first-class 'contract before acting' signal for the flywheel; a
        DECLINED (approved=False) or UNMET (acceptance_met=False) spec marks a turn train/convert.py must NOT
        keep as a completed change. Logged ONCE, at resolution - never per revision or while awaiting."""
        self._write({
            "type": "spec",
            "session_id": self.session_id,
            "ts": _ts(),
            "title": title,
            "goal": goal,
            "acceptance": list(acceptance or []),
            "non_goals": list(non_goals or []),
            "approved": bool(approved),
            "acceptance_met": bool(acceptance_met),
        })

    def end(self, outcome, final_text=None, terminated=None):
        self._write({
            "type": "session_end",
            "session_id": self.session_id,
            "ts": _ts(),
            # success | completed | verify_failed | no_action | protocol_stalled | max_steps | error |
            # unverified_completion (Phase 6) | ungrounded_completion (Phase 10) | manifest_declined (Phase 22)
            "outcome": outcome,
            "terminated": terminated,                 # how the loop ended (agent.RunResult)
            "steps": self.steps,
            "tool_calls": self.tool_calls,            # 0 == the agent did nothing
            "completion_tokens_total": self.completion_tokens,
            "final_text": (final_text or "")[:2000],
            "user_label": None,                       # fill from accept/reject UI later
        })
        self.f.close()
