#!/usr/bin/env python3
"""
summarize_log.py - extract the review-worthy signals from an openagent-code session log
(src/logsetup.py writes `HH:MM:SS LEVEL [name] message`). Bundled with the `review-log` skill
(specs/0008). Prints a bounded digest to stdout; the reviewer confirms each flag against the log.

Usage:  python summarize_log.py <path-to-.log>

Stdlib only, and defensive - a log line it doesn't recognize is ignored, never fatal.
"""
import re
import sys
from collections import Counter

_PREFIX = re.compile(r"^\d\d:\d\d:\d\d\s+\w+\s+\[[^\]]+\]\s+(.*)$")
_STEP = re.compile(r"^step \d+ \[(\w+)\] (\w+)\((.*?)\) ->")   # non-greedy: stop at the FIRST ') ->'
_RESULT_LABEL = re.compile(r"^(result \([^)]*\):|turn \d+ result:)\s*")
# a final answer that OPENS with chain-of-thought (mirrors src/prompts.py's reasoning-leak tells -
# incl. the narrow `according to (the )?guidelines`, NOT a bare `according to`).
_TELL = re.compile(
    r"^\s*(now (we|i|let|the)|we (need|should|can|have|must|will)|let'?s (produce|now|start)|"
    r"let me (produce|summar|now)|according to (the )?guidelines|the user (wants|asked|is asking|also)|"
    r"i (need|should|will|'ll) to|first,? (we|i)|okay,? (so|let|we)|thus\b)", re.I)
_MUTATORS = {"read_file", "edit_file", "write_file", "delete_file"}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # emit UTF-8 even on a Windows pipe
    except (AttributeError, ValueError):
        pass
    if len(sys.argv) < 2:
        print("usage: python summarize_log.py <path-to-.log>")
        return 2
    path = sys.argv[1]
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError as e:
        print(f"cannot read {path}: {e}")
        return 1

    start = None
    prompts = []
    tool_counts = Counter()
    fails = []          # (lineno, name)
    env_touch = []      # (lineno, name)
    challenges = []     # lineno
    compactions = []    # lineno
    retries = 0
    crashes = []        # lineno
    outcomes = []
    leaks = []          # lineno

    for i, raw in enumerate(lines, 1):
        m = _PREFIX.match(raw)
        if not m:
            continue   # a prefix-less CONTINUATION line of a multi-line message (e.g. a logged
                       # final answer that quotes a log) is not its own record - skip it so it
                       # can't fake a tool call / fail / retry.
        msg = m.group(1)
        # Structured step line FIRST: its result snippet can contain 'retrying' / 'compacted' /
        # 'rolling back', which would otherwise hijack it into a wrong loose-substring bucket below.
        s = _STEP.match(msg)
        if s:
            flag, name, argstr = s.group(1), s.group(2), s.group(3)
            tool_counts[(name, flag)] += 1
            if flag == "FAIL":
                fails.append((i, name))
            if ".env" in argstr and name in _MUTATORS:
                env_touch.append((i, name))
            continue
        if msg.startswith(("one-shot start", "REPL start")):
            start = msg
        elif msg.startswith("task:"):
            prompts.append((i, msg[5:].strip()[:160]))
        elif msg.startswith("turn ") and "you>" in msg:
            prompts.append((i, msg.split("you>", 1)[1].strip()[:160]))
        elif msg.startswith(("one-shot end", "REPL end")):
            outcomes.append(msg)
        elif msg.startswith("result (terminated") or re.match(r"turn \d+ result:", msg):
            body = _RESULT_LABEL.sub("", msg)
            if _TELL.match(body):
                leaks.append(i)
        elif "compacted" in msg and "msgs" in msg:
            compactions.append(i)
        elif msg.startswith("completion challenge"):
            challenges.append(i)
        elif "retrying" in msg:
            retries += 1
        elif "rolling back the turn" in msg:
            crashes.append(i)

    out = ["=== SESSION LOG DIGEST ===", start or "(no start line found - is this a session log?)"]
    total = sum(tool_counts.values())
    out.append(f"tool calls: {total} | fails: {len(fails)} | model retries: {retries} | "
               f"compactions: {len(compactions)} | completion-challenges: {len(challenges)}")

    if prompts:
        out.append("\nprompts:")
        out += [f"  L{ln}: {t}" for ln, t in prompts[:12]]
    if tool_counts:
        out.append("\ntool usage:")
        out += [f"  {name} [{flag}] x{n}" for (name, flag), n in tool_counts.most_common()]

    out.append("\n--- REVIEW FLAGS (each is a place to LOOK; confirm against the log line) ---")
    flagged = False
    for ln in leaks:
        out.append(f"  [REASONING-LEAK]  L{ln}: a final answer opens with chain-of-thought"); flagged = True
    for ln, n in env_touch:
        out.append(f"  [.ENV-TOUCH]      L{ln}: {n} on a .env path"); flagged = True
    for ln in challenges:
        out.append(f"  [FALSE-DONE?]     L{ln}: the completion gate challenged a 'done'"); flagged = True
    for ln in crashes:
        out.append(f"  [TURN-CRASH]      L{ln}: a turn was rolled back (error)"); flagged = True
    for name, n in Counter(nm for _, nm in fails).items():
        if n >= 3:
            out.append(f"  [THRASH]          {name} FAILED {n}x - repeated failing calls"); flagged = True
    if not flagged:
        out.append("  (no red-flag patterns detected - still spot-check the outcome + prompts above)")

    if outcomes:
        out.append("\noutcome: " + "; ".join(outcomes))
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
