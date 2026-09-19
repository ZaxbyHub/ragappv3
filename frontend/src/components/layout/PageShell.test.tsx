import type { HTMLAttributes, ReactNode } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { PageShell } from "./PageShell";

const { mockGetSettings } = vi.hoisted(() => ({
  mockGetSettings: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  getSettings: mockGetSettings,
}));

vi.mock("./Navigation", () => ({
  Navigation: () => <nav aria-label="Primary navigation" />,
}));

vi.mock("@/components/shared/UploadIndicator", () => ({
  UploadIndicator: () => null,
}));

vi.mock("framer-motion", () => ({
  AnimatePresence: ({ children }: { children: ReactNode }) => <>{children}</>,
  motion: {
    div: ({
      children,
      variants: _variants,
      initial: _initial,
      animate: _animate,
      exit: _exit,
      transition: _transition,
      ...props
    }: HTMLAttributes<HTMLDivElement> & Record<string, unknown>) => (
      <div {...props}>{children}</div>
    ),
  },
  useReducedMotion: () => true,
}));

const shellProps = {
  activeItem: "documents" as const,
  onItemSelect: vi.fn(),
  healthStatus: {
    backend: true,
    embeddings: true,
    chat: true,
    loading: false,
    lastChecked: null,
  },
};

function renderShell() {
  return render(
    <MemoryRouter initialEntries={["/documents"]}>
      <PageShell {...shellProps}>
        <button type="button">Page content</button>
      </PageShell>
    </MemoryRouter>
  );
}

// Default: a configured system (banner hidden). Individual tests override.
beforeEach(() => {
  mockGetSettings.mockResolvedValue({ chat_configured: true });
  sessionStorage.removeItem("unconfigured-chat-banner-dismissed");
});

describe("PageShell skip link", () => {
  it("keeps a relative fragment and moves focus to the main content target on activation", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/documents"]}>
        <PageShell
          activeItem="documents"
          onItemSelect={vi.fn()}
          healthStatus={{
            backend: true,
            embeddings: true,
            chat: true,
            loading: false,
            lastChecked: null,
          }}
        >
          <button type="button">Page content</button>
        </PageShell>
      </MemoryRouter>
    );

    const skipLink = screen.getByRole("link", { name: "Skip to main content" });
    const main = screen.getByRole("main");
    expect(skipLink).toHaveAttribute("href", "#main-content");
    expect(main).toHaveAttribute("id", "main-content");
    expect(main).toHaveAttribute("tabindex", "-1");

    await user.click(skipLink);
    expect(main).toHaveFocus();
  });
});

describe("PageShell unconfigured-chat banner (issue #622 / PRR-007)", () => {
  it("renders the banner inside the shell when chat_configured is false", async () => {
    mockGetSettings.mockResolvedValue({ chat_configured: false });
    sessionStorage.removeItem("unconfigured-chat-banner-dismissed");
    renderShell();

    expect(
      await screen.findByText(/chat is not configured yet/i)
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Settings → Models" })).toHaveAttribute(
      "href",
      "/settings"
    );
  });

  it("renders no banner when chat_configured is true", async () => {
    mockGetSettings.mockResolvedValue({ chat_configured: true });
    sessionStorage.removeItem("unconfigured-chat-banner-dismissed");
    renderShell();

    await waitFor(() => {
      expect(mockGetSettings).toHaveBeenCalled();
    });
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("renders no banner when the settings fetch fails (PRR-007: fail-safe)", async () => {
    mockGetSettings.mockRejectedValue(new Error("network down"));
    sessionStorage.removeItem("unconfigured-chat-banner-dismissed");
    renderShell();

    await waitFor(() => {
      expect(mockGetSettings).toHaveBeenCalled();
    });
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    // The rest of the shell (page content) is unaffected.
    expect(screen.getByText("Page content")).toBeInTheDocument();
  });
});
