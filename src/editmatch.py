"""
src/editmatch.py

Safe fuzzy fallback for edit_file (specs/0013, sub-phase A).

edit_file is exact-match-or-fail by design (tools.py): it forces the model to ground each edit in text
it actually read, and a failed edit is a clean training signal. This module is the fallback that runs
ONLY when the exact match found nothing. It locates old_string with a graded cascade

    exact  ->  whitespace/indentation-insensitive  ->  most-similar contiguous chunk (difflib)

and returns a match ONLY when the location is UNIQUE and (for the fuzziest tier) above a similarity
threshold. Two equally-good spots, or a low score, resolve to AMBIGUOUS / NOT_FOUND -> the caller keeps
edit_file's teaching error. So the "never silently corrupt" invariant holds: it recovers the trivial
whitespace-drift miss, it never guesses.

Pure stdlib (difflib only); imports nothing from tools -> no import cycle. Never raises.
"""
import difflib

MATCH = "match"
NOT_FOUND = "not_found"
AMBIGUOUS = "ambiguous"

# A runner-up window this close to the best is "too close to call" -> refuse rather than guess between
# two near-identical locations. Conservative on purpose (safety over recall).
_TIE_EPSILON = 0.03


class Result:
    """A located match (status == MATCH carries start/end char offsets + the strategy that found it),
    or a refusal (NOT_FOUND / AMBIGUOUS — the caller keeps exact-match's teaching error)."""
    __slots__ = ("status", "start", "end", "strategy")

    def __init__(self, status, start=None, end=None, strategy=None):
        self.status = status
        self.start = start
        self.end = end
        self.strategy = strategy


def resolve(text, old, threshold=0.9):
    """Locate `old` in `text` with a SAFE graded cascade. Returns a Result. A MATCH is returned ONLY
    when the location is unique; ties and low-confidence resolve to AMBIGUOUS / NOT_FOUND (never a
    guess). `threshold` (0..1) is the minimum similarity for the fuzziest tier."""
    if not old or not text:
        return Result(NOT_FOUND)
    # tier 0 — exact (the caller usually tried this already; kept so resolve() is self-contained)
    n = text.count(old)
    if n == 1:
        i = text.find(old)
        return Result(MATCH, i, i + len(old), "exact")
    if n > 1:
        return Result(AMBIGUOUS)
    # tier 1 — whitespace / indentation-insensitive, line-aligned
    r = _whitespace_insensitive(text, old)
    if r.status != NOT_FOUND:
        return r
    # tier 2 — most-similar contiguous chunk; refuse on a near-tie or below threshold
    return _most_similar_chunk(text, old, threshold)


def _line_starts(lines):
    """Char offset of the start of each line (lines kept WITH their newlines), plus a final sentinel =
    total length, so a window of line indices [i, j) spans chars [starts[i], starts[j])."""
    starts = [0]
    for ln in lines:
        starts.append(starts[-1] + len(ln))
    return starts


def _whitespace_insensitive(text, old):
    """Match `old`'s lines against a contiguous window of `text`'s lines ignoring each line's leading/
    trailing whitespace. Returns the ORIGINAL span (original indentation preserved) so the caller
    replaces exactly that region. Unique window -> MATCH; several -> AMBIGUOUS; none -> NOT_FOUND."""
    text_lines = text.splitlines(keepends=True)
    old_norm = [ln.strip() for ln in old.splitlines()]
    k = len(old_norm)
    if k == 0 or k > len(text_lines):
        return Result(NOT_FOUND)
    starts = _line_starts(text_lines)
    hits = [i for i in range(len(text_lines) - k + 1)
            if [w.strip() for w in text_lines[i:i + k]] == old_norm]
    if len(hits) == 1:
        i = hits[0]
        return Result(MATCH, starts[i], starts[i + k], "whitespace")
    if len(hits) > 1:
        return Result(AMBIGUOUS)
    return Result(NOT_FOUND)


def _most_similar_chunk(text, old, threshold):
    """Slide a window the size of `old` (in lines) over `text` and score each by difflib ratio. Return
    the best ONLY if it clears `threshold` AND beats the runner-up by more than _TIE_EPSILON; otherwise
    NOT_FOUND / AMBIGUOUS. Never guesses between two near-identical windows."""
    text_lines = text.splitlines(keepends=True)
    old_lines = old.splitlines(keepends=True)
    k = len(old_lines)
    if k == 0 or k > len(text_lines):
        return Result(NOT_FOUND)
    starts = _line_starts(text_lines)
    scored = [(difflib.SequenceMatcher(None, "".join(text_lines[i:i + k]), old).ratio(), i)
              for i in range(len(text_lines) - k + 1)]
    if not scored:
        return Result(NOT_FOUND)
    scored.sort(key=lambda t: t[0], reverse=True)
    best, best_i = scored[0]
    if best < threshold:
        return Result(NOT_FOUND)
    if len(scored) > 1 and best - scored[1][0] < _TIE_EPSILON:
        return Result(AMBIGUOUS)   # two near-identical spots — refuse rather than guess
    return Result(MATCH, starts[best_i], starts[best_i + k], "similar")
