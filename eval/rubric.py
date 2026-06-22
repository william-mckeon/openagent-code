"""
eval/rubric.py

Behavior scoring for agentic evals (specs/0004-agentic-evals.md).

Pure, deterministic heuristics over a trajectory's JSONL records — no model, no network.
The verify eval answers "did the code end up correct?"; this answers "did the agent BEHAVE
well?" (read enough, didn't refuse, finished, didn't over-ask). Crude but real signal on the
exact failures the live logs showed (shallow reviews, "narrow the scope" deflections), so the
flywheel can select good behavior to train on instead of us hand-patching the prompt.

Scoring is PER-TURN. A trajectory may be a one-shot task (one turn) or a multi-turn REPL
transcript (many). Each turn is scored on its own — so a deflection in turn 3 is caught even
if turn 5 finished cleanly — and the session score is the mean across turns.
"""
import json

# "narrow the scope / which part?" deflections — refusing a broad review instead of mapping
# it. Matched against a turn's final answer when that turn did little/no investigation.
REFUSAL_PHRASES = (
    "narrow the scope", "narrow it down", "could you narrow", "which part",
    "could you specify", "let me know which", "pick one of the following",
    "would be extremely long", "would be very long", "which file", "which service",
    "narrow down", "too long to", "select a subset",
)


def load_records(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# -- turn segmentation -------------------------------------------------------

def _turns(records):
    """Split a trajectory into per-turn record segments. A new turn starts at each
    `model_call` with step 0 (the loop's step counter resets per agent.run / per REPL
    turn). Records before the first model_call ride with the first turn; `session_end`
    rides with the last. A trajectory with no model_call is a single segment."""
    turns, cur, seen_mc = [], [], False
    for r in records:
        if r.get("type") == "model_call" and r.get("step") == 0 and seen_mc:
            turns.append(cur)
            cur, seen_mc = [], False
        cur.append(r)
        if r.get("type") == "model_call":
            seen_mc = True
    if cur:
        turns.append(cur)
    return turns or [records]


def _tool_calls(turn):
    return [r for r in turn if r.get("type") == "tool_call"]


def _reads(turn):
    paths = set()
    for tc in _tool_calls(turn):
        if tc.get("tool") == "read_file" and tc.get("ok"):
            p = (tc.get("args") or {}).get("path")
            if p:
                paths.add(p)
    return paths


def _final(turn):
    """The turn's closing assistant text: the last model response, or (for one-shot eval
    tasks) the session_end.final_text."""
    for r in reversed(turn):
        if r.get("type") == "model_call":
            return (r.get("response") or {}).get("content") or ""
    se = next((r for r in turn if r.get("type") == "session_end"), {})
    return se.get("final_text") or ""


def _is_refusal(final, n_tools):
    t = (final or "").lower()
    return any(p in t for p in REFUSAL_PHRASES) and n_tools <= 1


def _mentions_missed(final, must_mention):
    """Which required topics the final answer FAILED to mention (case-insensitive
    substring). This is what makes the behavior eval discriminate: a shallow review
    that reads the files but never names the planted issue (hardcoded secret, swallowed
    exception, ...) scores lower than one that actually catches them. An entry may be a
    LIST of alternatives — any one satisfies it (synonyms)."""
    low = (final or "").lower()
    missed = []
    for entry in must_mention:
        alts = entry if isinstance(entry, (list, tuple)) else [entry]
        if not any(str(a).lower() in low for a in alts):
            missed.append(str(alts[0]))
    return missed


# -- scoring -----------------------------------------------------------------

def score_turn(turn, rubric=None):
    """Score one turn's behavior. `rubric` (from the task yaml) selects which checks apply;
    absent a rubric only the general checks run."""
    rubric = rubric or {}
    tcs = _tool_calls(turn)
    reads = _reads(turn)
    final = _final(turn).strip()
    refused = _is_refusal(final, len(tcs))
    asked = [t for t in tcs if t.get("tool") == "ask_user"]
    over_ask = bool(asked) and len(tcs) > 1 and tcs[-1].get("tool") == "ask_user"

    # Delegated reviews read few files in the LEAD trajectory because each spawned child
    # reviews a whole area in its OWN trajectory. Credit spawns toward depth so good
    # decomposition isn't scored as shallow.
    n_spawns = sum(1 for t in tcs if t.get("tool") == "spawn_agent" and t.get("ok"))

    checks = {}
    if "min_files_read" in rubric:
        checks["depth"] = (len(reads) + n_spawns) >= int(rubric["min_files_read"])
    if rubric.get("no_refusal", True):
        checks["no_refusal"] = not refused
    if rubric.get("expect_final", True):
        checks["completed"] = bool(final) and not refused
    checks["no_over_ask"] = not over_ask
    # Findings check (Stage 3): did the review actually NAME the planted issues? This is
    # the discriminating one — reading files isn't enough, you must catch what matters.
    missed = _mentions_missed(final, rubric["must_mention"]) if rubric.get("must_mention") else []
    if rubric.get("must_mention"):
        checks["found_issues"] = not missed

    passed = sum(1 for v in checks.values() if v)
    return {"checks": checks, "files_read": len(reads), "tool_calls": len(tcs),
            "refused": refused, "missed_mentions": missed, "score": passed / (len(checks) or 1)}


def score(records, rubric=None):
    """Score a whole trajectory: mean of its per-turn scores. Aggregate `checks` hold only
    if they hold in EVERY turn (so `missed` flags any turn that failed). Returns the same
    keys the one-shot path expects, plus per-turn detail under `turns`."""
    per = [score_turn(t, rubric) for t in _turns(records)]
    n = len(per) or 1
    keys = set().union(*[set(p["checks"]) for p in per]) if per else set()
    agg = {k: all(p["checks"].get(k, True) for p in per) for k in keys}
    missed = sorted({m for p in per for m in p.get("missed_mentions", [])})
    return {
        "score": sum(p["score"] for p in per) / n,
        "turns": per,
        "checks": agg,
        "files_read": sum(p["files_read"] for p in per),
        "tool_calls": sum(p["tool_calls"] for p in per),
        "refused": any(p["refused"] for p in per),
        "missed_mentions": missed,
    }


def is_refusal(records):
    """True if ANY turn is a refusal — a 'narrow the scope' deflection with no real work.
    Used by train/convert.py to keep refusals out of the training set."""
    return any(score_turn(t)["refused"] for t in _turns(records))


def files_read(records):
    paths = set()
    for t in _turns(records):
        paths |= _reads(t)
    return paths
