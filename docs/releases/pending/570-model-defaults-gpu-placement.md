# No shipped chat models, GPU pinning, chunk-docstring fix (Issue #570)

## What changed

### The system ships no default chat model or endpoint

`CHAT_MODEL`, `INSTANT_CHAT_MODEL`, `OLLAMA_CHAT_URL`, and
`INSTANT_CHAT_URL` now default to empty everywhere (backend Settings,
`.env.example`, compose interpolation). Previously the shipped thinking
default was `gemma-4-26b-a4b-it-apex` — a community quantization no
documented install step ever obtained, so a fresh install following
INSTALLATION.md ended with an unresolvable model name. The re-scoped fix
removes the fiction entirely rather than substituting another model:
operators point Meridian at their own inference — Ollama, LM Studio, vLLM,
llama-server, or a remote OpenAI-compatible API — because most deployments
do not co-locate the app with inference (the documented host itself runs
thinking on a separate vLLM box).

"Unconfigured" is now a designed mode:

- the app boots cleanly with chat clients absent; embeddings/reranking work
  immediately via the bundled TEI containers;
- chat requests answer **409** with setup guidance ("configure it in
  Settings → Models") instead of failing on a garbage model name;
- configuring an endpoint pair in Settings constructs and wires the missing
  client **live, no restart** (the client's transport starts lazily on the
  first request; keep-alive tasks resume on the next restart);
- the model health checker reports `not_configured` distinctly from
  unreachable, and draft jobs fail with the same actionable message rather
  than sending a fabricated model name.

Operator impact: fresh installs configure endpoints at setup (env vars or
Settings → Models) before chat works; anyone with explicit values in
`.env` — including the deployed host, which sets its own thinking endpoint
and model — is unaffected. A first-setup wizard step that walks new
operators through endpoint selection is tracked as #622.

### GPU placement is now expressible: `docker-compose.gpu-pins.yml`

Both TEI services (`harrier-embed`, `reranker`) reserved `count: 1` GPUs
with no `device_ids`, so a multi-GPU host could not pin them; the operator
of the documented 2× RTX 2000E + RTX A1000 host had hand-edited its
deployed compose to UUID-pin its GPU services. The new opt-in override
file pins both services through `EMBED_GPU_DEVICE_IDS` /
`RERANK_GPU_DEVICE_IDS` (index or `GPU-<uuid>`; requires Compose ≥ 2.24 for
the `!override` tag — plain overrides append a second reservation instead
of replacing):

    EMBED_GPU_DEVICE_IDS=1 RERANK_GPU_DEVICE_IDS=0 \
      docker compose -f docker-compose.yml -f docker-compose.gpu-pins.yml up -d

Unset variables fail the command explicitly; without the file the default
render is byte-identical to before (`count: 1`, unpinned).

### Host-layout guide and refreshed model guidance

`docs/gpu-host-layouts.md` documents the deployed layout with the real
served models (off-box vLLM thinking at 172.16.50.41:8000 with 512K
context; MiniCPM5-2B instant on a 16 GB card; both TEI services pinned),
the fully-local alternative layout with current family sizing
(gemma4:26b/12b, qwen3.5, minicpm5), the compose pinning mechanism, and
host-side Ollama/LM Studio placement notes. README's models section is
current-2026 guidance framed as operator choice — nothing is presented as
a shipped default.

### Chunk-size docstrings corrected

`Settings.chunk_size_chars` / `chunk_overlap_chars` docstrings said 1200/120
while the effective defaults are 2000/200 (`.env.example` + field
validators, pinned by tests). The docstrings now state 2000/200, and a new
docstring-vs-effective-value test keeps them from drifting apart again.

### Doc checker hardened

`scripts/check_installation_doc.py`'s pull-model extraction no longer
counts commented-out `ollama pull` lines as provisioning, and
`scripts/check_config_contract.py` now enforces the emptiness contract
across every default-bearing surface (backend Settings, `.env.example`,
compose) so no model default can silently return.

## Verification

- Frozen acceptance checks C1–C13: discriminating checks RED at base
  66bd172 and GREEN at head (empty-default contract, unconfigured-mode
  behavior tests, GPU-pins render, docs content); preserving checks green
  throughout (default compose render, scope fences, ruff + module suites).
- Unconfigured-mode behavior pinned by `backend/tests/test_unconfigured_models.py`
  (factory guards, RAGEngine None construction, live rebind activation
  through the real engine, chat 409, checker status).
- Blast radius green: test_chat_mode, test_llm_integration,
  test_llm_thinking_controls, test_settings, test_ssrf_services,
  test_config realigned to the new contract and passing.
- `docker compose config` renders `device_ids: ['<env>']` under both TEI
  services with the override + vars set; the original `count: 1` twice
  with zero `device_ids` without them. Live-host facts verified by
  read-only SSH inventory (2026-09-17).

## Rollback

Revert the commit. No database, vector-store, or embedding-identity surface
is involved; operators who adopted `docker-compose.gpu-pins.yml` drop the
`-f` flag. Rolling back the empty defaults restores the old out-of-box
behavior only for installs that never set the variables.
