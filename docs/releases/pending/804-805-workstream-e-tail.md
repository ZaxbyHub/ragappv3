# Workstream E tail: scoped service accounts on wiki reads / unified search + read-only MCP adapter (#804, #805)

## What changed

- **Service accounts can read the wiki surface and unified search (#804).**
  `GET /api/wiki/pages`, `GET /api/wiki/pages/{page_id}` (wiki.py) and
  `GET /api/search/unified` (search.py) now authenticate through
  `get_current_user_or_service_account`: human JWT sessions resolve exactly
  as before (JWT first), and scoped `sak_` service-account keys are accepted
  with the shipped `documents:read` scope check (a write-only key gets 403).
  The vault-read policy seam (`_require_vault_read`) is unchanged and now
  runs for service-account principals on the two swapped wiki read routes
  (the other wiki read routes remain JWT-only by design — see the PR's
  recurrence census). Service-account principals are minted
  role="superadmin" by the shipped mechanism (deps.py), so the vault
  policy allows them every vault — the documented SA global-read model,
  identical to POST /api/search. The unified-search chat arm is the one
  deliberate exception: SA principals are scoped to unowned sessions
  only (their scope vocabulary never includes chat, and chat titles are
  the first 50 characters of a user's message).
- **New `WikiReadTool` (#804).** A read-only agentic tool
  (`app/services/agentic_tools.py`) that resolves a wiki page by id and
  returns its markdown, gated on a single
  `evaluate(principal, "vault", page.vault_id, "read")` call before any
  content leaves the tool. Missing, denied, and failing reads share one
  public failure envelope (`wiki page not available`), preventing a
  page-existence oracle; the distinct cause is logged server-side only.
  Sources carry `W<n>` labels compatible with
  `citation_validator.parse_wiki_citations`.
- **New read-only MCP-style tool adapter (#805)**
  (`app/services/mcp_server.py`). Two HTTP routes mounted under `/api`
  (`GET /api/mcp/tools`, `POST /api/mcp/tools/call`), both authenticated
  through the combined dependency:
  - `GET /api/mcp/tools` lists exactly `{"retrieval", "wiki_read"}` with
    JSON-schema input descriptions (top-level type `object`, non-empty
    `properties`), independent of `settings.agentic_rag_enabled`.
  - `POST /api/mcp/tools/call` validates a strict argument envelope
    (exact key sets; `retrieval` requires `query` + `vault_id`; `wiki_read`
    requires `page_id`; non-boolean positive integers; non-blank 1-1000
    char queries; unknown keys rejected), then authorizes
    `("vault", vault_id, "read")` BEFORE executing anything. The
    generative `synthesis` tool is not allowlisted and is never invoked.
  - Response matrix: 422 malformed envelope, 404 unknown tool, 400 invalid
    arguments, 403 vault-read denial (nothing executed), 200 with a
    `{"result": {...}}` payload otherwise (tool-level failures surface as
    `success=false` with empty output/sources).
  - Retrieval runs through the engine when present (operator-configured
    `retrieval_top_k` governs; the adapter never writes engine state) or
    through a typed minimal pipeline over the DI-resolved services;
    results carry `S<n>`/`W<n>` labels that round-trip through
    `parse_citations`/`parse_wiki_citations`. Private `_`-prefixed keys
    are stripped at the wire; error text is replaced by stable generic
    messages (`retrieval unavailable`).
- `POST /api/mcp/tools/call` is rate-limited with the search limiter
  (D01 roster updated: `app.services.mcp_server.call_mcp_tool`).

## Why

Workstream E (access control and integrations) tail: closes the last two
slots (PR 4 of 5 and PR 5 of 5) by extending the shipped scoped
service-account mechanism to the wiki read surface and exposing the
read-only tool surface for tool-consuming principals, per the
audit-consolidation contracts in #804 and #805.

## Verification

- Frozen acceptance checks C1-C19 (arm's-length authored, checkpoint
  anchored on #804): DISCRIMINATING RED→GREEN, NEW-SURFACE
  ERROR→GREEN, PRESERVING GREEN→GREEN.
- Companion integration suite (`test_q02q03_companions.py`): per-route
  auth independence, malformed/valid matrix, both retrieval execution
  paths with recorded arguments, rerank propagation, content-hash
  mutation sentinel (calibrated), two-vault human + SA semantics,
  identity comparison, sanitization, missing-vs-denied envelope identity.
- One sanctioned CHECK_WRONG amendment (C16's dependency-tree walker
  assumed a pre-FastAPI-0.139 wrapper shape; tree-walk semantics
  unchanged; acceptance-table semantics digest constant).
