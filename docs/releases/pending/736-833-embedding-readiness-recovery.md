# Embedding-model changes can no longer silently degrade retrieval, and the app can recover in-app (#736, #833)

Issues: #736 (Workstream G PR 4), #833.

## What changed

**Ingest gating (issue #736 AC2).** New uploads, directory scans, and
watcher-discovered files can no longer write new-model vectors into an
old-model vector index, and can no longer be silently discarded by a staged
rebuild's cutover:

- `require_ingest_ready` (`backend/app/api/deps.py`) — a new route
  dependency on both upload routes (`POST /documents` and
  `POST /documents/upload`), `POST /documents/scan`, and
  `POST /documents/{file_id}/retry-chunks` and
`POST /documents/admin/retry/{file_id}`. It refuses with 503
  "Embedding model mismatch — admin reindex required before ingesting
  documents" when the vector store's readiness gate is down, and 503
  "Document reindex in progress — retry when it completes" while a staged
  rebuild is in flight.
- Processor-level backstops at every ordinary-ingest vector-write seam —
  the three document-chunk seams (`backend/app/services/
  document_processor.py`: process_file, process_existing_file's upload arm,
  and the failed-chunk retry) plus the enrichment atom-proxy write
  (`backend/app/services/background_tasks.py` `_write_atom_proxies`): each
  refuses the vector write under either gate condition. The document-chunk
  seams settle the file row to the new stable error code
  `EMBEDDING_MODEL_MISMATCH` (registered in `_INGEST_ERROR_REASONS`; the
  all-vault reindex's selection re-processes errored rows, so the outcome
  is recoverable); the enrichment seam takes the same gate check and its
  bounded retry re-reaches the work after recovery. Reindex-context work is
  exempt — staged rebuild targets and the all-vault in-place recovery keep
  writing.

**Empty-index recovery (final-critic round).** A zero-file all-vault
reindex no longer early-returns before the guarded identity-record and
readiness lift: a fresh install whose admin sets the embedding model
before the first upload (or a deployment that emptied its index) can
recover in-app instead of being locked out by the now-gated uploads.

**Vault-scoped completions never lift a down gate (issue #736 AC7).** A
vault-scoped reindex completing while the embedding-mismatch gate is down no
longer records a validating identity or marks the store ready over the
other vaults' old-model vectors (previously deterministic — not a race). In
the steady state (gate already up) scoped completions keep today's behavior.
The readiness lift is now epoch-guarded and mutually exclusive with the
settings-save invalidation (`readiness_guard` /
`readiness_epoch` in `backend/app/services/vector_store.py`): a settings
save that lands during a reindex completion's metadata write can no longer
be overwritten by a stale `mark_ready(True)`.

**Metric-aware distance threshold (issue #736 AC3).**
`DocumentRetrievalService.filter_relevant`
(`backend/app/services/document_retrieval.py`) now applies
`2 × MAX_DISTANCE_THRESHOLD` when `VECTOR_METRIC=l2` (LanceDB l2 is
squared-L2, exactly 2× the cosine-equivalent on unit vectors), instead of
comparing raw squared-L2 distances against the cosine-calibrated bound and
dropping results sitting at the calibrated center. `cosine` (the default)
and `dot` behavior is unchanged, as is the score-tier floor.

**In-app recovery wiring (issue #833).** The Maintenance tab now has an
"Embeddings recovery" card with a "Rebuild embeddings (all-vault reindex)"
action (dialog-confirmed `POST /documents/reindex`, always all-vault) and
copy stating the recovery contract: an embedding-mismatch 503 requires an
ALL-VAULT reindex run to COMPLETION, and restarting does not clear it. The
SettingsPage "Reindex required" banner no longer points at reprocess /
wiki-recompile paths that never restore readiness, and
`ReindexConfirmDialog`'s false claims are replaced: when an identity
field (model/prefixes) changed, the copy states the gated truth (queries
and uploads pause until the all-vault reindex completes; restarting does
not clear it); for chunking/vector-only changes it keeps the accurate
stale-embeddings wording without claiming a 503 (identity-conditional
copy, new `IDENTITY_REINDEX_FIELDS` + `reindexIdentityChanged`).

## Notes for operators

- After changing `EMBEDDING_MODEL` (or an embedding prefix) at runtime:
  chat/search/memories/wiki 503 and uploads pause until an all-vault
  reindex completes — start it from Settings → Maintenance → Embeddings
  recovery. Restarting does not clear the gate.
- Files ingested during a gated window settle to
  `EMBEDDING_MODEL_MISMATCH` errors; the all-vault reindex re-processes
  them automatically.

## Testing

Frozen checks C1–C9 (test_g04_embedding_readiness_and_metric.py +
MaintenanceSettings.reindex.test.tsx) plus direct coverage in
test_g04b_ingest_gating.py (route/alias/scan/retry-chunks gating, backstop
combination matrix, watcher/legacy path, staged-rebuild and all-vault
recovery preservation legs, both interleaving races plus the
epoch-capture-window leg, production-path l2 threshold).
