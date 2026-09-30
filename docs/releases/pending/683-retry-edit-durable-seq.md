# Retry and Edit stop deleting earlier chat turns: durable seq carried through live sends (Issue #683)

## What changed

- `frontend/src/hooks/useSendMessage.ts` — `migrateId` now carries the
  server-issued `seq` from each `addChatMessagesBatch` result row into the chat
  store (both the id-rename branch and the same-id branch), alongside the
  existing `created_at`/`saveState` merge. Rows saved during the current
  page-life previously ended with `seq === undefined`, so `durableKeepSeq` —
  the Retry/Edit truncate anchor in `TranscriptPane` — computed `keep_seq = 0`
  and the server's truncate endpoint (`DELETE ... WHERE seq > 0`) permanently
  deleted every earlier, successfully saved turn of the session. The local
  transcript still showed the turns; the loss surfaced only after reload.
- Rows that were genuinely never persisted (pre-token Stop, empty answer,
  failed save) still contribute nothing to the anchor: `durableKeepSeq` only
  counts `typeof seq === "number"`, and a `null`/absent `seq` (legacy rows
  saved before the column existed) is ignored exactly as before (PRR-020).
  A non-numeric `seq` cannot occur: the column is a SQLite INTEGER assigned as
  `MAX(seq)+1` on insert. Known residual (tracked in issue #815): turns saved
  only through the page-unload keepalive path do not yet carry `seq` in the
  current page's store; after a bfcache restore, a Retry in that page can
  still anchor below such a turn.
- Send-path test fixtures now mirror the `add_messages_batch` response
  contract (every mocked saved row carries a numeric `seq`): the three
  batch-result sites in `useSendMessage.test.ts`, the mapped rows in
  `useSendMessage.issue553.test.ts`, and — for class consistency — the mocks
  in `useSendMessage.evidence.test.ts` and `useSendMessage.issue552.test.ts`.
- New CI quality-contract gate `scripts/check_batch_mock_seq.py` (wired into
  the Quality contracts job in `ci.yml`, mirrored in the `justfile`
  `quality-contracts` recipe and the documented script inventories): fails the
  build when the send-path suites' batch-save mocks drop `seq`, so new fixtures
  cannot silently re-create the blind spot that hid this defect. The gate
  scans five send-path suites, ignores mock mentions inside comments and
  string/template literals, accepts bare or quoted keys, requires the `seq`
  value to be numeric at runtime (rejecting `null`/`undefined`/stringly
  values), flags result setters invoked with no argument, and anchors paths
  to the repository root independently of the working directory. Known
  limitation: rows with no inline object literal at the result-setter call
  site — factored through helper functions or variables
  (`mockResolvedValue(savedRows(messages))`) or resolved through a promise
  captured elsewhere (`resolveBatch([...])`) — are not detected; those
  fixtures are normalized in this change and swept by the recurrence
  predicates recorded in the issue trace.
- New regression coverage: `useSendMessage.liveSeq.test.ts` (live-saved rows
  carry the server-issued seq) and `TranscriptPane.liveSeqRevision.test.tsx`
  (Retry and Edit anchor the truncate at the first turn's highest server seq
  after two live-saved turns — the exact data-loss scenario).

## User impact

Clicking Retry or Edit on a turn sent since the page was loaded no longer
wipes earlier turns from the saved conversation; after a reload, everything
before the revised turn is still there.
