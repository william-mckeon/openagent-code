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
from .tools import (TOOLS, WEB_TOOLS, MEMORY_TOOLS, SKILL_TOOLS, PATCH_TOOLS, GOAL_TOOLS, EFFORT_TOOLS,
                    openai_schemas)
from .mcp_client import mcp_tools


def active_tools():
    """Base tools + opt-in memory/web tools + any connected MCP tools."""
    tools = list(TOOLS)
    if config.MEMORY:
        tools += MEMORY_TOOLS
    if config.SKILLS:
        tools += SKILL_TOOLS
    if config.APPLY_PATCH:
        tools += PATCH_TOOLS
    # The ONLY site that offers `pursue` (Phase 20). Adding it to the base TOOLS list and refusing it at
    # call time instead would change every trajectory's logged tool_schemas even with the flag OFF - a
    # toolset change, which is exactly what corrupts conversion (ROADMAP Phase 3).
    if config.GOAL_LOOP:
        tools += GOAL_TOOLS
    # Offer escalate_effort only when adaptive effort is on AND the model is allowed to self-escalate
    # (the 'off' policy owns the level, so exposing the tool would be a lie). Flag-off -> not offered,
    # so a flag-off run's tool_schemas are byte-identical.
    if config.ADAPTIVE_EFFORT and (config.EFFORT_POLICY or "reactive").strip().lower() not in ("off", "none"):
        tools += EFFORT_TOOLS
    if config.ENABLE_WEB:
        tools += WEB_TOOLS
    tools += mcp_tools()
    return tools


def active_schemas():
    return openai_schemas(active_tools())
