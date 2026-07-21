import { useState, useEffect, useCallback, useRef } from "react";
import apiClient, { type HealthResponse } from "@/lib/api";
import type { HealthStatus } from "@/types/health";

interface UseHealthCheckOptions {
  pollInterval?: number;
}

// Deep-probe (real LLM + embedding service checks) every Nth poll; the polls
// in between are lightweight and return `services.embeddings/chat = null`,
// meaning "not probed this time" — NOT "down". At the default 30s poll
// interval this re-verifies the model services roughly every 5 minutes.
const POLLS_PER_DEEP_CHECK = 10;

/** Polls the backend health endpoint and returns service availability status. */
export function useHealthCheck(options?: UseHealthCheckOptions): HealthStatus {
  const [health, setHealth] = useState<HealthStatus>({
    backend: false,
    embeddings: false,
    chat: false,
    loading: true,
    lastChecked: null,
  });

  const pollCount = useRef(0);

  const checkHealth = useCallback(async () => {
    try {
      // First poll (and every POLLS_PER_DEEP_CHECK-th after) probes the model
      // services; the rest are lightweight backend-liveness checks.
      const deep = pollCount.current % POLLS_PER_DEEP_CHECK === 0;
      pollCount.current += 1;
      const params = deep ? { deep: true } : {};

      const response = await apiClient.get<HealthResponse>("/health", { params });
      const services = response.data.services;

      const newBackend = services?.backend ?? response.data.status === "ok";

      setHealth((prev) => {
        return {
          backend: newBackend,
          // Lightweight polls return null for these ("not probed"), which
          // must NOT be read as down — keep the last deep-probed value.
          // Reading null as false here made the chat/embeddings badges and
          // the reconnect banner go permanently red ~30s after page load.
          embeddings: services?.embeddings ?? prev.embeddings,
          chat: services?.chat ?? prev.chat,
          loading: false,
          lastChecked: new Date(),
        };
      });
    } catch {
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
