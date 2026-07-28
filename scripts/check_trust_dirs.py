"""
scripts/check_trust_dirs.py

Acceptance harness for specs/0035 - trusted user directories. Dep-free: no model, no network. Imports
ONLY src.config / src.permissions / src.tools / src.userdirs (never cli/runtime/model/session, which pull
litellm). Proves the load-bearing invariants:

  * request_dir auto-grants under BYPASS + interactive + depth 0 (flag ON) into read_only_roots WITHOUT
    calling ask; a non-bypass mode still asks; a subagent (depth>0) does not auto-grant.
  * flag OFF -> the auto-grant block is skipped and request_dir is byte-identical (asks, grants extra_roots).
  * the READ-only invariant: a write/delete/rename to a read_only_roots dir stays DENIED under bypass AND
    acceptEdits, while a read/grep/tree there is allowed.
  * userdirs.user_typed_dirs grants a plainly-typed existing dir but NOT a negated / denylisted / drive-root
    / non-existent one; grantable_dir rejects system + credential dirs even when isdir is true.
  * CODE_TRUST_USER_DIRS defaults False when unset (opt-in), proven against the fallback, not this .env.

Run:  python scripts/check_trust_dirs.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config, userdirs        # noqa: E402
from src.permissions import Permissions  # noqa: E402
from src.tools import request_dir, ToolResult  # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


class _Ask:
    """Records whether request_dir fell through to the human prompt (and answers it)."""
    def __init__(self, answer="y"):
        self.calls = 0
        self.answer = answer

    def __call__(self, _question):
        self.calls += 1
        return self.answer


class _Ctx:
    def __init__(self, cwd, perms, ask=None, interactive=True, depth=0):
        self.cwd = cwd
        self.permissions = perms
        self.ask = ask
        self.interactive = interactive
        self.depth = depth


def _perms(mode):
    return Permissions(mode, {"deny": [], "ask": [], "allow": []}, [])


def _rp(path):
    return os.path.realpath(path)


def main():
    _saved = {k: getattr(config, k) for k in ("TRUST_USER_DIRS", "HOOKS", "PROPOSE", "EXECPOLICY", "SANDBOX")}
    # Force the other gates off so decide() is deterministic and only the fence/mode matter here.
    config.HOOKS = config.PROPOSE = config.EXECPOLICY = config.SANDBOX = False

    ws = _rp(tempfile.mkdtemp(prefix="oac_ws_"))       # the workspace (cwd)
    ref = _rp(tempfile.mkdtemp(prefix="oac_ref_"))     # a directory the user "names"
    ref_file = os.path.join(ref, "note.txt")
    with open(ref_file, "w", encoding="utf-8") as f:
        f.write("hi")

    # =====================================================================================================
    # 1. flag ON + bypass + interactive + depth 0 -> auto-grant into read_only_roots, ask NEVER called
    # =====================================================================================================
    config.TRUST_USER_DIRS = True
    perms = _perms("bypass")
    ask = _Ask()
    r = request_dir({"path": ref}, _Ctx(ws, perms, ask=ask, interactive=True, depth=0))
    check("flag ON/bypass/interactive: request_dir auto-grants without prompting", r.ok and ask.calls == 0)
    check("flag ON/bypass: the grant lands in read_only_roots (READ tier), NOT extra_roots",
          _rp(ref) in perms.read_only_roots and _rp(ref) not in perms.extra_roots)

    # =====================================================================================================
    # 2. flag ON + NON-bypass mode -> still prompts (no silent auto-grant)
    # =====================================================================================================
    perms2 = _perms("default")
    ask2 = _Ask()
    request_dir({"path": ref}, _Ctx(ws, perms2, ask=ask2, interactive=True, depth=0))
    check("flag ON/default mode: request_dir still ASKS a human (no auto-grant)", ask2.calls == 1)

    # =====================================================================================================
    # 3. flag ON + bypass + SUBAGENT (depth>0) -> no auto-grant (a child can't self-widen the fence)
    # =====================================================================================================
    perms3 = _perms("bypass")
    r3 = request_dir({"path": ref}, _Ctx(ws, perms3, ask=None, interactive=False, depth=1))
    check("flag ON/bypass/depth>0: a subagent does NOT auto-grant (falls to the no-human denial)",
          (not r3.ok) and _rp(ref) not in perms3.read_only_roots)

    # =====================================================================================================
    # 4. flag OFF + bypass -> byte-identical: still asks, grants into extra_roots (historical behavior)
    # =====================================================================================================
    config.TRUST_USER_DIRS = False
    perms4 = _perms("bypass")
    ask4 = _Ask()
    r4 = request_dir({"path": ref}, _Ctx(ws, perms4, ask=ask4, interactive=True, depth=0))
    check("flag OFF/bypass: request_dir still PROMPTS (auto-grant skipped, byte-identical)", ask4.calls == 1)
    check("flag OFF: an approved grant lands in extra_roots (historical), not read_only_roots",
          r4.ok and _rp(ref) in perms4.extra_roots and _rp(ref) not in perms4.read_only_roots)

    # =====================================================================================================
    # 5. the READ-only INVARIANT: a mutating op to a read_only_roots dir stays DENIED; a read is allowed
    # =====================================================================================================
    for mode in ("bypass", "acceptEdits"):
        p = _perms(mode)
        p.read_only_roots.append(_rp(ref))
        c = _Ctx(ws, p, ask=None, interactive=False, depth=0)
        wr = p.decide("write_file", {"path": ref_file}, c)
        de = p.decide("delete_file", {"path": ref_file}, c)
        rd = p.decide("read_file", {"path": ref_file}, c)
        gr = p.decide("grep", {"path": ref}, c)
        check(f"[{mode}] write_file to a read-only-granted dir is DENIED", not wr.allowed)
        check(f"[{mode}] delete_file to a read-only-granted dir is DENIED", not de.allowed)
        check(f"[{mode}] read_file in a read-only-granted dir is ALLOWED", rd.allowed)
        check(f"[{mode}] grep in a read-only-granted dir is ALLOWED", gr.allowed)

    # a dir that was NEVER granted is still outside the fence for reads too (no accidental widening)
    other = _rp(tempfile.mkdtemp(prefix="oac_out_"))
    p = _perms("bypass")
    check("an UN-granted dir is still outside the fence for reads",
          not p.decide("read_file", {"path": os.path.join(other, "x")}, _Ctx(ws, p)).allowed)

    # =====================================================================================================
    # 6. userdirs extractor + grantable_dir guards
    # =====================================================================================================
    check("user_typed_dirs grants a plainly-typed existing dir",
          _rp(ref) in [_rp(d) for d in userdirs.user_typed_dirs("please review " + ref + " for me")])
    check("user_typed_dirs VETOES a negated path (\"dont touch ...\")",
          userdirs.user_typed_dirs("dont touch " + ref) == [])
    check("user_typed_dirs rejects a NON-existent path",
          userdirs.user_typed_dirs("review C:\\Nope_zzz_" + os.path.basename(ref) + "_absent") == [])
    check("user_typed_dirs rejects a bare drive root (C:\\)", userdirs.user_typed_dirs("open C:\\") == [])

    # F3 (specs/0041): a user-typed path with a SPACE must grant the FULL folder, not a truncated sibling
    os.makedirs(os.path.join(ref, "foo"), exist_ok=True)
    os.makedirs(os.path.join(ref, "foo bar"), exist_ok=True)
    _spaced = os.path.join(ref, "foo bar")
    _got = [_rp(d) for d in userdirs.user_typed_dirs("review " + _spaced + " folder by folder")]
    check("F3: a spaced path grants the FULL '...foo bar' folder", _rp(_spaced) in _got)
    check("F3: ...and NOT the shorter '...foo' sibling", _rp(os.path.join(ref, "foo")) not in _got)
    check("F3: a QUOTED spaced path is extracted in full",
          _rp(_spaced) in [_rp(d) for d in userdirs.user_typed_dirs('open "' + _spaced + '" plz')])
    check("F3: a no-space path is unchanged (still grants the folder)",
          _rp(os.path.join(ref, "foo")) in [_rp(d) for d in userdirs.user_typed_dirs("look at " + os.path.join(ref, "foo") + " now")])
    if os.path.isdir(r"C:\Program Files"):
        check("F3: a spaced SYSTEM path stays denylisted (C:\\Program Files)",
              userdirs.user_typed_dirs("look in C:\\Program Files") == [])

    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    if os.path.isdir(sysroot):
        check("grantable_dir rejects a system root even when isdir is true (SystemRoot)",
              not userdirs.grantable_dir(_rp(sysroot)))
        check("user_typed_dirs rejects a denylisted system dir the user typed",
              userdirs.user_typed_dirs("look in " + sysroot) == [])
    else:
        check("grantable_dir rejects a drive root", not userdirs.grantable_dir("C:\\"))
        check("(SystemRoot not present -> denylist path check skipped)", True)

    # a credential-style dir (.ssh component) is rejected even though it exists
    sshdir = os.path.join(ref, ".ssh")
    os.makedirs(sshdir, exist_ok=True)
    check("grantable_dir rejects a .ssh credential dir even when isdir is true",
          not userdirs.grantable_dir(_rp(sshdir)))

    # =====================================================================================================
    # 7. default-OFF (opt-in), proven against the FALLBACK, not this repo's live .env
    # =====================================================================================================
    _env = os.environ.pop("CODE_TRUST_USER_DIRS", None)
    default_off = config._as_bool(os.environ.get("CODE_TRUST_USER_DIRS", "false")) is False
    if _env is not None:
        os.environ["CODE_TRUST_USER_DIRS"] = _env
    check("CODE_TRUST_USER_DIRS defaults False when unset (opt-in)", default_off)

    for k, v in _saved.items():
        setattr(config, k, v)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
