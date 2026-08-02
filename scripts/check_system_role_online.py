"""
scripts/check_system_role_online.py

LIVE probe (needs network + CODE_API_KEY) — NOT a dep-free acceptance check.

Question it answers: does the model honor a role:"system" instruction END-TO-END, or does the endpoint /
chat-template deprioritize (or override) it? It sends a SYSTEM-only directive with a distinctive marker plus
a throwaway user turn, straight to the OpenAI-compatible endpoint via stdlib urllib (NO litellm, so the
OneDrive import hang can't bite). Model / api_base / key are read from src.config (which loads .env); the
API key is NEVER printed — only its length.

Run:  python scripts/check_system_role_online.py

Read it as:
  * Both directives obeyed  -> the system role IS honored; persona parroting is soft adherence + echoable
                               persona TEXT, so trimming CODE_AGENT_PERSONA is the fix (no TM override).
  * Neither obeyed          -> the model/template follows SYSTEM instructions weakly; lean on structural
                               gates over prompt rules (and still trim the persona).
  * Mixed                   -> inconsistent small-model adherence.
"""
import json
import os
import sys
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config  # noqa: E402  (loads .env; lightweight — no litellm, no network at import)

_PROVIDER_PREFIXES = ("openai", "together_ai", "openrouter", "vllm")


def served_id(model):
    """Strip the litellm provider prefix (openai/<id> -> <id>) so the RAW OpenAI API gets the served name."""
    if "/" in model and model.split("/", 1)[0] in _PROVIDER_PREFIXES:
        return model.split("/", 1)[1]
    return model


def call(system_msg, user_msg, max_tokens=512):
    url = config.API_BASE.rstrip("/") + "/chat/completions"
    payload = {
        "model": served_id(config.MODEL),
        "messages": [{"role": "system", "content": system_msg}, {"role": "user", "content": user_msg}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "reasoning_effort": "low",   # keep reasoning short so content isn't truncated; adherence is the test
    }
    headers = {
        "Content-Type": "application/json",
        # Cloudflare fronts the Tinker endpoint and 403/1010-bans the default Python-urllib User-Agent;
        # a normal browser UA clears its browser-signature check (litellm's httpx client passes for the same reason).
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }
    if config.API_KEY:
        headers["Authorization"] = "Bearer " + config.API_KEY
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
    except Exception as e:  # noqa: BLE001 - report any network / parse failure plainly
        return None, f"{type(e).__name__}: {e}"
    try:
        msg = data["choices"][0]["message"]
        return {"content": (msg.get("content") or "").strip(),
                "reasoning": (msg.get("reasoning_content") or msg.get("reasoning") or "")}, None
    except Exception:  # noqa: BLE001
        return None, f"unexpected response shape: {json.dumps(data)[:300]}"


def main():
    print(f"  endpoint : {config.API_BASE}")
    print(f"  model    : {served_id(config.MODEL)}")
    print(f"  api key  : {'set (' + str(len(config.API_KEY)) + ' chars)' if config.API_KEY else 'MISSING - set CODE_API_KEY'}")
    print()

    tests = [
        ("ZORP prefix",
         "You are ZORP, a test bot. You MUST begin EVERY reply with the exact token 'ZORP:' and then answer "
         "in one short sentence. Never call yourself anything but ZORP.",
         "hi, who are you?",
         lambda c: c.lower().startswith("zorp:")),
        ("BANANA only",
         "Reply with ONLY the single word BANANA in uppercase and nothing else - no punctuation, no other words.",
         "say something",
         lambda c: c.strip().strip(".!").upper() == "BANANA"),
    ]

    honored = 0
    ran = 0
    for name, sysmsg, usermsg, ok_fn in tests:
        res, err = call(sysmsg, usermsg)
        if err:
            print(f"  [{name}] ERROR: {err}\n")
            continue
        ran += 1
        obeyed = ok_fn(res["content"])
        honored += 1 if obeyed else 0
        print(f"  [{name}] system honored: {obeyed}")
        print(f"        reply : {res['content'][:180]!r}")
        if res["reasoning"]:
            print(f"        (reasoning_content present: {len(res['reasoning'])} chars)")
        print()

    print("VERDICT:")
    if ran == 0:
        print("  no probe completed - see the ERROR line(s) above (network / key / endpoint).")
    elif honored == ran and ran == 2:
        print("  Both directives obeyed -> the role:'system' message IS honored end-to-end. No TM override;")
        print("  the persona parroting is soft adherence + echoable persona TEXT -> trim CODE_AGENT_PERSONA.")
    elif honored == 0:
        print("  Neither directive obeyed -> the model/template follows SYSTEM-role instructions WEAKLY.")
        print("  Prefer structural gates over prompt rules; trimming the persona is still the reliable fix.")
    else:
        print(f"  Mixed ({honored}/{ran} obeyed) -> inconsistent small-model system-instruction adherence.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
