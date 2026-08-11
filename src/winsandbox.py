"""
src/winsandbox.py

specs/0083: the one OS-LEVEL / KERNEL boundary in OAC (everything else is in-process or advisory). On Windows,
spawn a run_command child under a RESTRICTED TOKEN inside a JOB OBJECT:

- Restricted token (#2) — `CreateRestrictedToken(DISABLE_MAX_PRIVILEGE | LUA_TOKEN [| WRITE_RESTRICTED])`
  derives a *lesser* version of OUR OWN token: every privilege dropped and powerful groups (Administrators)
  disabled/deny-only. Because it is a restricted derivative of the caller's token (not a different user),
  `CreateProcessAsUserW` can assign it WITHOUT `SE_ASSIGNPRIMARYTOKEN` — the standard Chromium-sandbox technique.
  A prompt-injected command that reaches the shell now runs with markedly less authority than the agent.
- Job object (#14) — `CreateJobObject` + `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` (+ optional per-process memory /
  active-process caps). When we close the job handle (or die), the whole child TREE is killed — no orphaned
  runaway process escapes the turn — and a fork-bomb / memory-bomb is capped.

FAIL-CLOSED is the invariant. `available()` does a REAL probe spawn (cached) and only reports True if a
restricted-token child actually ran — it never *claims* confinement it can't deliver. `run_shell()` raises
`SandboxUnavailable` rather than silently running unconfined; the caller (tools.run_command) decides whether an
unavailable sandbox means REFUSE (CODE_SANDBOX_REQUIRED) or fall back with a logged warning.

Everything degrades on a non-Windows host / a locked-down interpreter: `available()` is False and the feature is
simply off. The pure helpers (token/job flag construction) are import-safe and unit-tested without spawning.
"""
import os
import threading

from . import config, logsetup

log = logsetup.get_logger("winsandbox")


class SandboxUnavailable(RuntimeError):
    """The OS sandbox can't be established on this host (non-Windows, no ctypes, or the probe spawn failed)."""


# -- CreateRestrictedToken flags -------------------------------------------------------------------------------
DISABLE_MAX_PRIVILEGE = 0x1
SANDBOX_INERT = 0x2
LUA_TOKEN = 0x4
WRITE_RESTRICTED = 0x8

# -- Job-object limit flags ------------------------------------------------------------------------------------
JOB_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JobObjectExtendedLimitInformation = 9

# -- CreateProcess flags ---------------------------------------------------------------------------------------
_CREATE_SUSPENDED = 0x00000004
_CREATE_NO_WINDOW = 0x08000000
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_STARTF_USESTDHANDLES = 0x00000100
_HANDLE_FLAG_INHERIT = 0x00000001
_WAIT_TIMEOUT = 0x00000102


def restricted_token_flags(write_restricted=None):
    """The CreateRestrictedToken flag word (pure; unit-tested). DISABLE_MAX_PRIVILEGE drops every privilege from
    the child; LUA_TOKEN makes it a filtered (non-elevated) token. WRITE_RESTRICTED (opt-in, stricter) adds
    write-SID checking — off by default because it needs the workspace granted to a restricting SID or the
    child's writes fail wholesale."""
    wr = config.SANDBOX_WRITE_RESTRICTED if write_restricted is None else write_restricted
    flags = DISABLE_MAX_PRIVILEGE | LUA_TOKEN
    if wr:
        flags |= WRITE_RESTRICTED
    return flags


def job_limit_flags(mem_mb=None, max_procs=None):
    """The JOBOBJECT limit-flag word (pure; unit-tested). Always KILL_ON_JOB_CLOSE (the point of the job);
    PROCESS_MEMORY / ACTIVE_PROCESS added only when a positive cap is configured."""
    mem_mb = config.SANDBOX_JOB_MEM_MB if mem_mb is None else mem_mb
    max_procs = config.SANDBOX_JOB_MAX_PROCS if max_procs is None else max_procs
    flags = JOB_LIMIT_KILL_ON_JOB_CLOSE
    if mem_mb and mem_mb > 0:
        flags |= JOB_LIMIT_PROCESS_MEMORY
    if max_procs and max_procs > 0:
        flags |= JOB_LIMIT_ACTIVE_PROCESS
    return flags


_K = None   # cached, argtypes-configured kernel32 / advapi32 (must be cached: argtypes live on the wrapper)
_A = None


def _configure(ctypes, wintypes, k, a):
    """Set argtypes/restype on every Win32 function we call. ESSENTIAL on 64-bit: without it ctypes infers
    c_int for pointer/HANDLE args and truncates (or overflows on the -1 pseudo-handle) — a silent
    wrong-handle bug in security-critical code."""
    H, D, BOOL, LPW, LPCW = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.LPWSTR, wintypes.LPCWSTR
    PH, VP, PD = ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)
    k.GetCurrentProcess.restype = H; k.GetCurrentProcess.argtypes = []
    k.CreatePipe.restype = BOOL; k.CreatePipe.argtypes = [PH, PH, VP, D]
    k.SetHandleInformation.restype = BOOL; k.SetHandleInformation.argtypes = [H, D, D]
    k.CreateFileW.restype = H; k.CreateFileW.argtypes = [LPCW, D, D, VP, D, D, H]
    k.CreateJobObjectW.restype = H; k.CreateJobObjectW.argtypes = [VP, LPCW]
    k.SetInformationJobObject.restype = BOOL; k.SetInformationJobObject.argtypes = [H, ctypes.c_int, VP, D]
    k.AssignProcessToJobObject.restype = BOOL; k.AssignProcessToJobObject.argtypes = [H, H]
    k.ResumeThread.restype = D; k.ResumeThread.argtypes = [H]
    k.WaitForSingleObject.restype = D; k.WaitForSingleObject.argtypes = [H, D]
    k.GetExitCodeProcess.restype = BOOL; k.GetExitCodeProcess.argtypes = [H, PD]
    k.TerminateProcess.restype = BOOL; k.TerminateProcess.argtypes = [H, wintypes.UINT]
    k.CloseHandle.restype = BOOL; k.CloseHandle.argtypes = [H]
    a.OpenProcessToken.restype = BOOL; a.OpenProcessToken.argtypes = [H, D, PH]
    a.CreateRestrictedToken.restype = BOOL
    a.CreateRestrictedToken.argtypes = [H, D, D, VP, D, VP, D, VP, PH]
    a.CreateProcessAsUserW.restype = BOOL
    a.CreateProcessAsUserW.argtypes = [H, LPCW, LPW, VP, VP, BOOL, D, VP, LPCW, VP, VP]


def _win():
    """(ctypes, wintypes, kernel32, advapi32) or raise SandboxUnavailable off Windows / when ctypes can't load.
    The DLL wrappers are cached and argtypes-configured exactly once."""
    global _K, _A
    if os.name != "nt":
        raise SandboxUnavailable("OS sandbox is Windows-only")
    try:
        import ctypes
        from ctypes import wintypes
        if _K is None or _A is None:
            _K = ctypes.WinDLL("kernel32", use_last_error=True)
            _A = ctypes.WinDLL("advapi32", use_last_error=True)
            _configure(ctypes, wintypes, _K, _A)
        return ctypes, wintypes, _K, _A
    except SandboxUnavailable:
        raise
    except Exception as e:  # noqa: BLE001 - a broken/partial ctypes -> sandbox unavailable, not a crash
        raise SandboxUnavailable(f"ctypes unavailable: {type(e).__name__}: {e}")


def _env_block(ctypes, env):
    """A CREATE_UNICODE_ENVIRONMENT block (KEY=VAL\\0...\\0) for `env` (a dict), or None to inherit."""
    if env is None:
        return None
    items = [f"{k}={v}" for k, v in env.items() if k]
    buf = ("\0".join(items) + "\0\0")
    return ctypes.create_unicode_buffer(buf)


def _structs(ctypes, wintypes):
    SIZE_T = ctypes.c_size_t

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                    ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                    ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                    ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", wintypes.BOOL)]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", SIZE_T),
                    ("MaximumWorkingSetSize", SIZE_T), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in
                    ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                     "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", SIZE_T), ("JobMemoryLimit", SIZE_T),
                    ("PeakProcessMemoryUsed", SIZE_T), ("PeakJobMemoryUsed", SIZE_T)]

    return (STARTUPINFOW, PROCESS_INFORMATION, SECURITY_ATTRIBUTES,
            JOBOBJECT_EXTENDED_LIMIT_INFORMATION)


def _make_job(ctypes, wintypes, kernel32, EXT):
    """A job object with KILL_ON_JOB_CLOSE (+ configured caps), or None on failure. Handle owned by caller."""
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = EXT()
    info.BasicLimitInformation.LimitFlags = job_limit_flags()
    if config.SANDBOX_JOB_MEM_MB and config.SANDBOX_JOB_MEM_MB > 0:
        info.ProcessMemoryLimit = int(config.SANDBOX_JOB_MEM_MB) * 1024 * 1024
    if config.SANDBOX_JOB_MAX_PROCS and config.SANDBOX_JOB_MAX_PROCS > 0:
        info.BasicLimitInformation.ActiveProcessLimit = int(config.SANDBOX_JOB_MAX_PROCS)
    ok = kernel32.SetInformationJobObject(job, _JobObjectExtendedLimitInformation,
                                          ctypes.byref(info), ctypes.sizeof(info))
    if not ok:
        kernel32.CloseHandle(job)
        return None
    return job


def _restricted_token(ctypes, wintypes, advapi32, kernel32):
    """A restricted PRIMARY token derived from the current process token, or raise SandboxUnavailable."""
    TOKEN_ACCESS = 0x2 | 0x8 | 0x1 | 0x80  # DUPLICATE|QUERY|ASSIGN_PRIMARY|ADJUST_DEFAULT
    hproc = kernel32.GetCurrentProcess()
    htok = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(hproc, TOKEN_ACCESS, ctypes.byref(htok)):
        raise SandboxUnavailable(f"OpenProcessToken failed ({ctypes.get_last_error()})")
    try:
        restricted = wintypes.HANDLE()
        ok = advapi32.CreateRestrictedToken(htok, restricted_token_flags(), 0, None, 0, None, 0, None,
                                            ctypes.byref(restricted))
        if not ok:
            raise SandboxUnavailable(f"CreateRestrictedToken failed ({ctypes.get_last_error()})")
        return restricted
    finally:
        kernel32.CloseHandle(htok)


def _spawn(argv, cwd, env, timeout, stdin_devnull):
    """Spawn `argv` under a restricted token + job object, capture MERGED stdout+stderr, enforce `timeout`.
    Returns (returncode, output_text, timed_out). Raises SandboxUnavailable if any sandbox primitive fails
    (fail-closed: we never fall through to an unconfined spawn here)."""
    import subprocess as _sp
    ctypes, wintypes, kernel32, advapi32 = _win()
    STARTUPINFOW, PROCESS_INFORMATION, SECURITY_ATTRIBUTES, EXT = _structs(ctypes, wintypes)

    token = _restricted_token(ctypes, wintypes, advapi32, kernel32)
    job = _make_job(ctypes, wintypes, kernel32, EXT)
    if not job:
        kernel32.CloseHandle(token)
        raise SandboxUnavailable(f"CreateJobObject/SetInformationJobObject failed ({ctypes.get_last_error()})")

    sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, True)  # inheritable handles
    rd = wintypes.HANDLE()
    wr = wintypes.HANDLE()
    if not kernel32.CreatePipe(ctypes.byref(rd), ctypes.byref(wr), ctypes.byref(sa), 0):
        kernel32.CloseHandle(token); kernel32.CloseHandle(job)
        raise SandboxUnavailable("CreatePipe failed")
    kernel32.SetHandleInformation(rd, _HANDLE_FLAG_INHERIT, 0)  # parent read end must NOT be inherited

    # stdin: the NUL device (inheritable) so the child never blocks on input it will never receive.
    GENERIC_READ, OPEN_EXISTING, FILE_SHARE = 0x80000000, 3, 0x3
    hnul = kernel32.CreateFileW("NUL", GENERIC_READ, FILE_SHARE, ctypes.byref(sa), OPEN_EXISTING, 0, None)

    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(STARTUPINFOW)
    si.dwFlags = _STARTF_USESTDHANDLES
    si.hStdInput = hnul
    si.hStdOutput = wr
    si.hStdError = wr
    pi = PROCESS_INFORMATION()

    cmdline = ctypes.create_unicode_buffer(_sp.list2cmdline(argv))
    env_buf = _env_block(ctypes, env)
    flags = _CREATE_SUSPENDED | _CREATE_NO_WINDOW | (_CREATE_UNICODE_ENVIRONMENT if env_buf else 0)

    ok = advapi32.CreateProcessAsUserW(token, None, cmdline, None, None, True, flags,
                                       env_buf, cwd, ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        err = ctypes.get_last_error()
        for h in (wr, rd, hnul, token, job):
            kernel32.CloseHandle(h)
        raise SandboxUnavailable(f"CreateProcessAsUserW failed ({err})")

    # Confine BEFORE the first instruction runs: assign to the job while still suspended, then resume.
    kernel32.AssignProcessToJobObject(job, pi.hProcess)
    kernel32.ResumeThread(pi.hThread)
    kernel32.CloseHandle(wr)   # parent drops the write end so the reader sees EOF at child exit
    kernel32.CloseHandle(hnul)

    import msvcrt
    fd = msvcrt.open_osfhandle(rd.value, os.O_RDONLY)
    reader_out = {}

    def _drain():
        try:
            with os.fdopen(fd, "rb", closefd=True) as f:
                reader_out["data"] = f.read()
        except Exception:  # noqa: BLE001
            reader_out["data"] = b""

    t = threading.Thread(target=_drain, daemon=True)
    t.start()

    timed_out = False
    ms = 0xFFFFFFFF if timeout is None else int(timeout * 1000)
    if kernel32.WaitForSingleObject(pi.hProcess, ms) == _WAIT_TIMEOUT:
        timed_out = True
        kernel32.TerminateProcess(pi.hProcess, 1)

    rc = wintypes.DWORD(0)
    kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(rc))
    t.join(timeout=5)
    out = (reader_out.get("data") or b"").decode("utf-8", "replace")

    kernel32.CloseHandle(pi.hThread)
    kernel32.CloseHandle(pi.hProcess)
    kernel32.CloseHandle(token)
    kernel32.CloseHandle(job)   # KILL_ON_JOB_CLOSE reaps any surviving descendant
    return (1 if timed_out else int(rc.value)), out, timed_out


_AVAIL = None   # cached probe result; the harness may set this to force a value


def available():
    """True iff a restricted-token child ACTUALLY ran on this host (a real, cached probe spawn). False off
    Windows, without ctypes, or when the spawn lacks the needed privilege — so the caller can fail closed. Never
    raises."""
    global _AVAIL
    if _AVAIL is not None:
        return _AVAIL
    try:
        rc, _out, timed = _spawn(["cmd", "/c", "exit 0"], None, None, 10, True)
        _AVAIL = (rc == 0 and not timed)
    except Exception as e:  # noqa: BLE001
        log.info("winsandbox unavailable: %s", e)
        _AVAIL = False
    return _AVAIL


def run_shell(argv, cwd, env, timeout, stdin_devnull=True):
    """Run `argv` in the OS sandbox, returning (returncode, merged_output, timed_out). Raises SandboxUnavailable
    if the sandbox isn't available on this host — the caller decides refuse-vs-fallback. Output is stdout+stderr
    MERGED (a single pipe keeps the ctypes spawn simple and robust); the caller presents it as command output."""
    if not available():
        raise SandboxUnavailable("OS sandbox not available on this host")
    return _spawn(argv, cwd, env, timeout, stdin_devnull)
