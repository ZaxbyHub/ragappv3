import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { getJwtAccessToken, refreshAccessToken } from "@/lib/api";
import {
  draftRoomKeys,
  getDraftEventsUrl,
  type DraftDetail,
  type DraftRoomCapabilities,
} from "@/lib/api/draftRoom";

export type DraftRoomEventType =
  | "subscribed"
  | "job_started"
  | "stage_started"
  | "stage_progress"
  | "stage_completed"
  | "finding_created"
  | "job_completed"
  | "job_failed"
  | "job_cancelled"
  | "heartbeat";

export interface DraftRoomEvent {
  type: DraftRoomEventType;
  draft_id?: number;
  job_id?: number;
  input_id?: number;
  revision_id?: number;
  finding_id?: number;
  job_type?: string;
  status?: string;
  stage?: string;
  attempt?: number;
  progress_percent?: number;
  error_code?: string;
  severity?: string;
  category?: string;
}

export interface UseDraftRoomEventsResult {
  connected: boolean;
  /** true once streaming has failed enough that bounded polling took over */
  pollingFallback: boolean;
  lastEvent: DraftRoomEvent | null;
}

const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 30000;
const MAX_BUFFER_BYTES = 64 * 1024;
const MAX_CONSECUTIVE_FAILURES = 3;
const DEFAULT_POLL_INTERVAL_SECONDS = 2;
const STAGE_PROGRESS_COALESCE_MS = 1000;

const KNOWN_EVENT_TYPES: ReadonlySet<string> = new Set([
  "subscribed",
  "job_started",
  "stage_started",
  "stage_progress",
  "stage_completed",
  "finding_created",
  "job_completed",
  "job_failed",
  "job_cancelled",
  "heartbeat",
]);

function isDraftRoomEvent(value: unknown): value is DraftRoomEvent {
  if (typeof value !== "object" || value === null) return false;
  const type = (value as { type?: unknown }).type;
  return typeof type === "string" && KNOWN_EVENT_TYPES.has(type);
}

/**
 * Subscribe to the Draft Room compile-job SSE stream for a single draft using
 * an authenticated `fetch` (Bearer header), never `EventSource` — mirrors
 * `useWikiEventStream`. `EventSource` can't set an Authorization header and
 * the JWT lives in memory, not a cookie.
 *
 * The server never sends a `heartbeat` data event; it emits bare SSE comment
 * lines (`: keepalive`) on a 15s idle timeout, which this hook silently
 * discards. There is no `Last-Event-ID` support and no replay, so every
 * state-changing event triggers a React Query invalidation (a REST refetch)
 * rather than a local cache patch — that invalidation is the only way a gap
 * in the stream (reconnects, missed frames) gets healed.
 */
export function useDraftRoomEvents(
  draftId: number | null | undefined,
  options?: { enabled?: boolean }
): UseDraftRoomEventsResult {
  const enabled = options?.enabled ?? true;
  const queryClient = useQueryClient();
  const [connected, setConnected] = useState(false);
  const [pollingFallback, setPollingFallback] = useState(false);
  const [lastEvent, setLastEvent] = useState<DraftRoomEvent | null>(null);

  useEffect(() => {
    setConnected(false);
    setPollingFallback(false);
    setLastEvent(null);

    if (!enabled || draftId == null) return;
    // jsdom unit tests without a fetch streaming shim should mount cleanly.
    if (typeof fetch === "undefined") return;

    const id = draftId;
    const controller = new AbortController();
    const encoder = new TextEncoder();
    let failureCount = 0;
    let lastStageProgressInvalidateAt = 0;
    let pollTimer: ReturnType<typeof setInterval> | null = null;

    const stopPolling = () => {
      if (pollTimer != null) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
      setPollingFallback(false);
    };

    const startPolling = () => {
      if (pollTimer != null) return;
      setPollingFallback(true);
      const capabilities = queryClient.getQueryData<DraftRoomCapabilities>(draftRoomKeys.capabilities());
      const configuredSeconds = capabilities?.limits?.poll_interval_seconds;
      const pollSeconds =
        typeof configuredSeconds === "number" ? configuredSeconds : DEFAULT_POLL_INTERVAL_SECONDS;
      const intervalMs = Math.max(500, pollSeconds * 1000);
      pollTimer = setInterval(() => {
        const detail = queryClient.getQueryData<DraftDetail>(draftRoomKeys.detail(id));
        if (detail && detail.active_compile_job == null) {
          stopPolling();
          return;
        }
        queryClient.invalidateQueries({ queryKey: draftRoomKeys.detail(id) });
        queryClient.invalidateQueries({ queryKey: draftRoomKeys.jobs(id) });
      }, intervalMs);
    };

    // Invalidate the canonical React Query keys for a state-changing event —
    // never hand-patch a cache, since the stream guarantees no replay.
    const invalidateForEvent = (evt: DraftRoomEvent) => {
      switch (evt.type) {
        case "job_started":
          queryClient.invalidateQueries({ queryKey: draftRoomKeys.detail(id) });
          queryClient.invalidateQueries({ queryKey: draftRoomKeys.jobs(id) });
          return;
        case "stage_started":
        case "stage_completed":
          queryClient.invalidateQueries({ queryKey: draftRoomKeys.jobs(id) });
          return;
        case "stage_progress": {
          const now = Date.now();
          if (now - lastStageProgressInvalidateAt < STAGE_PROGRESS_COALESCE_MS) return;
          lastStageProgressInvalidateAt = now;
          queryClient.invalidateQueries({ queryKey: draftRoomKeys.jobs(id) });
          return;
        }
        case "finding_created":
          queryClient.invalidateQueries({ queryKey: draftRoomKeys.findings(id) });
          return;
        case "job_completed":
          queryClient.invalidateQueries({ queryKey: draftRoomKeys.detail(id) });
          queryClient.invalidateQueries({ queryKey: draftRoomKeys.jobs(id) });
          queryClient.invalidateQueries({ queryKey: draftRoomKeys.revisions(id) });
          queryClient.invalidateQueries({ queryKey: draftRoomKeys.findings(id) });
          queryClient.invalidateQueries({ queryKey: draftRoomKeys.claims(id) });
          return;
        case "job_failed":
        case "job_cancelled":
          queryClient.invalidateQueries({ queryKey: draftRoomKeys.detail(id) });
          queryClient.invalidateQueries({ queryKey: draftRoomKeys.jobs(id) });
          return;
        case "subscribed":
        case "heartbeat":
        default:
          return;
      }
    };

    const dispatch = (raw: string) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(raw);
      } catch {
        return; // malformed payload — never throw out of the read loop.
      }
      if (!isDraftRoomEvent(parsed)) return; // unknown/unrecognised type — ignore silently.
      setLastEvent(parsed);
      invalidateForEvent(parsed);
    };

    // Consume complete SSE events ("\n\n"-separated) from the buffer and
    // return the unconsumed tail (a partial event awaiting more bytes).
    const drainBuffer = (buffer: string): string => {
      let working = buffer;
      let sep = working.indexOf("\n\n");
      while (sep !== -1) {
        const rawEvent = working.slice(0, sep);
        working = working.slice(sep + 2);
        const dataLines: string[] = [];
        for (const line of rawEvent.split("\n")) {
          if (line.startsWith(":")) continue; // SSE comment (keepalive) — ignore.
          if (line.startsWith("data:")) {
            dataLines.push(line.slice(5).replace(/^ /, ""));
          }
          // Other SSE fields (event:, id:, retry:) are not used by this stream.
        }
        if (dataLines.length > 0) {
          dispatch(dataLines.join("\n"));
        }
        sep = working.indexOf("\n\n");
      }
      // A hostile or broken stream must not grow memory without limit: if no
      // "\n\n" terminator has arrived within 64 KiB, drop the partial buffer
      // and keep reading.
      if (encoder.encode(working).length > MAX_BUFFER_BYTES) {
        return "";
      }
      return working;
    };

    // Open one connection. Returns:
    //   'stop'  — fatal (token_invalid, user_inactive, aborted); do not reconnect.
    //   'error' — transient failure; reconnect after the current backoff delay.
    //   'clean' — server closed the stream cleanly (done=true); reconnect from
    //             base delay (backoff reset).
    const connectOnce = async (): Promise<"stop" | "error" | "clean"> => {
      const headers: Record<string, string> = {};
      const token = getJwtAccessToken();
      if (token) headers["Authorization"] = `Bearer ${token}`;

      let response: Response;
      try {
        response = await fetch(getDraftEventsUrl(id), {
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
          const detail = body && typeof body.detail === "string" ? body.detail : "";
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

      failureCount = 0;
      stopPolling();
      setConnected(true);

      const decoder = new TextDecoder();
      let buffer = "";
      let cleanEnd = false;
      try {
        for (;;) {
          const { value, done } = await reader.read();
          if (done) {
            cleanEnd = true;
            break;
          }
          buffer += decoder.decode(value, { stream: true });
          buffer = drainBuffer(buffer);
        }
      } catch {
        // Stream interrupted — fall through; cleanEnd stays false.
      }
      setConnected(false);
      if (controller.signal.aborted) return "stop";
      return cleanEnd ? "clean" : "error";
    };

    void (async () => {
      let backoff = RECONNECT_BASE_MS;
      while (!controller.signal.aborted) {
        const result = await connectOnce();
        if (result === "stop" || controller.signal.aborted) break;
        if (result === "clean") {
          backoff = RECONNECT_BASE_MS;
        } else {
          failureCount += 1;
          if (failureCount >= MAX_CONSECUTIVE_FAILURES) {
            startPolling();
          }
        }
        await new Promise((resolve) => setTimeout(resolve, backoff));
        backoff = Math.min(backoff * 2, RECONNECT_MAX_MS);
      }
    })();

    return () => {
      controller.abort();
      stopPolling();
    };
  }, [draftId, enabled, queryClient]);

  return { connected, pollingFallback, lastEvent };
}
