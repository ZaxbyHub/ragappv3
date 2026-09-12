// Negative-compile interface contracts (issue #258 / TEST-006).
//
// The previous "interface tests" (src/lib/api.interfaces.test.ts, removed in
// the same change) constructed self-authored literals at runtime and asserted
// their own shape — they could not fail when an interface regressed. This
// file is their typecheck-enabled replacement: it assigns deliberately
// INVALID shapes to the real interfaces exported from `@/lib/api`, each
// guarded by `// @ts-expect-error`, plus positive assignments that must
// compile with no directive.
//
// Gate: `npm run typecheck:contracts` (tsc --noEmit -p tsconfig.contracts.json)
// must exit 0. Every @ts-expect-error must be CONSUMED by a real compile
// error; if an interface regresses (e.g. `vault_id?: number`), the directive
// goes unused → TS2578 → non-zero exit. A positive assignment that stops
// compiling is also a non-zero exit.

import type { CreateSessionRequest, Vault } from "@/lib/api";

// ---------------------------------------------------------------------------
// CreateSessionRequest — vault_id is REQUIRED
// ---------------------------------------------------------------------------

/** Valid shape: title + vault_id compiles with no directive. */
export const validCreateSessionRequestWithTitle: CreateSessionRequest = {
  title: "My Session",
  vault_id: 42,
};

/** Valid minimal shape: vault_id alone (title is optional) compiles. */
export const validMinimalCreateSessionRequest: CreateSessionRequest = {
  vault_id: 99,
};

// @ts-expect-error vault_id is required on CreateSessionRequest — omitting it must not compile.
export const createSessionRequestMissingVaultId: CreateSessionRequest = {
  title: "No vault",
};

// ---------------------------------------------------------------------------
// Vault — no is_default field; required fields are required
// ---------------------------------------------------------------------------

/** Valid full shape: every required field present, no excess properties. */
export const validVault: Vault = {
  id: 1,
  name: "Test Vault",
  description: "A test vault description",
  created_at: "2024-01-01T00:00:00Z",
  updated_at: "2024-01-01T00:00:00Z",
  file_count: 10,
  memory_count: 5,
  session_count: 3,
  org_id: null,
  current_user_permission: "admin",
  enrichment_enabled: null,
  effective_enrichment_enabled: true,
  multimodal_provider_enabled: null,
  effective_multimodal_enabled: false,
};

// Vault has no is_default field; the pinned excess property below carries the
// directive (TS2353 reports on the property line, not the literal start).
export const vaultWithIsDefault: Vault = {
  id: 2,
  name: "Full Vault",
  description: "With all fields",
  created_at: "2024-01-01T00:00:00Z",
  updated_at: "2024-01-01T00:00:00Z",
  file_count: 5,
  memory_count: 3,
  session_count: 2,
  org_id: 10,
  current_user_permission: "write",
  enrichment_enabled: null,
  effective_enrichment_enabled: true,
  multimodal_provider_enabled: null,
  effective_multimodal_enabled: false,
  // @ts-expect-error excess property: Vault has no is_default field (TS2353 reports on this line).
  is_default: true,
};

// @ts-expect-error Vault requires id/name/created_at/... — omitting the required name/description must not compile.
export const vaultMissingRequiredFields: Vault = {
  id: 3,
  created_at: "2024-01-01T00:00:00Z",
  updated_at: "2024-01-01T00:00:00Z",
  file_count: 0,
  memory_count: 0,
  session_count: 0,
  org_id: null,
  effective_enrichment_enabled: true,
  effective_multimodal_enabled: true,
};
