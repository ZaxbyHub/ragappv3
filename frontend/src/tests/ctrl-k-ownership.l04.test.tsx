// Issue #775 AC1: the app-shell palette and chat-session search share the
// default Ctrl/Cmd+K combo. Mount both real consumers so one keypress is
// observed at the document boundary and the /chat owner wins deterministically.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { CommandPalette } from "@/components/shared/CommandPalette";
import { ChatSearchInput } from "@/components/chat/SessionRail";

describe("#775 Ctrl-K ownership", () => {
  afterEach(() => {
    cleanup();
    document.body.focus();
  });

  it("settles on exactly one /chat action after one Ctrl-K keypress", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/chat"]}>
          <CommandPalette />
          <ChatSearchInput value="" onChange={vi.fn()} />
        </MemoryRouter>
      </QueryClientProvider>
    );

    const search = screen.getByRole("textbox", { name: "Search chat sessions" });
    let searchFocusCount = 0;
    search.addEventListener("focus", () => {
      searchFocusCount += 1;
    });
    search.blur();

    fireEvent.keyDown(document.body, { key: "k", code: "KeyK", ctrlKey: true });

    // Wait through the document listeners and React commit before inspecting
    // the final owner. A transient palette render must not satisfy the check.
    await vi.waitFor(() => {
      expect(searchFocusCount).toBe(1);
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });
});
