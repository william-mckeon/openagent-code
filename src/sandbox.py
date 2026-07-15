"""
src/sandbox.py

Phase 17 (specs/0017) — FS confinement for run_command. The file-tool fence already confines
write_file/edit_file/delete_file to the workspace (cwd + CODE_ADD_DIRS); this extends the SAME fence to
run_command's writes, so a command can't shell out to `echo x > /etc/passwd`, `cp secret ../out`, or
`dd of=/dev/sda` even under an allow rule or bypass.

It parses a command's WRITE TARGETS — output redirects (`> >> 2> &>`) and the destination of common write
commands (cp/mv/install/tee/dd, PowerShell Out-File/Set-Content/Copy-Item/…) — via execpolicy's segment
parse (0016), and reports any that resolve OUTSIDE the roots. tools.py refuses those. v1 fences the
SHELL-level writes; a program that writes outside via its OWN logic needs an OS jail (a follow-up on this
seam). Pure, dep-free, NEVER raises (a command it can't parse -> no reported escape; the mode gate +
execpolicy still apply). Off by default -> run_command is byte-identical to today.
"""
import os
import shlex

from . import execpolicy

# Write commands whose DESTINATION is a filesystem path we can fence.
_WRITE_LAST_ARG = {"cp", "mv", "install", "copy", "move", "rsync"}   # dest is the last path arg
_WRITE_ALL_ARGS = {"tee"}                                            # writes each file arg
_PS_WRITE = {"out-file", "set-content", "add-content", "export-csv", "export-clixml",
             "copy-item", "move-item", "new-item"}


def _toks(seg, shell):
    try:
        return seg.split() if shell == "powershell" else shlex.split(seg, posix=True)
    except ValueError:
        return seg.split()


def _command_write_dests(seg, shell):
    toks = _toks(seg, shell)
    if not toks:
        return []
    cmd = toks[0].lower().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if cmd.endswith(".exe"):
        cmd = cmd[:-4]
    args = [t for t in toks[1:] if not t.startswith("-")]   # drop flags
    if cmd in _WRITE_LAST_ARG and len(args) >= 2:
        return [args[-1]]
    if cmd in _WRITE_ALL_ARGS:
        return args
    if cmd == "dd":
        return [t[3:] for t in toks if t.lower().startswith("of=")]
    if cmd == "sort":                                   # sort -o FILE / --output FILE writes FILE
        for i, t in enumerate(toks):
            if t in ("-o", "--output") and i + 1 < len(toks):
                return [toks[i + 1]]
            if t.startswith("-o") and len(t) > 2:       # -oFILE (attached)
                return [t[2:]]
            if t.startswith("--output="):
                return [t.split("=", 1)[1]]
        return []
    if cmd == "yq" and any(t in ("-i", "--inplace") for t in toks) and args:
        return [args[-1]]                               # in-place edit: writes its file (the last positional)
    if cmd in _PS_WRITE:
        for i, t in enumerate(toks):                        # -Path / -FilePath / -Destination X
            if t.lower() in ("-path", "-filepath", "-literalpath", "-destination") and i + 1 < len(toks):
                return [toks[i + 1]]
        return args[:1]
    return []


def write_targets(command, shell="bash"):
    """The paths a command WRITES to: output redirects + the destinations of common write commands. Best
    effort, and it never raises — an unparseable line just yields no targets (the mode gate still runs)."""
    targets = []
    try:
        for seg in execpolicy.split_segments(command, shell):
            rt = execpolicy._redirect_target(seg)
            if rt:
                targets.append(rt)
            targets.extend(_command_write_dests(seg, shell))
    except Exception:  # noqa: BLE001 - parsing must never crash run_command
        return []
    return [t for t in targets if t and not t.startswith("&")]


def _within(abs_path, real_roots):
    return any(abs_path == r or abs_path.startswith(r + os.sep) for r in real_roots)


def escapes(command, cwd, roots, shell="bash"):
    """The write targets in `command` that resolve OUTSIDE `roots` (cwd + CODE_ADD_DIRS). [] == confined.
    Absolute, parent-escaping (`../..`), and device paths land outside; a workspace-relative path is in."""
    real_roots = [os.path.realpath(r) for r in (roots or []) if r]
    if not real_roots:
        return []
    out = []
    for t in write_targets(command, shell):
        tt = t.strip().strip("'\"").replace("\\", "/") if os.name != "nt" else t.strip().strip("'\"")
        if not tt:
            continue
        ap = tt if os.path.isabs(tt) else os.path.join(cwd, tt)
        ap = os.path.realpath(os.path.normpath(ap))
        if not _within(ap, real_roots):
            out.append(t)
    return out
