"""
src/execpolicy.py

Phase 16 (specs/0016) — parse a run_command line into SEGMENTS and CLASSIFY each as read_only /
mutating / dangerous, so the permission gate reasons about what a command actually DOES instead of
matching a raw prefix. A prefix matcher sees only the first token: `cd src && rm -rf x` reads as "cd",
so a `deny run_command(rm:*)` rule never fires. execpolicy splits on the shell operators
(`&& || ; |` and newlines), unwraps `$(...)` / backtick substitutions, and judges each piece.

Shell-aware: bash and Windows PowerShell 5.1 (where `&&` / `||` are NOT valid operators — the model
emits them anyway). PURE + dependency-free, and it NEVER raises: a line it can't parse degrades to one
opaque `mutating` segment (the conservative default). Caller-agnostic — permissions.py consults
assess(); nothing here imports config or the agent.
"""
import re
from collections import namedtuple

READ_ONLY, MUTATING, DANGEROUS = "read_only", "mutating", "dangerous"
_RANK = {READ_ONLY: 0, MUTATING: 1, DANGEROUS: 2}

Assessment = namedtuple("Assessment", "worst segments flagged ps_invalid")
# worst: the highest-rank class across segments · segments: [(segment, class)] ·
# flagged: [segment] classified dangerous · ps_invalid: a `&&`/`||` used in a PowerShell command.

# -- command tables (data, so a new command is a one-line edit, not new logic) -----------------------
_READ_ONLY_CMDS = {
    "ls", "dir", "cat", "type", "bat", "head", "tail", "less", "more", "grep", "egrep", "fgrep", "rg",
    "ripgrep", "ag", "find", "fd", "echo", "printf", "pwd", "cd", "which", "where", "whereis", "whoami",
    "id", "hostname", "date", "cal", "wc", "sort", "uniq", "cut", "tr", "comm", "cmp", "diff", "file",
    "stat", "du", "df", "env", "printenv", "tree", "basename", "dirname", "realpath", "readlink", "test",
    "true", "false", "sleep", "seq", "jq", "yq", "xxd", "od", "hexdump", "md5sum", "sha1sum", "sha256sum",
    "uname", "arch", "nproc", "free", "uptime", "ps", "top", "man", "help", "history", "clear", "tty",
    "get-childitem", "get-content", "get-location", "get-item", "get-process", "get-command", "get-help",
    "get-date", "test-path", "resolve-path", "split-path", "join-path", "select-string", "measure-object",
    "select-object", "where-object", "sort-object", "format-list", "format-table", "out-string",
    "write-host", "write-output",
}
# Multi-verb tools: classify by the SUBcommand (else conservative mutating). Dangerous variants (git push
# --force, git reset --hard) are caught by the pattern list below, which runs first.
_SUBCMD_READONLY = {
    "git": {"status", "log", "diff", "show", "branch", "remote", "config", "ls-files", "rev-parse",
            "describe", "blame", "tag", "cat-file", "reflog", "shortlog", "name-rev", "grep", "whatchanged"},
    "npm": {"ls", "list", "view", "outdated", "audit", "-v", "--version"},
    "pip": {"show", "list", "freeze", "-v", "--version", "check"},
    "pip3": {"show", "list", "freeze", "-v", "--version", "check"},
    "go": {"version", "env", "list", "doc", "vet"},
    "cargo": {"--version", "tree"},
    "docker": {"ps", "images", "logs", "version", "info", "inspect"},
    "kubectl": {"get", "describe", "logs", "version"},
    "poetry": {"show", "version"},
}
_DANGEROUS_PATTERNS = [re.compile(p, re.I) for p in (
    r"\brm\b[^|;&]*\s-[a-z]*r",                 # rm -r / -rf / -fr
    r"\brm\b\s+-[a-z]*f[a-z]*\s+/",             # rm -f /
    r"\brmdir\b\s+/s", r"\bdel\b\s+/[sq]", r"\brd\b\s+/s",   # windows recursive delete
    r"\bdd\b\s+if=", r"\bmkfs", r"\bshred\b", r"\bfdisk\b", r"\bparted\b",
    r"\b(shutdown|reboot|halt|poweroff)\b", r"\binit\b\s+[06]\b",
    r">\s*/dev/sd", r">\s*/dev/null\s+2>&1\s*$",   # (the /dev/sd one is the real danger)
    r"\bchmod\b\s+(-[a-z]*r|[^|;&]*777)", r"\bchown\b\s+-[a-z]*r",
    r"\bgit\b[^|;&]*\bpush\b[^|;&]*(--force|-f)\b",
    r"\bgit\b[^|;&]*\breset\b[^|;&]*--hard", r"\bgit\b[^|;&]*\bclean\b[^|;&]*-[a-z]*f",
    r"^\s*(sudo\s+)?(sh|bash|zsh|dash)\b\s*$",  # a bare shell (reading piped/downloaded input)
    r"\b(iex|invoke-expression)\b",
    r"\bremove-item\b[^|;&]*-(recurse|force)", r"\b(format-volume|clear-disk|remove-partition)\b",
    r":\s*\(\s*\)\s*\{",                        # fork bomb :(){
    r"\bnpm\b\s+publish\b", r"\bkill(all)?\b\s+-9", r"\bpkill\b\s+-9",
)]
# a download piped into a shell — checked on the RAW line (survives segment splitting)
_PIPE_TO_SHELL = re.compile(r"\b(curl|wget|iwr|invoke-webrequest)\b[^\n]*\|\s*(sudo\s+)?(sh|bash|zsh|iex)\b", re.I)
_PS_ANDOR = re.compile(r"(?<!&)&&(?!&)|\|\|")   # && or || (invalid operators in PowerShell 5.1)
# leading noise to strip before the command token: env-assignments, sudo/command/nohup/time/exec, `\`
_PREFIX = re.compile(r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+|sudo\s+|command\s+|nohup\s+|time\s+|exec\s+|\\\s*)+")


def _extract_substitutions(s):
    """Pull `$(...)` and backtick contents out as separate command strings (their contents execute too),
    leaving a placeholder. Substitutions inside SINGLE quotes are literal, so they're left alone."""
    subs, out, i, n, sq = [], [], 0, len(s), False
    while i < n:
        c = s[i]
        if sq:
            out.append(c)
            if c == "'":
                sq = False
            i += 1
        elif c == "'":
            sq = True; out.append(c); i += 1
        elif s[i:i + 2] == "$(":
            depth, j = 1, i + 2
            while j < n and depth:
                depth += (s[j] == "(") - (s[j] == ")")
                j += 1
            subs.append(s[i + 2:j - 1]); out.append(" "); i = j
        elif c == "`":
            j = s.find("`", i + 1)
            j = n if j == -1 else j
            subs.append(s[i + 1:j]); out.append(" "); i = j + 1
        else:
            out.append(c); i += 1
    return subs, "".join(out)


def _split_top_level(s, shell):
    """Split at top-level operators (&& || ; | newline, and bash '&'), respecting quotes and parens."""
    parts, buf, i, n = [], [], 0, len(s)
    sq = dq = False
    depth = 0
    while i < n:
        c = s[i]
        if sq:
            buf.append(c); sq = c != "'"; i += 1; continue
        if dq:
            buf.append(c); dq = c != '"'; i += 1; continue
        if c == "'":
            sq = True; buf.append(c); i += 1; continue
        if c == '"':
            dq = True; buf.append(c); i += 1; continue
        if c == "(":
            depth += 1; buf.append(c); i += 1; continue
        if c == ")":
            depth = max(0, depth - 1); buf.append(c); i += 1; continue
        if depth == 0:
            if s[i:i + 2] in ("&&", "||"):
                parts.append("".join(buf)); buf = []; i += 2; continue
            if c in (";", "|", "\n"):
                parts.append("".join(buf)); buf = []; i += 1; continue
            if c == "&" and shell != "powershell":
                parts.append("".join(buf)); buf = []; i += 1; continue
        buf.append(c); i += 1
    parts.append("".join(buf))
    return parts


def split_segments(command, shell="bash"):
    """A flat list of the executable segments in `command` (operators split them; substitutions add
    their own). Never raises — the worst case is returning the whole line as one segment."""
    out = []
    try:
        _collect(command or "", shell, out)
    except Exception:  # noqa: BLE001 - parsing must never crash the gate
        out = [(command or "").strip()]
    return [s for s in out if s]


def _collect(command, shell, out):
    subs, stripped = _extract_substitutions(command)
    for seg in _split_top_level(stripped, shell):
        seg = seg.strip()
        if seg:
            out.append(seg)
    for sub in subs:
        _collect(sub, shell, out)   # a substitution's contents are commands too


def _first_token(seg):
    m = re.match(r"[^\s]+", seg)
    return m.group(0) if m else ""


def classify(segment, shell="bash"):
    """Classify ONE segment: read_only / mutating / dangerous. Unknown -> mutating (conservative)."""
    seg = _PREFIX.sub("", segment or "").strip()
    if not seg:
        return READ_ONLY
    for rx in _DANGEROUS_PATTERNS:
        if rx.search(seg):
            return DANGEROUS
    tok = _first_token(seg).lower().strip("'\"")
    tok = re.split(r"[\\/]", tok)[-1]           # basename of the command path
    if tok.endswith(".exe"):
        tok = tok[:-4]
    if tok in _SUBCMD_READONLY:
        sub = _first_token(seg[len(_first_token(seg)):].strip()).lower()
        return READ_ONLY if sub in _SUBCMD_READONLY[tok] else MUTATING
    if tok in _READ_ONLY_CMDS:
        return READ_ONLY
    return MUTATING


def assess(command, shell="bash"):
    """Parse + classify a whole command line. Returns an Assessment (see the namedtuple)."""
    segs = split_segments(command, shell)
    classed = [(s, classify(s, shell)) for s in segs] or [(command or "", READ_ONLY)]
    if _PIPE_TO_SHELL.search(command or ""):     # curl ... | sh  (danger survives the split)
        classed.append((command, DANGEROUS))
    worst = max((c for _, c in classed), key=lambda c: _RANK[c], default=READ_ONLY)
    flagged = [s for s, c in classed if c == DANGEROUS]
    ps_invalid = shell == "powershell" and bool(_PS_ANDOR.search(command or ""))
    return Assessment(worst, classed, flagged, ps_invalid)
