import type { HTMLAttributes, ReactNode } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { PageShell } from "./PageShell";

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
