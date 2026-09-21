# F3: Meridian integration qualification (exact build 67527cd9)

## Rollout

Deployed the final integrated build to the R640AI supported deployment (container `knowledgevault`, subpath `/meridian`), qualified the full battery at `edd2c741…` (image `65aad900…`), then resolved both blockers and re-qualified at the fixed build `67527cd9…` (image `1714ba3b…`, master + this PR): the operator restored the thinking-model endpoint (live streamed thinking turns with citations, a like-for-like thinking perf row, and a full source-only-rewrite draft lifecycle are GREEN on it), and this PR fixes the reindex dimension-probe bug the qualification exposed (a latent #529 defect — not the #645 class first suspected — with a contract regression test proven RED on revert; reindex now completes end to end). The draft gates qualified with a recorded limitation: the source-only rewrite completed its full lifecycle; the mixed-source compose qualified through its copy stage but its standards-stage call exceeds the hardcoded 300s non-streaming client timeout on the restored always-reasoning server (production cause and operator unblock in the evidence matrix). Evidence matrix: `docs/eval/2026-09-meridian-qualification.md` (+ `-data.json`, raw evidence committed under `-evidence/`). Dispositions for all 207 original audit IDs, the 54 supplemental registry obligations and the 55 legacy migration rows are recorded there.

## Migration Compatibility

No schema or config migration is required: the build is the current master line plus this PR's probe fix; the deployed SQLite/LanceDB data volumes carried over in place (`data` bind mount). Operator settings are unchanged (Draft Room was already enabled at runtime; retrieval calibration `max_distance_threshold` 0.75 from PR #647 is active). Access via the public proxy domains continues to work as before; raw-IP browser origins remain rejected by the same-origin CSRF design (a temporary qualification-time allowlist addition was restored).

## Rollback

Rollback images are pinned at both stages: `ragappv3-knowledgevault:rollback-pre-f3-281bd714` (00d553f3eade…, pre-F3) and `ragappv3-knowledgevault:rollback-f3-stage1-edd2c741` (65aad900…, stage-1 qualified build). Rollback: recreate the `knowledgevault` service from either tag (or check out that commit + rebuild); the bind-mounted data volume is untouched by all paths. Health-gate on `/api/health`.

## Operator-Visible Outcomes

- The app serves the final integrated build at `/meridian` with the #647 relevance calibration live (distance bands and 0.75 max-distance), the restored thinking model verified live, and Draft Room enabled.
- Reindex is repaired by this PR: the #513 W13 dimension probe no longer tuple-unpacks the fail_fast=True embedding return, so `POST /api/documents/reindex` completes (two pre-existing corrupt CDP corpus files still fail per-file parse and are flagged for operator data cleanup).
- Draft compiles are long-running against the restored always-reasoning thinking server (minutes per model call; the deployment's pre-outage reference compile took 56 minutes); the mixed-source compose draft still needs one more retry after the server is fully warm, or after restoring the pre-outage serving profile — the stage-cache resume finishes the remaining stages.
