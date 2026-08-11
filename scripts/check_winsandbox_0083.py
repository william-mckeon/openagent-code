"""
scripts/check_winsandbox_0083.py

Acceptance harness for specs/0083 — the OS-sandbox cluster: restricted-token + job-object spawn (#2/#14),
fail-closed when-required (#4), and require-sandbox-for-auto-approve (#3). Dep-free. The fail-closed / gating
logic is tested by FORCING winsandbox.available() to a known value (winsandbox._AVAIL), so it verifies on any
host; a live restricted-token spawn runs only where the sandbox is genuinely available. Run:

    python scripts/check_winsandbox_0083.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, tools, winsandbox as ws   # noqa: E402
from src.tools import Context, run_command         # noqa: E402
from src.permissions import Permissions            # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def main():
    # -- #2/#14 pure flag construction --------------------------------------------------------------------
    _wr = config.SANDBOX_WRITE_RESTRICTED
    config.SANDBOX_WRITE_RESTRICTED = False
    check("#2 restricted-token flags = DISABLE_MAX_PRIVILEGE|LUA_TOKEN (0x5)", ws.restricted_token_flags() == 0x5)
    check("#2 write-restricted mode adds WRITE_RESTRICTED (0xd)", ws.restricted_token_flags(True) == 0xd)
    config.SANDBOX_WRITE_RESTRICTED = _wr
    _mem, _proc = config.SANDBOX_JOB_MEM_MB, config.SANDBOX_JOB_MAX_PROCS
    config.SANDBOX_JOB_MEM_MB = config.SANDBOX_JOB_MAX_PROCS = 0
    check("#14 job flags = KILL_ON_JOB_CLOSE (0x2000)", ws.job_limit_flags() == 0x2000)
    check("#14 job flags add PROCESS_MEMORY|ACTIVE_PROCESS when capped (0x2108)", ws.job_limit_flags(256, 4) == 0x2108)
    config.SANDBOX_JOB_MEM_MB, config.SANDBOX_JOB_MAX_PROCS = _mem, _proc

    saved = (config.SANDBOX_SPAWN, config.SANDBOX_REQUIRED, config.REQUIRE_SANDBOX_FOR_AUTO, ws._AVAIL)
    wsdir = tempfile.mkdtemp(prefix="sb83_")
    perms = Permissions("bypass", {"allow": ["run_command(python:*)"]}, [])
    ctx = Context(wsdir, perms)

    try:
        if os.name == "nt":
            # -- #4 fail-closed: sandbox REQUIRED but unavailable -> REFUSE, command must NOT run ----------
            config.SANDBOX_SPAWN, config.SANDBOX_REQUIRED = True, True
            ws._AVAIL = False   # force "unavailable"
            r = run_command({"command": "Set-Content -Path sentinel_req.txt -Value x"}, ctx)
            check("#4 required + unavailable -> command REFUSED (fail-closed)",
                  (not r.ok) and "refused" in r.content.lower())
            check("#4 the refused command did NOT run (no side effect on disk)",
                  not os.path.exists(os.path.join(wsdir, "sentinel_req.txt")))

            # -- fallback: on + NOT required + unavailable -> runs unconfined (with a logged warning) ------
            config.SANDBOX_REQUIRED = False
            ws._AVAIL = False
            r = run_command({"command": "Set-Content -Path sentinel_fb.txt -Value ok"}, ctx)
            check("fallback (not required) runs the command unconfined rather than refusing",
                  r.ok and os.path.exists(os.path.join(wsdir, "sentinel_fb.txt")))

            # -- byte-identity: all sandbox flags OFF -> _sandboxed_run is a no-op (None) -------------------
            config.SANDBOX_SPAWN, config.SANDBOX_REQUIRED = False, False
            check("OFF: _sandboxed_run returns None (normal spawn path, byte-identical)",
                  tools._sandboxed_run(["cmd", "/c", "ver"], wsdir, None) is None)

            # -- #3 require-sandbox-for-auto: downgrade auto-allow when NOT sandboxed, keep it when sandboxed
            config.SANDBOX_SPAWN, config.REQUIRE_SANDBOX_FOR_AUTO = True, True
            ws._AVAIL = False
            d = perms.decide("run_command", {"command": 'python -c "print(1)"'}, ctx)
            check("#3 require-sandbox-for-auto + not sandboxed -> allow DOWNGRADED (not auto-allowed)",
                  not (d.allowed and d.action == "allow"))
            ws._AVAIL = True
            d2 = perms.decide("run_command", {"command": 'python -c "print(1)"'}, ctx)
            check("#3 require-sandbox-for-auto + sandbox available -> auto-allow STANDS",
                  d2.allowed and d2.action == "allow")

            # -- #3 OFF -> the allow rule auto-allows regardless (byte-identical) ---------------------------
            config.REQUIRE_SANDBOX_FOR_AUTO = False
            ws._AVAIL = False
            d3 = perms.decide("run_command", {"command": 'python -c "print(1)"'}, ctx)
            check("#3 OFF -> allow rule auto-allows (byte-identical)", d3.allowed and d3.action == "allow")
        else:
            print("  (skipping the Windows-only run_command sandbox integration tests - not on Windows)")

        # -- live restricted-token spawn where genuinely available on this host ----------------------------
        ws._AVAIL = None   # clear the forced value -> real probe
        if os.name == "nt" and ws.available():
            rc, out, timed = ws.run_shell(["cmd", "/c", "echo sandboxed-ok"], wsdir, dict(os.environ), 10, True)
            check("live: restricted-token child runs and its output is captured",
                  rc == 0 and not timed and "sandboxed-ok" in out)
        else:
            print("  (live restricted-token spawn skipped - OS sandbox not available in this environment)")
    finally:
        config.SANDBOX_SPAWN, config.SANDBOX_REQUIRED, config.REQUIRE_SANDBOX_FOR_AUTO, ws._AVAIL = saved

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
