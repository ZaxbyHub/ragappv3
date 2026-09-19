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

const PORT = 9090;

const nowIso = () => new Date().toISOString();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const ACCESS_TOKEN = "stub-access-token";
// Login state: refresh/me only serve after a real POST /login in this
// process, so anonymous contexts get 401 and the login form renders.
let loggedIn = false;

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

function readBody(req) {
  return new Promise((resolve) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
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
        needs_setup: false,
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
    if (method === "POST" && path === "/api/auth/refresh") {
      // Real-client contract: silent refresh only works with the httpOnly
      // cookie a prior login set — an anonymous boot must get 401 so the
      // login form actually renders (otherwise the stub would authenticate
      // every fresh context and the login scenario could never run).
      const cookies = req.headers.cookie ?? "";
      if (!loggedIn || !cookies.includes("ragapp_refresh_token=")) {
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
      return sendJson(req, res, 200, { vaults: [VAULT] });
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

    // ---- fallback ----
    return sendJson(req, res, 404, { detail: `stub has no route ${method} ${path}` });
  } catch (err) {
    sendJson(req, res, 500, { detail: `stub error: ${String(err)}` });
  }
});

server.listen(PORT, () => {
  console.log(`[stub-backend] listening on http://localhost:${PORT}`);
});
