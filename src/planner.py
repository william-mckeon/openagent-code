"""
src/planner.py

Tool-calling protocols — the only thing that differs between CODE_TOOL_MODE values.

Both planners turn one model turn into a uniform Decision (an assistant message to
append, a list of tool calls to run, and an optional final answer), so the agent
loop and the trajectory logging are identical regardless of mode.

  NativePlanner — passes the OpenAI `tools` schema; reads msg.tool_calls. Requires
                  the serving stack to parse tool calls (e.g. vLLM launched with
                  --enable-auto-tool-choice --tool-call-parser).

  JsonPlanner   — sends NO `tools`; the tool catalog lives in the system prompt and
                  the model replies with a JSON action we parse here. Works on any
                  OpenAI-compatible endpoint, no server tool-parser needed.
"""
import json


class Decision:
    def __init__(self, assistant, calls, final, nudge=None, gave_up=False):
        self.assistant = assistant   # message dict to append to the conversation
        self.calls = calls           # list of {"id", "name", "args"}
        self.final = final           # str when the model is done, else None
        self.nudge = nudge           # corrective user message when the model broke protocol
        self.gave_up = gave_up       # True when the model never produced a usable action


class NativePlanner:
    def __init__(self, model, schemas):
        self.model = model
        self.schemas = schemas

    def step(self, messages, step):
        msg = self.model.complete(messages, self.schemas, step)
        assistant = {"role": "assistant", "content": msg.content or ""}
        calls = []
        if msg.tool_calls:
            assistant["tool_calls"] = [{
                "id": tc.id, "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            } for tc in msg.tool_calls]
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                calls.append({"id": tc.id, "name": tc.function.name, "args": args})
        return Decision(assistant, calls, None if calls else msg.content)

    def format_result(self, call, result):
        return {"role": "tool", "tool_call_id": call["id"], "content": result.content}


class JsonPlanner:
    MAX_NUDGES = 3

    def __init__(self, model):
        self.model = model
        self.nudges = 0

    def step(self, messages, step):
        msg = self.model.complete(messages, None, step)   # no tools param
        content = msg.content or ""
        assistant = {"role": "assistant", "content": content}

        parsed = _extract_json(content)

        # An actionable tool call.
        if isinstance(parsed, dict) and parsed.get("tool") and parsed["tool"] != "final":
            self.nudges = 0
            call = {"id": f"json-{step}", "name": parsed["tool"], "args": parsed.get("args") or {}}
            return Decision(assistant, [call], None)

        # An explicit, structured finish — the ONLY way to end in json mode.
        if isinstance(parsed, dict) and parsed.get("tool") == "final":
            return Decision(assistant, [], (parsed.get("args") or {}).get("answer") or content)

        # The model broke protocol. Two distinct failure shapes, with tailored
        # nudges, because they have different causes:
        #   * empty content  -> the model tried a native/harmony tool call that the
        #                        worker dropped (the gpt-oss failure mode). Tell it
        #                        explicitly that native calling is unavailable.
        #   * prose / a plan  -> it narrated instead of acting.
        # Nudge a few times, then GIVE UP honestly (gave_up=True) rather than
        # passing an empty string off as a finished task.
        if self.nudges < self.MAX_NUDGES:
            self.nudges += 1
            if content.strip() == "":
                nudge = ('Your last reply had no visible content. You may be trying to use built-in '
                         'function/tool-calling, which is NOT available here — it is silently dropped. '
                         'The ONLY way to act is to write one JSON object as plain text, e.g. '
                         '{"tool":"glob","args":{"pattern":"**/*.py"}}. Emit that now.')
            else:
                nudge = ('That reply was not a valid action. Respond with EXACTLY one JSON object and '
                         'nothing else — a tool call {"tool":"<name>","args":{...}} or, when the task '
                         'is finished, {"tool":"final","args":{"answer":"..."}}. No prose, no plan.')
            return Decision(assistant, [], None, nudge=nudge)

        final = content.strip() or "(no action taken: the model never emitted a usable tool call)"
        return Decision(assistant, [], final, gave_up=True)

    def format_result(self, call, result):
        status = "ok" if result.ok else "error"
        return {
            "role": "user",
            "content": (f'Tool {call["name"]} [{status}] returned:\n{result.content}\n\n'
                        'Reply with the next JSON object, or '
                        '{"tool": "final", "args": {"answer": "..."}} if done.'),
        }


def _extract_json(text):
    """Return the first balanced JSON object in text, or None.

    Robust to a model that prefixes prose or wraps the object — it scans from the
    first '{' to its matching '}', ignoring braces inside strings.
    """
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        return None
    return None


def make_planner(mode, model, schemas):
    if mode == "json":
        return JsonPlanner(model)
    return NativePlanner(model, schemas)
