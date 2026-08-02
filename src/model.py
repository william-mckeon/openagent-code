"""
src/model.py

Model gateway — the swappable boundary.

The harness calls ONLY this. Everything below (RunPod vLLM, Bedrock, OpenRouter,
a local Ollama) is a CODE_* env change, never a code change. That is the point
of routing through LiteLLM: the data-sovereignty choice stays a one-line swap.

CODE_MODEL / CODE_API_BASE examples (see src/config.py and .env.example):
  Thinking Machines Lab / Tinker (OpenAI-compatible) — the CURRENT deployment:
    CODE_MODEL=openai/thinkingmachines/Inkling-Small:peft:262144
    CODE_API_BASE=https://tinker.thinkingmachines.dev/services/tinker-prod/oai/api/v1
  Together (OpenAI-compatible):
    CODE_MODEL=openai/thinkingmachines/Inkling
    CODE_API_BASE=https://api.together.xyz/v1
  RunPod / self-hosted vLLM (gpt-oss-120b):
    CODE_MODEL=openai/gpt-oss-120b
    CODE_API_BASE=https://<your-pod>-8000.proxy.runpod.net/v1
  AWS Bedrock:
    CODE_MODEL=bedrock/openai.gpt-oss-120b-1:0
    CODE_API_BASE=            # unset; Bedrock uses AWS_* credentials
"""
import os
import random
import time
from types import SimpleNamespace

# Use LiteLLM's BUNDLED model-cost map instead of fetching it from GitHub on import. The
# remote fetch phones raw.githubusercontent.com at startup and times out when the network is
# offline/slow — adding launch latency and a scary warning to a self-hosted tool that should
# never need GitHub to start. MUST be set BEFORE `import litellm`.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import litellm

from . import config
from .prompts import SUMMARIZE_PROMPT
from .logsetup import get_logger

log = get_logger("model")

# Quiet LiteLLM's third-party noise: it prints a "Give Feedback / Get Help: <github url>"
# banner on every error/retry, which clutters our own clean retry logs. Behavior unchanged.
litellm.suppress_debug_info = True

# Let LiteLLM reshape the message list to each provider's rules. Bedrock's Converse API
# requires strict user<->assistant alternation, and maps tool-results to user-side blocks;
# a turn that ends in tool-results (or any consecutive same-role run) is otherwise rejected.
# With this on, LiteLLM inserts the needed continue/dummy messages instead of erroring.
# Benign for the OpenAI/vLLM path (no reshaping needed there).
litellm.modify_params = True


def _non_retryable(e):
    """True for errors that retrying can't fix — a 400 BadRequest or a context-window
    overflow. Re-sending the identical oversized/malformed request only fails again, so we
    raise immediately rather than backing off through every retry."""
    name = type(e).__name__.lower()
    if "badrequest" in name or "contextwindow" in name or "invalidrequest" in name:
        return True
    msg = str(e).lower()
    return any(s in msg for s in (
        "context length", "maximum context", "context window", "input is too long",
        "input length", "too many tokens", "exceeds the maximum", "maximum allowed",
    ))


def _effort_kwargs(effort):
    """The legacy low/medium/high reasoning_effort path (pre-0044), unchanged. The LiteLLM `bedrock/`
    provider takes it as a TOP-LEVEL param (maps to additionalModelRequestFields, where extra_body is
    ignored); OpenAI-compatible endpoints (vLLM / Together / Bedrock's /openai/v1) take it via extra_body,
    which lands verbatim in the request body. Empty = send nothing."""
    if not effort:
        return {}
    if config.MODEL.startswith("bedrock/"):
        return {"reasoning_effort": effort}
    return {"extra_body": {"reasoning_effort": effort}}


def _reasoning_kwargs(effort=None):
    """Provider-aware reasoning control, in precedence order:

      1. An explicit per-Model `effort` override (the grounding / guardian subagents at CODE_GROUNDING_EFFORT
         / CODE_GUARDIAN_EFFORT, the adaptive ladder) -> ALWAYS the legacy low/medium/high string path, so a
         per-subagent effort is never silently replaced by the global pass-through.
      2. Else, the GLOBAL pass-through (specs/0044): if CODE_REASONING_VALUE is set, send
         {CODE_REASONING_PARAM: <value>} — a raw string, an int budget, or a JSON object — TOP-LEVEL when
         CODE_REASONING_TOPLEVEL (or a bedrock/ model), else via extra_body. Lets the operator target
         whatever reasoning control the served model (Inkling) accepts, no code change, no _EFFORTS allowlist.
      3. Else, the legacy GLOBAL config.REASONING_EFFORT string path.

    With CODE_REASONING_VALUE empty (default), branches 1+3 reproduce the pre-0044 behavior EXACTLY:
    _effort_kwargs(effort or config.REASONING_EFFORT) for every effort/model combination."""
    if effort:
        return _effort_kwargs(effort)                    # per-Model override — legacy path, unchanged
    if config.REASONING_VALUE not in (None, ""):         # global pass-through (specs/0044)
        payload = {config.REASONING_PARAM: config.REASONING_VALUE}
        top_level = config.REASONING_TOPLEVEL or config.MODEL.startswith("bedrock/")
        return payload if top_level else {"extra_body": payload}
    return _effort_kwargs(config.REASONING_EFFORT)       # legacy global effort, unchanged


def _assemble_stream(chunks):
    """Reassemble a STREAMED litellm response (CODE_STREAM, specs/0043) into an attribute-shaped
    object EQUIVALENT to a non-streaming resp (resp.choices[0].message + resp.usage), so complete()'s
    dropped-call check, trajectory.log_model_call, and the planner reasoning fold consume it UNCHANGED.
    Hand-rolled (not litellm.stream_chunk_builder) so the reassembly is dep-free unit-testable with a
    fake chunk iterator. Folds content and reasoning_content fragments, and tool-call fragments whose
    name/arguments arrive split across chunks (keyed by index); usage rides the terminal include_usage
    chunk (stays None if the provider omits it — log_model_call tolerates a None usage)."""
    content, reasoning, usage, finish = [], [], None, None
    tools = {}   # index -> {"id", "name", "args": [fragments]}
    for chunk in chunks:
        u = getattr(chunk, "usage", None)
        if u is not None:
            usage = u
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue                       # a usage-only terminal chunk carries no choices
        choice0 = choices[0]
        if getattr(choice0, "finish_reason", None):
            finish = choice0.finish_reason
        delta = getattr(choice0, "delta", None)
        if delta is None:
            continue
        if getattr(delta, "content", None):
            content.append(delta.content)
        if getattr(delta, "reasoning_content", None):
            reasoning.append(delta.reasoning_content)
        for td in (getattr(delta, "tool_calls", None) or []):
            slot = tools.setdefault(getattr(td, "index", 0) or 0, {"id": None, "name": None, "args": []})
            if getattr(td, "id", None):
                slot["id"] = td.id
            fn = getattr(td, "function", None)
            if fn is not None:
                if getattr(fn, "name", None):
                    slot["name"] = fn.name
                if getattr(fn, "arguments", None):
                    slot["args"].append(fn.arguments)
    tool_calls = None
    if tools:
        tool_calls = [
            SimpleNamespace(id=s["id"], type="function",
                            function=SimpleNamespace(name=s["name"], arguments="".join(s["args"])))
            for _, s in sorted(tools.items())
        ]
    # content/reasoning None (not "") when nothing streamed, to mirror a non-streaming tool-only turn.
    msg = SimpleNamespace(role="assistant",
                          content=("".join(content) if content else None),
                          reasoning_content=("".join(reasoning) if reasoning else None),
                          tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(index=0, finish_reason=finish, message=msg)],
                           usage=usage)


def _output_cap(messages):
    """The per-request output cap sent as max_tokens (specs/0045), or None to add NO key (BYTE-IDENTICAL
    default). A fixed CODE_MODEL_MAX_OUTPUT_TOKENS is sent as-is; 'auto' derives
    MODEL_MAX_TOKENS - estimate_tokens(messages) - OUTPUT_MARGIN_TOKENS, floored at MIN_OUTPUT_TOKENS so a
    large prompt can never yield a non-positive cap (which would truncate the answer or 400)."""
    if config.MODEL_MAX_OUTPUT_TOKENS_AUTO:
        from .context import estimate_tokens   # lazy — avoids the model<->context import cycle, like summarize()
        remaining = config.MODEL_MAX_TOKENS - estimate_tokens(messages) - config.OUTPUT_MARGIN_TOKENS
        return max(config.MIN_OUTPUT_TOKENS, remaining)
    if config.MODEL_MAX_OUTPUT_TOKENS > 0:
        return config.MODEL_MAX_OUTPUT_TOKENS
    return None


class Model:
    def __init__(self, trajectory, effort=None):
        self.traj = trajectory
        self.effort = effort   # per-instance reasoning-effort override (None = inherit the global)

    def _params(self):
        # timeout is generous on purpose (config.REQUEST_TIMEOUT, default 600s):
        # a scale-to-zero worker cold-starts on its first call, and a short read
        # timeout would abort the spin-up. Copied from openagent-infra, which
        # absorbs the cold start at call time rather than failing fast.
        kw = {"model": config.MODEL, "temperature": config.TEMPERATURE,
              "timeout": config.REQUEST_TIMEOUT}
        if config.API_BASE:
            kw["api_base"] = config.API_BASE
        if config.API_KEY:
            kw["api_key"] = config.API_KEY
        kw.update(_reasoning_kwargs(self.effort))   # provider-aware (bedrock top-level vs extra_body)
        if config.EXTRA_BODY:                        # specs/0049: merge operator extra_body params (default {} = no-op, byte-identical)
            kw["extra_body"] = {**config.EXTRA_BODY, **kw.get("extra_body", {})}   # reasoning knob wins on a key collision
        return kw

    def _invoke(self, kwargs):
        """The single primary-turn litellm call site (specs/0043). CODE_STREAM off (default): a plain
        non-streaming litellm.completion — the passed kwargs and the returned resp are BYTE-IDENTICAL
        to the old inline call (no stream keys added, original dict untouched). On: stream the same
        request and reassemble via _assemble_stream so complete()'s consumers stay unchanged. Only the
        primary turn streams — warm_up() and _summarize_once() call litellm.completion directly."""
        if not config.STREAM:
            return litellm.completion(**kwargs)
        streamed = {**kwargs, "stream": True, "stream_options": {"include_usage": True}}
        return _assemble_stream(litellm.completion(**streamed))

    def summarize(self, messages):
        """Compress older turns into a briefing for the ContextManager.

        Deliberately does NOT call log_model_call — a compaction summary is not an
        agent step. The ContextManager logs a `compaction` record instead. No tools.

        specs/0034: the input is BOUNDED — a single litellm call never renders more than
        config.SUMMARIZE_INPUT_MAX_TOKENS, so a huge `messages` block (a resumed session's whole history,
        larger than the model window) is summarized in chunks and folded instead of overflowing in one shot.
        The fast path (input already fits) is byte-identical to the old single-shot render + call.
        """
        from .context import bounded_summary   # lazy: keeps model.py free of a context import cycle
        return bounded_summary(messages, config.SUMMARIZE_INPUT_MAX_TOKENS * 4,
                               self._summarize_once, self._render)

    @staticmethod
    def _render(messages):
        return "\n\n".join(f"[{m.get('role')}] {m.get('content') or ''}" for m in messages)

    def _summarize_once(self, rendered):
        """One bounded summarize call (input guaranteed under the window by bounded_summary)."""
        resp = litellm.completion(
            messages=[
                {"role": "system", "content": SUMMARIZE_PROMPT},
                {"role": "user", "content": rendered},
            ],
            **self._params(),
        )
        return resp.choices[0].message.content or ""

    def complete(self, messages, schemas, step):
        """One model turn. `schemas` is the OpenAI tools list for native mode, or
        None for json mode (where tools live in the system prompt instead).

        Retries (CODE_MODEL_RETRIES) make a flaky / intermittent endpoint usable:
        transient errors AND dropped-tool-call responses are retried, so a request
        that lands on a misconfigured worker is re-sent and likely hits a healthy
        one. Only the FINAL response is logged — the retried glitches are infra
        noise, not agent decisions, so the trajectory stays clean."""
        kwargs = self._params()
        kwargs["messages"] = messages
        if schemas:
            kwargs["tools"] = schemas
            kwargs["tool_choice"] = "auto"
        cap = _output_cap(messages)   # specs/0045: optional per-request output cap (None = no key, byte-identical)
        if cap is not None:
            kwargs["max_tokens"] = cap

        warmed_once = False   # re-warm the endpoint at most ONCE per call (no ×retries)
        for attempt in range(config.MODEL_RETRIES + 1):
            last = attempt == config.MODEL_RETRIES
            try:
                t0 = time.time()
                resp = self._invoke(kwargs)   # non-streaming by default; streamed+reassembled when CODE_STREAM (specs/0043)
                latency_ms = (time.time() - t0) * 1000
            except Exception as e:
                # A 400 / context-window-exceeded is NOT transient: re-sending the same
                # oversized or malformed request just fails again. Fail FAST instead of
                # burning every retry (we watched a context overflow waste ~55s over 6
                # retries). Transient errors (timeout, 5xx, connection) still back off.
                if last or _non_retryable(e):
                    log.error("model call failed (%s%s): %s", type(e).__name__,
                              ", non-retryable" if _non_retryable(e) else ", retries exhausted",
                              str(e)[:200])
                    raise
                log.warning("model call %s (attempt %d/%d) — retrying", type(e).__name__,
                            attempt + 1, config.MODEL_RETRIES)
                self._backoff(attempt, type(e).__name__)
                continue

            msg = resp.choices[0].message
            # Dropped tool call (native mode): empty content AND no tool_calls — the
            # signature of a worker that went cold/scale-to-zero again MID-SESSION (not
            # just at startup). A short backoff (a few seconds) can't outwait a 30-60s
            # cold spin-up, which is how a turn ended in "(no output)". So re-absorb the
            # cold start the same way startup does — warm_up() waits for a real tool call
            # — then retry. Accept the empty response only on the final attempt.
            dropped = bool(schemas) and not (msg.content or "").strip() and not (msg.tool_calls or [])
            if dropped and not last:
                # First drop on a WARMABLE endpoint (CODE_API_BASE set): re-warm once — a
                # mid-session cold start. Re-running warm-up on every retry is what turned a
                # bad-endpoint turn into ~30 min of "cold worker" spam, hence once only.
                # Bedrock has no API_BASE to warm (warm_up is a no-op there), so don't claim
                # to — just back off and retry the transient empty response.
                if config.API_BASE and not warmed_once:
                    if config.VERBOSE:
                        print("  [retry] empty response (dropped tool call?) - re-warming the endpoint once")
                    warm_up()
                    warmed_once = True
                else:
                    self._backoff(attempt, "empty response (dropped tool call?)")
                continue

            tool_names = [t["function"]["name"] for t in schemas] if schemas else []
            self.traj.log_model_call(
                step, messages, tool_names,
                msg, getattr(resp, "usage", None), latency_ms,
                effort=self.effort or config.REASONING_EFFORT,   # the RESOLVED level this call ran at (0021)
            )
            return msg

    def _backoff(self, attempt, why):
        # Exponential with jitter, capped at config.BACKOFF_CAP. The jitter de-syncs
        # retries and the higher cap matters for serverless Bedrock, which throws bursts
        # of transient 503s ("ServiceUnavailableError") on large requests — a flat 8s cap
        # gives up before the burst clears. Pair with a higher CODE_MODEL_RETRIES.
        delay = min(2 ** attempt, config.BACKOFF_CAP) + random.uniform(0, 1)
        if config.VERBOSE:
            print(f"  [retry] {why} - attempt {attempt + 1}/{config.MODEL_RETRIES}, waiting {delay:.1f}s")
        time.sleep(delay)


def _fetch_context_length(model_id, api_base, api_key):
    """Best-effort GET {api_base}/models -> the served model's context_length (Together / vLLM expose it).
    Returns None on ANY failure. Stdlib urllib, no new dependency. The served id is the part after the
    litellm provider prefix (openai/<id> -> <id>)."""
    import json as _json
    import urllib.request
    served = model_id.split("/", 1)[1] if "/" in model_id else model_id
    try:
        # A browser User-Agent (specs/0054): some endpoints (Tinker) are fronted by Cloudflare, which
        # 403/1010-bans the default python-urllib UA on browser signature — the probe would silently fail and
        # the auto window would never resolve. A normal UA clears that check (litellm's httpx client passes for
        # the same reason). Harmless on endpoints without Cloudflare.
        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
            "Accept": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        req = urllib.request.Request(api_base.rstrip("/") + "/models", headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            data = _json.load(r)
        rows = data if isinstance(data, list) else data.get("data", [])
        for m in rows:
            mid = str(m.get("id", ""))
            if mid == served or mid.endswith("/" + served) or served.endswith(mid):
                cl = m.get("context_length") or m.get("context_window") or m.get("max_model_len")
                if cl:
                    return int(cl)
    except Exception:
        return None
    return None


def resolve_model_window():
    """Resolve the model's real context window when CODE_MODEL_MAX_TOKENS=auto (specs/0045); a NO-OP
    otherwise. NEVER raises — on any failure it leaves the 131072 pre-resolution fallback in place. Tries
    litellm's bundled model-info map first (offline, instant), then the OpenAI-compatible /models
    context_length. Called ONCE at startup BEFORE any ContextManager is built, so the compaction budgets
    (COMPACT_HARD_AT_TOKENS / SUMMARIZE_INPUT_MAX_TOKENS) derive from the true window."""
    if not config.MODEL_MAX_TOKENS_AUTO:
        return config.MODEL_MAX_TOKENS
    window = None
    try:
        info = litellm.get_model_info(config.MODEL)
        window = info.get("max_input_tokens") or info.get("max_tokens")
    except Exception:
        window = None
    if not window and config.API_BASE:
        window = _fetch_context_length(config.MODEL, config.API_BASE, config.API_KEY)
    if window:
        config._recompute_window_budgets(int(window))
        if config.VERBOSE:
            print(f"  [model] auto context window resolved: {config.MODEL_MAX_TOKENS}")
    elif config.VERBOSE:
        print(f"  [model] auto window unresolved — keeping fallback {config.MODEL_MAX_TOKENS}")
    return config.MODEL_MAX_TOKENS


def warm_up():
    """Absorb a scale-to-zero cold start ONCE, before the first task.

    A cold serverless worker (RunPod scale-to-zero) returns 200s with EMPTY
    tool_calls until it is fully warm — so the first real task would otherwise eat
    the cold start and likely burn its retries on those empty responses. This sends
    a throwaway tool-call probe and waits, the way openagent-infra absorbs a cold
    start on the first /chat call with a generous read timeout: keep probing until a
    real tool_call comes back (warm AND parser active), or the budget expires.

    Returns True if the endpoint warmed within budget (or warm-up is disabled / not
    applicable), False if it was still cold at the deadline. NEVER raises and is
    NEVER logged to a trajectory — this is infra warm-up, not an agent step. No-op
    when there is no remote endpoint (CODE_API_BASE empty, e.g. Bedrock).
    """
    if not config.WARMUP or not config.API_BASE:
        return True

    kw = {"model": config.MODEL, "temperature": config.TEMPERATURE,
          "timeout": config.REQUEST_TIMEOUT,
          "messages": [{"role": "user", "content": "Call the ping tool now."}],
          "tools": [{
              "type": "function",
              "function": {
                  "name": "ping",
                  "description": "Reply by calling ping to confirm tool-calling is active.",
                  "parameters": {"type": "object", "properties": {}},
              },
          }],
          "tool_choice": "auto"}
    if config.API_BASE:
        kw["api_base"] = config.API_BASE
    if config.API_KEY:
        kw["api_key"] = config.API_KEY
    kw.update(_reasoning_kwargs())   # provider-aware (bedrock top-level vs extra_body)

    start = time.time()
    deadline = start + config.WARMUP_BUDGET
    attempt = 0
    empties = 0       # CONSECUTIVE 200s with no tool_call
    hard_errors = 0   # CONSECUTIVE exceptions (500 / auth / connection)
    while True:
        try:
            resp = litellm.completion(**kw)
            if resp.choices[0].message.tool_calls:
                if config.VERBOSE:
                    print("  [warmup] endpoint warm - tool-calling active")
                return True
            # 200 with no tool_calls = cold/warming, OR a worker that won't emit tool
            # calls at all (serving / tool-parser issue). Count it; bail if it persists.
            empties += 1
            hard_errors = 0
            reason = "cold worker (empty tool_calls)"
        except Exception as e:
            hard_errors += 1
            empties = 0
            reason = f"endpoint error ({type(e).__name__})"

        # Bail FAST on a persistent failure instead of grinding the whole budget — neither
        # of these is a cold start that waiting fixes:
        #   - repeated exceptions  -> broken/misconfigured endpoint (a 500 never warms);
        #   - many empty responses -> the worker answers but won't emit a tool call.
        if hard_errors >= 3:
            if config.VERBOSE:
                print(f"  [warmup] {reason} x{hard_errors} - endpoint is erroring, not cold. "
                      "Check CODE_API_BASE (needs /v1), CODE_MODEL, and the worker. Proceeding.")
            return False
        if empties >= 40:
            if config.VERBOSE:
                print(f"  [warmup] still no tool call after {empties} probes - the worker answers but "
                      "isn't emitting tool calls (serving/tool-parser issue, not a cold start). Proceeding.")
            return False
        if time.time() >= deadline:
            if config.VERBOSE:
                print(f"  [warmup] not ready after {config.WARMUP_BUDGET:.0f}s ({reason}) - proceeding")
            return False
        attempt += 1
        # Throttle the log: a real cold start can take dozens of probes — don't print
        # one line per probe (that's what looked like an "endless loop").
        if config.VERBOSE and (attempt == 1 or attempt % 5 == 0):
            print(f"  [warmup] {reason} - waiting for spin-up ({int(time.time() - start)}s)")
        time.sleep(min(2 ** attempt, 8))
