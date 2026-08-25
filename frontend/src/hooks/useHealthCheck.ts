import { useState, useEffect, useCallback, useRef } from "react";
import apiClient, { type HealthResponse } from "@/lib/api";
import type { HealthStatus } from "@/types/health";

interface UseHealthCheckOptions {
  pollInterval?: number;
}

/** Deep re-check backstop: at least one real (deep) check this often, so a
 * retained "up" status can never go stale indefinitely. */
const DEEP_RECHECK_INTERVAL = 90_000;

/** Consecutive fetch failures required before services flip to "down", so a
 * single transient blip doesn't flash the reconnect banner. */
const FAILURE_THRESHOLD = 2;

/** Polls the backend health endpoint and returns service availability status.
 *
 * A `null`/absent service value from the backend means "not checked this
 * cycle" — the previous value is retained, never coerced to `false`. Only an
 * explicit `false` from the backend, or repeated fetch failures, mark a
 * service as down.
 */
export function useHealthCheck(options?: UseHealthCheckOptions): HealthStatus {
  const [health, setHealth] = useState<HealthStatus>({
    backend: false,
    embeddings: false,
    chat: false,
    loading: true,
    lastChecked: null,
  });

  const isFirstCheck = useRef(true);
  const failStreak = useRef(0);
  const hadSuccess = useRef(false);
  const lastDeepAt = useRef(0);

  const checkHealth = useCallback(async () => {
    const deep =
      isFirstCheck.current ||
      Date.now() - lastDeepAt.current >= DEEP_RECHECK_INTERVAL;
    try {
      // First check and periodic backstops include deep model probing;
      // other polls are lightweight (server serves cached last-known status)
      const params = deep ? { deep: true } : {};
      if (deep) lastDeepAt.current = Date.now();
      isFirstCheck.current = false;
      failStreak.current = 0;
      hadSuccess.current = true;

      const response = await apiClient.get<HealthResponse>("/health", { params });
      const services = response.data.services;

      const newBackend = services?.backend ?? response.data.status === "ok";

      setHealth((prev) => ({
        backend: newBackend,
        // null/undefined = "not checked": retain last known value
        embeddings: services?.embeddings ?? prev.embeddings,
        chat: services?.chat ?? prev.chat,
        loading: false,
        lastChecked: new Date(),
      }));
    } catch {
      failStreak.current += 1;
      // Before the first successful check, surface failure immediately (the
      // initial state is already "down"); afterwards require consecutive
      // failures so transient blips don't flap the banner.
      if (hadSuccess.current && failStreak.current < FAILURE_THRESHOLD) return;
      setHealth((prev) => {
        if (
          !prev.backend &&
          !prev.embeddings &&
          !prev.chat &&
          !prev.loading
        ) {
          return prev;
        }
        return {
          backend: false,
          embeddings: false,
          chat: false,
          loading: false,
          lastChecked: new Date(),
        };
      });
    }
  }, []);

  useEffect(() => {
    checkHealth();

    if (options?.pollInterval) {
      const interval = setInterval(checkHealth, options.pollInterval);
      return () => clearInterval(interval);
    }
  }, [checkHealth, options?.pollInterval]);

  return health;
}
