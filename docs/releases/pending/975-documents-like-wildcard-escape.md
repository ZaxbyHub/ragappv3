# Documents routes: LIKE wildcard escaping in file-name search (Issue #975)

## What changed

### Backend — `backend/app/api/routes/documents.py`

- New module helper `_escape_like_term(term)` (next to
  `_build_files_fts_query`): escapes `\`, `%` and `_` so they match
  literally in a SQL LIKE pattern instead of acting as wildcards.
- All five `LOWER(file_name) LIKE ?` sites that bind user-typed search text
  now bind the escaped term and carry `ESCAPE '\'`:
  - `list_documents` (`GET /api/documents?search=...`) — the vault-scoped
    FTS arm, the global FTS arm, and the filename-only fallback arm used
    when the term has no FTS tokens (e.g. a bare `%`); the clause strings
    are shared by the count and page queries via `extra_where`.
  - `search_documents` (`GET /api/documents/search?q=...`) — the filename
    arm of the ranked SQL and of the count-only re-derive SQL (both bind
    the same `like_query`).
- The recipe mirrors the unified-search chat-title fix from #718 AC6
  (`backend/app/api/routes/search.py`) and the pre-existing escaped LIKEs in
  `backend/app/api/routes/users.py` and `backend/app/api/routes/groups.py`,
  so every user-search LIKE in the repo now uses the same pattern.
- The unified search route's document arm (`GET /api/search/unified`,
  `types=document`) inherits the same wildcard→literal behavior: it reuses
  `search_documents` verbatim, so its filename matching changes with it.
- `list_documents`' `search` query param is now capped at 200 characters
  (`max_length=200`, mirroring `q` on `/documents/search`), because the
  escaped pattern roughly doubles special-dense terms and SQLite raises
  "LIKE or GLOB pattern too complex" past ~50k pattern chars (an uncaught
  500 otherwise).

### Tests

- `backend/tests/test_issue975_documents_like_escape.py` (new): both routes
  are covered against a vault holding "alpha.pdf", "beta.pdf" and
  "margin_50%.pdf"/"notes_100%.txt" (literal `%`/`_` names). AC1 tests pin
  that `search=%`, `search=alp_a`, `q=port%` and `q=alp_a` return only
  literal matches (RED at base, GREEN after the fix); the count-only
  re-derive path is pinned via `q=port%&offset=10`. AC2 tests pin that
  plain terms, substring matches, and searches for names that literally
  contain `%`/`_` keep working, and that a bare `q=%` is still rejected
  with 400 (no FTS tokens) on the ranked search.
- Mutation-verified positive controls (`test_control_*`): a literal
  underscore/backslash probe must still FIND the literal-named file via
  each LIKE arm — the global list arm, the ranked filename arm, the
  count-only re-derive, the helper's backslash-doubling step (a\b.pdf vs
  the ab.pdf confusable), and the unified route's document arm — so
  dropping any arm's `ESCAPE` clause (or the doubling step) flips a test
  to failure instead of silently keeping the suite green. The list
  route's 200-char `search` cap is pinned by a 422 test.

## Why

The documents routes bound the raw (lowercased) user search term into
`LOWER(file_name) LIKE ?` with no `ESCAPE` clause, so a query containing
`%` or `_` acted as a SQL wildcard: `GET /api/documents?search=%` matched
every file, and `search=alp_a` wildcard-matched "alpha.pdf". The queries
were fully parameterized (no SQL injection — low security impact); the
defect is search correctness, the same wildcard-injection class as #718
AC6, left out of #718's declared route scope.

## Migration steps

- `GET /api/documents` search terms are now capped at 200 characters;
  longer values return 422, mirroring `/api/documents/search`'s `q` cap.
  Otherwise: no schema, config, or dependency changes, and responses are
  byte-compatible for every query that does not contain `%`, `_` or `\`.
  Searches that previously over-matched by accident (e.g. `q=%` returning
  everything) now return only literal matches, which is the intended
  behavior.

## Known caveats

- On `GET /documents/search`, queries whose word tokens prefix-match
  indexed text still match via the FTS arms (unchanged, by design): e.g.
  `q=alpha%` still surfaces "alpha.pdf" through `files_search_fts`, not
  through the LIKE arm. The fix only makes the LIKE arms treat `%`/`_`
  literally.
- A bare `%` on `GET /documents/search` keeps returning 400 ("Query has no
  searchable tokens") because the FTS tokenizer drops punctuation-only
  input; it never reached the LIKE arm before or after this change.
- The escape character is `\`, matching the rest of the repo. At base
  (no `ESCAPE` clause) a literal backslash in a search term already
  matched literally — no error, no escape-into-next-character — and the
  fix preserves exactly that literal matching now that the clauses
  declare `ESCAPE '\'`.
