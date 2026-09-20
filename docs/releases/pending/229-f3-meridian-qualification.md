# F3: Meridian integration qualification (exact build edd2c741)

## Rollout

Deployed the final integrated master build (`edd2c741855c34dd1e330e6e79b7210996b353d7`, image `65aad900…`) to the R640AI supported deployment (container `knowledgevault`, subpath `/meridian`) and ran the F3 integration qualification battery against it: full user-journey chain, exception paths, keyboard/touch-class and mobile/subpath layouts, history preservation across replacement/restart, the eight symptom rechecks, and the F2-matching performance spot-checks. Evidence matrix: `docs/eval/2026-09-meridian-qualification.md` (+ `-data.json`). Journey outcomes, dispositions for all 207 original audit IDs, the 54 supplemental registry obligations and the 55 legacy migration rows are recorded there. Two environment-blocked gates (live-thinking, draft compose/rewrite — external thinking-model outage) and one live finding of the open #645 defect class (reindex event-loop failure) are recorded as FAILs with production causes and re-run procedures; the issue closes only when those flip.

## Migration Compatibility

No schema or config migration is required: the build is the current master line; the deployed SQLite/LanceDB data volumes carried over in place (`data` bind mount). Operator settings are unchanged (Draft Room was already enabled at runtime; retrieval calibration `max_distance_threshold` 0.75 from PR #647 is active). Access via the public proxy domains continues to work as before; raw-IP browser origins remain rejected by the same-origin CSRF design (a temporary qualification-time allowlist addition was restored).

## Rollback

The pre-F3 image is pinned as `ragappv3-knowledgevault:rollback-pre-f3-281bd714` (image `00d553f3eade…`). Rollback: recreate the `knowledgevault` service from that tag (or `git checkout 281bd714` + rebuild); the bind-mounted data volume is untouched by both paths. Health-gate on `/api/health`.

## Operator-Visible Outcomes

- The app serves the final integrated build at `/meridian` with the #647 relevance calibration live (distance bands and 0.75 max-distance).
- While the external thinking model (http://172.16.50.41:8000) is down, the UI shows `Using instant — thinking unavailable`, chat falls back to instant with full citations, and Draft Room compose reports `provider_unavailable` with bounded retries; restore that service to re-enable thinking and draft generation, then re-run the three recorded FAIL gates (procedure in the evidence matrix).
- Reindex (`POST /api/documents/reindex`) currently fails with an event-loop embedding error — open issue #645 owns that repair; ordinary upload/parse/search/chat paths are unaffected.

