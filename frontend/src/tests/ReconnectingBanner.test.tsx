// frontend/src/tests/ReconnectingBanner.test.tsx
/**
 * FR-019: ReconnectingBanner tests
 *
 * Verifies the banner appears when backend or chat service is down,
 * is hidden when healthy, and has correct accessibility attributes.
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";

import ReconnectingBanner from "@/components/ReconnectingBanner";
import type { HealthStatus } from "@/types/health";

// =============================================================================
// HELPERS
// =============================================================================

function healthyHealth(): HealthStatus {
  return {
    backend: true,
    embeddings: true,
    chat: true,
    loading: false,
    lastChecked: new Date(),
  };
}

function backendDownHealth(): HealthStatus {
  return {
    backend: false,
    embeddings: false,
    chat: false,
    loading: false,
    lastChecked: new Date(),
  };
}

function chatDownHealth(): HealthStatus {
  return {
    backend: true,
    embeddings: true,
    chat: false,
    loading: false,
    lastChecked: new Date(),
  };
}

function loadingHealth(): HealthStatus {
  return {
    backend: false,
    embeddings: false,
    chat: false,
    loading: true,
    lastChecked: null,
  };
}

// =============================================================================
// TESTS — FR-019: ReconnectingBanner
// =============================================================================

describe("FR-019: ReconnectingBanner", () => {
  it("renders when backend is down", () => {
    render(<ReconnectingBanner health={backendDownHealth()} />);
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText("Connection lost — reconnecting...")).toBeInTheDocument();
  });

  it("renders when chat is down but backend is up", () => {
    render(<ReconnectingBanner health={chatDownHealth()} />);
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText("Chat service unavailable — attempting to reconnect...")).toBeInTheDocument();
  });

  it("is hidden when all services are healthy", () => {
    const { container } = render(<ReconnectingBanner health={healthyHealth()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("is hidden when health is still loading", () => {
    const { container } = render(<ReconnectingBanner health={loadingHealth()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("uses role=alert for accessibility", () => {
    render(<ReconnectingBanner health={backendDownHealth()} />);
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });

  it("uses aria-live=polite for screen reader announcements", () => {
    render(<ReconnectingBanner health={backendDownHealth()} />);
    const banner = screen.getByRole("alert");
    expect(banner).toHaveAttribute("aria-live", "polite");
    expect(banner).toHaveAttribute("aria-atomic", "true");
  });

  it("displays amber/chat-down variant when only chat is unavailable", () => {
    render(<ReconnectingBanner health={chatDownHealth()} />);
    const banner = screen.getByRole("alert");
    // Warning-token background for non-severe (chat only) — no destructive red
    // class. Tokenized in UI-VIS-4 (#294) so the banner adapts to dark/high-contrast themes.
    expect(banner).not.toHaveClass("bg-destructive/95");
    expect(banner).toHaveClass("bg-warning/95");
    expect(banner).toHaveClass("text-warning-foreground");
  });

  it("displays red/severe variant when backend is down", () => {
    render(<ReconnectingBanner health={backendDownHealth()} />);
    const banner = screen.getByRole("alert");
    // Red/destructive background for severe (backend down)
    expect(banner).toHaveClass("bg-destructive/95");
  });

  it("is sticky and full-width", () => {
    render(<ReconnectingBanner health={backendDownHealth()} />);
    const banner = screen.getByRole("alert");
    expect(banner).toHaveClass("sticky", "top-0", "w-full", "z-50");
  });
});
