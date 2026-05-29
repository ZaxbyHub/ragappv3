import { useEffect, useRef } from "react";
import { API_BASE_URL, getJwtAccessToken, refreshAccessToken } from "@/lib/api";

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
  const callbackRef = useRef(onJobTerminal);
  useEffect(() => {
    callbackRef.current = onJobTerminal;
  }, [onJobTerminal]);

  useEffect(() => {
    if (!vaultId) return;
    // jsdom unit tests without a fetch streaming shim should mount cleanly.
    if (typeof fetch === "undefined") return;

    const controller = new AbortController();

    const dispatch = (raw: string) => {
      try {
        const data = JSON.parse(raw) as WikiStreamEvent;
        if (data.type === "job_completed" || data.type === "job_failed") {
          callbackRef.current();
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
      const headers: Record<string, string> = {};
      const token = getJwtAccessToken();
      if (token) headers["Authorization"] = `Bearer ${token}`;

      let response: Response;
      try {
        response = await fetch(wikiEventsUrl(vaultId), {
          method: "GET",
          headers,
          signal: controller.signal,
        });
      } catch {
        return controller.signal.aborted ? "stop" : "error";
      }

      if (!response.ok) {
        if (response.status === 401 && token) {
          const body = await response.json().catch(() => null);
          const detail =
            body && typeof body.detail === "string" ? body.detail : "";
          if (detail.includes("token_invalid") || detail.includes("user_inactive")) {
            return "stop"; // fatal — refreshing won't help; stop looping.
          }
          if (detail.includes("token_expired")) {
            const newToken = await refreshAccessToken();
            return newToken && !controller.signal.aborted ? "error" : "stop";
          }
        }
        return controller.signal.aborted ? "stop" : "error";
      }

      const reader = response.body?.getReader();
      if (!reader) return controller.signal.aborted ? "stop" : "error";

      const decoder = new TextDecoder();
      let buffer = "";
      let cleanEnd = false;
      try {
        for (;;) {
          const { value, done } = await reader.read();
          if (done) { cleanEnd = true; break; }
          buffer += decoder.decode(value, { stream: true });
          buffer = drainBuffer(buffer);
        }
      } catch {
        // Stream interrupted — fall through; cleanEnd stays false.
      }
      if (controller.signal.aborted) return "stop";
      return cleanEnd ? "clean" : "error";
    };

    void (async () => {
      let backoff = RECONNECT_BASE_MS;
      while (!controller.signal.aborted) {
        const result = await connectOnce();
        if (result === "stop" || controller.signal.aborted) break;
        // Reset backoff on a clean server-side close so the next reconnect is
        // fast (≈1 s) rather than inheriting the last error-backoff value.
        if (result === "clean") backoff = RECONNECT_BASE_MS;
        await new Promise((resolve) => setTimeout(resolve, backoff));
        backoff = Math.min(backoff * 2, RECONNECT_MAX_MS);
      }
    })();

    return () => {
      controller.abort();
    };
  }, [vaultId]);
}
