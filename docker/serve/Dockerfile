# =============================================================================
# openagent-code - serving image  (distillation Stage 6, specs/0005)
# =============================================================================
# Serve the merged student on vLLM, which gives an OpenAI-compatible /v1 endpoint AND
# parses NATIVE tool-calls (--tool-call-parser) the way the student was trained. The agent
# then reaches it through the SAME CODE_API_BASE boundary as Bedrock — zero src/ changes.
#
#   docker compose up serve            # serves train/checkpoints/student-merged on :8000
# then in .env:  CODE_MODEL=openai/student   CODE_API_BASE=http://localhost:8000/v1
#
# The serve command + flags live in docker-compose.yml (model path, tool-call parser,
# dtype, max-len) so they're easy to tweak without rebuilding.
#
# Blackwell note (RTX 50, sm_120): vLLM needs a build with cu128/sm_120 kernels. If this
# tag won't run on the 5080, bump VLLM_TAG to a newer release. If vLLM still won't cooperate
# on Blackwell, the documented fallback is a transformers-based server (train/README.md) run
# from the docker/train image (we've already proven torch cu128 works on the card) with the
# agent in CODE_TOOL_MODE=json.
# =============================================================================
ARG VLLM_TAG=latest
FROM vllm/vllm-openai:${VLLM_TAG}

# The vllm/vllm-openai image's entrypoint IS the OpenAI API server; compose supplies the args.
# (Merged model mounted at /checkpoints; HF cache mounted for any tokenizer fetch.)
EXPOSE 8000
LABEL org.opencontainers.image.title="openagent-code serve" \
      org.opencontainers.image.description="vLLM OpenAI-compatible server for the distilled student (Stage 6)"
