# Draft Room fail-closed guards: empty candidate/outline/rewrite, provider preflight, editorial no-think, test Redis guard

PR: #1147

## What changed

- **Fail-closed empty-candidate / outline / rewrite guards**: a compile can no
  longer ship a zero-byte revision. Each desk stage (copy, standards, fact,
  assemble) refuses an empty candidate with the non-retryable stage code
  `candidate_empty`; an outline with zero sections fails the Outline stage with
  `outline_empty` (on a fresh model call and on a reused checkpoint); a
  rewrite-mode draft with no manuscript-role input is refused at enqueue with
  422 `rewrite_requires_manuscript` on both the compile and the retry routes,
  before any job row exists. Ready, Export and Promote refuse an empty
  revision with 409 `revision_empty`.
- **Provider reachability preflight**: compile and retry now probe each
  provider (`GET /v1/models`, short timeout) after the origin-allowlist check
  and before the job is created; an unreachable provider answers 503
  `provider_unreachable` instead of burning a ~10 minute pipeline run first.
  A genuine `Idempotency-Key` replay returns the stored job without a live
  probe, so a replay of an accepted request stays 202 with the same job id
  even while a provider is down. The editorial lane's own endpoint
  (`DRAFT_EDITORIAL_CHAT_URL`, falling back to the thinking URL) is the one
  checked and policy-gated, matching the runtime resolver.
- **Editorial lane defaults to no-think (#712)**: editorial desk requests now send
  `enable_thinking=False` by default so a reasoning-on template cannot spend
  the whole token budget and return empty content. Set
  `DRAFT_EDITORIAL_ENABLE_THINKING=true` to omit the control and let the
  provider default govern (same posture as `INSTANT_ENABLE_THINKING`). The
  variable is now passed through `docker-compose.yml` and documented in
  `.env.example`.
- **Test-suite Redis guard**: a pytest-session guard now refuses any
  production-shaped Redis URL (loopback, wildcard, compose service names,
  loopback-resolving hosts) on both `redis.from_url` and
  `redis.asyncio.from_url`, so a test can no longer reach a real Redis it did
  not start (`TEST_ALLOW_REAL_REDIS=1` opts out). Test-only; no runtime
  behavior change.

## Operator notes

- **API behavior change**: compile/retry may now answer 503
  `provider_unreachable` where they previously enqueued a job that later
  failed. Compose deployments that set `DRAFT_EDITORIAL_CHAT_URL` must make
  that endpoint reachable from the backend container.
- `DRAFT_EDITORIAL_ENABLE_THINKING` defaults to `false`; no action needed
  unless an editorial model requires its template default.
