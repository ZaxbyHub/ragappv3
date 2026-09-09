# Unified document upload, organization and responsive library (Issue #514)

## What changed

### User-visible behavior

- **Bounded, decoupled upload transfers.** Chat and document uploads share one store-backed
  transfer pool (`UPLOAD_CONCURRENCY = 3`): a slot is held only while bytes are in flight,
  so a slow first document no longer holds the next transfer behind indexing or Wiki work.
  Upload monitoring is one batched status poll (attempt-ordered, terminal states stick) that
  stops completely when you navigate away and resumes if you come back — no orphaned
  pollers.
- **One batched status endpoint.** New `GET /documents/status?ids=1,2,3` (cap 100 ids)
  returns per-file status including searchable/Wiki/KMS state with per-id errors; the old
  per-file status endpoint is otherwise unchanged (it gains additive `partial_embeddings`
  and `extraction_diagnostics` fields), so existing clients keep working. The client
  pages id sets larger than one request through multiple batches automatically.
- **Accurate status, no contradictions.** A late or out-of-order response can no longer
  overwrite newer search results, document details, or attachment states; Wiki status keeps
  refreshing until a compile finishes even when the document list doesn't change; C2's
  `partial` ingest status now shows as "Partial" with its failure description instead of
  "Unknown".
- **Reliable library organization.** Bulk delete reconciles with the server (failed rows
  stay, totals and stats match the list, and the authoritative list is refetched); deleting
  a folder subtree clears a stale folder filter; select-all reflects the visible rows
  (with an indeterminate state); folder moves are transactional, so concurrent moves can no
  longer create a cycle that hides folders.
- **Mobile and responsive library.** Mobile document cards now open `/documents/<id>` from
  the filename; desktop navigation keeps its highlight on document/KMS detail routes; the
  mobile/desktop selection indicators agree (canonical string ids end to end).
- **Truthful upload limits.** The client's size limit and "Max size" guidance derive from
  the server-configured `max_file_size_mb` (50 MB and 200 MB deployments both behave
  correctly), and small files no longer trigger a false "file too large" rejection.
- **Accessible controls.** Progress bars expose real `aria-valuenow`; the custom radio's
  visible circle is clickable; the tag-create button has an accessible name and reuses an
  existing tag instead of erroring on duplicates; a failed vault load shows an error with
  Retry instead of pretending the account is empty.
- **Parse-quality inspection.** Document detail gains a "Parse Quality" panel (pages with
  text, OCR use, low-content pages, tables/captions detected, extraction version) backed by
  structured diagnostics recorded at parse time and exposed via the status/batched
  endpoints — a scanned page with no extractable text or a dropped table is visible even
  when every embedding succeeded, with a Reprocess path via the existing reindex job.
- **Ask about this document.** Document detail offers "Ask about this document" once the
  file is searchable; it scopes the next chat question to that document (server-enforced
  `document_ids` retrieval scope, one-shot) and waits (disabled with a hint) until indexing
  completes.

### Rollout and compatibility

All API changes are additive: the batched status route and `document_ids` chat field are
new and optional; `partial_embeddings` / `extraction_diagnostics` response fields have
defaults. Old clients continue to poll per-file and ignore the new fields. The one schema
change is a nullable `files.extraction_diagnostics` JSON column added idempotently for
existing databases. Rollback is a plain revert; no data migration is destructive.

### Known limitations

- `ocr_used` in parse diagnostics is threaded from whether the OCR/image-text path actually
  ran for a document; text-layer PDFs always report `false` even when a text layer exists.
- The chat document scope is one-shot (applies to the next question) and there is no
  persistent scope chip in the composer yet; the scope indicator lives on the ask button.
- Batched status caps at 100 ids per request; larger queues page through multiple batches.
- Parse-quality diagnostics exist only for documents ingested after this change (older
  documents have no recorded diagnostics until reprocessed).
