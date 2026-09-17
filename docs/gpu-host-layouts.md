# GPU host layouts — model placement for 172.16.50.159

Owner: operations. Source: issue #570 (Workstream L, PR 1 of 4, enhancement E15),
grounded in the 2026-09-11 frontier audit (REPORT.md §12.5, gitignored) plus a
read-only live inventory of the host taken 2026-09-17 (see "Observed state"
below). This document describes **both candidate layouts as candidates** —
which one is "the" layout is an open maintainer decision (audit §11 Q11: is
the 8 GB card the reranker's dedicated home; Q12: does thinking stay remote or
move local). Nothing here selects one.

## The host

R640AI (`172.16.50.159`), verified by `nvidia-smi -L`:

| GPU index | Card | VRAM |
|---|---|---|
| 0 | NVIDIA RTX A1000 | 8 GB |
| 1 | NVIDIA RTX 2000E Ada Generation | 16 GB |
| 2 | NVIDIA RTX 2000E Ada Generation | 16 GB |

Compose-managed GPU services: `harrier-embed` (TEI embeddings) and `reranker`
(TEI reranking). Host-managed (outside compose): Ollama (if used locally) and
LM Studio / any instant-chat server.

## Observed state (2026-09-17, read-only inventory)

The host currently runs a de facto **Layout A** shape, achieved by hand-editing
the deployed compose (the working tree at `/home/afmostai/ragappv3` — at repo
revision 66bd172 — carries local modifications replacing both TEI `count: 1`
reservations with UUID-pinned `device_ids`):

| Service | GPU | Measured VRAM |
|---|---|---|
| reranker (TEI) | GPU 0 (A1000, 8 GB) | ~1.3 GB |
| harrier-embed (TEI) | GPU 1 (2000E, 16 GB) | ~1.3 GB |
| minicpm5-instant (llama-server container) | GPU 2 (2000E, 16 GB) | ~5.6 GB |
| Ollama | not running locally — thinking served remotely (`OLLAMA_CHAT_URL=http://172.16.50.41:8000`, `CHAT_MODEL=ChatGPTN` in the operator `.env`) | — |

This is an observation of one operator's current arrangement, not a
recommendation. The mechanism below replaces the hand edit with tracked
configuration.

## Candidate layouts

### Layout A — thinking remote, instant + embeddings on the 16 GB cards

| Role | GPU | Model class (example) | Approx. VRAM |
|---|---|---|---|
| Instant chat (LM Studio or server container) | one RTX 2000E (16 GB) | 4B–8B local model (deployed example: minicpm5-2b, ~5.6 GB) | 4–8 GB |
| Embeddings (`harrier-embed`) | the other RTX 2000E (16 GB) | microsoft/harrier-oss-v1-0.6b | ~1.3 GB |
| Reranking (`reranker`) | RTX A1000 (8 GB) | BAAI/bge-reranker-v2-m3 (or a small Qwen3-Reranker) | ~1.3 GB |
| Thinking chat | remote host (e.g. a DGX-class box serving via `OLLAMA_CHAT_URL`) | any size the remote host fits | 0 GB local |

Properties: maximizes local headroom (both 16 GB cards stay under ~50 %
duty); the 8 GB card carries the two light TEI services with room to spare;
thinking quality is bounded by the remote endpoint, not local VRAM. This is
the shape currently observed on the host.

### Layout B — thinking local on a 16 GB card

| Role | GPU | Model class (example) | Approx. VRAM |
|---|---|---|---|
| Thinking chat (Ollama) | one RTX 2000E (16 GB) | `gpt-oss:20b` (MXFP4) | ~14 GB |
| Instant chat | the other RTX 2000E (16 GB) | 4B–8B local model | 4–8 GB |
| Embeddings + reranking (both TEI services) | RTX A1000 (8 GB), shared | microsoft/harrier-oss-v1-0.6b + BAAI/bge-reranker-v2-m3 | ~2.6 GB combined; use reduced TEI batch limits |
| Thinking fallback | none — single-host layout | — | — |

Properties: full local autonomy (no remote dependency); the 16 GB card runs
near capacity (~14 of 16 GB), leaving little headroom for context spikes; the
8 GB card shares both TEI services, which couples embedding and reranking
throughput and calls for reduced TEI batch sizes.

Per audit grounding (REPORT.md §12.5 via issue #570): `gpt-oss:20b` at ~14 GB
MXFP4 is the named thinking candidate for Layout B; Ollama library sizes for
the other roles are as listed in README's chat-model table.

## Pinning mechanism (compose-managed services)

`docker-compose.gpu-pins.yml` pins both TEI services to specific cards via
`deploy.resources.reservations.devices.device_ids`, driven by environment
variables. Requires Docker Compose ≥ 2.24 (the `!override` tag; the live host
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
stable across hardware reordering; the hand-pinned deployment on this host
uses UUID form). Unset variables fail the compose command with an explicit
error instead of silently unpinning. Verify with:

```bash
EMBED_GPU_DEVICE_IDS=1 RERANK_GPU_DEVICE_IDS=0 \
  docker compose -f docker-compose.yml -f docker-compose.gpu-pins.yml config \
  | grep -A2 device_ids
```

Docker treats `count` and `device_ids` as mutually exclusive within one
device reservation; the override file replaces (not extends) the base
`count: 1` reservation, which is why it uses `!override`.

## Pinning mechanism (host-managed services)

**Ollama** runs on the host, outside compose:

- Single instance, all GPUs: run as-is; Ollama schedules layers across cards.
- Single instance, specific GPUs: `CUDA_VISIBLE_DEVICES=1` (systemd drop-in
  `Environment=` or shell export) restricts it to the listed cards.
- One instance per GPU (Layout B's strongest form: an instant/small model on
  one card, the thinking model on another): start one `ollama serve` per card
  with distinct `OLLAMA_HOST` ports and per-instance `CUDA_VISIBLE_DEVICES`,
  e.g. `OLLAMA_HOST=127.0.0.1:11434 CUDA_VISIBLE_DEVICES=2 ollama serve` for
  the thinking card; point the app's `OLLAMA_CHAT_URL` at the instance that
  hosts `CHAT_MODEL`.

**LM Studio** (or any instant-chat server): enable/disable GPUs per model in
the app's GPU controls (Settings → GPU, or the per-model "GPU offload"
toggle), and set the model's GPU priority so a small model stays on the
intended card. LM Studio's server port is what `INSTANT_CHAT_URL` points at;
the container form (as deployed on this host: a `llama-server` container) can
instead use the same compose `device_ids` mechanism as the TEI services.

## Fresh-install default

Independent of layout choice, the shipped `CHAT_MODEL` default
(`llama3.2:latest`, ~4 GB) is pullable by INSTALLATION.md's own `ollama pull`
step, so a fresh install without an explicit override starts working on any
single GPU. Operators adopting either layout set `CHAT_MODEL` (and, for
Layout B, `OLLAMA_CHAT_URL`) explicitly in `.env` — the shipped default never
overrides an explicit setting.
