"""
scripts/check_native_toolcalls.py

Definitive check: does the configured model endpoint return OpenAI-format
tool_calls (i.e. is NATIVE tool-calling working)? Run this AFTER reconfiguring
the vLLM worker with --enable-auto-tool-choice --tool-call-parser.

    python scripts/check_native_toolcalls.py

Reads the CODE_* settings from .env (via src/config.py): CODE_MODEL,
CODE_API_BASE, CODE_API_KEY. Prints a clear WORKING / NOT WORKING verdict and an
exit code (0 = working, 1 = dropped, 2 = endpoint error).
"""
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import litellm  # noqa: E402
from src import config  # noqa: E402

litellm.drop_params = True

TOOLS = [{"type": "function", "function": {
    "name": "get_weather",
    "description": "Get the weather for a city",
    "parameters": {"type": "object",
                   "properties": {"city": {"type": "string"}},
                   "required": ["city"]},
}}]


def main():
    print(f"endpoint : {config.API_BASE or '(none)'}")
    print(f"model    : {config.MODEL}")
    print("sending a tool-call request (cold start may take 30-60s)...\n")

    kw = dict(
        model=config.MODEL,
        messages=[{"role": "user",
                   "content": "Use the get_weather tool to check the weather in Paris."}],
        tools=TOOLS, tool_choice="auto", temperature=0, timeout=240,
    )
    if config.API_BASE:
        kw["api_base"] = config.API_BASE
    if config.API_KEY:
        kw["api_key"] = config.API_KEY

    try:
        resp = litellm.completion(**kw)
    except Exception:
        traceback.print_exc()
        print("\nVERDICT: [ERROR] could not contact the endpoint (check CODE_API_BASE / CODE_API_KEY)")
        return 2

    m = resp.choices[0].message
    tcs = m.tool_calls or []
    print(f"content    : {(m.content or '')[:80]!r}")
    print(f"reasoning  : {(getattr(m, 'reasoning_content', None) or '')[:120]!r}")
    print(f"tool_calls : {[(tc.function.name, tc.function.arguments) for tc in tcs]}")

    if tcs:
        print("\nVERDICT: [OK] native tool-calling WORKING -> set CODE_TOOL_MODE=native")
        return 0
    print("\nVERDICT: [FAIL] the worker is DROPPING tool calls (empty tool_calls).")
    print("Relaunch vLLM with: --enable-auto-tool-choice --tool-call-parser <gpt-oss parser>")
    return 1


if __name__ == "__main__":
    sys.exit(main())
