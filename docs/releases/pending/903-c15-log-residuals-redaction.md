# C15 log-disclosure residuals redacted (#903, #908, #909, #910, #906)

## What changed

Collated closure of the five C15-family residuals from the #710/PR #904
remediation. Contract (#287): no raw user-derived text at the default log
level.

- **Embedding provider bodies (#903)** — the nine `response.text` WARNING
  f-strings in `backend/app/services/embeddings.py` now emit
  `body_len=<n>` summaries; the first 2000 characters of the body moved to
  `logger.debug` beside each WARNING (full body stays off the log). Status codes, modes, outage detection, retry/breaker
  behavior, and the raised `EmbeddingError` messages (which never carried
  the body) are unchanged.
- **Sub-query text logged as a label (#908)** — the multi-sub-query
  orchestrator passes the RAW sub-query string in the tuple slot
  `_execute_retrieval` names `variant_type`; a vector-store failure
  WARNING logged it untruncated. The WARNING now carries the variant index
  and exception only; the two label-only embedding loops rename their
  variable to `variant_label` so the AST guard can ban `variant_type`
  without false positives.
- **Auto-named session titles (#909)** — both auto-name INFO sites log the
  session id and a `title_len` summary instead of the LLM-summarized
  title, and the failure WARNING logs the exception class only (full text
  at DEBUG), closing the provider-echo channel through `LLMError` bodies.
  The persisted title value and fallback behavior are unchanged.
- **Access-log query values + uvicorn channel (#910)** —
  `_sanitize_query` redacts VALUES of provenance-classified user-text
  params (`q`, `query`, `search`, `tag`, `entity`) after percent-decoding
  the parameter NAME (encoded spellings like `%71=` cannot bypass);
  credential-name scrubbing is unchanged and additive. The Dockerfile CMD
  adds `--no-access-log` (for requests that reach the application stack,
  the middleware record is the app-level access log — see the blind-spot
  caveat below); `docs/admin-guide.md` documents the redacted log shape.
- **Guard robustness (#906)** — the `no-query-text` AST guard bans
  `variant_type` and `title` (collision-checked), skips
  unreadable/unparseable files with a counted INFO note instead of an
  opaque crash, survives cross-drive `relpath` on Windows, and scopes its
  walk to git-tracked files so generated trees cannot change the verdict.
  The scanned-file floor is test-level.

## Why

Provider error bodies can echo the request payload (token-overflow errors
quote the offending input); the auto-named title is an LLM paraphrase of
the user's first message; the access log carried free-text search values;
all reach the default log on live paths. These are the residuals the #710
census and PR #904 review identified and filed.

## Migration

None. Log consumers see `body_len=`/`title_len=` summaries and
`name=[redacted]` query params where raw values used to appear; DEBUG
carries the detail. Operators grepping uvicorn's access log in containers
should use the application's `http_request` records for requests that reach
the application stack (uvicorn's duplicate access log is now disabled).

## Known caveats

- **Access-log blind spot.** With uvicorn's access log disabled, the response
  classes answered before the logging middleware runs emit no application
  access line: maintenance-window 503s, invalid-Host 400s, and unhandled 500s
  (the logging middleware is the innermost one). uvicorn's own access log
  covered them before; re-enable it per deployment if those lines are needed.

- The recurrence census (trace `08a`) routes a wider same-class family to
  a follow-up issue: provider bodies composed into raised
  `LLMError`-style messages (`llm_client.py`, `model_checker.py`) and 15
  user-chosen filename/vault-name default-level log sites.
- `SCRUB_FIELDS` substring matching over-redacts in the safe direction
  (pre-existing semantics; now also applied to decoded names).
