# Provider-native thinking controls and surfaced reasoning (Issue #554)

## What changed

- The thinking client (Ollama) now sends an explicit reasoning control,
  `reasoning_effort: "high"`, on `/v1/chat/completions`. Ollama's
  OpenAI-compatibility documentation lists `reasoning_effort`
  (high/medium/low/max/none) as the supported request field on that
  endpoint and documents no `think` field there (`think` is native
  `/api/chat`-only). Deployments that do not recognize the field are
  expected to ignore it (standard JSON-decoding behavior; derived from
  Ollama's documentation, not verified against a live pre-`reasoning_effort`
  deployment) and keep the provider default.
- The instant client's no-think control is now selected by the configured
  `instant_chat_model` family instead of being hard-coded for every model:
  Qwen-family names send `chat_template_kwargs={'enable_thinking': False}`,
  and unrecognized families (including the nemotron default, whose card
  names no template mechanism) log a warning and send no control — fail
  open, never raise. `instant_enable_thinking=True` still means "send no
  control" (issue #494 semantics). The settings hot-rebind re-runs the same
  family selection, so a runtime model swap installs or removes the control.
- Provider reasoning deltas (`reasoning`/`reasoning_content` on streamed
  chunks) are no longer silently discarded. They ride a distinct typed
  channel from `LLMClient.chat_completion_stream`, surface as a new
  additive `reasoning_delta` SSE event, and never enter the answer-content
  channel or the think-tag filter — reasoning embedded in `<think>` tags
  inside content is still stripped exactly as before.
- `llm_metrics` on the done event gains `reasoning_tokens_estimate`
  (chars//4 estimate) and `reasoning_duration_ms` (first-to-last reasoning
  delta).
- The chat UI renders a collapsible "Thinking for Ns" block with the
  reasoning duration and token estimate, collapsed by default, delivered
  through a typed message-parts model (`frontend/src/lib/messageParts.ts`,
  text/reasoning/source part kinds) instead of another ad-hoc message
  field.
- Factory docstrings now name the backends the clients actually talk to by
  default (thinking: Ollama; instant: LM Studio), replacing stale
  gpt-oss-120b / DGX Spark / Gemma-4 claims.

## Rollout and rollback

No schema change and no new settings keys. The new SSE event type is
additive: old frontends drop it like any unknown event, and consumers
without an `onReasoning` handler are unaffected. Reasoning is transient
stream state — it is not persisted to session rows, so a reload shows the
answer without the thinking block (bounded by the issue's no-schema-change
rollout contract). Revert the commit to roll back; reasoning returns to
being discarded with no data migration.
