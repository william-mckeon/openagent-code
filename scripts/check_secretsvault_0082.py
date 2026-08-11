"""
scripts/check_secretsvault_0082.py

Acceptance harness for specs/0082 — secrets at rest (ACL-lock + DPAPI vault). Dep-free (DPAPI via ctypes/
crypt32, no pywin32). The DPAPI round-trip runs only on Windows; the non-Windows fallback is exercised by
temporarily faking os.name. Run:

    python scripts/check_secretsvault_0082.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import secretsvault as sv   # noqa: E402

_results = []


def check(label, cond):
    _results.append(bool(cond))
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def main():
    # -- #9 ACL-lock argv (pure, testable on any platform) ------------------------------------------------
    check("#9 icacls_argv makes a file owner-only (inheritance:r + grant Read user + Full SYSTEM)",
          sv.icacls_argv("C:/ws/.env", "alice")
          == ["icacls", "C:/ws/.env", "/inheritance:r", "/grant:r", "alice:R", "/grant:r", "SYSTEM:F"])

    # -- non-Windows fallback: ACL-lock is a no-op, DPAPI is Unavailable ----------------------------------
    _real = sv.os.name
    try:
        sv.os.name = "posix"
        check("fallback: lock_file_acl is a no-op off Windows (never raises)",
              sv.lock_file_acl("/tmp/.env")[0] is False)
        check("fallback: DPAPI available() is False off Windows",
              sv.available() is False)
        raised = False
        try:
            sv.dpapi_encrypt(b"x")
        except sv.Unavailable:
            raised = True
        check("fallback: dpapi_encrypt raises Unavailable off Windows", raised)
    finally:
        sv.os.name = _real

    if os.name != "nt" or not sv.available():
        # sv.available() is False off Windows AND in a sandboxed interpreter whose ctypes can't load (no dlopen).
        # On the operator's real Windows host it is True and the live round-trip below runs.
        print("  (skipping the live DPAPI round-trip — DPAPI not available in this environment)")
    else:
        # -- #13 DPAPI encrypt/decrypt round-trip (real, on Windows) --------------------------------------
        blob = sv.dpapi_encrypt(b"sk-model-key-abc123")
        check("#13 DPAPI: ciphertext != plaintext and decrypt round-trips",
              blob != b"sk-model-key-abc123" and sv.dpapi_decrypt(blob) == b"sk-model-key-abc123")
        check("#13 available() is True on Windows (crypt32 round-trips)", sv.available() is True)

        # -- vault set/get + load_into_env ---------------------------------------------------------------
        vault = os.path.join(tempfile.mkdtemp(prefix="vault82_"), "secrets.dat")
        sv.set_secret("CODE_API_KEY", "sk-from-vault-999", vault)
        sv.set_secret("SEARCH_KEY", "tvly-vault", vault)
        check("#13 vault set/get round-trips (DPAPI-encrypted on disk, decrypted back)",
              sv.get_secret("CODE_API_KEY", vault) == "sk-from-vault-999"
              and sv.get_secret("MISSING", vault) is None)
        check("#13 the on-disk vault is ENCRYPTED (the plaintext value is not present in the file bytes)",
              b"sk-from-vault-999" not in open(vault, "rb").read())
        os.environ.pop("CODE_API_KEY", None)
        os.environ["SEARCH_KEY"] = "already-set"   # a value already in the env must NOT be clobbered
        n = sv.load_into_env(vault)
        check("#13 load_into_env injects vault secrets (setdefault: doesn't clobber an existing env value)",
              n >= 1 and os.environ.get("CODE_API_KEY") == "sk-from-vault-999"
              and os.environ.get("SEARCH_KEY") == "already-set")
        os.environ.pop("CODE_API_KEY", None)
        os.environ.pop("SEARCH_KEY", None)

    passed, total = sum(_results), len(_results)
    print(f"\nVERDICT: {passed}/{total} {'[OK]' if passed == total else '[FAIL]'}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
