# GET /settings/connection now redacts infra topology for non-admins (issue #660)

## What changed

### Backend (issue #660 — F1/E1)

- **Role-aware redaction in `test_connection`** (`backend/app/api/routes/settings.py`):
  the endpoint resolves the caller's role exactly like `GET /settings` does
  and, for callers below the admin role (`UserRole.level < admin`), post-processes
  the result dict before returning:
  - every probed target's `url` becomes the non-identifying target key
    (`embeddings` / `chat` / `reranker`) — replacement is unconditional for
    probed entries (fail-closed) so future handler drift cannot dodge it;
  - the local-reranker fallback entry keeps its non-identifying
    `"local (sentence-transformers)"` url but drops the `model` field (the
    reranker model name is in `INFRA_REDACTED_FIELDS`);
  - error values keep their classification prefix verbatim
    (`SSRF blocked: ` / `transport failure: ` / `embedding inference failed`)
    but the detail is reduced to the exception type name
    (`SSRF blocked: URLBlocked`, `transport failure: ConnectError`), which
    closes every exception-text channel at once: the `host!r` echo in
    URLBlocked messages, resolved-IP:port tuples embedded by httpx connect
    errors, and the URLBlocked "URL is malformed: ..." port-fragment echo.
  - `ok` and `status` are untouched, so member-facing diagnostics keep
    working (the frontend reads only `v?.ok`).
- **Admins and superadmins are unchanged**: the post-process block is skipped
  for `UserRole.level >= admin` (including the `users_enabled=False`
  operator-secret principal), so admin responses are byte-for-byte identical
  to before.
- The embeddings target keeps its POST-probe semantics (issue #494 OPS-007)
  and the `SSRFSafeTransport` + `assert_url_safe` flow is untouched.

### Tests

- `backend/tests/test_issue660_connection_redaction.py`: branch-by-branch
  leak checks (success / SSRF-blocked / transport-failure / local fallback x
  viewer+member), admin field-set preservation, diagnostics-prefix
  preservation, and a structural response-shape contract that seeds the whole
  `INFRA_REDACTED_FIELDS` `*_url`/`*_model` family, sweeps settings views and
  every parameterless GET route as a viewer, and proves its walker
  non-vacuous against a synthetic leaky-handler fixture.
- `backend/tests/test_issue660_nonadmin_shape.py`: pins the exact non-admin
  representation (`url ==` target key, still a str; `model` absent; error ==
  prefix + type name with no configured host/IP/URL fragment anywhere),
  the non-admin embeddings-HTTP-500 body, and the shipped-default
  empty-config case (`ollama_chat_url=""` keeps a clean
  `SSRF blocked: URLBlocked`).

## Why

`GET /settings/connection` predates the MED-4 contract (#387/#403) and echoed
the configured embeddings/chat/reranker endpoint URLs — plus the local-mode
reranker model name — to any authenticated user, exposing the deployment's
internal inference topology to viewer/member JWTs. MED-4's acceptance
criteria pinned only `GET /settings`, so this sibling kept the raw echo and
was carried on the CHANGELOG "Known residuals" list since PR #418. The
response-shape contract test closes the drift class (E1): no settings-view
reachable below admin can disclose a configured `*_url`/`*_model` value
without failing the build.

## Migration / deployment impact

None. No config, schema, or frontend changes. Viewers/members see target
names instead of URLs in the connection-test response; the member UI
(Maintenance settings "Test connections") reads only the per-service `ok`
flag. Admin/operator responses are unchanged. Rollback is a single-commit
revert.
