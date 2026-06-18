"""
src/model.py

Model gateway — the swappable boundary.

The harness calls ONLY this. Everything below (RunPod vLLM, Bedrock, OpenRouter,
a local Ollama) is a CODE_* env change, never a code change. That is the point
of routing through LiteLLM: the data-sovereignty choice stays a one-line swap.

CODE_MODEL / CODE_API_BASE examples (see src/config.py and .env.example):
  RunPod / self-hosted vLLM (gpt-oss-120b):
    CODE_MODEL=openai/gpt-oss-120b
    CODE_API_BASE=https://<your-pod>-8000.proxy.runpod.net/v1
  AWS Bedrock:
    CODE_MODEL=bedrock/openai.gpt-oss-120b-1:0
    CODE_API_BASE=            # unset; Bedrock uses AWS_* credentials
"""
import time

import litellm

from . import config
from .prompts import SUMMARIZE_PROMPT


class Model:
    def __init__(self, trajectory):
        self.traj = trajectory

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
        if config.REASONING_EFFORT:
            # extra_body lands it verbatim in the request the endpoint receives.
            kw["extra_body"] = {"reasoning_effort": config.REASONING_EFFORT}
        return kw

    def summarize(self, messages):
        """Compress older turns into a briefing for the ContextManager.

        Deliberately does NOT call log_model_call — a compaction summary is not an
        agent step. The ContextManager logs a `compaction` record instead. No tools.
        """
        rendered = "\n\n".join(
            f"[{m.get('role')}] {m.get('content') or ''}" for m in messages
        )
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

        warmed_once = False   # re-warm the endpoint at most ONCE per call (no ×retries)
        for attempt in range(config.MODEL_RETRIES + 1):
            last = attempt == config.MODEL_RETRIES
            try:
                t0 = time.time()
                resp = litellm.completion(**kwargs)
                latency_ms = (time.time() - t0) * 1000
            except Exception as e:
                if last:
                    raise
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
                # First drop: re-warm once (a mid-session cold start). After that, a short
                # backoff — re-running the full warm-up on every retry is what turned a
                # bad-endpoint turn into ~30 minutes of "cold worker" spam.
                if not warmed_once:
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
            )
            return msg

    def _backoff(self, attempt, why):
        delay = min(2 ** attempt, 8)
        if config.VERBOSE:
            print(f"  [retry] {why} - attempt {attempt + 1}/{config.MODEL_RETRIES}, waiting {delay}s")
        time.sleep(delay)


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
    if config.REASONING_EFFORT:
        kw["extra_body"] = {"reasoning_effort": config.REASONING_EFFORT}

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
