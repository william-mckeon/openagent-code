"""
src/runtime.py

Agent construction, shared by the CLI and the eval harness so both build the
agent identically (same tools, same tool-mode, same system prompt).
"""
from . import config
from .tools import Registry, openai_schemas
from .toolset import active_tools
from .model import Model
from .planner import make_planner
from .prompts import build_system_prompt
from .context import ContextManager
from .agent import Agent


def build_agent(trajectory, initial_working=None, pinned_plan=None, memory=None, granted_dirs=None):
    """Build an agent. For resume, pass `initial_working` (the rehydrated history)
    and `pinned_plan` (restored from the trajectory) — see src/session.py. `memory`
    is the loaded cross-session project memory (Phase 4 #7); None for eval/subagents
    so they stay isolated/lean. `granted_dirs` are reference dirs beyond the workspace
    (--add-dir / CODE_ADD_DIRS), advertised in the prompt so the agent uses them."""
    model = Model(trajectory)
    tools = active_tools()   # base + memory + web (+ MCP) — the dynamic toolset for this run
    planner = make_planner(config.TOOL_MODE, model, openai_schemas(tools))
    system_prompt = build_system_prompt(config.TOOL_MODE, tools, memory=memory, granted_dirs=granted_dirs)
    cm = ContextManager(system_prompt, model, trajectory, verbose=config.VERBOSE,
                        initial_working=initial_working)
    if pinned_plan:
        cm.set_pinned(pinned_plan)
    return Agent(planner, Registry(tools), trajectory, config.MAX_STEPS, cm)
