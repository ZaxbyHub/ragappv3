# Resolvable chat-model default, GPU pinning, chunk-docstring fix (Issue #570)

## What changed

### `CHAT_MODEL`'s default is now a model the documented install actually pulls

The shipped default `chat_model` was `gemma-4-26b-a4b-it-apex` — a community
GGUF quantization that no script, Modelfile, or install step in this
repository ever obtained. A fresh install following INSTALLATION.md's own
`ollama pull` step ended with `llama3.2` pulled and `CHAT_MODEL` pointing at
a name Ollama could not resolve, so every Thinking-mode request failed on
model resolution. The default is now `llama3.2:latest` (the value the guide's
pull step provisions and README's quickstart already set), moved in lockstep
across every mirror: `backend/app/config.py`, the `docker-compose.yml`
interpolation, `.env.example`, `INSTALLATION.md`, `README.md`,
`docs/release.md`, the `create_thinking_client` docstring, and the settings
UI placeholder. `backend/tests/test_config.py` pins the new default, and
`scripts/check_config_contract.py` now enforces that the default appears
among INSTALLATION.md's active `ollama pull` lines — this check fails on the
old tree and cannot silently regress.

Operator impact: only installs whose `.env` never sets `CHAT_MODEL` change
behavior — a fresh container without an explicit override now requests
`llama3.2:latest` (resolvable, ~4 GB) instead of an unresolvable name.
Anyone with `CHAT_MODEL` set in `.env` (including the deployed host at
172.16.50.159, which sets `CHAT_MODEL=ChatGPTN`) is unaffected: compose
default-interpolation and the pydantic default both apply only when the
variable is absent.

### GPU placement is now expressible: `docker-compose.gpu-pins.yml`

Both TEI services (`harrier-embed`, `reranker`) reserved `count: 1` GPUs
with no `device_ids`, so on a multi-GPU host (the documented 2× RTX 2000E
Ada 16 GB + RTX A1000 8 GB machine) neither container could be pinned to a
specific card — the operator of that host had hand-edited its deployed
compose to UUID-pin all three GPU services. The new opt-in override file
pins both TEI services through `EMBED_GPU_DEVICE_IDS` /
`RERANK_GPU_DEVICE_IDS` (index or `GPU-<uuid>` form; requires Compose ≥ 2.24
for `!override`):

    EMBED_GPU_DEVICE_IDS=1 RERANK_GPU_DEVICE_IDS=0 \
      docker compose -f docker-compose.yml -f docker-compose.gpu-pins.yml up -d

Unset variables fail the command explicitly. Without the file, behavior is
byte-identical to before (`count: 1`, unpinned) — single-GPU hosts need to
change nothing.

### Host-layout guide and refreshed model table

`docs/gpu-host-layouts.md` documents the host's GPU inventory, both candidate
layouts (A: thinking remote, instant + embeddings on the 16 GB cards, reranker
on the 8 GB card; B: local `gpt-oss:20b` thinking on a 16 GB card, both TEI
services sharing the 8 GB card) with per-role model choices and approximate
VRAM footprints, the compose pinning mechanism, and host-side Ollama
(`CUDA_VISIBLE_DEVICES`, per-GPU instances on separate `OLLAMA_HOST` ports)
and LM Studio (per-model GPU enable/priority) placement notes. Neither layout
is presented as chosen — that selection is the open §11 Q11/Q12 maintainer
decision. README's chat-model table now lists only models that fit the stated
16/16/8 GB host (`llama3.2:latest` ~4 GB, `gpt-oss:20b` ~14 GB,
`mistral:latest` ~8 GB), replacing the 2024-era `qwen2.5:32b/72b` rows that
needed ~22/~45 GB.

### Chunk-size docstrings corrected

`Settings.chunk_size_chars` / `chunk_overlap_chars` docstrings said "Default
1200 chars / 120 chars" while the effective defaults are 2000/200 (set by
`.env.example` and the field validators, pinned by tests). The docstrings now
state 2000/200, and a new docstring-vs-effective-value test keeps them from
drifting apart again.

### Doc checker hardened

`scripts/check_installation_doc.py`'s pull-model extraction no longer counts
commented-out `ollama pull` lines as provisioning (the old regex was why the
unpullable default passed the gate for months).

## Verification

- `scripts/check_config_contract.py` — new CHAT_MODEL assertions fail on the
  pre-fix tree (default not pullable) and pass after.
- `docker compose config` renders `device_ids: ['<env>']` under both TEI
  services with the override + vars set; renders the original `count: 1`
  twice with zero `device_ids` without them.
- `backend/tests/test_config.py` full module + `ruff check .` green.
- Live-host facts in the guide verified by read-only SSH inventory
  (2026-09-17).

## Rollback

Revert the commit. No database, vector-store, or embedding-identity surface
is involved; operators who adopted `docker-compose.gpu-pins.yml` simply drop
the `-f` flag.
