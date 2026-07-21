"""
src/mcp_client.py

MCP (Model Context Protocol) client — Stage B of tool breadth.

Connects to the MCP servers listed in CODE_MCP_CONFIG (a JSON file), discovers
their tools, and exposes each as an ordinary tool-dict named
`mcp__<server>__<tool>` that the agent uses like any other tool. So instead of
hand-writing web/git/etc. tools, you point at MCP servers that provide them.

The agent loop is SYNCHRONOUS and the MCP SDK is ASYNC, so a dedicated background
event-loop thread owns the sessions; each tool call is dispatched to it via
run_coroutine_threadsafe and blocks for the result. Transport: stdio only (local
servers — the common case); HTTP/SSE is a follow-up.

Soft dependency: if the `mcp` SDK isn't installed, or CODE_MCP_CONFIG is unset,
this is a no-op and the agent runs with just the built-in tools.

Config (CODE_MCP_CONFIG -> JSON):
    { "mcpServers": { "<name>": { "command": "...", "args": [...], "env": {...} } } }
"""
import os
import json
import asyncio
import threading

from . import config

_TOOLS = []      # discovered MCP tools as tool-dicts (read by toolset.active_tools)
_LOOP = None     # background asyncio loop
_THREAD = None
_STACK = None    # AsyncExitStack keeping the sessions open until disconnect()


def mcp_tools():
    """The currently-connected MCP tools (empty when nothing is connected)."""
    return list(_TOOLS)


def _load_servers():
    path = config.MCP_CONFIG
    if not path or not os.path.isfile(path):
        return {}
    try:
        cfg = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        print(f"WARNING: could not read CODE_MCP_CONFIG ({path}): {e}")
        return {}
    return cfg.get("mcpServers") or cfg.get("servers") or {}


def _call_sync(coro):
    """Run a coroutine on the background loop and block for its result."""
    return asyncio.run_coroutine_threadsafe(coro, _LOOP).result()


def _result_to_toolresult(result, is_web, ctx, args):
    """Turn a raw MCP call result into a ToolResult. For a WEB-marked server (specs/0029) the output is routed
    through the SAME choke point native web tools use: fenced as untrusted external content AND its URLs
    recorded on the read-ledger - so MCP web content can't inject and a cited MCP URL grounds. An error result
    is NEVER fenced. A non-web server is byte-identical to before. Order mirrors web_fetch: truncate the body,
    record the RAW text, THEN wrap (so the closing fence is never sliced)."""
    from .tools import ToolResult
    parts = [getattr(c, "text", None) or str(c) for c in (getattr(result, "content", None) or [])]
    text = "\n".join(p for p in parts if p) or "(no content)"
    ok = not getattr(result, "isError", False)
    body = text[:8000]
    if is_web and ok:
        from .tools import _wrap_external, record_external
        record_external(ctx, body, args)
        return ToolResult(True, _wrap_external(body))
    return ToolResult(ok, body)


def _wrap(server, session, tool, is_web=False):
    """Turn a discovered MCP tool into one of our tool-dicts. A WEB-marked server's output is fenced +
    ledger-recorded (specs/0029) and the dict carries `web: True`; a non-web server is byte-identical."""
    full_name = f"mcp__{server}__{tool.name}"
    schema = tool.inputSchema or {"type": "object", "properties": {}}

    def _fn(args, ctx):
        from .tools import ToolResult
        try:
            result = _call_sync(session.call_tool(tool.name, args))
        except Exception as e:
            return ToolResult(False, f"mcp error: {type(e).__name__}: {e}")
        return _result_to_toolresult(result, is_web, ctx, args)

    d = {
        "name": full_name,
        "fn": _fn,
        "description": (tool.description or f"MCP tool {tool.name} from {server}")[:400],
        "parameters": schema,
    }
    if is_web:
        d["web"] = True   # prompts.py teaches the untrusted-web note; connect() flips config.MCP_WEB_ACTIVE
    return d


def connect():
    """Connect configured MCP servers and register their tools. Returns the count."""
    global _LOOP, _THREAD, _STACK, _TOOLS
    servers = _load_servers()
    if not servers:
        return 0
    try:
        from contextlib import AsyncExitStack
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError:
        print("WARNING: CODE_MCP_CONFIG is set but the 'mcp' SDK is not installed; skipping MCP.")
        return 0

    _LOOP = asyncio.new_event_loop()
    _THREAD = threading.Thread(target=_LOOP.run_forever, daemon=True)
    _THREAD.start()

    async def _setup():
        stack = AsyncExitStack()
        tools = []
        for name, spec in servers.items():
            try:
                params = StdioServerParameters(
                    command=spec["command"],
                    args=spec.get("args", []),
                    env={**os.environ, **(spec.get("env") or {})},
                )
                read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                listed = await session.list_tools()
                is_web = bool(spec.get("web"))   # specs/0029: route this server's output through the web fence + ledger
                for t in listed.tools:
                    tools.append(_wrap(name, session, t, is_web))
            except Exception as e:
                print(f"WARNING: MCP server {name!r} failed to start: {type(e).__name__}: {e}")
        return stack, tools

    _STACK, _TOOLS = _call_sync(_setup())
    # specs/0029: flip the runtime web flag so the grounding gate (web_grounding_active) treats a cited
    # MCP-surfaced URL like a native one, even with CODE_ENABLE_WEB off.
    config.MCP_WEB_ACTIVE = any(t.get("web") for t in _TOOLS)
    return len(_TOOLS)


def disconnect():
    """Close MCP sessions and stop the background loop."""
    global _TOOLS, _STACK, _LOOP, _THREAD
    if _STACK is not None:
        try:
            _call_sync(_STACK.aclose())
        except Exception:
            pass
    if _LOOP is not None:
        _LOOP.call_soon_threadsafe(_LOOP.stop)
    config.MCP_WEB_ACTIVE = False   # specs/0029: no web-marked MCP server is connected anymore
    _TOOLS, _STACK, _LOOP, _THREAD = [], None, None, None
