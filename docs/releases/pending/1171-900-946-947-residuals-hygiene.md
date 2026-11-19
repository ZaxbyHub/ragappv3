## Fixed
- `redact_url` (backend/app/utils/secrets.py) now masks a username-only
  userinfo (`redis://TOKENuser@host` — a live redis-py credential), redacts
  `password=`/`username=` query parameters and any other `...password`-suffixed
  key (including `ssl_password`, the TLS private-key passphrase redis-py 8.x
  routes to SSLConnection, percent-encoded key names like `pass%77ord`,
  duplicates, and values inside combined userinfo+query URLs), and fails closed on malformed credential-bearing netlocs instead of
  echoing them verbatim: `redis://:sup3rs3cretpw` (no host) masks to
  `redis://:***` and an unescaped-`/` password (`redis://:Ab3/xYz9+Q@host:6379`)
  masks to `redis://:***@host:6379` (port-only netlocs like `redis://:6379`
  are preserved). All three default-level boot INFO channels (rate limiter,
  embeddings L2 cache, draining-config capture) render the hardened output;
  the #1169 pin test now asserts NO leak (#1171).
- c04-family and prompt-suite test fixtures no longer leak temp resources when
  SETUP fails: the shared `backend/tests/c04_fixtures.py` home (fresh_db /
  seeded_env / prompts_client) guards teardown on every failure path
  (conn pre-bound to None; try/finally from mkdtemp). Ten modules fixed:
  five converted to the shared home (test_c04_ab_exposure_failure,
  test_c04_ab_lookup_failure, test_c04_prompt_warning_contract,
  test_c04_review_hardening, test_prompt_ab_variants) and five guarded in
  place (the frozen test_c04_prompt_created_by, test_prompt_versioning,
  test_c03_orgless_prompt, test_prompt_org_overrides - both fresh_db sites,
  the class-scoped multi_org_db, and the autouse setup_db mkdtemp - and
  test_issue645_converted_route_http). Behavioral setup-failure probes pin
  the contract (#946, #900 items 1-2).
- `backend/tests/test_c04_journal_signal_probe.py` describes itself accurately
  (a committed, collected test) (#900 item 3).
- README's reindex polling recipe no longer carries the stale cookie flag on
  the bearer-only job-status GET (#947 AC2, maintainer-rescoped).
- `backend/security/README.md`'s bandit snapshot table matches the committed
  baseline again (128 findings / 30 files / B608=63; severity bands and
  families refreshed), guarded by the new
  `backend/tests/test_readme_bandit_counts.py` drift check (#947 AC3,
  #900 items 4-5).
