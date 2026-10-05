// frontend/e2e/stub-backend.mjs — issue #573 (AC7) acceptance check C7.
//
// Zero-dependency (node:http only) stub backend on :9090 implementing the
// minimum contract the REAL frontend client exercises, read directly from
// the client code:
//
//   Auth (frontend/src/stores/useAuthStore.ts, lib/api/core.ts):
//     GET  /api/auth/setup-status  -> { needs_setup, auth_mode, users_enabled } (no auth)
//     GET  /api/csrf-token         -> { csrf_token } + X-CSRF-Token cookie
//                                     (core.ts ensureCsrfToken: GET ${API_BASE_URL}/csrf-token)
//     POST /api/auth/login         -> { access_token, user } + httpOnly refresh cookie
//                                     (login forces a fresh CSRF token first)
//     POST /api/auth/refresh       -> { access_token } (init after reload uses the cookie)
//     GET  /api/auth/me            -> user
//     POST /api/auth/logout        -> {}
//     CSRF is permissive: any/no X-CSRF-Token header is accepted (the client
//     always sends one it obtained from /api/csrf-token).
//
//   Health (lib/api/health.ts; App root useHealthCheck):
//     GET /api/health              -> { status, services: {...} }
//     GET /api/llm-health/modes    -> { thinking, instant } (Composer polls it)
//
//   Vaults (stores/useVaultStore.ts -> lib/api/vaults.ts):
//     GET /api/vaults/accessible | /api/vaults -> { vaults: [...] }
//
//   Chat sessions (lib/api/sessions.ts; hooks/useSendMessage.ts persistTurn):
//     GET  /api/chat/sessions                 -> { sessions } (newest first)
//     POST /api/chat/sessions                 -> ChatSession
//     GET  /api/chat/sessions/:id             -> { ...session, messages } (incl.
//                                                 persisted "interrupted" rows)
//     POST /api/chat/sessions/:id/messages    -> ChatSessionMessage
//     POST /api/chat/sessions/:id/messages/batch -> { messages } (durable turn save;
//                                                 Stop persists status "interrupted" here)
//     POST /api/chat/sessions/:id/truncate    -> { remaining_count, tail_seq }
//
//   Chat stream (lib/api/sessions.ts chatStream POST /api/chat/stream):
//     Body { messages, vault_id, mode, temperature, retrieval_mode,
//            citation_mode, metadata_filter, document_ids, session_id, turn_id }.
//     SSE frames (data: <json>\n\n): mode, several content chunks, version-1
//     evidence candidates, mid-stream sources, terminal done with final
//     sources + llm_metrics. Request markers in the LAST user message:
//       * content containing "SLOW"  -> stream one content chunk + evidence,
//         then keep the stream open (ping comments) — never sends done, so
//         the Stop test can interrupt mid-generation.
//       * content containing "LENGTH" -> done with
//         llm_metrics.finish_reason "length"; otherwise "stop".
//
// All state is in memory; the stub resets when the process restarts.

import http from "node:http";

// E2E_STUB_PORT (issue #781): local runs can move the stub off the default
// :9090 (e.g. when a foreign service squats it) — playwright.config.ts and
// the vite preview proxy read the same variable, so the whole e2e tier moves
// together. CI never sets it, so the default keeps CI byte-identical.
const PORT = Number(process.env.E2E_STUB_PORT || 9090);

const nowIso = () => new Date().toISOString();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const ACCESS_TOKEN = "stub-access-token";
// Login state: refresh/me only serve after a real POST /login in this
// process, so anonymous contexts get 401 and the login form renders.
let loggedIn = false;

// Issue #782: first-run onboarding milestone state. DEFAULT OFF — the
// checklist surface and the setup wizard are opt-in via the /_e2e/* control
// routes so the other shared-stub specs (chat-smoke, chat-width-budget)
// never observe them, whatever the run order. Module-level defaults re-arm
// at process start (the PRR-010 loggedIn pattern).
let setupMode = false;
const onboarding = {
  enabled: false,
  vault_created: false,
  upload_indexed: false,
  first_question_asked: false,
  first_citation_opened: false,
  dismissed: false,
};

function onboardingPayload() {
  const allFour =
    onboarding.vault_created &&
    onboarding.upload_indexed &&
    onboarding.first_question_asked &&
    onboarding.first_citation_opened;
  // Mirrors the server formula (GET /onboarding/milestones): hidden once
  // dismissed or complete — gated additionally on the test-control enable.
  return {
    vault_created: onboarding.vault_created,
    upload_indexed: onboarding.upload_indexed,
    first_question_asked: onboarding.first_question_asked,
    first_citation_opened: onboarding.first_citation_opened,
    show_checklist: onboarding.enabled && !onboarding.dismissed && !allFour,
  };
}

function resetOnboarding() {
  onboarding.enabled = false;
  onboarding.vault_created = false;
  onboarding.upload_indexed = false;
  onboarding.first_question_asked = false;
  onboarding.first_citation_opened = false;
  onboarding.dismissed = false;
}

const USER = {
  id: 1,
  username: "e2e-user",
  full_name: "E2E User",
  role: "superadmin",
  is_active: true,
};

const VAULT = {
  id: 1,
  name: "E2E Vault",
  description: "stub vault",
  created_at: nowIso(),
  updated_at: nowIso(),
  file_count: 2,
  memory_count: 0,
  session_count: 0,
  org_id: null,
  current_user_permission: "admin",
  enrichment_enabled: null,
  effective_enrichment_enabled: false,
  multimodal_provider_enabled: null,
  effective_multimodal_enabled: false,
};

const SOURCE = {
  id: "42_default_0",
  file_id: "42",
  filename: "handbook.pdf",
  section: "Maintenance",
  source_label: "S1",
  page_number: 3,
  snippet: "The coolant interval is 500 hours.",
  score: 0.87,
  score_type: "rerank",
};

// ---- in-memory state --------------------------------------------------------

const sessions = new Map(); // id -> { session, messages: [] }
let nextSessionId = 1;
let nextMessageId = 1;

// Mutable vault list (issue #781): the first-run walkthrough creates a vault
// through the real /vaults UI, so GET must reflect POSTs.
const vaults = [VAULT];
let nextVaultId = 2;

// Uploaded documents (issue #781): the walkthrough uploads one file through
// the real UploadDropzone; the list and stats reflect it.
const documents = [];
let nextDocumentId = 1;

// Activity tray job seeds (issue #784): created through POST /_e2e/jobs.
// Every family's seed materializes in the REAL route shape the tray's
// useActivityJobs adapters poll (wiki/kms/reindex job rows, a draft + its
// job for draft-room, and a processing document row for ingest — seed
// status "running" is not a document status, so it lands as "processing").
// The action routes below mutate these same seed records, which is what
// GET /_e2e/jobs?family= reads back.
const E2E_JOB_FAMILIES = ["ingest", "wiki", "kms", "draft-room", "reindex"];
const e2eSeeds = Object.fromEntries(E2E_JOB_FAMILIES.map((f) => [f, []]));
let nextE2eJobId = 1;
let nextE2eDraftId = 1;

const E2E_TERMINAL_JOB_STATUSES = ["completed", "failed", "cancelled"];

function resetE2eJobs() {
  for (const family of E2E_JOB_FAMILIES) {
    e2eSeeds[family] = [];
  }
  // Ingest seeds materialize document rows; drop only the rows the seeder
  // created (flagged at seed time), never walkthrough-uploaded documents.
  for (let i = documents.length - 1; i >= 0; i -= 1) {
    if (documents[i].e2eSeed) documents.splice(i, 1);
  }
}

function e2eJobShape(family, seed) {
  const base = {
    id: seed.id,
    status: seed.status,
    // A failed seed's title rides the error field - the same place the
    // real backend puts a failed job's reason, and the field the tray
    // surfaces as a failed row's title.
    error: seed.status === "failed" ? seed.title ?? `job ${seed.id} failed` : null,
    result_json: "{}",
    created_at: seed.created_at,
    started_at: seed.created_at,
    completed_at: null,
    retry_count: 0,
  };
  if (family === "wiki") {
    return { ...base, vault_id: 1, trigger_type: "manual", trigger_id: null };
  }
  if (family === "kms") {
    return { ...base, vault_id: 1, trigger_type: "manual", trigger_id: null, input_json: "{}" };
  }
  if (family === "reindex") {
    return { ...base, vault_id: 1, trigger_type: "api", trigger_id: "1", input_json: "{}" };
  }
  // draft-room
  return {
    ...base,
    draft_id: seed.draftId,
    job_type: "compile",
    start_stage: null,
    active_stage: seed.status === "running" ? "compiling" : null,
    progress_percent: 0,
    model_call_count: 0,
    max_model_calls: 0,
    parent_job_id: null,
    attempt_no: 1,
    compile_input_sha256: null,
    prompt_bundle_version: null,
    timeout_seconds: 1800,
    cancel_requested_at: null,
    heartbeat_at: null,
    error_code: null,
    error_message: null,
  };
}

function createSession(vaultId = 1) {
  const id = nextSessionId++;
  const session = {
    id,
    vault_id: vaultId,
    title: null,
    created_at: nowIso(),
    updated_at: nowIso(),
    message_count: 0,
    forked_from_session_id: null,
    fork_message_index: null,
  };
  sessions.set(id, { session, messages: [] });
  return session;
}

function addMessage(sessionId, { role, content, sources = null, turn_id = null, status = null, mode = null }) {
  const entry = sessions.get(sessionId);
  if (!entry) return null;
  const message = {
    id: nextMessageId++,
    role,
    content,
    sources,
    memories: null,
    wiki_refs: null,
    kms_refs: null,
    created_at: nowIso(),
    feedback: null,
    mode,
    seq: entry.messages.length + 1,
    turn_id,
    status,
    citation_confidence: null,
    unverifiable_claims: null,
    currency_warnings: null,
    citation_enforcement: null,
  };
  entry.messages.push(message);
  entry.session.message_count = entry.messages.length;
  entry.session.updated_at = nowIso();
  return message;
}

// ---- helpers ----------------------------------------------------------------

function corsHeaders(req) {
  return {
    "Access-Control-Allow-Origin": req.headers.origin || "*",
    "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
    "Access-Control-Allow-Headers":
      "Content-Type,Authorization,X-CSRF-Token,Last-Event-ID",
    "Access-Control-Allow-Credentials": "true",
    "Access-Control-Expose-Headers": "X-CSRF-Token",
  };
}

function sendJson(req, res, status, body, extraHeaders = {}) {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Cache-Control": "no-store",
    ...corsHeaders(req),
    ...extraHeaders,
  });
  res.end(payload);
}

const MAX_BODY_BYTES = 5 * 1024 * 1024; // PRR-012: bound request bodies (413 beyond this)

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on("data", (c) => {
      size += c.length;
      if (size > MAX_BODY_BYTES) {
        reject(new Error("payload too large"));
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString("utf-8");
      try {
        resolve(raw ? JSON.parse(raw) : {});
      } catch {
        resolve({});
      }
    });
    req.on("error", () => resolve({}));
  });
}

function sseFrame(res, obj) {
  res.write(`data: ${JSON.stringify(obj)}\n\n`);
}

// ---- the stub ----------------------------------------------------------------

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  const path = url.pathname.replace(/\/+$/, "") || "/";
  const method = req.method || "GET";

  if (method === "OPTIONS") {
    res.writeHead(204, corsHeaders(req));
    res.end();
    return;
  }

  try {
    // ---- health ----
    if (method === "GET" && path === "/api/health") {
      return sendJson(req, res, 200, {
        status: "ok",
        version: "e2e-stub",
        timestamp: nowIso(),
        services: { backend: true, embeddings: true, chat: true, vector_store: true },
      });
    }
    if (method === "GET" && path === "/api/llm-health/modes") {
      return sendJson(req, res, 200, { thinking: true, instant: true });
    }

    // ---- auth ----
    if (method === "GET" && path === "/api/auth/setup-status") {
      return sendJson(req, res, 200, {
        needs_setup: setupMode,
        auth_mode: "jwt",
        users_enabled: true,
      });
    }
    if (method === "GET" && (path === "/api/csrf-token" || path === "/api/auth/csrf")) {
      return sendJson(
        req,
        res,
        200,
        { csrf_token: "stub-csrf-token" },
        { "Set-Cookie": "X-CSRF-Token=stub-csrf-token; Path=/; SameSite=Lax" }
      );
    }
    if (method === "POST" && path === "/api/auth/login") {
      await readBody(req); // accept any credentials
      loggedIn = true;
      return sendJson(
        req,
        res,
        200,
        { access_token: ACCESS_TOKEN, user: USER },
        {
          "Set-Cookie": [
            "ragapp_refresh_token=stub-refresh-token; Path=/; HttpOnly; SameSite=Lax",
            "X-CSRF-Token=stub-csrf-token; Path=/; SameSite=Lax",
          ],
        }
      );
    }
    if (method === "POST" && path === "/api/auth/register") {
      // Issue #782 setup leg: the wizard's account step registers the first
      // admin. Mirrors login's session side-effects (PRR-010-critical: the
      // post-wizard reloads POST /api/auth/refresh) and ends setup mode.
      await readBody(req); // accept any payload
      loggedIn = true;
      setupMode = false;
      return sendJson(
        req,
        res,
        200,
        { access_token: ACCESS_TOKEN, user: USER },
        {
          "Set-Cookie": [
            "ragapp_refresh_token=stub-refresh-token; Path=/; HttpOnly; SameSite=Lax",
            "X-CSRF-Token=stub-csrf-token; Path=/; SameSite=Lax",
          ],
        }
      );
    }
    if (method === "POST" && path === "/api/auth/refresh") {
      // Real-client contract: silent refresh only works with the httpOnly
      // cookie a prior login set — an anonymous boot must get 401 so the
      // login form actually renders (otherwise the stub would authenticate
      // every fresh context and the login scenario could never run).
      const cookies = req.headers.cookie ?? "";
      if (!loggedIn || !cookies.includes("ragapp_refresh_token=")) {
        // PRR-010: an anonymous boot re-arms the gate so a stale login state
        // (process-global, reused server) can never leak into a fresh run.
        loggedIn = false;
        return sendJson(req, res, 401, { detail: "not authenticated" });
      }
      return sendJson(req, res, 200, { access_token: ACCESS_TOKEN });
    }
    if (method === "GET" && path === "/api/auth/me") {
      const auth = req.headers.authorization ?? "";
      if (!auth.includes(ACCESS_TOKEN)) {
        return sendJson(req, res, 401, { detail: "not authenticated" });
      }
      return sendJson(req, res, 200, USER);
    }
    if (method === "POST" && path === "/api/auth/logout") {
      return sendJson(req, res, 200, { ok: true });
    }

    // ---- vaults ----
    if (method === "GET" && (path === "/api/vaults/accessible" || path === "/api/vaults")) {
      return sendJson(req, res, 200, { vaults });
    }
    if (method === "POST" && path === "/api/vaults") {
      // Issue #781 walkthrough: create a vault via the real VaultsPage
      // dialog. Accept { name, description? } and append to the list so the
      // selector (and the chat recovery leg) can pick it up.
      const body = await readBody(req);
      const name = String(body.name || "E2E Vault").slice(0, 100);
      const vault = {
        ...VAULT,
        id: nextVaultId++,
        name,
        description: String(body.description || ""),
        created_at: nowIso(),
        updated_at: nowIso(),
        file_count: 0,
      };
      vaults.push(vault);
      onboarding.vault_created = true;
      return sendJson(req, res, 200, vault);
    }

    // ---- documents (issue #781 walkthrough; #784 adds the status filter
    // the Activity tray's ingest adapter polls) ----
    if (method === "GET" && path === "/api/documents") {
      const vaultId = Number(url.searchParams.get("vault_id") || 0);
      const status = url.searchParams.get("status");
      let scoped = vaultId ? documents.filter((d) => d.vault_id === vaultId) : documents;
      if (status) {
        scoped = scoped.filter((d) => (d.metadata?.status ?? "") === status);
      }
      return sendJson(req, res, 200, { documents: scoped, total: scoped.length });
    }
    if (method === "GET" && path === "/api/documents/stats") {
      return sendJson(req, res, 200, {
        total_documents: documents.length,
        total_chunks: documents.length,
        total_size_bytes: documents.reduce((sum, d) => sum + (d.size ?? 0), 0),
        documents_by_status: documents.length ? { processed: documents.length } : {},
      });
    }
    if (method === "POST" && path === "/api/documents") {
      // Multipart upload — read the raw body and lift the filename from the
      // content-disposition part header; the stub never parses the payload.
      const raw = await new Promise((resolve) => {
        const chunks = [];
        let size = 0;
        req.on("data", (c) => {
          size += c.length;
          if (size > MAX_BODY_BYTES) {
            resolve("");
            req.destroy();
            return;
          }
          chunks.push(c);
        });
        req.on("end", () => resolve(Buffer.concat(chunks).toString("utf-8")));
        req.on("error", () => resolve(""));
      });
      const disposition = raw.match(/filename="([^"]+)"/);
      const filename = disposition ? disposition[1] : `e2e-upload-${nextDocumentId}.txt`;
      const vaultId = Number(url.searchParams.get("vault_id") || 0) || vaults[0]?.id || 1;
      const doc = {
        id: String(nextDocumentId++),
        filename,
        vault_id: vaultId,
        content_type: "text/plain",
        size: raw.length,
        created_at: nowIso(),
        processed_at: nowIso(),
        error_message: null,
        metadata: { status: "processed", chunk_count: 1 },
      };
      documents.push(doc);
      onboarding.upload_indexed = true;
      return sendJson(req, res, 200, { id: doc.id, filename, status: "processed" });
    }

    // ---- Activity tray job routes (issue #784) ----
    // List routes mirror the real backend shapes the tray's adapters poll.
    // (Dedicated matcher: the chat section's `let m` is declared further
    // down — using it here would hit its temporal dead zone.)
    let trayMatch;
    // Wiki/KMS lists honor the vault_id query the real adapters send, so a
    // multi-vault stub state cannot duplicate rows per vault the way the
    // real per-vault backend never would (review F-003/M-PLAT-01).
    const trayVaultId = Number(url.searchParams.get("vault_id") || 0);
    const inTrayVault = (seed) => !trayVaultId || (seed.vaultId ?? 1) === trayVaultId;
    if (method === "GET" && path === "/api/wiki/jobs") {
      return sendJson(req, res, 200, {
        jobs: e2eSeeds.wiki.filter(inTrayVault).map((s) => e2eJobShape("wiki", s)),
      });
    }
    if (method === "GET" && path === "/api/kms/jobs") {
      return sendJson(req, res, 200, {
        jobs: e2eSeeds.kms.filter(inTrayVault).map((s) => e2eJobShape("kms", s)),
      });
    }
    if (method === "GET" && path === "/api/documents/reindex/jobs") {
      return sendJson(req, res, 200, {
        jobs: e2eSeeds.reindex.map((s) => e2eJobShape("reindex", s)),
      });
    }
    if (method === "GET" && path === "/api/draft-room/drafts") {
      const items = e2eSeeds["draft-room"].map((s) => ({
        id: s.draftId,
        vault_id: 1,
        title: s.title ?? `Draft ${s.draftId}`,
        status: "ready",
        active_job_id: s.status === "running" ? s.id : null,
      }));
      return sendJson(req, res, 200, { items, total: items.length, page: 1, per_page: 50 });
    }
    if ((trayMatch = path.match(/^\/api\/draft-room\/drafts\/(\d+)\/jobs$/)) && method === "GET") {
      const draftId = Number(trayMatch[1]);
      const items = e2eSeeds["draft-room"]
        .filter((s) => s.draftId === draftId)
        .map((s) => e2eJobShape("draft-room", s));
      return sendJson(req, res, 200, { items, total: items.length, page: 1, per_page: 50 });
    }
    // Action routes: mutate the SAME seed records the list routes and
    // GET /_e2e/jobs read, with the production terminal-state gates — a
    // cancel on a terminal job 404s (wiki_store refuses non-pending/
    // non-running), a retry only from failed, an ingest cancel on a
    // finished document 409s (documents.py) — so the e2e cannot pass with
    // an affordance the real server would refuse (review F-003/M-PLAT-01,
    // PRR-026).
    if ((trayMatch = path.match(/^\/api\/wiki\/jobs\/(\d+)\/(cancel|retry)$/)) && method === "POST") {
      const seed = e2eSeeds.wiki.find((s) => s.id === Number(trayMatch[1]));
      if (!seed) return sendJson(req, res, 404, { detail: "Job not found or not cancellable" });
      if (trayMatch[2] === "cancel") {
        if (E2E_TERMINAL_JOB_STATUSES.includes(seed.status)) {
          return sendJson(req, res, 404, { detail: "Job not found or not cancellable" });
        }
        seed.status = "cancelled";
      } else {
        if (seed.status !== "failed") {
          return sendJson(req, res, 404, { detail: "Job is not in 'failed' state" });
        }
        seed.status = "pending";
      }
      return sendJson(req, res, 200, { job_id: seed.id, status: seed.status });
    }
    if (
      (trayMatch = path.match(/^\/api\/draft-room\/drafts\/(\d+)\/jobs\/(\d+)\/(cancel|retry)$/)) &&
      method === "POST"
    ) {
      const seed = e2eSeeds["draft-room"].find(
        (s) => s.draftId === Number(trayMatch[1]) && s.id === Number(trayMatch[2])
      );
      if (!seed) return sendJson(req, res, 404, { detail: "job not found" });
      if (trayMatch[3] === "cancel") {
        if (E2E_TERMINAL_JOB_STATUSES.includes(seed.status)) {
          return sendJson(req, res, 404, { detail: "job not cancellable" });
        }
        seed.status = "cancelled";
      } else {
        if (seed.status !== "failed") {
          return sendJson(req, res, 404, { detail: "job not in 'failed' state" });
        }
        seed.status = "pending";
      }
      return sendJson(req, res, 200, e2eJobShape("draft-room", seed));
    }
    if ((trayMatch = path.match(/^\/api\/documents\/(\d+)\/cancel$/)) && method === "POST") {
      const doc = documents.find((d) => String(d.id) === trayMatch[1]);
      if (!doc) return sendJson(req, res, 404, { detail: "file not found" });
      const current = doc.metadata?.status ?? "";
      if (current === "cancelled") {
        return sendJson(req, res, 200, { file_id: doc.id, status: "cancelled" });
      }
      if (["indexed", "partial", "error"].includes(current)) {
        return sendJson(req, res, 409, { detail: "Ingest already finished; nothing to cancel" });
      }
      doc.metadata = { ...(doc.metadata ?? {}), status: "cancelled" };
      const seed = e2eSeeds.ingest.find((s) => String(s.docId) === trayMatch[1]);
      if (seed) seed.status = "cancelled";
      return sendJson(req, res, 200, { file_id: doc.id, status: "cancelled" });
    }

    // ---- chat sessions ----
    if (method === "GET" && path === "/api/chat/sessions") {
      const list = Array.from(sessions.values())
        .map((e) => e.session)
        .sort((a, b) => b.id - a.id); // newest first
      return sendJson(req, res, 200, { sessions: list });
    }
    if (method === "POST" && path === "/api/chat/sessions") {
      const body = await readBody(req);
      const session = createSession(Number(body.vault_id) || 1);
      return sendJson(req, res, 200, session);
    }
    let m;
    if ((m = path.match(/^\/api\/chat\/sessions\/(\d+)$/)) && method === "GET") {
      const entry = sessions.get(Number(m[1]));
      if (!entry) return sendJson(req, res, 404, { detail: "session not found" });
      return sendJson(req, res, 200, { ...entry.session, messages: entry.messages });
    }
    if ((m = path.match(/^\/api\/chat\/sessions\/(\d+)\/messages$/)) && method === "POST") {
      const body = await readBody(req);
      const row = addMessage(Number(m[1]), body);
      if (!row) return sendJson(req, res, 404, { detail: "session not found" });
      return sendJson(req, res, 200, row);
    }
    if ((m = path.match(/^\/api\/chat\/sessions\/(\d+)\/messages\/batch$/)) && method === "POST") {
      const body = await readBody(req);
      const rows = (body.messages || []).map((msg) => addMessage(Number(m[1]), msg));
      if (rows.some((r) => r === null)) {
        return sendJson(req, res, 404, { detail: "session not found" });
      }
      return sendJson(req, res, 200, { messages: rows });
    }
    if ((m = path.match(/^\/api\/chat\/sessions\/(\d+)\/truncate$/)) && method === "POST") {
      const body = await readBody(req);
      const entry = sessions.get(Number(m[1]));
      if (!entry) return sendJson(req, res, 404, { detail: "session not found" });
      const keepSeq = Number(body.keep_seq) || 0;
      entry.messages = entry.messages.filter((row) => (row.seq ?? 0) <= keepSeq);
      entry.session.message_count = entry.messages.length;
      return sendJson(req, res, 200, {
        remaining_count: entry.messages.length,
        tail_seq: entry.messages.length ? entry.messages[entry.messages.length - 1].seq : null,
      });
    }

    // ---- chat stream (SSE) ----
    if (method === "POST" && path === "/api/chat/stream") {
      const body = await readBody(req);
      const lastUser = (body.messages || []).filter((x) => x.role === "user").pop();
      if (lastUser) onboarding.first_question_asked = true;
      const userText = String(lastUser?.content ?? "");
      const slow = /SLOW/i.test(userText);
      const finishReason = /LENGTH/i.test(userText) ? "length" : "stop";

      res.writeHead(200, {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache, no-transform",
        Connection: "keep-alive",
        "X-Accel-Buffering": "no",
        ...corsHeaders(req),
      });

      if (slow) {
        // One chunk + evidence, then hold the stream open (ping comments) so
        // the Stop test can interrupt mid-generation. No done frame.
        sseFrame(res, { type: "mode", mode: "thinking" });
        await sleep(60);
        sseFrame(res, { type: "content", content: "The coolant interval is 500 hours according to the manual" });
        await sleep(60);
        sseFrame(res, {
          type: "evidence",
          version: 1,
          phase: "candidates",
          candidates: [SOURCE],
        });
        const ping = setInterval(() => {
          try {
            res.write(": ping\n\n");
          } catch {
            clearInterval(ping);
          }
        }, 400);
        res.on("close", () => clearInterval(ping));
        return;
      }

      // Normal exchange: mode -> content chunks -> evidence -> sources -> done.
      sseFrame(res, { type: "mode", mode: "thinking" });
      await sleep(50);
      const chunks = [
        "Here is the streamed answer ",
        "with a citation [S1]. ",
        "The manual states the coolant interval is 500 hours.",
      ];
      for (const chunk of chunks) {
        sseFrame(res, { type: "content", content: chunk });
        await sleep(40);
      }
      sseFrame(res, {
        type: "evidence",
        version: 1,
        phase: "candidates",
        candidates: [SOURCE],
      });
      await sleep(40);
      sseFrame(res, { type: "sources", sources: [SOURCE], score_type: "rerank" });
      await sleep(40);
      sseFrame(res, {
        type: "done",
        sources: [SOURCE],
        turn_id: body.turn_id ?? "stub-turn-1",
        llm_metrics: { finish_reason: finishReason, reasoning_duration_ms: 12 },
      });
      res.end();
      return;
    }

    // ---- onboarding milestones (issue #782) ----
    if (method === "GET" && path === "/api/onboarding/milestones") {
      return sendJson(req, res, 200, onboardingPayload());
    }
    if (method === "POST" && path === "/api/onboarding/milestones/citation-opened") {
      await readBody(req);
      onboarding.first_citation_opened = true;
      return sendJson(req, res, 200, { ok: true });
    }
    if (method === "POST" && path === "/api/onboarding/milestones/dismiss") {
      await readBody(req);
      onboarding.dismissed = true;
      return sendJson(req, res, 200, { ok: true });
    }

    // ---- e2e control routes (issue #782; test-only) ----
    // Activity tray seeding (issue #784): POST /_e2e/jobs body
    // { family, status?, title? } seeds one job; default status "running".
    // Ingest seeds materialize as a PROCESSING document row (the adapter
    // polls GET /api/documents?status=...); draft-room seeds as a draft +
    // its job; wiki/kms/reindex land in vault 1.
    if (method === "POST" && path === "/_e2e/jobs") {
      const body = await readBody(req);
      const family = String(body.family || "");
      if (!E2E_JOB_FAMILIES.includes(family)) {
        return sendJson(req, res, 400, { detail: `unknown family ${family}` });
      }
      const status = String(body.status || "running");
      const title = body.title ? String(body.title) : null;
      const id = nextE2eJobId++;
      const seed = { id, family, status, title, created_at: nowIso(), vaultId: 1 };
      if (family === "ingest") {
        const docId = nextDocumentId++;
        seed.docId = String(docId);
        documents.push({
          id: String(docId),
          filename: title ?? `activity-tray-ingest-${docId}.txt`,
          vault_id: 1,
          size: 128,
          content_type: "text/plain",
          created_at: seed.created_at,
          // Seeded phase text so the tray's documentProgress-derived phase
          // is assertable end-to-end.
          metadata: {
            status: status === "pending" ? "pending" : "processing",
            phase: "parsing",
            chunk_count: 0,
          },
          e2eSeed: true,
        });
      } else if (family === "draft-room") {
        seed.draftId = nextE2eDraftId++;
      }
      e2eSeeds[family].push(seed);
      return sendJson(req, res, 200, { id: seed.id, family: seed.family, status: seed.status });
    }
    if (method === "GET" && path === "/_e2e/jobs") {
      const family = url.searchParams.get("family") || "";
      if (!E2E_JOB_FAMILIES.includes(family)) {
        return sendJson(req, res, 400, { detail: `unknown family ${family}` });
      }
      return sendJson(req, res, 200, { jobs: e2eSeeds[family] });
    }
    // Reset the tray's seed state between tests: exact-count and id-scoped
    // assertions must not depend on seeds leaked from an earlier spec or a
    // reused stub server (PRR-003 / review F-004).
    if (method === "POST" && path === "/_e2e/reset-jobs") {
      await readBody(req);
      resetE2eJobs();
      return sendJson(req, res, 200, { ok: true });
    }
    if (method === "POST" && path === "/_e2e/onboarding") {
      const body = await readBody(req);
      resetOnboarding();
      if (body.enable) onboarding.enabled = true;
      return sendJson(req, res, 200, onboardingPayload());
    }
    if (method === "POST" && path === "/_e2e/setup-mode") {
      const body = await readBody(req);
      setupMode = Boolean(body.needs_setup);
      return sendJson(req, res, 200, { needs_setup: setupMode });
    }

    // ---- fallback ----
    return sendJson(req, res, 404, { detail: `stub has no route ${method} ${path}` });
  } catch (err) {
    sendJson(req, res, 500, { detail: `stub error: ${String(err)}` });
  }
});

// PRR-012: bind loopback only — the stub never needs LAN exposure.
server.listen(PORT, "127.0.0.1", () => {
  console.log(`[stub-backend] listening on http://localhost:${PORT}`);
});
