"""
train/curate.py

Offline corpus curation (Phase 11 / specs/0011). Runs the DETERMINISTIC half of the grounding core
(src/grounding.py) over the captured trajectory corpus to flag PHANTOM CITATIONS — a closing answer
that references a file the trajectory never shows the agent open.

Unlike the runtime grounding gate (Phase 10), this is a BATCH pass over saved trajectories/*.jsonl:
the sandbox workspace is deleted after each run, so it reconstructs the facts from the JSONL records
and makes NO model call. The SEMANTIC honest-but-wrong class (a real file, the wrong facts) is caught
LIVE by the runtime gate, not here — re-judging a deleted workspace offline with a tool-less model call
would just hallucinate, so it is deliberately out of scope.

Conservative BY DESIGN: a false DROP (excluding a correct trajectory) poisons the tiny corpus worse
than a missed phantom, so a citation is flagged ONLY if it appears in NEITHER the engaged-files set
(grounding.touched_paths — files the agent read/wrote) NOR anywhere the agent's tools LISTED (the
[:4000]-capped tool-result content). So discovery-without-read never causes a false drop; only a path
the model referenced but never saw at all is flagged.

Two modes (config.CURATE_MODE), consumed by train/convert.py:
  flag    (default) — tag every session with its verdict (stamped on each SFT row); the corpus is
                      never silently shrunk. Review the flagged rows yourself.
  exclude           — drop ungrounded sessions from the SFT set (via convert.is_trainable), counted in
                      report.json's dropped ledger (no silent drops).

Run standalone for a report:  python -m train.curate
"""
import os
import sys
import glob
import json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import grounding  # noqa: E402

TRAJ_GLOB = os.path.join(ROOT, "trajectories", "**", "*.jsonl")
EVAL_TRAJ_DIR = os.path.normpath(os.path.join(ROOT, "trajectories", "eval"))


def _is_eval_trajectory(path):
    """The held-out eval gate is firewalled from the corpus (specs/0005) — mirror convert._is_eval_trajectory."""
    ap = os.path.normpath(os.path.abspath(path))
    return ap.startswith(EVAL_TRAJ_DIR + os.sep)


def _final_answer(records):
    """The session's closing answer: session_end.final_text, else the last model_call's content."""
    end = next((r for r in records if r.get("type") == "session_end"), None)
    if end and end.get("final_text"):
        return end["final_text"]
    for r in reversed(records):
        if r.get("type") == "model_call":
            return (r.get("response") or {}).get("content") or ""
    return ""


def _seen_blob(records):
    """Lowercased concatenation of every tool RESULT (the [:4000]-capped listings from tree/glob/grep
    and read_file). Used ONLY for conservatism: a cited path that appears here — even in a directory
    listing the agent never opened — is treated as grounded, so discovery-without-read is not a phantom."""
    # specs/0077: normalize backslashes to '/' so a PowerShell listing (`src\main.py`) matches a cited path
    # (grounding normalizes citations to '/'). Without this, a file DISCOVERED only via a Windows-style tool
    # listing was falsely flagged a phantom citation and its whole session dropped from the corpus.
    return "\n".join(str(r.get("result") or "") for r in records
                     if r.get("type") == "tool_call").replace("\\", "/").lower()


def curation_verdict(records):
    """(grounded: bool, ungrounded_paths: list[str]). Deterministic phantom-citation check: a cited path
    is ungrounded only if it is NEITHER engaged (grounding.touched_paths) NOR seen in any tool listing."""
    # Subagent trajectories (depth>0) are intermediate, and a grounding VERIFIER (Phase 10 Tier 2) cites
    # paths it asserts are ABSENT by design — mirror the runtime gate's depth-0-only rule (grounding.
    # problems) so a verifier's own captured trajectory is never flagged for doing its job.
    start = next((r for r in records if r.get("type") == "session_start"), {})
    if (start.get("depth") or 0) != 0:
        return True, []
    cited = grounding.cited_paths(_final_answer(records), strict=True)
    if not cited:
        return True, []
    touched = grounding.touched_paths(records)
    blob = _seen_blob(records)

    def has_evidence(p):
        return grounding.grounded_by(p, touched) or p.lower() in blob
    ungrounded = grounding.deterministic_problems(cited, has_evidence)
    return (not ungrounded), ungrounded


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except (OSError, json.JSONDecodeError):
        return []


def main():
    files = [p for p in sorted(glob.glob(TRAJ_GLOB, recursive=True)) if not _is_eval_trajectory(p)]
    grounded, empty, flagged = 0, 0, []
    for path in files:
        records = _load(path)
        if not records:
            empty += 1
            continue
        ok, ungrounded = curation_verdict(records)
        if ok:
            grounded += 1
        else:
            flagged.append((os.path.relpath(path, ROOT).replace(os.sep, "/"), ungrounded))

    print(f"curate | corpus={len(files)} grounded={grounded} flagged={len(flagged)} empty={empty}")
    for rel, paths in flagged:
        print(f"  [UNGROUNDED] {rel}")
        for p in paths:
            print(f"      {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
