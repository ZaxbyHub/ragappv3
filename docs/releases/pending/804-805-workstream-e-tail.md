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
  (the other wiki read routes remain JWT-only by design — the PR's
  recurrence sweep dispositioned 48 candidate routes: 12 already covered,
  3 fixed in the PR, 19 ruled out, 14 recorded as deliberate scope
  boundaries for product triage). Service-account principals are minted role="superadmin" by the shipped
  mechanism (deps.py), so the vault policy allows them every vault — the
  documented SA global-read model, identical to POST /api/search. The
  unified-search chat arm is the one deliberate exception: SA principals
  are scoped to unowned sessions only (their scope vocabulary never
  includes chat, and chat titles are the first 50 characters of a user's
  message). A human bearer that fails JWT validation keeps the
  token-specific 401 body and its `WWW-Authenticate: Bearer` challenge on
  the swapped routes (the generic `Not authenticated` body is reserved for
  `sak_`-shaped keys the mechanism cannot validate).
- **New `WikiReadTool` (#804).** A read-only agentic tool
  (`app/services/agentic_tools.py`) that resolves a wiki page by id and
  returns its markdown, gated on a single
  `evaluate(principal, "vault", page.vault_id, "read")` call before any
  content leaves the tool. Missing, denied, and failing reads share one
  public failure envelope (`wiki page not available`), preventing a
  page-existence oracle; the distinct cause is logged server-side only.
  The page row is read first because the vault id is only known from that
  row; callers holding a thread-safe pooled connection can pass
  `offload_store_read=True` so that synchronous read runs off the event
  loop, and the query never loads the page's claims/entities/lint
  relations. Sources carry `W<n>` labels compatible with
  `citation_validator.parse_wiki_citations`.
- **New read-only MCP-style tool adapter (#805)**
  (`app/services/mcp_server.py`). Two HTTP routes mounted under `/api`
  (`GET /api/mcp/tools`, `POST /api/mcp/tools/call`), both authenticated
  through the combined dependency:
  - `GET /api/mcp/tools` lists exactly `{"retrieval", "wiki_read"}` with
    JSON-schema input descriptions (top-level type `object`, non-empty
    `properties`), independent of `settings.agentic_rag_enabled`.
  - `POST /api/mcp/tools/call` validates a strict argument envelope
    (exact key sets, unknown top-level keys rejected; `retrieval` requires
    `query` + `vault_id`; `wiki_read` requires `page_id`; non-boolean
    positive integers inside the SQLite integer range; non-blank 1-1000
    char queries), then authorizes before executing: `retrieval` denies
    with 403 on the adapter-owned gate (nothing executed), while
    `wiki_read` authorizes inside the tool after the page lookup and
    reports denial through the shared failure envelope. The two swapped
    read routes bound their `vault_id` query parameter the same way, so an
    id outside 1..2**63-1 is a 422 there (base returned 200 with an empty
    list for a non-positive id; no caller sends one). The generative
    `synthesis` tool is not allowlisted and is never invoked.
  - Response matrix: 422 malformed envelope, 404 unknown tool, 400 invalid
    arguments, 503 when `wiki_read` is called with the wiki subsystem
    disabled (`settings.wiki_enabled=False`, matching the wiki router's
    master switch), 403 vault-read denial on the retrieval gate (nothing
    executed), 200 with a `{"result": {...}}` payload otherwise
    (tool-level failures — including a vault-policy evaluator fault —
    surface as `success=false` with empty output/sources).
  - Retrieval runs through the engine when present (the engine's
    `retrieval_top_k` governs — with the pre-existing caveat that
    `RetrievalTool` can overwrite that value on the shared engine, so a
    prior agentic call in the same process can change it; the adapter
    writes no persistent engine state — the one in-memory touch is the
    engine's own `_sync_document_retrieval_settings()` refresh, the same
    call `RetrievalTool` makes) or through a typed minimal pipeline over
    the DI-resolved services; results carry `S<n>`/`W<n>` labels that
    round-trip through `parse_citations`/`parse_wiki_citations`. Private
    `_`-prefixed keys are stripped at the wire; internal error text is
    replaced by stable generic messages (`retrieval unavailable`).
  - **Consumers must treat the returned `output` as untrusted third-party
    content.** Wiki page markdown and query echoes can contain label-like
    tokens (`[S1]`, `[W2]`); the adapter does not neutralize them, and the
    adapter's own label list is appended into that same string with no
    structural boundary, so a token inside the content cannot be
    distinguished from an adapter-appended one. The trustworthy signals are
    the STRUCTURED fields only: `sources[].source_label`, the boolean
    `success`, and the HTTP status — where `retrieval` denial is `403` and
    `wiki_read` denial is `200` with `success=false` and the shared
    `wiki page not available` error (a `404` from this surface means
    "unknown tool", never an authorization outcome). Downstream LLM
    consumers and renderers must not interpret `[S#]`/`[W#]` tokens found
    in `output` as citation labels.
- Both adapter routes are rate-limited with the search limiter (D01
  roster updated: `app.services.mcp_server.call_mcp_tool` and
  `app.services.mcp_server.list_mcp_tools`).

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
  identity comparison, sanitization, missing-vs-denied envelope identity,
  plus the feedback-round legs: per-route combined-dependency assertion,
  write-only 403 across all five service-account routes, the
  input-boundary matrix (2**63 ids, 1001-char and NUL queries, unknown
  top-level keys), the wiki-disabled 503, the policy-fault envelope, the
  off-loop relation-free store read, and the human 401 contract.
- One sanctioned CHECK_WRONG amendment (C16's dependency-tree walker
  assumed a pre-FastAPI-0.139 wrapper shape; tree-walk semantics
  unchanged; acceptance-table semantics digest constant).
