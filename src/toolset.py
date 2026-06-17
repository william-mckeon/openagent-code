"""
src/toolset.py

The ACTIVE toolset for a run.

Tool breadth (Phase 4) makes the toolset DYNAMIC: it's no longer the static
`tools.TOOLS`, but assembled per run from config and (Stage B) connected MCP
servers. Everything that needs the toolset — the planner schemas, the system
prompt, the registry, and the trajectory's logged tool_schemas — goes through
`active_tools()` so they all agree on exactly what is offered this run. That
per-run agreement is what keeps the Phase-3 self-containment gate accurate as the
toolset varies.
"""
from . import config
from .tools import TOOLS, WEB_TOOLS, MEMORY_TOOLS, openai_schemas
from .mcp_client import mcp_tools


def active_tools():
    """Base tools + opt-in memory/web tools + any connected MCP tools."""
    tools = list(TOOLS)
    if config.MEMORY:
        tools += MEMORY_TOOLS
    if config.ENABLE_WEB:
        tools += WEB_TOOLS
    tools += mcp_tools()
    return tools


def active_schemas():
    return openai_schemas(active_tools())
