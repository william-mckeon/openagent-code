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
    # find/fd that DELETE or RUN a command per match — `find` is a read-only verb below, so without this
    # `find . -delete` / `find . -exec rm {} \;` would auto-run as "read-only" and defeat a rm deny rule.
    r"\bfind\b[^|;&]*\s-(?:delete|exec|execdir|ok|okdir|fls|fprintf?)\b",
    r"\bfd\b[^|;&]*\s(?:-x|-X|--exec(?:-batch)?)\b",
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


# specs/0081: an interpreter WRAPPER whose real command hides in a -Command / -c / /c argument (or a
# -EncodedCommand base64). Without lowering, `powershell -Command "rm -rf x"` classified on the wrapper token
# `powershell` and the inner `rm -rf x` was invisible to deny/ask rules and the dangerous-pattern check.
_WRAP_INNER = re.compile(
    r"(?i)^\s*(?:[\w.:/\\-]*[/\\])?(?:powershell|pwsh|bash|sh|zsh|dash|cmd)(?:\.exe)?\b"
    r".*?\s(?:-c|-lc|-command|/c)\b\s+(.*)$", re.S)
_WRAP_ENC = re.compile(
    r"(?i)^\s*(?:[\w.:/\\-]*[/\\])?(?:powershell|pwsh)(?:\.exe)?\b.*?\s-e(?:nc|ncodedcommand)?\b\s+([A-Za-z0-9+/=]{8,})",
    re.S)


def _interpreter_inner(seg):
    """specs/0081: if `seg` is an interpreter WRAPPER (powershell -Command "…", bash -c "…", cmd /c …, or
    powershell -EncodedCommand <b64>), return the INNER command string so it is decomposed and assessed too;
    else None. Closes wrapper-smuggling — a dangerous inner command hidden behind a benign wrapper token.
    Never raises."""
    try:
        m = _WRAP_ENC.match(seg)
        if m:
            import base64
            return base64.b64decode(m.group(1) + "=" * (-len(m.group(1)) % 4)).decode("utf-16-le", "replace")
        m = _WRAP_INNER.match(seg)
        if m:
            inner = m.group(1).strip()
            if len(inner) >= 2 and inner[0] in "\"'" and inner[-1] == inner[0]:
                inner = inner[1:-1]          # peel one layer of surrounding quotes
            return inner or None
    except Exception:  # noqa: BLE001 - lowering must never crash the gate
        return None
    return None


def _collect(command, shell, out):
    subs, stripped = _extract_substitutions(command)
    for seg in _split_top_level(stripped, shell):
        seg = seg.strip()
        if seg:
            out.append(seg)
            inner = _interpreter_inner(seg)   # specs/0081: lower the wrapper's INNER command and assess it too
            if inner and inner != seg:
                _collect(inner, shell, out)
    for sub in subs:
        _collect(sub, shell, out)   # a substitution's contents are commands too


def _first_token(seg):
    m = re.match(r"[^\s]+", seg)
    return m.group(0) if m else ""


# > >> 2> 2>> &> >& (an output redirect = a WRITE). NO leading-whitespace requirement: `echo x>f` (no
# space) is a real redirect that a `(?:^|\s)` anchor missed, so it was mis-read as read-only AND slipped
# the sandbox fence. The negative lookbehind keeps `->` / `=>` / `<>` (arrows, not redirects) out; fd
# dups (`2>&1`, `>&2`) are still discarded by _redirect_target's target filter.
_REDIRECT = re.compile(r"(?<![-=<>])(?:\d*>>?|&>|>&)")
_VERSION_FLAGS = {"-v", "-version", "--version", "version"}


def _mask_quotes(s):
    """Replace quoted spans (and the quotes) with spaces, preserving length — so an operator INSIDE
    quotes (`echo 'a > b'`) isn't mistaken for a real one."""
    out, sq, dq = [], False, False
    for c in s:
        if sq:
            out.append(" "); sq = c != "'"
        elif dq:
            out.append(" "); dq = c != '"'
        elif c == "'":
            sq = True; out.append(" ")
        elif c == '"':
            dq = True; out.append(" ")
        else:
            out.append(c)
    return "".join(out)


def _redirect_target(seg):
    """If `seg` has a top-level output redirect to a FILE, return its target token ('' if none visible);
    else None. Quote-aware, and ignores fd duplications (`2>&1`, `>&2`) which don't write a file."""
    m = _REDIRECT.search(_mask_quotes(seg))
    if not m:
        return None
    target = _first_token(seg[m.end():].strip())
    if target.startswith("&") or target.isdigit():   # fd dup (2>&1 / >&2), not a file write
        return None
    return target


def _redirect_is_dangerous(target):
    """A redirect whose target is absolute / parent-escaping / a device or system path is dangerous; a
    plain workspace-relative file is merely mutating."""
    t = (target or "").strip().strip("'\"").replace("\\", "/")
    if not t:
        return False
    if t.startswith(("/dev/", "/etc/", "/proc/", "/sys/")):
        return True
    if t.startswith("/") or re.match(r"^[A-Za-z]:", t):    # absolute (posix or windows drive)
        return True
    return ".." in t.split("/")                            # parent escape


_SORT_O = re.compile(r"(?:^|\s)-o\s*(\S+)|(?:^|\s)--output(?:[= ])(\S+)")   # sort -o FILE / --output FILE
_YQ_INPLACE = re.compile(r"(?:^|\s)(?:-i|--inplace)\b")                      # yq -i (edits the input file)


def _flag_write_class(tok, seg):
    """A read-only VERB that WRITES via a flag (not a redirect), so it must not be read-only-relaxed:
    `sort -o FILE` writes/truncates FILE; `yq -i` edits its input in place. Returns DANGEROUS / MUTATING,
    or None when no such flag is present. Other read-only verbs write only via redirects (already caught)."""
    if tok == "sort":
        m = _SORT_O.search(seg)
        if m:
            dest = m.group(1) or m.group(2) or ""
            return DANGEROUS if _redirect_is_dangerous(dest) else MUTATING
    if tok == "yq" and _YQ_INPLACE.search(seg):
        return MUTATING
    return None


def classify(segment, shell="bash"):
    """Classify ONE segment: read_only / mutating / dangerous. Unknown -> mutating (conservative)."""
    seg = _PREFIX.sub("", segment or "").strip()
    if not seg:
        return READ_ONLY
    for rx in _DANGEROUS_PATTERNS:
        if rx.search(seg):
            return DANGEROUS
    # An output redirect makes ANY command a WRITE, even a read-only verb (`git ls-files > x.txt` was
    # auto-run as "read-only" and silently created a file). To a workspace file -> mutating; to an
    # absolute / parent-escaping / device path -> dangerous.
    rt = _redirect_target(seg)
    if rt is not None:
        return DANGEROUS if _redirect_is_dangerous(rt) else MUTATING
    # A bare version check (`node -v`, `python --version`) can't mutate (no target).
    parts = seg.split()
    if len(parts) == 2 and parts[1].lower() in _VERSION_FLAGS:
        return READ_ONLY
    tok = _first_token(seg).lower().strip("'\"")
    tok = re.split(r"[\\/]", tok)[-1]           # basename of the command path
    if tok.endswith(".exe"):
        tok = tok[:-4]
    fw = _flag_write_class(tok, seg)            # a read-only verb that writes via a flag (sort -o, yq -i)
    if fw is not None:
        return fw
    if tok in ("%", "foreach-object", "foreach"):
        # ForEach-Object projecting a property (`% Count`) is read-only; a script block (`% { ... }`)
        # can run anything -> stay conservative.
        return MUTATING if "{" in seg else READ_ONLY
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
