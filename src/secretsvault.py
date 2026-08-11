"""
src/secretsvault.py

specs/0082: protect secrets AT REST on Windows — the two controls a Codex-vs-OAC review flagged, done with
stdlib + Windows built-ins (no pywin32 dependency).

- ACL-lock (CODE_LOCK_SECRETS): OAC's .env (holding CODE_API_KEY) is created with default INHERITED ACLs,
  readable by any process running as the user. lock_file_acl() runs `icacls` to strip inheritance and grant
  read only to the current user + SYSTEM — the Windows equivalent of `chmod 0600`.
- DPAPI vault (CODE_SECRETS_VAULT): OAC keeps CODE_API_KEY as PLAINTEXT in .env. DPAPI (crypt32
  CryptProtectData/CryptUnprotectData, reached via ctypes) ties ciphertext to the user account with NO key
  material to store. set_secret/get_secret read/write a DPAPI-encrypted `secrets.dat`; a startup loader can
  inject the values into os.environ, after which env-scrub (specs/0078) keeps them out of children.

Everything degrades gracefully: on a non-Windows host (the Linux training substrate) the ACL-lock is a no-op
and DPAPI raises `Unavailable`, which callers treat as "feature off".
"""
import os
import subprocess


class Unavailable(RuntimeError):
    """DPAPI / the Windows ACL tooling is not available on this platform."""


def icacls_argv(path, user=None):
    """specs/0082: the `icacls` argv that makes `path` owner-only — strip inheritance, then grant Read to the
    current user and Full to SYSTEM. Pure (dep-free, testable); lock_file_acl runs it."""
    who = user or os.environ.get("USERNAME") or os.environ.get("USER") or "%USERNAME%"
    return ["icacls", path, "/inheritance:r", "/grant:r", f"{who}:R", "/grant:r", "SYSTEM:F"]


def lock_file_acl(path):
    """Make `path` owner-only via icacls (Windows). Returns (ok: bool, message). No-op / (False, ...) off
    Windows or when the file is missing. Best-effort — never raises."""
    if os.name != "nt":
        return False, "ACL-lock is Windows-only (no-op here)"
    if not os.path.isfile(path):
        return False, f"{path} does not exist"
    try:
        p = subprocess.run(icacls_argv(path), capture_output=True, text=True, timeout=15)
        return (p.returncode == 0), (p.stdout or p.stderr or "").strip()
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def _crypt():
    """(ctypes, crypt32, kernel32), or raise Unavailable off Windows / when ctypes can't load. Some sandboxed
    interpreters ship a partial `_ctypes` (no dlopen) where even `import ctypes` fails — that must surface as
    Unavailable ("feature off"), never as a raw ImportError that breaks a caller."""
    if os.name != "nt":
        raise Unavailable("DPAPI is Windows-only")
    try:
        import ctypes
        return ctypes, ctypes.windll.crypt32, ctypes.windll.kernel32
    except Exception as e:  # noqa: BLE001 - a broken/partial ctypes -> treat DPAPI as unavailable
        raise Unavailable(f"ctypes unavailable: {type(e).__name__}: {e}")


def _blob(ctypes_mod, data: bytes):
    class _DATA_BLOB(ctypes_mod.Structure):
        _fields_ = [("cbData", ctypes_mod.c_uint), ("pbData", ctypes_mod.POINTER(ctypes_mod.c_char))]
    buf = ctypes_mod.create_string_buffer(bytes(data), len(data))
    return _DATA_BLOB(len(data), ctypes_mod.cast(buf, ctypes_mod.POINTER(ctypes_mod.c_char))), _DATA_BLOB


def dpapi_encrypt(data: bytes) -> bytes:
    """DPAPI-encrypt `data` (tied to the current Windows user). Raises Unavailable off Windows / no ctypes."""
    ctypes, crypt32, kernel32 = _crypt()
    blob_in, DATA_BLOB = _blob(ctypes, data)
    blob_out = DATA_BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise Unavailable("CryptProtectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def dpapi_decrypt(blob: bytes) -> bytes:
    """DPAPI-decrypt a blob produced by dpapi_encrypt. Raises Unavailable off Windows / on failure."""
    ctypes, crypt32, kernel32 = _crypt()
    blob_in, DATA_BLOB = _blob(ctypes, blob)
    blob_out = DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise Unavailable("CryptUnprotectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def available():
    """True iff DPAPI round-trips on this host (Windows with crypt32)."""
    try:
        return dpapi_decrypt(dpapi_encrypt(b"x")) == b"x"
    except Exception:  # noqa: BLE001
        return False


def _read_vault(path):
    import json
    try:
        return json.loads(dpapi_decrypt(open(path, "rb").read()).decode("utf-8"))
    except Exception:  # noqa: BLE001 - missing / corrupt / undecryptable -> empty
        return {}


def set_secret(name, value, path):
    """DPAPI-encrypt and store {name: value} in the vault at `path` (a DPAPI-encrypted JSON blob). Raises
    Unavailable off Windows."""
    import json
    data = _read_vault(path) if os.path.isfile(path) else {}
    data[str(name)] = str(value)
    with open(path, "wb") as f:
        f.write(dpapi_encrypt(json.dumps(data).encode("utf-8")))


def get_secret(name, path):
    """The decrypted value for `name` from the vault at `path`, or None."""
    return _read_vault(path).get(str(name)) if os.path.isfile(path) else None


def load_into_env(path):
    """Inject every vault secret into os.environ (startup) WITHOUT clobbering a value already set in the real
    environment (setdefault). Returns the count loaded; 0 when unavailable / no vault. env-scrub (0078) then
    keeps these out of run_command children."""
    if os.name != "nt" or not path or not os.path.isfile(path):
        return 0
    n = 0
    for k, v in _read_vault(path).items():
        if k not in os.environ:
            os.environ[k] = str(v)
            n += 1
    return n
