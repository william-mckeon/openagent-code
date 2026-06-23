"""
openagent-code — a self-hosted coding agent instrumented for training data.

Part of the OpenAgent family, but deliberately standalone: it does not join the
openagent-network and depends on no other OpenAgent service. It brings its own
model gateway (LiteLLM -> any OpenAI-compatible / BYOC endpoint) and writes its
own training-data capture as local JSONL.

Four layers, each a stable interface to the next:
  1. serving       your model behind an OpenAI-compatible endpoint (vLLM / Bedrock)
  2. model.py      LiteLLM gateway — swap providers via CODE_* env, never code
  3. tools.py      the tool boundary — ergonomics that make the agent proficient
                   AND emit ok/fail + retry signal
  4. trajectory.py every session -> schema-versioned JSONL (the flywheel fuel)
"""

__version__ = "0.6.0"
