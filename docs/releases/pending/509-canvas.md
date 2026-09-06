# Code & document canvas (Issue #509)

## What changed

### Backend
- **Versioned canvas artifacts** — new additive storage (`canvas_artifacts`, `canvas_versions` via
  `migrate_add_canvas_tables`; fresh and existing databases both receive them) with stable public
  artifact IDs (`cav_…`), per-artifact version chains (`UNIQUE(artifact_id, version_no)`),
  sha256 content hashes, named versions, and bounded origins (`created`, `user_edit`,
  `model_edit`, `restore`). Restores append new revisions — history is never rewritten.
- **Canvas API** (`backend/app/api/routes/canvas.py`, registered under `/api`):
  - `POST /chat/sessions/{id}/artifacts` / `GET /chat/sessions/{id}/artifacts` — create/list
    canvas artifacts from chat answers (independent identities; duplicate names allowed).
  - `GET /canvas/capabilities` — fail-closed feature surface for the frontend.
  - `GET /canvas/artifacts/{uid}` (+ `/versions`, `/versions/{n}`) — artifact and exact-version reads.
  - `POST /canvas/artifacts/{uid}/versions` — save edits (optimistic concurrency: stale
    `base_version_no` → `409 canvas_version_conflict`; `force` appends without losing history).
  - `POST /canvas/artifacts/{uid}/restore` — restore a historical version as a new revision.
  - `POST /canvas/artifacts/{uid}/edit-range` — targeted model edit of a selected line range; only
    the selected lines can change (server-side splice), range/instruction validated, model client
    taken from app state, `502 canvas_model_unavailable` on provider failure.
  - `GET /canvas/artifacts/{uid}/versions/{n}/download` — exact bytes of the selected version with
    provenance headers; `GET /canvas/artifacts/{uid}/export` — JSON manifest carrying the source
    snapshot and originating session/turn/message references.
- **Configuration** — `CANVAS_ENABLED` (default `true`) and `CANVAS_MAX_ARTIFACT_KB`
  (default `512`) in `backend/app/config.py`, mirrored in `.env.example` and `docker-compose.yml`
  and enrolled in the config-contract check. Rollback: set `CANVAS_ENABLED=false` (all canvas
  routes return 503; frontend entry points hide).
- **Authorization** — every route enforces vault policy via the same `evaluate(...)` mechanism as
  chat; mutations sit behind CSRF; create/edit-range are rate-limited; message lineage is
  validated (`404` when the message is not in the session).

### Frontend
- **Canvas workspace** — new `/chat/:sessionId/canvas/:artifactUid` route: plain-textarea editor
  with read-only preview for supported formats (code via Shiki; markdown via sanitized
  react-markdown; anything else explicitly labeled unsupported), version rail with origin badges,
  two-version diff (existing `diff` dependency), restore, named versions, download of exactly the
  selected version, and manifest export.
- **Chat entry points** — "Open in canvas" on generated code blocks and "Open as document" in
  assistant message actions, both capability-gated (hidden until the backend reports canvas
  enabled; visible only when the answer carries content). Citation markers inside code are
  preserved verbatim.
- **Unsaved-edit protection** — drafts persist to localStorage (best-effort, try/catch), rehydrate
  on reload with a notice, and are guarded against navigation (beforeunload + in-app link
  interception); server conflicts surface a banner with reload/overwrite choices. Chat streaming
  cannot touch canvas state; canvas saves never modify chat messages.
- **Known behaviors** — targeted model edits can be stopped from the edit dialog (client-side
  abort; a server-side call that already finished still records its guarded version). Canvas
  artifacts belong to their originating session: forks start with a clean canvas, and each
  "Open in canvas" creates an independent artifact; reopen an existing artifact via its durable
  `/chat/{sessionId}/canvas/{artifactUid}` URL.

### Docs & config surfaces
- `.env.example`, `docker-compose.yml`: `CANVAS_ENABLED`, `CANVAS_MAX_ARTIFACT_KB`.
- `scripts/check_config_contract.py`: new `canvas_*` settings blocks enforcing parity.

## Security posture
- No new unauthenticated surface: all canvas endpoints require an authenticated user, vault-policy
  authorization (read for views/downloads, write for edits), CSRF on mutations, and rate limits on
  artifact creation and model-edit calls.
- Model calls for targeted edits send only the selected lines plus the user instruction — never
  the whole artifact or unrelated conversation content.
- Artifact size is bounded (`CANVAS_MAX_ARTIFACT_KB`); SQL is fully parameterized; download
  filenames are sanitized.
- Existing safeguards unchanged: chat routes, evidence handling, and Draft Room gates are
  untouched by this feature.
