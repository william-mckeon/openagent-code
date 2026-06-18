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

TRAJ_GLOB = os.path.join(ROOT, "trajectories", "**", "*.jsonl")
OUT_DIR = os.path.join(ROOT, "train", "dataset")
OUT_FILE = os.path.join(OUT_DIR, "sft.jsonl")
REPORT_FILE = os.path.join(OUT_DIR, "report.json")

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


def is_trainable(records):
    """(keep: bool, reason: str). Reason is the drop cause when keep is False."""
    end = _first(records, "session_end")
    if end is None:
        return False, "incomplete"  # crashed before close
    outcome = end.get("outcome")
    if outcome not in KEEP_OUTCOMES:
        return False, outcome or "unknown"
    if (end.get("tool_calls") or 0) == 0:
        return False, "no_tool_calls"
    verifications = [r for r in records if r.get("type") == "verification"]
    if verifications and not all(v.get("ok") for v in verifications):
        return False, "verify_failed"
    # Behavior gate (specs/0004): even a verify-passing run is bad training data if the
    # agent REFUSED (a "narrow the scope" deflection) — we don't want to teach that.
    if rubric.is_refusal(records):
        return False, "refusal"
    return True, "kept"


def tools_for_session(records):
    """Prefer schemas logged in session_start (Phase B); else reattach current ones."""
    start = _first(records, "session_start") or {}
    logged = start.get("tool_schemas")
    if logged:
        return logged, "logged"
    return CURRENT_TOOLS, "reattached"


def _assistant_from_response(resp):
    """Convert a logged model response into an OpenAI-format assistant message."""
    msg = {"role": "assistant", "content": resp.get("content") or ""}
    tcs = resp.get("tool_calls") or []
    if tcs:
        msg["tool_calls"] = [{
            "id": tc["id"], "type": "function",
            "function": {"name": tc["name"], "arguments": tc["arguments"]},
        } for tc in tcs]
    return msg


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
    return {
        "messages": prefix,                                  # the input the agent saw
        "completion": _assistant_from_response(mc["response"]),  # the action it took
        "tools": tools,
        "meta": {
            **base_meta,
            "step": mc.get("step"),
            "view": used,
            "tools_called": [tc.get("tool") for tc in tcs],
            "all_ok": (all(tc.get("ok") for tc in tcs) if tcs else None),
            "max_retry": max([tc.get("retry_index", 0) for tc in tcs], default=0),
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

    rows, raw_prefix, pending = [], [], None
    for r in records:
        t = r.get("type")
        if t == "model_call":
            if pending is not None:
                rows.append(_step_row(pending, view, tools, base_meta))
            pending = {"mc": r, "prefix_raw": list(raw_prefix), "tcs": []}
        elif t == "turn":
            raw_prefix.append(r["message"])
        elif t == "tool_call" and pending is not None:
            pending["tcs"].append(r)
    if pending is not None:
        rows.append(_step_row(pending, view, tools, base_meta))
    return rows


def main():
    files = sorted(glob.glob(TRAJ_GLOB, recursive=True))
    rows, dropped, schema_src = [], {}, {"logged": 0, "reattached": 0}
    versions = set()
    kept_sessions = 0

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
        "sessions_kept": kept_sessions,
        "rows_written": len(rows),
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

    print(f"SFT convert | sessions={len(files)} kept={kept_sessions} -> rows={len(rows)} (per-step)")
    if dropped:
        print("dropped: " + ", ".join(f"{k}={v}" for k, v in sorted(dropped.items())))
    print(f"tool schemas: {schema_src['reattached']} reattached, {schema_src['logged']} logged")
    if "warning" in report:
        print("WARNING: " + report["warning"])
    print(f"wrote {report['output']}  (+ report.json)")


if __name__ == "__main__":
    main()
