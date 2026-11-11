# Boot sync tolerates malformed `payload_json` in every scanned status (#950 subset)

## Summary

The boot job-sync (`_sync_missing_ingest_job_rows`) no longer aborts on a malformed
`payload_json` ingestion row in ANY status. PR #872 guarded the new latest-job scans
and the terminal arms; this closes the remaining gap — the three PRE-EXISTING
pending/running scans:

- the INSERT's coverage anti-join (`status IN ('pending','running')`),
- the processing-reset UPDATE's pending subquery,
- the cap-settle arm's `NOT IN` exclusion (`status IN ('pending','running')`).

All three now use the same `CASE WHEN json_valid(...)` + WHERE-guard idiom as the
guarded arms, so a corrupted or planted pending/running row is skipped instead of
raising `OperationalError` inside the boot `BEGIN IMMEDIATE` and killing the whole
recovery migration. Regression coverage plants malformed rows in every scanned
status (completed/pending/running) and pins: latest-capped still excluded+settled
(#702), a superseded-capped cohort still recovers, and a file covered by a valid
pending job keeps its coverage (no duplicate minted, processing reset intact).
Verified discriminating: the extended test fails with `OperationalError` on the
unguarded base (`a1833834`) and passes at head.

Refs #950 (marks off the boot-path subset of item 1; the remaining census sites
stay with that issue).
