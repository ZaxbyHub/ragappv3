# GPU host layouts — model placement for 172.16.50.159

Owner: operations. Source: issue #570 (Workstream L, PR 1 of 4, enhancement
E15) plus read-only live inventories of the R640 (2026-09-17) and its
off-box thinking host. The app itself ships **no default** chat model or
endpoint (issue #570 re-scope): every chat role is operator-configured, so
this guide is about placing *your* inference, not prescribing a model.

## The host

R640AI (`172.16.50.159`), verified by `nvidia-smi -L`:

| GPU index | Card | VRAM |
|---|---|---|
| 0 | NVIDIA RTX A1000 | 8 GB |
| 1 | NVIDIA RTX 2000E Ada Generation | 16 GB |
| 2 | NVIDIA RTX 2000E Ada Generation | 16 GB |

Compose-managed GPU services: `harrier-embed` (TEI embeddings) and `reranker`
(TEI reranking). Host- or off-box-managed: whatever serves your chat models
(Ollama, LM Studio, llama-server, vLLM — any OpenAI-compatible endpoint).

## Layout A — the deployed layout (observed state, 2026-09-17)

This is what the R640 actually runs today, verified by a read-only
inventory. Thinking runs **off-box**; the local cards carry the low-latency
and embedding roles:

| Role | Where | Model (observed) | Measured VRAM |
|---|---|---|---|
| Thinking chat | off-box vLLM at `172.16.50.41:8000` (512K context) | `ChatGPTN` (also serves `deepseek-v4-flash-vision-exp`) | 0 GB local |
| Instant chat | llama-server container on GPU 2 (2000E) | `MiniCPM5-2B` (Q4_K_M) | ~5.6 GB |
| Embeddings (`harrier-embed`) | GPU 1 (2000E) | `microsoft/harrier-oss-v1-0.6b` (TEI) | ~1.3 GB |
| Reranking (`reranker`) | GPU 0 (A1000, 8 GB) | `BAAI/bge-reranker-v2-m3` (TEI) | ~1.3 GB |
| Editorial drafts | off-box (`qwen38-27b-aggressive`) | served from the 172.16.50.41 / LAN endpoints | — |

Properties: the 8 GB card comfortably carries both TEI services; each 16 GB
card has generous headroom; thinking quality is bounded by the off-box
endpoint, not local VRAM. This is a description of the current deployment —
a starting point for planning, not a mandate.

## Layout B — fully local alternative

For single-host operation with no off-box inference, the same cards can
carry everything, with a local thinking model taking one 16 GB card:

| Role | GPU | Model class (current families) | Approx. footprint |
|---|---|---|---|
| Thinking chat | one RTX 2000E (16 GB) | `gemma4:26b` (MoE, 4B active) at reduced context, or `gemma4:12b` at full 256K | 19 GB download needs reduced ctx on 16 GB; 12b ≈ 7.6 GB |
| Instant chat | the other RTX 2000E (16 GB) | small current model, e.g. `minicpm5-2b` | ~2–6 GB by quant |
| Embeddings + reranking (both TEI) | RTX A1000 (8 GB), shared | unchanged bundled models | ~2.6 GB combined; use reduced TEI batch sizes |

Properties: full autonomy, no remote dependency; the 16 GB thinking card
runs near capacity; sharing the 8 GB card couples embedding and reranking
throughput.

## Pinning mechanism (compose-managed services)

`docker-compose.gpu-pins.yml` pins both TEI services to specific cards via
`deploy.resources.reservations.devices.device_ids`, driven by environment
variables. Requires Docker Compose ≥ 2.24 (the `!override` tag; the R640
runs 2.40.3):

```bash
# Layout A example (embed on GPU 1, rerank on the 8 GB GPU 0):
EMBED_GPU_DEVICE_IDS=1 RERANK_GPU_DEVICE_IDS=0 \
  docker compose -f docker-compose.yml -f docker-compose.gpu-pins.yml up -d

# Layout B example (both TEI services share the 8 GB GPU 0):
EMBED_GPU_DEVICE_IDS=0 RERANK_GPU_DEVICE_IDS=0 \
  docker compose -f docker-compose.yml -f docker-compose.gpu-pins.yml up -d
```

Values are GPU indexes or full UUIDs from `nvidia-smi -L` (`GPU-<uuid>` —
stable across hardware reordering; the deployed R640 pins use UUID form).
Unset variables fail the compose command with an explicit error instead of
silently unpinning. Docker treats `count` and `device_ids` as mutually
exclusive within one device reservation; the override file replaces (not
extends) the base `count: 1` reservation, which is why it uses `!override`.

The deployed R640 achieved its current pinning by hand-editing the local
compose (uncommitted `M docker-compose.yml` on the host, verified 2026-09-17
at repo revision 66bd172); this file replaces that hand edit with tracked
configuration.

## Pinning mechanism (host- or off-box-managed services)

**Ollama** (if you run it on the R640 itself):

- Single instance, all GPUs: run as-is; Ollama schedules layers across cards.
- Single instance, specific GPUs: `CUDA_VISIBLE_DEVICES=1` (systemd
  drop-in or shell export) restricts it to the listed cards.
- One instance per card: start one `ollama serve` per card with distinct
  `OLLAMA_HOST` ports and per-instance `CUDA_VISIBLE_DEVICES`, and point
  `OLLAMA_CHAT_URL` at the instance hosting your `CHAT_MODEL`.

**LM Studio / llama-server** (instant-class servers): enable/disable GPUs
per model in the app's GPU controls and set model GPU priority; the server's
port is what `INSTANT_CHAT_URL` points at. A containerized server (as the
R640's MiniCPM5 instant container) can use the same compose `device_ids`
mechanism as the TEI services.

**Off-box vLLM** (the deployed thinking arrangement): nothing to pin
locally — point `OLLAMA_CHAT_URL` at the remote endpoint (e.g.
`http://172.16.50.41:8000`) and set `CHAT_MODEL` to the served model id
(observed: `ChatGPTN`, 512K context). The app talks plain
OpenAI-compatible `/v1/chat/completions`, so any remote API works the same
way.

## Fresh installs

There is no default to size for: a fresh install boots with chat
unconfigured (chat requests answer 409 with setup guidance until
`OLLAMA_CHAT_URL` + `CHAT_MODEL` are set — env, or Settings → Models, which
activates the client live without a restart). Embeddings and reranking work
out of the box via the bundled TEI containers. Model suggestions for local
serving live in README's models section; the placement math for this host
is the two layouts above.
