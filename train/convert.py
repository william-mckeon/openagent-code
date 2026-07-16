"""
train/convert.py

SFT converter — turns captured trajectories into training rows. "Makes every run count."

Run:  python -m train.convert

Pipeline:
  1. read every trajectories/**/*.jsonl
  2. FILTER to trainable sessions (outcome success/completed, verification ok if
     present, at least one tool call) — drop no_action / protocol_stalled /
     verify_failed / max_steps / error / incomplete, and say why (no silent drops)
  3. FLATTEN each kept session into PER-STEP rows: one row per agent action
     (model_call) = {messages: the prefix it saw, completion: the action it took},
     plus the tool schemas. User/tool messages live in the prefix, never their own
     target row. This is the unit step-level filtering / DPO / RL operate on.
  4. WRITE train/dataset/sft.jsonl + train/dataset/report.json (auditable counts)

Forward-compatible tool schemas (the Phase-B gate):
  Native-mode trajectories log only tool NAMES (the full schemas go through the
  API `tools` param). So today we reattach the CURRENT schemas from src/tools.py.
  Phase B will log the full schemas once in session_start under "tool_schemas";
  this converter already PREFERS that field when present and only falls back to
  reattachment — so when the gate lands, it starts using the richer, self-
  contained data automatically, with zero changes here. See ROADMAP.md.
"""
import os
import sys
import glob
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.tools import TOOLS, openai_schemas  # noqa: E402
from src.trajectory import Trajectory  # noqa: E402  (for SCHEMA_VERSION)
from src import config  # noqa: E402  (for SFT_VIEW)
from eval import rubric  # noqa: E402  (behavior gate — specs/0004-agentic-evals.md)
from src.prompts import strip_reasoning_preamble  # noqa: E402  (keep leaked CoT out of targets)
from train import curate  # noqa: E402  (Phase 11 corpus curation — specs/0011)

TRAJ_GLOB = os.path.join(ROOT, "trajectories", "**", "*.jsonl")
# TRAIN/EVAL FIREWALL: the eval suite is the HELD-OUT promotion gate. Converting its
# trajectories would train the student on the very tasks we judge it by — teaching to the
# test, which makes the gate meaningless (specs/0005). So any trajectory under
# trajectories/eval/ is excluded from the corpus. EVERY other run still counts.
EVAL_TRAJ_DIR = os.path.normpath(os.path.join(ROOT, "trajectories", "eval"))
OUT_DIR = os.path.join(ROOT, "train", "dataset")
OUT_FILE = os.path.join(OUT_DIR, "sft.jsonl")
REPORT_FILE = os.path.join(OUT_DIR, "report.json")


def _is_eval_trajectory(path):
    """True if `path` lives under trajectories/eval/ (the held-out gate)."""
    ap = os.path.normpath(os.path.abspath(path))
    return ap.startswith(EVAL_TRAJ_DIR + os.sep)

KEEP_OUTCOMES = {"success", "completed"}
CURRENT_TOOLS = openai_schemas(TOOLS)


def load_session(path):
    """Parse one trajectory file into a list of records (skips malformed lines)."""
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _first(records, rec_type):
    for r in records:
        if r.get("type") == rec_type:
            return r
    return None


def _has_denial(records):
    """True if any tool call in the session was BLOCKED (permission allowed==False) — a guardian/permission
    denial. Such a call must never become a positive SFT target (we'd be training a blocked action)."""
    return any(r.get("type") == "permission" and r.get("allowed") is False for r in records)


def _contested_turns(records):
    """Turn indices that contained a BLOCKED (denied) tool call — ride-5 corpus integrity. A denied action
    is a negative, not a training target, and the turn built around it (the give-up / work-around it
    provoked) is tainted, so the whole turn is excluded. Empty when there are no turn_outcome records."""
    out, contested, seg = set(), False, 1
    for r in records:
        t = r.get("type")
        if t == "permission" and r.get("allowed") is False:
            contested = True
        elif t == "turn_outcome":
            idx = r.get("turn", seg)
            if contested:
                out.add(idx)
            contested, seg = False, idx + 1
    return out


def trainable_turns(records):
    """{turn -> bool}: which REPL turns are trainable — an honest keeper outcome, that turn's OWN
    verifications all passed, AND no blocked (guardian-denied) call in it. Empty dict when the trajectory
    has no `turn_outcome` records (one-shot / legacy), which keeps the old whole-session behavior. The
    verify + contest checks are scoped PER TURN, so a single late failing/contested turn no longer drops
    the good turns beside it (0.7.0 + ride-5 corpus integrity)."""
    contested = _contested_turns(records)
    turns, verif_ok, seg = {}, True, 1
    for r in records:
        t = r.get("type")
        if t == "verification":
            verif_ok = verif_ok and bool(r.get("ok"))
        elif t == "turn_outcome":
            idx = r.get("turn", seg)
            turns[idx] = (r.get("outcome") in KEEP_OUTCOMES) and verif_ok and (idx not in contested)
            verif_ok, seg = True, idx + 1
    return turns


def is_trainable(records):
    """(keep: bool, reason: str). Reason is the drop cause when keep is False.

    A multi-turn REPL session (0.7.0) is judged PER TURN via trainable_turns(): keep the session if any
    turn is trainable (to_rows emits only those turns). A one-shot / legacy session has no turn_outcome
    records, so it is judged as one unit by session_end exactly as before."""
    end = _first(records, "session_end")
    if end is None:
        return False, "incomplete"  # crashed before close

    turns = trainable_turns(records)
    if turns:  # multi-turn REPL — per-turn honesty
        # Refusal / curation are still session-level (they judge the closing answer).
        if rubric.is_refusal(records):
            return False, "refusal"
        if config.CURATE and config.CURATE_MODE == "exclude":
            grounded, _ung = curate.curation_verdict(records)
            if not grounded:
                return False, "ungrounded_answer"
        if any(turns.values()):
            return True, "kept"
        return False, "no_trainable_turn"

    # One-shot / legacy: the whole session is one unit, gated by session_end (unchanged behavior).
    outcome = end.get("outcome")
    if outcome not in KEEP_OUTCOMES:
        return False, outcome or "unknown"
    if (end.get("tool_calls") or 0) == 0:
        return False, "no_tool_calls"
    verifications = [r for r in records if r.get("type") == "verification"]
    if verifications and not all(v.get("ok") for v in verifications):
        return False, "verify_failed"
    # Ride-5 corpus integrity: a one-shot run that hit a guardian/permission denial is CONTESTED — its
    # blocked action is a negative, so drop the whole session rather than train a blocked emission.
    if _has_denial(records):
        return False, "guardian_contested"
    # Behavior gate (specs/0004): even a verify-passing run is bad training data if the
    # agent REFUSED (a "narrow the scope" deflection) — we don't want to teach that.
    if rubric.is_refusal(records):
        return False, "refusal"
    # Corpus curation (Phase 11): in EXCLUDE mode, drop a session whose closing answer cites a file it
    # never opened (a phantom citation). In FLAG mode (default) keep it — to_rows tags the rows instead.
    if config.CURATE and config.CURATE_MODE == "exclude":
        grounded, _ung = curate.curation_verdict(records)
        if not grounded:
            return False, "ungrounded_answer"
    return True, "kept"


def tools_for_session(records):
    """Prefer schemas logged in session_start (Phase B); else reattach current ones."""
    start = _first(records, "session_start") or {}
    logged = start.get("tool_schemas")
    if logged:
        return logged, "logged"
    return CURRENT_TOOLS, "reattached"


def _assistant_from_response(resp):
    """Convert a logged model response into an OpenAI-format assistant message (the SFT target).

    On a TOOL-CALL turn, FOLD the reasoning channel into content exactly as the runtime planner does
    (src/planner.py): gpt-oss keeps its PLAN in a separate reasoning channel with empty content on a
    tool-call turn, so reading only resp['content'] gave a target of {content:'', tool_calls:[...]} —
    training the student to emit reasoning-FREE tool calls, the looping the specs/0005 Stage-2 fix was
    meant to prevent. The final answer (no tool calls) stays clean (its reasoning is NOT folded — the
    preamble strip in _step_row handles a leak there), matching the planner."""
    content = resp.get("content") or ""
    tcs = resp.get("tool_calls") or []
    if tcs:
        reasoning = (resp.get("reasoning") or "").strip()
        if reasoning:
            body = content.strip()
            content = f"{reasoning}\n\n{body}" if body else reasoning
        return {"role": "assistant", "content": content, "tool_calls": [{
            "id": tc["id"], "type": "function",
            "function": {"name": tc["name"], "arguments": tc["arguments"]},
        } for tc in tcs]}
    return {"role": "assistant", "content": content}


def _step_row(step, view, tools, base_meta):
    """One model_call -> one per-step SFT row (prefix messages -> the agent action)."""
    mc, tcs = step["mc"], step["tcs"]
    # raw: the uncompacted history up to this step (from the `turn` stream).
    # as_sent: exactly what the model received (possibly compacted). Pre-0.3.0 has
    # no turns, so raw falls back to as_sent.
    if view == "raw" and step["prefix_raw"]:
        prefix, used = list(step["prefix_raw"]), "raw"
    else:
        prefix = list(mc["request"]["messages"])
        used = "as_sent" if view == "as_sent" else "as_sent_fallback"
    completion = _assistant_from_response(mc["response"])
    # Data hygiene: strip a leaked reasoning preamble from a FINAL-answer target so the corpus never
    # teaches the model to dump chain-of-thought before its user-facing answer (seen live with gpt-oss).
    # ONLY on the final answer (no tool_calls) — a tool-call target's content is the DELIBERATELY folded
    # plan (above), which must NOT be stripped. This mirrors the runtime planner (strip final, keep plan).
    if not completion.get("tool_calls"):
        completion["content"] = strip_reasoning_preamble(completion.get("content") or "")
    return {
        "messages": prefix,                                  # the input the agent saw
        "completion": completion,                            # the action it took (preamble-stripped)
        "tools": tools,
        "meta": {
            **base_meta,
            "step": mc.get("step"),
            "view": used,
            "tools_called": [tc.get("tool") for tc in tcs],
            "all_ok": (all(tc.get("ok") for tc in tcs) if tcs else None),
            "max_retry": max([tc.get("retry_index", 0) for tc in tcs], default=0),
            "effort": mc.get("effort") or None,   # the reasoning level this step ran at (specs/0021) - a
                                                   # step-level/DPO filter can weight by it. Neutral to keep/drop.
        },
    }


def to_rows(records, view):
    """One kept session -> a LIST of per-step rows (one per model_call).

    Each agent action (model_call response) is its own training row, with the
    conversation-so-far as the prompt. User and tool messages live inside that
    prompt — they are never their own target row (we don't train the model to
    speak as the user). This is the unit step-level filtering / DPO / RL need.
    """
    tools, src = tools_for_session(records)
    start = _first(records, "session_start") or {}
    end = _first(records, "session_end") or {}
    base_meta = {
        "session_id": start.get("session_id"),
        "outcome": end.get("outcome"),
        "tool_schema_source": src,
        "parent_session_id": start.get("parent_session_id"),  # links subagent rows
        "depth": start.get("depth", 0),
    }
    # Curation tag (Phase 11): stamp every row of the session with the grounding verdict so downstream
    # step-level / DPO filtering can drop or down-weight phantom-citation rows even in FLAG mode.
    if config.CURATE:
        grounded, ungrounded = curate.curation_verdict(records)
        base_meta["curation"] = {"grounded": grounded, "ungrounded": ungrounded}

    # Per-turn filtering (0.7.0): tag each step with the REPL turn it belongs to (turns are delimited by
    # `turn_outcome` records), then emit only steps from a trainable turn. A one-shot / legacy session has
    # no turn_outcome records -> turn_ok is empty and every step is kept (is_trainable already gated it).
    turn_ok = trainable_turns(records)
    rows, raw_prefix, pending, cur_turn = [], [], None, 1
    for r in records:
        t = r.get("type")
        if t == "model_call":
            if pending is not None:
                rows.append((_step_row(pending, view, tools, base_meta), cur_turn))
            pending = {"mc": r, "prefix_raw": list(raw_prefix), "tcs": []}
        elif t == "turn":
            raw_prefix.append(r["message"])
        elif t == "tool_call" and pending is not None:
            pending["tcs"].append(r)
        elif t == "turn_outcome":
            if pending is not None:
                rows.append((_step_row(pending, view, tools, base_meta), cur_turn))
                pending = None
            cur_turn = r.get("turn", cur_turn) + 1
    if pending is not None:
        rows.append((_step_row(pending, view, tools, base_meta), cur_turn))

    if not turn_ok:                       # one-shot / legacy: whole session already gated
        return [row for row, _ in rows]
    return [row for row, turn in rows if turn_ok.get(turn, False)]


def main():
    all_files = sorted(glob.glob(TRAJ_GLOB, recursive=True))
    # Firewall: drop the held-out eval gate before anything else (no silent contamination).
    files = [p for p in all_files if not _is_eval_trajectory(p)]
    excluded_eval = len(all_files) - len(files)
    rows, dropped, schema_src = [], {}, {"logged": 0, "reattached": 0}
    versions = set()
    kept_sessions = 0
    contested_turns_total = 0   # ride-5: turns excluded from KEPT sessions because they held a denied call

    for path in files:
        records = load_session(path)
        if not records:
            dropped["empty"] = dropped.get("empty", 0) + 1
            continue
        start = _first(records, "session_start") or {}
        versions.add(start.get("schema_version"))

        keep, reason = is_trainable(records)
        if not keep:
            dropped[reason] = dropped.get(reason, 0) + 1
            continue
        session_rows = to_rows(records, config.SFT_VIEW)
        if not session_rows:
            dropped["no_model_calls"] = dropped.get("no_model_calls", 0) + 1
            continue
        rows.extend(session_rows)
        kept_sessions += 1
        contested_turns_total += len(_contested_turns(records))
        schema_src[session_rows[0]["meta"]["tool_schema_source"]] += 1

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    report = {
        "converter_schema_versions_seen": sorted(v for v in versions if v),
        "current_trajectory_schema": Trajectory.SCHEMA_VERSION,
        "sft_view": config.SFT_VIEW,
        "row_unit": "per_step",
        "total_sessions": len(files),
        "excluded_eval_gate": excluded_eval,
        "sessions_kept": kept_sessions,
        "rows_written": len(rows),
        "contested_turns_excluded": contested_turns_total,   # ride-5: denied-call turns dropped from kept sessions
        "dropped": dropped,
        "tool_schema_source": schema_src,
        "output": os.path.relpath(OUT_FILE, ROOT).replace(os.sep, "/"),
    }
    # Reattached rows are TOOLSET-FRAGILE: they carry no schemas of their own, so
    # they get the CURRENT src/tools.py toolset stapled on. Correct only while the
    # toolset is unchanged — the moment a tool is added/removed (Phase 4), these
    # rows mis-convert (claiming tools the run never had). Surface it loudly so it
    # can't be silently forgotten. See ROADMAP.md Phase 3.
    if schema_src["reattached"]:
        report["warning"] = (
            f"{schema_src['reattached']} row(s) used REATTACHED schemas (pre-0.2.0 "
            "trajectories). They will mis-convert if the toolset changes — delete or "
            "re-capture them before changing tools (ROADMAP Phase 3).")

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print(f"SFT convert | corpus={len(files)} (excluded {excluded_eval} eval-gate) "
          f"kept={kept_sessions} -> rows={len(rows)} (per-step)")
    if dropped:
        print("dropped: " + ", ".join(f"{k}={v}" for k, v in sorted(dropped.items())))
    print(f"tool schemas: {schema_src['reattached']} reattached, {schema_src['logged']} logged")
    if "warning" in report:
        print("WARNING: " + report["warning"])
    print(f"wrote {report['output']}  (+ report.json)")


if __name__ == "__main__":
    main()
