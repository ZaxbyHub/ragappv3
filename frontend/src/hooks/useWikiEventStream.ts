import { useEffect, useMemo, useRef, useSyncExternalStore } from "react";
import { API_BASE_URL, getJwtAccessToken, refreshAccessToken } from "@/lib/api";
import { captureAuthPrincipalGeneration, isCurrentAuthOwner, subscribeAuthPrincipal } from "@/lib/api/auth-lifecycle";
import { useAuthOwner } from "@/hooks/useAuthOwner";

export function wikiEventsUrl(vaultId: number | string): string {
  return `${API_BASE_URL}/wiki/events?vault_id=${encodeURIComponent(String(vaultId))}`;
}

type WikiStreamEvent = { type?: string };

const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30000;

/**
 * Subscribe to the wiki compile-job SSE stream for a vault using an
 * authenticated `fetch` (Bearer header), not `EventSource`.
 *
 * EventSource cannot set an Authorization header, and the app never sets an
 * `access_token` cookie (the access JWT lives in memory and rides the Bearer
 * header). An EventSource subscription therefore always 401s and the browser
 * auto-reconnect floods the console. This mirrors the fetch-based streaming +
 * `refreshAccessToken` retry pattern used by `chatStream` in `@/lib/api`, with
 * explicit, bounded reconnect instead of EventSource's unbounded retry.
 *
 * `onJobTerminal` fires for terminal job events (`job_completed`/`job_failed`).
 * It is held in a ref so the live connection only resets when `vaultId` changes,
 * not on every render.
 */
export function useWikiEventStream(
  vaultId: number | null | undefined,
  onJobTerminal: () => void,
): void {
  // A mounted private stream belongs to the principal that opened it.
  // Owner replacement retires this mount; a new mount admits the new owner.
  const owner = useRef(useAuthOwner()).current;
  const generation = useRef(useSyncExternalStore(subscribeAuthPrincipal, captureAuthPrincipalGeneration, captureAuthPrincipalGeneration)).current;
  const scope = useMemo(() => ({ owner, generation, vaultId }), [owner, generation, vaultId]);
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const callbackRef = useRef({ scope, callback: onJobTerminal });
  callbackRef.current = { scope, callback: onJobTerminal };

  useEffect(() => {
    const scopedVaultId = scope.vaultId;
    if (!scopedVaultId) return;
    // jsdom unit tests without a fetch streaming shim should mount cleanly.
    if (typeof fetch === "undefined") return;

    const controller = new AbortController();
    let activeReader: ReadableStreamDefaultReader<Uint8Array> | null = null;
    const current = () => !controller.signal.aborted && scopeRef.current === scope && isCurrentAuthOwner(scope.owner) && captureAuthPrincipalGeneration() === scope.generation;
    let retired = false;
    const retire = () => {
      if (retired) return;
      retired = true;
      controller.abort();
      void activeReader?.cancel().catch(() => {});
    };
    const onOwnerAbort = () => retire();
    scope.owner.signal.addEventListener("abort", onOwnerAbort, { once: true });
    const unsubscribePrincipal = subscribeAuthPrincipal(() => { if (!current()) retire(); });

    const dispatch = (raw: string) => {
      try {
        const data = JSON.parse(raw) as WikiStreamEvent;
        if (data.type === "job_completed" || data.type === "job_failed") {
          if (!current() || callbackRef.current.scope !== scope) return; callbackRef.current.callback();
        }
      } catch {
        // Ignore malformed payloads; keepalive comment lines never reach here.
      }
    };

    // Consume complete SSE events ("\n\n"-separated) from the buffer and return
    // the unconsumed tail (a partial event awaiting more bytes).
    const drainBuffer = (buffer: string): string => {
      let working = buffer;
      let sep = working.indexOf("\n\n");
      while (sep !== -1) {
        const rawEvent = working.slice(0, sep);
        working = working.slice(sep + 2);
        for (const line of rawEvent.split("\n")) {
          if (line.startsWith("data:")) {
            dispatch(line.slice(5).trim());
          }
          // ":"-prefixed keepalive comments and other SSE fields are ignored.
        }
        sep = working.indexOf("\n\n");
      }
      return working;
    };

    // Open one connection. Returns:
    //   'stop'  — fatal (token_invalid, user_inactive, aborted); do not reconnect.
    //   'error' — transient failure; reconnect after the current backoff delay.
    //   'clean' — server closed the stream cleanly (done=true); reconnect from
    //             base delay (backoff reset) so a normal server restart doesn't
    //             leave the client waiting up to RECONNECT_MAX_MS.
    const connectOnce = async (): Promise<"stop" | "error" | "clean"> => {
      if (!current()) return "stop";
      const headers: Record<string, string> = {};
      const token = getJwtAccessToken();
      if (token) headers["Authorization"] = `Bearer ${token}`;

      let response: Response;
      try {
        response = await fetch(wikiEventsUrl(scopedVaultId), {
          method: "GET",
          headers,
          signal: controller.signal,
        });
      } catch {
        return !current() ? "stop" : "error";
      }

      if (!current()) return "stop";
      if (!response.ok) {
        if (response.status === 401 && token) {
          const body = await response.json().catch(() => null);
          if (!current()) return "stop";
          const detail =
            body && typeof body.detail === "string" ? body.detail : "";
          if (detail.includes("token_invalid") || detail.includes("user_inactive")) {
            return "stop"; // fatal — refreshing won't help; stop looping.
          }
          if (detail.includes("token_expired")) {
            // refreshAccessToken rejects on transport failures (#774) — treat
            // that exactly like a null refresh (fatal for this connection)
            // rather than letting it escape as an unhandled rejection.
            let newToken: string | null;
            try {
              newToken = await refreshAccessToken(scope.owner);
            } catch {
              newToken = null;
            }
            return newToken && current() ? "error" : "stop";
          }
        }
        return !current() ? "stop" : "error";
      }

      const reader = response.body?.getReader();
      if (!reader) return !current() ? "stop" : "error";

      activeReader = reader;
      const readWithAbort = () => new Promise<ReadableStreamReadResult<Uint8Array>>((resolve, reject) => {
        if (controller.signal.aborted) { reject(new DOMException("Aborted", "AbortError")); return; }
        let settled = false;
        const finish = (callback: () => void) => {
          if (settled) return;
          settled = true;
          controller.signal.removeEventListener("abort", onAbort);
          callback();
        };
        const onAbort = () => finish(() => reject(new DOMException("Aborted", "AbortError")));
        controller.signal.addEventListener("abort", onAbort, { once: true });
        reader.read().then((value) => finish(() => resolve(value)), (error) => finish(() => reject(error)));
      });
      const decoder = new TextDecoder();
      let buffer = "";
      let cleanEnd = false;
      try {
        for (;;) {
          const { value, done } = await readWithAbort();
          if (!current()) return "stop";
          if (done) { cleanEnd = true; break; }
          buffer += decoder.decode(value, { stream: true });
          buffer = drainBuffer(buffer);
        }
      } catch {
        // Stream interrupted — fall through; cleanEnd stays false.
      } finally {
        if (activeReader === reader) activeReader = null;
        try { reader.releaseLock(); } catch { /* already released */ }
      }
      if (!current()) return "stop";
      return cleanEnd ? "clean" : "error";
    };

    void (async () => {
      let backoff = RECONNECT_BASE_MS;
      while (current()) {
        const result = await connectOnce();
        if (result === "stop" || !current()) break;
        // Reset backoff on a clean server-side close so the next reconnect is
        // fast (≈1 s) rather than inheriting the last error-backoff value.
        if (!current()) break;
        if (result === "clean") backoff = RECONNECT_BASE_MS;
        // Abort-aware backoff (#774 / TQ-sibling-batch-06-04): unmount aborts
        // wake the sleep early so the loop (and this closure) exit promptly
        // instead of lingering until the timer fires.
        // Listener detached on BOTH settle paths: with {once:true} it would
        // leak one closure per reconnect for the mount's lifetime whenever
        // the timer (the normal path) wins the race (PRR-003 / Copilot).
        await new Promise<void>((resolve) => {
          const onAbort = () => {
            controller.signal.removeEventListener("abort", onAbort);
            clearTimeout(timer);
            resolve();
          };
          const timer = setTimeout(onAbort, backoff);
          controller.signal.addEventListener("abort", onAbort);
        });
        if (!current()) break;
        backoff = Math.min(backoff * 2, RECONNECT_MAX_MS);
      }
    })();

    return () => { unsubscribePrincipal(); scope.owner.signal.removeEventListener("abort", onOwnerAbort); retire(); };
  }, [scope]);
}
