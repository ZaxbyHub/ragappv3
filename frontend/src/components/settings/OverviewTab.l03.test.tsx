/**
 * Issue #774 L03 red checkpoint — OverviewTab must not render a definite
 * "down" verdict for services while the first health poll is still loading.
 *
 * useHealthCheck's initial state is
 * { backend: false, embeddings: false, chat: false, loading: true,
 *   lastChecked: null } — unknown, not down. The tab currently renders
 * `health.backend ? "ok" : "down"` for each service without ever consulting
 * `health.loading`, so the pre-first-poll frame announces all services down.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ConnectionTestResult } from "@/lib/api";
import type { HealthStatus } from "@/types/health";
import { OverviewTab } from "./OverviewTab";

describe("issue 774 OverviewTab loading frame", () => {
  it("does not report services down while the first health poll is loading", () => {
    // The exact initial state useHealthCheck seeds before the first response.
    const initialHealth: HealthStatus = {
      backend: false,
      embeddings: false,
      chat: false,
      loading: true,
      lastChecked: null,
    };

    render(
      <OverviewTab
        health={initialHealth}
        connectionResult={null as ConnectionTestResult | null}
        isTestingConnections={false}
        onTestConnections={() => undefined}
        curatorEnabled={false}
        wikiEnabled={false}
      />,
    );

    // Unknown must not read as a definite negative: while loading, no service
    // row may be labeled "down".
    expect(screen.queryAllByText("down").length).toBe(0);
  });
});
