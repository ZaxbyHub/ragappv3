# Draft Room

Draft Room is a private drafting workspace for producing a vault-grounded document
(a rewrite of source material, or a composed piece backed by vault research) through
a durable, staged editorial pipeline with human sign-off before anything leaves the
project. This guide covers enabling it, its configuration limits, what it discloses
to model providers, how its data is retained and deleted, and what its two key
states — "source-only" and "Ready" — actually mean.

---

## Table of Contents

1. [What Draft Room Is (and Is Not)](#what-draft-room-is-and-is-not)
2. [Enabling Draft Room](#enabling-draft-room)
3. [Model Provider Disclosure and the Origin Allowlist](#model-provider-disclosure-and-the-origin-allowlist)
4. [Limits](#limits)
5. [Retention and Deletion](#retention-and-deletion)
6. [What "Source-Only" Means](#what-source-only-means)
7. [What "Ready" Means (and Does Not Mean)](#what-ready-means-and-does-not-mean)
8. [Export](#export)
9. [Promotion](#promotion)
10. [Troubleshooting](#troubleshooting)

---

## What Draft Room Is (and Is Not)

Draft Room lets a vault member upload source material (or ask for research against
a vault), run it through a staged pipeline (research, outline, draft, deterministic
lint, copy desk, standards desk, a fact-checking gate, and final assembly), review
the findings and citations it produces, and — once satisfied — mark a specific
revision **Ready** and export or **promote** it into the vault as a normal document.

**Draft Room does not:**

- **Publish anything.** A Ready draft is not visible to other vault members and is
  not indexed. The only way its content reaches the rest of the vault is the
  explicit **Promotion** action described below.
- **Verify that a document is true.** The fact-checking stage compares claims
  against retrieved evidence and reports contradicted, unsupported, ambiguous, or
  stale claims; it does not, and cannot, establish universal factual truth. See
  [What "Ready" Means](#what-ready-means-and-does-not-mean).
- **Detect AI-generated writing.** There is no AI-authorship detector anywhere in
  the pipeline. Nothing in the product should be read as attesting to who or what
  wrote a passage.
- **Score confidence or entailment.** Per-citation matching in the evidence/claims
  ledger is a lexical-overlap comparison, not a confidence score, support
  probability, or entailment judgment.

---

## Enabling Draft Room

Draft Room is controlled by a single master switch:

```env
DRAFT_ROOM_ENABLED=false
```

**Default: `false`.** This is verified against `_require_enabled()` in
`backend/app/api/routes/draft_room.py`, which is the sole gate: every mutating or
generative route calls it first and raises `503 draft_room_disabled` when the flag
is off.

**While disabled, the following stay available** (so an owner can always inspect
and clean up their own private data, even if an administrator turns the feature
off later):

- `GET /api/draft-room/capabilities` — capability discovery (its `enabled` field
  reflects the current flag)
- Listing and reading a draft's own drafts, detail, jobs, job stages, revisions,
  evidence, claims, and findings
- `GET .../inputs/{input_id}` — reading a stored input's content
- Exporting a revision
- `POST .../jobs/{job_id}/cancel` — cancelling an active job
- `DELETE /api/draft-room/drafts/{draft_id}` — whole-draft deletion
- `DELETE .../inputs/{input_id}` — deleting a single input

**While disabled, these return `503 draft_room_disabled`:**

- Creating, updating, archiving, or restoring a draft
- Uploading or editing an input
- Creating a manual revision
- Retrying a job or compiling (`POST .../compile`, `POST .../jobs/{job_id}/retry`)
- Marking a revision Ready (`POST .../revisions/{revision_id}/ready`)
- Disposing (resolving/waiving) a finding
- Promoting (`POST .../promote`)

Navigation to Draft Room stays hidden in the UI while the flag is off.

---

## Model Provider Disclosure and the Origin Allowlist

Draft Room's compile pipeline sends **project manuscript text and matching vault
passages to the configured model provider**. Before compile, the UI names the
configured provider/model class and discloses this. That disclosure is informational,
not an extra permission grant — what actually controls whether a call is allowed to
go out is the origin allowlist below.

Two independent, empty-by-default settings gate this:

```env
DRAFT_ALLOWED_MODEL_ORIGINS=
DRAFT_SENSITIVE_ALLOWED_MODEL_ORIGINS=
```

- **`DRAFT_ALLOWED_MODEL_ORIGINS`** — comma-separated exact `scheme://host:port`
  origins allowed to receive **ordinary**-tier Draft Room content (the `standard`
  and `high_stakes` draft tiers). Empty **blocks compile entirely** (fail closed).
- **`DRAFT_SENSITIVE_ALLOWED_MODEL_ORIGINS`** — a stricter, independent allowlist
  required **in addition to** the ordinary allowlist for the `sensitive` draft tier.
  Empty blocks sensitive-tier compile. Membership in one allowlist does not imply
  membership in the other.

An administrator must populate at least `DRAFT_ALLOWED_MODEL_ORIGINS` before any
compile can succeed.

**Normalization and matching rules** (`backend/app/services/draft_provider_policy.py`):

- Entries must be exact `http://host:port` or `https://host:port` origins — no
  path, query, fragment, or embedded credentials.
- **Wildcards are rejected outright.** An entry containing `*` fails deployment
  configuration validation (`provider_policy_misconfigured`) rather than being
  silently ignored.
- **Suffix/prefix matching is not supported.** Matching is exact-origin only; a
  configured `https://models.example.com` does **not** match
  `https://api.models.example.com` or any other host.
- Only `http`/`https` schemes are accepted.
- The check is purely lexical and runs **before** the SSRF/DNS safety check, and
  again immediately before every model call during a job — so a mid-job policy
  change (an administrator narrowing the allowlist) is honored before the next call.
- **Redirects are blocked.** Draft Room's model HTTP clients set
  `follow_redirects=False`; any 3xx response from a provider fails the call with
  `provider_redirect_blocked` rather than being followed.
- **The `sensitive` tier additionally requires HTTPS**, except for a literal
  loopback origin (`127.0.0.1`, `[::1]`, or `localhost`) that is itself explicitly
  present in `DRAFT_SENSITIVE_ALLOWED_MODEL_ORIGINS`. Cleartext LAN/Docker
  hostnames (e.g. `host.docker.internal`) remain HTTPS-only even if listed.
- A rejected request never returns the configured allowlist or the full rejected
  URL to the client — at most the bare hostname appears in the error.

---

## Limits

Every `DRAFT_*` setting, its default from `backend/app/config.py`, and what a user
sees when it is hit:

| Setting | Default | What happens when it's hit |
|---|---:|---|
| `DRAFT_MAX_INPUTS` | `10` | Uploading another input once a project already holds this many returns `413 limit_exceeded`. |
| `DRAFT_MAX_TOTAL_INPUT_MB` | `250` | Uploading an input that would push the project's total raw input bytes over this cap returns `413 limit_exceeded`. The global per-file `MAX_FILE_SIZE_MB` cap (100 MB by default) still applies to each individual upload on top of this. |
| `DRAFT_MAX_TOTAL_PARSED_CHARS` | `500000` | Once an input's extracted text would push the project's total normalized parsed characters over this cap, that input's parse job fails asynchronously with `parse_error=parsed_text_limit_exceeded` — the upload itself succeeds (202), but the input ends up in a `failed` parse state rather than `ready`. |
| `DRAFT_PARSE_TIMEOUT_SECONDS` | `300` | A single input's extraction job that runs longer than this is failed with `job_timeout`. |
| `DRAFT_UPLOAD_RATE_LIMIT` | `20/minute` | Further upload requests from the same user return `HTTP 429` until the window resets. |
| `DRAFT_MAX_SECTIONS` | `12` | A compile job whose outline would exceed this many sections fails with `section_budget_exceeded`. |
| `DRAFT_QA_RETRY_LIMIT` | `2` | Each bounded editorial revision loop (lint/copy/standards) stops retrying after this many passes and surfaces any remaining findings unresolved rather than looping further. |
| `DRAFT_JOB_TIMEOUT_SECONDS` | `1800` | A compile job that runs longer than this total wall-clock budget fails with `job_timeout`. |
| `DRAFT_JOB_MAX_MODEL_CALLS` | `40` | A compile job that would exceed this many model calls across every stage fails with `model_call_budget_exceeded`. |
| `DRAFT_COMPILE_RATE_LIMIT` | `5/minute` | Further compile or retry requests from the same user return `HTTP 429` until the window resets. A retry counts as a compile request. |
| `DRAFT_RESEARCH_RETRIEVAL_LIMIT` | `8` | Caps how many sources are retrieved per research facet; not an error condition, just a breadth bound. |
| `DRAFT_TRANSIENT_RETRY_LIMIT` | `2` | Maximum automatic retries for a transient provider/retrieval error inside one compile job, with bounded backoff, before the job fails outright. |
| `DRAFT_LINT_REWRITE_LIMIT` | `2` | Maximum automatic rewrite attempts the deterministic lint stage may apply before surfacing remaining boilerplate findings unresolved. |
| `DRAFT_POLL_INTERVAL_SECONDS` | `2.0` | Not user-facing — how often the durable job processor polls for queued work. Lowering it increases responsiveness at the cost of more frequent DB polling. |
| `DRAFT_BOILERPLATE_RULE_VERSION` | `"1"` | Not a cap — the curated-boilerplate rule version recorded on lint findings and required to match for a waiver to remain valid at Ready time. |
| `DRAFT_DEFAULT_LOGICAL_MODE` | `thinking` | Not a cap — default model mode (`instant` or `thinking`) for compile stages that don't pin a specific mode. |

`DRAFT_ROOM_ENABLED`, `DRAFT_ALLOWED_MODEL_ORIGINS`, and
`DRAFT_SENSITIVE_ALLOWED_MODEL_ORIGINS` are covered above under
[Enabling Draft Room](#enabling-draft-room) and
[Model Provider Disclosure](#model-provider-disclosure-and-the-origin-allowlist).

The current effective values (along with which pipeline stages/gates are installed)
are always readable at `GET /api/draft-room/capabilities`.

---

## Retention and Deletion

**Where private input bytes live:** uploaded raw inputs are stored outside the
vault's normal upload directories, under `{DATA_DIR}/draft-room/<owner_id>/<draft_id>/inputs/<input_id>/`.
This storage is never read by ordinary document ingestion and is only ever written
to by Draft Room's own upload/promotion code paths.

**Deleting a single input** (`DELETE .../inputs/{input_id}`) tombstones and removes
its stored bytes and database row — but only if the input has not been used by a
completed compile and has no active parse job. If it has been used, the request
returns `409 input_in_use`: the derived revisions and evidence that reference it
are immutable project history and are not deleted along with the input. Deleting
those derivatives selectively is out of scope — the only way to remove them is
whole-draft deletion.

**Deleting a whole draft** (`DELETE /api/draft-room/drafts/{draft_id}`) is a
comprehensive purge: every input file, revision, piece of evidence, claim,
finding, job, and project event for that draft is removed, via cascading deletes
plus the same tombstone-then-delete-row sequence used for a single input (bytes
are moved to a `.trash` staging area and only permanently discarded after the
database transaction commits — if the transaction fails, the tombstoned files are
restored). A running job must be cancelled first; pending jobs are cancelled
automatically. The record of any promotion made from that draft
(`draft_promotions`) is deleted along with it — the promoted **document itself
remains in the vault**, since promotion is a one-way copy, not a link back to the
draft.

**What derived artifacts are retained:** revisions, evidence snapshots, claims,
and findings are kept for the life of the draft (including while archived) as
project history — archiving does not delete inputs or artifacts. If a vault
document, wiki claim/page, or KMS entry that a draft's evidence points to is
later updated or deleted while the vault itself remains, the evidence row is
marked with a `source_deleted_at`/changed-hash flag rather than removed, and any
affected Ready state is invalidated (see [Ready](#what-ready-means-and-does-not-mean)).
That historical evidence snapshot survives until the draft itself is deleted.

**Stale temporary files:** the job processor runs a startup reconciliation pass
that removes anything left in the internal `.incoming` (partially uploaded) or
`.trash` (tombstoned, not yet committed) staging areas after 24 hours, and removes
any `<owner_id>/<draft_id>` directory that no longer corresponds to an existing
draft. This is a fixed internal safeguard, not a configurable setting.

---

## What "Source-Only" Means

A compile run is **source-only** when it completed with no vault evidence
available to check its claims against. This is recorded on the revision's QA
summary, not inferred from anything else.

A source-only revision cannot be marked Ready without the caller explicitly
passing `acknowledge_source_only=true` on the Ready request. This is not a
workaround or a defect being tolerated — it is a deliberate checkpoint: because
there was no vault evidence to fact-check against, the human owner must actively
acknowledge that fact before approving the revision, rather than the system
silently treating an unchecked draft the same as a checked one.

---

## What "Ready" Means (and Does Not Mean)

Marking a revision **Ready** means: **the authenticated draft owner has reviewed
this exact, fact-checked revision and approved it under this workflow.** It is a
human sign-off on a specific byte-identical piece of text, nothing more.

**Ready does not mean:**

- The content is universally, factually true.
- The content has been published or is visible to anyone else.
- Any AI system has verified or attested to the content.

Only the authenticated owner's `POST .../revisions/{revision_id}/ready` request can
make this transition — no job, retry, or automated finding-resolution path can set
a draft to Ready. The transition is refused (`409`, with a stable reason code) if
any of the following are true:

- the draft was modified since the client last read it (`lock_version` mismatch)
- the draft is not currently `needs_review` (`invalid_state`)
- a job is currently active for the draft (`active_job`)
- the specified revision is not the draft's current revision (`not_current_revision`)
- the revision's `fact_status` is not `passed` or `findings` — i.e. it is
  `not_run`, `running`, or `invalidated` (`fact_not_current`)
- the successful Fact-stage candidate's content hash does not exactly match the
  revision's content hash (`fact_candidate_mismatch`)
- any open blocking finding is non-waivable (`non_waivable_blocker`), or any open
  blocking finding — waivable or not — remains unresolved (`unresolved_blocker`)
- a waived blocker is missing a valid actor, reason, or matching rule version
  (`invalid_waiver`), or the waived text changed since the waiver was recorded
  (`stale_waiver`)
- any factual claim on the revision is still open and marked contradicted,
  unsupported, ambiguous, or stale (`unresolved_claim_blocker`) — these are
  non-waivable
- evidence this revision depends on changed or was deleted since it was checked
  (`evidence_changed` / a related freshness code) — this also invalidates the
  revision and reopens the draft for review
- the run was source-only and `acknowledge_source_only=true` was not supplied
  (`source_only_acknowledgment_required`)

---

## Export

`POST /api/draft-room/drafts/{draft_id}/revisions/{revision_id}/export?format=md`
returns the **exact stored revision bytes**, byte for byte, with no warning banner
or other text injected into the body. The response always carries:

- `X-Draft-Fact-Status` — the stored `fact_status` verbatim
- `X-Draft-Approval-Status` — `ready` or `not_ready`
- `X-Draft-Content-Sha256` — the content hash of the exported bytes

The filename takes one of three forms, based on the revision's state at export
time:

| Revision state | Filename | Notes |
|---|---|---|
| Fact status is `not_run`, `running`, or `invalidated` | `<title>-rev<N>-UNVERIFIED.md` | Requires `acknowledge_not_fact_checked=true` on the request, or the export is refused with `422 export_ack_required`. |
| Fact status is `passed` or `findings`, but this is not the draft's current human-Ready revision | `<title>-rev<N>-REVIEW.md` | No acknowledgement required. |
| This is the draft's current Ready revision (`draft.ready_revision_id` matches, draft status is `ready`, and it is still the current revision) | `<title>-rev<N>.md` | The plain filename — no status tag. |

`format` currently only accepts `md`; any other value returns
`422 unsupported_export_format`.

---

## Promotion

`POST /api/draft-room/drafts/{draft_id}/promote` copies a draft's selected input or
revision into the vault as a **new, ordinary document** — it is the only path by
which Draft Room content becomes visible to other vault members.

Key behavior:

- **Requires vault `write` permission** — unlike every other Draft Room operation
  (which only requires ownership plus vault `read`), promotion creates a document
  other vault members can see, so it needs the stronger permission.
- **Copies, never mutates or indexes the private draft material.** The source
  `draft_inputs`/`draft_revisions` row is only read; the promoted bytes are a
  fresh copy written into the vault's normal upload directory. Promoting an input
  copies its raw bytes unchanged; promoting a revision renders its exact
  `content_md` as a new `.md` file.
- **Goes through the exact same internal ingestion seam as a normal upload** —
  the same duplicate-check, `files`-row creation, and background-enqueue sequence
  used by `app.api.routes.documents`. It calls that seam directly
  (`app.services.draft_promotion`); it never makes an HTTP request to the upload
  route.
- **Starts queued, not indexed.** Like any other upload, the promoted document is
  enqueued to the background processor and goes through the ordinary
  parse/chunk/embed pipeline before it is searchable.
- **Follows ordinary duplicate handling.** If a file with identical content
  already exists in the destination vault (pending, processing, or indexed), the
  request is refused with `409 duplicate_document` and the conflicting file's ID.
- **Records provenance.** A `draft_promotions` row is written with the source
  type/ID, its content hash, the destination vault/file ID, who promoted it, and
  when — plus a `draft_promoted` security-audit event. This provenance row is
  deleted if the source draft is later deleted, but the promoted document itself
  is unaffected (see [Retention and Deletion](#retention-and-deletion)).
- **Cannot promote from an archived draft** (`409 invalid_state`), and cannot
  promote an input that has not finished parsing (`409 input_not_ready`).
- Optionally organizes the new document into a folder and/or up to 50 tags in the
  destination vault; the folder/tags are validated to exist and belong to the
  target vault *before* any copy happens, so a bad `folder_id`/`tag_ids` value
  never leaves an orphaned document behind.

---

## Troubleshooting

Stable error codes an operator or user is most likely to see, and what they mean:

| Code | HTTP status | Meaning |
|---|---:|---|
| `draft_room_disabled` | 503 | `DRAFT_ROOM_ENABLED=false`. See [Enabling Draft Room](#enabling-draft-room) for what still works. |
| `vault_access_revoked` | 403 | The draft owner no longer has `read` (or `write`, for promotion) on the draft's vault. |
| `limit_exceeded` | 413 | `DRAFT_MAX_INPUTS` or `DRAFT_MAX_TOTAL_INPUT_MB` was hit for this project. |
| `input_too_large` | 413 | A single upload exceeded the global `MAX_FILE_SIZE_MB` cap. |
| `unsupported_input` | 415 | The file's extension or detected content signature isn't accepted. |
| `parsed_text_limit_exceeded` | — (async parse failure, not an HTTP status) | `DRAFT_MAX_TOTAL_PARSED_CHARS` would be exceeded; the input's parse job fails and its status becomes `failed`. |
| `provider_origin_not_allowed` / `provider_scheme_not_allowed` / `provider_policy_misconfigured` | 503 | The configured model endpoint isn't on the relevant origin allowlist, uses a disallowed scheme, or the allowlist itself is misconfigured (e.g. contains a wildcard). See [Model Provider Disclosure](#model-provider-disclosure-and-the-origin-allowlist). |
| `section_budget_exceeded` | — (job failure) | `DRAFT_MAX_SECTIONS` would be exceeded. |
| `model_call_budget_exceeded` | — (job failure) | `DRAFT_JOB_MAX_MODEL_CALLS` would be exceeded. |
| `job_timeout` | — (job failure) | `DRAFT_JOB_TIMEOUT_SECONDS` (or `DRAFT_PARSE_TIMEOUT_SECONDS` for a parse job) elapsed. |
| `active_job` | 409 | A job is already running/pending for this draft; wait for it or cancel it before retrying the requested action. |
| `input_in_use` | 409 | Attempted to delete an input that a completed compile already used, or that has an active parse job. Delete the whole draft instead if the derived artifacts must go too. |
| `fact_not_current` / `fact_candidate_mismatch` | 409 | The revision has no current fact-check result, or the successful Fact candidate doesn't match this exact revision's text. Re-run compile from the Fact stage. |
| `non_waivable_blocker` / `unresolved_blocker` / `invalid_waiver` / `stale_waiver` / `unresolved_claim_blocker` | 409 | A Ready-blocking condition; see [Ready](#what-ready-means-and-does-not-mean) for the full list. |
| `evidence_changed` / `source_deleted` | 409 | Evidence the revision depended on changed or disappeared; the draft is returned to `needs_review` and a new compile run is required. |
| `source_only_acknowledgment_required` | 409 | The run had no vault evidence; resubmit the Ready request with `acknowledge_source_only=true`. |
| `export_ack_required` | 422 | Exporting a not-fact-current revision without `acknowledge_not_fact_checked=true`. |
| `duplicate_document` | 409 | Promotion found identical content already present in the destination vault; the conflicting `file_id` is included. |
| `input_not_ready` | 409 | Attempted to promote an input whose parse job hasn't reached `ready`. |
| `folder_not_found` / `tag_not_found` / `folder_wrong_vault` / `tag_wrong_vault` | 404 / 409 | The `folder_id`/`tag_ids` supplied to promotion don't exist or belong to a different vault. |

For rate limits (`DRAFT_UPLOAD_RATE_LIMIT`, `DRAFT_COMPILE_RATE_LIMIT`), a request
over the configured rate returns `HTTP 429` until the window resets.
