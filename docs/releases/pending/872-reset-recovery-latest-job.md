# Operator resets recover files whose latest job completed (#872)

Issue: #872. Collation note: this PR originally collated #819/#820 with
#872; those two landed first via PR #1107 (dimension refusals settle on
attempt 1; failed lease jobs keep their result diagnostics), so this PR
ships only the #872 recovery fix and the review fixes from its own
broad-review round.

## What changed

### Boot job sync keys capped-failure exclusion on the file's LATEST job

`_sync_missing_ingest_job_rows`
(`backend/app/services/background_tasks.py`) decided "this file is covered
by an existing job" with ANY-row anti-joins: any historical
`status='failed' AND attempts >= JOBS_MAX_ATTEMPTS` ingestion job excluded
a file forever, even after a LATER job for the same file completed
(nothing prunes `jobs`). An operator reset
(`scripts/reset_embeddings.py` / `scripts/migrate_embeddings.py`) of such
a file minted no new job and the file settled
`error/lease_attempt_cap_exceeded` — the printed "will auto-reprocess"
promise was false for that cohort.

Both capped-failure predicates (the mint arm and the cap-settle arm) now
check the file's LATEST ingestion job: the anti-join matches only rows in
the per-file latest-job-id set, computed once per statement as an uncorrelated
`MAX(id) ... GROUP BY file_id` subquery (O(N) over `jobs`; a per-row
correlated probe measurably stalls boot inside `BEGIN IMMEDIATE` on
failure-storm deployments — pinned by the review round's benchmarks).
A file whose LATEST job is still capped-failed keeps the #702 terminal
treatment (no silent cap bypass); a NULL/missing `file_id` row can never
supersede or match a real file.

The boot sync's two terminal-settle arms in the ingest lease transport now
also gate the files-row write on the fenced settle having landed: a worker
that lost its lease mid-run no longer writes `files` state a successor
owns (PR #1106 review round 1, Copilot finding).

### Reset-script caveat corrected

`scripts/reset_embeddings.py`'s printed note now states the precise
contract: a file whose LATEST ingestion job is still capped-failed keeps
the terminal `error` state (per-file admin retry); older capped failures
that a later job completed no longer block reset recovery; and files
already settled to `error` by an OLDER build stay in `error` (this script
resets only `indexed`/`partial`/`pending` rows) — recover those via the
per-file admin retry endpoint as well. Sibling operator notes
(`705-`, `702-`, `559-` fragments) were updated to match the latest-job
semantics and the #820 terminal shape.

## Operator visible changes

- After `scripts/reset_embeddings.py`, files with older capped failures
  but a later completed job are reset and auto-reprocessed at startup
  (previously reset files with that history were re-stranded
  `error/lease_attempt_cap_exceeded` with no job and no recovery path).
- Rows already settled `error/lease_attempt_cap_exceeded` by an older
  build are NOT resurrected by reset or boot (they are `error` rows);
  recover them once via the per-file admin retry endpoint.
- Boot-time job-sync cost on failure-storm deployments returns to one O(N)
  set-based pass per arm instead of a per-capped-row rescan of the jobs table.

## Notes

- Review artifacts: `.zcode/pr-review/pr1106-20261110/` (git-excluded);
  the broad-review round's benchmarks and probes are recorded there.
