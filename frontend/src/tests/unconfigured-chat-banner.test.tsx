// frontend/src/tests/unconfigured-chat-banner.test.tsx
/**
 * Issue #622 acceptance checks — UnconfiguredChatBanner (gap G5, AC2).
 *
 * FROZEN SPEC — the fix copies this file VERBATIM to
 * frontend/src/tests/unconfigured-chat-banner.test.tsx and makes it pass.
 * The assertions encode the interface below; they are not to be edited.
 *
 * Pinned component interface (prop-driven, matching the ReconnectingBanner
 * precedent):
 *
 *   UnconfiguredChatBanner({ chatConfigured }: { chatConfigured: boolean })
 *   — default export from @/components/UnconfiguredChatBanner
 *
 * Behavior contract:
 * - Renders nothing when chatConfigured is true (a configured system shows
 *   no banner — for ANY role: the flag is computed server-side from the
 *   unredacted pair, so a non-admin on a configured system gets
 *   chat_configured=true and must NOT see a false banner).
 * - When chatConfigured is false it renders a region with role "status"
 *   whose text says chat is not configured yet.
 * - It deep-links to the settings Models tab: an anchor labelled
 *   "Settings → Models" whose href resolves to the /settings route
 *   (verified in App.tsx; the Models tab lives inside that page).
 * - A "Dismiss" button hides the banner and records the dismissal in
 *   sessionStorage under "unconfigured-chat-banner-dismissed" so it stays
 *   hidden for the session; a pre-existing dismissal flag suppresses the
 *   banner on mount.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import UnconfiguredChatBanner from "@/components/UnconfiguredChatBanner";

const DISMISS_KEY = "unconfigured-chat-banner-dismissed";

function renderBanner(chatConfigured: boolean) {
  return render(
    <MemoryRouter>
      <UnconfiguredChatBanner chatConfigured={chatConfigured} />
    </MemoryRouter>
  );
}

describe("UnconfiguredChatBanner", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.removeItem(DISMISS_KEY);
  });

  afterEach(() => {
    sessionStorage.removeItem(DISMISS_KEY);
  });

  it("renders when chat is not configured", () => {
    renderBanner(false);
    const banner = screen.getByRole("status");
    expect(banner).toBeInTheDocument();
    expect(screen.getByText(/chat is not configured yet/i)).toBeInTheDocument();
  });

  it("is hidden when chat is configured", () => {
    const { container } = renderBanner(true);
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("deep-links to the settings Models tab", () => {
    renderBanner(false);
    const link = screen.getByRole("link", { name: "Settings → Models" });
    // react-router renders the "to" target as the anchor href; the settings
    // route (App.tsx) is /settings and hosts the Models tab.
    expect(link.getAttribute("href")).toBe("/settings");
  });

  it("Dismiss hides the banner for the rest of the session", async () => {
    const user = userEvent.setup();
    renderBanner(false);

    await user.click(screen.getByRole("button", { name: "Dismiss" }));

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(sessionStorage.getItem(DISMISS_KEY)).toBe("1");

    // Session persistence: a re-mount (e.g. route change) stays dismissed.
    renderBanner(false);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("stays hidden when the session already dismissed it", () => {
    sessionStorage.setItem(DISMISS_KEY, "1");
    const { container } = renderBanner(false);
    expect(container).toBeEmptyDOMElement();
  });

  it("non-admin false-banner regression: a configured system shows no banner", () => {
    // Regression case for gap G5: below-admin callers get blanked chat URL
    // and model fields, but chat_configured is computed server-side from
    // the unredacted pair. The banner consumes ONLY that boolean, so a
    // non-admin on a configured system (chat_configured=true despite the
    // blanked fields) must not be told chat is unconfigured.
    const nonAdminSettingsView = {
      ollama_chat_url: "",
      chat_model: "",
      chat_configured: true,
    } as const;
    const { container } = renderBanner(nonAdminSettingsView.chat_configured);
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByText(/chat is not configured yet/i)).not.toBeInTheDocument();
  });
});
