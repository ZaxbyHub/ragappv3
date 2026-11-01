import type { ReactNode } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { GroupFormDialog as GroupFormDialogForCandidate } from "@/components/groups/GroupFormDialog";

const mockListOrganizations = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, listOrganizations: mockListOrganizations };
});

vi.mock("@/components/ui/dialog", () => ({
  Dialog: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DialogContent: ({ children }: { children: ReactNode }) => <section>{children}</section>,
  DialogDescription: ({ children }: { children: ReactNode }) => <p>{children}</p>,
  DialogFooter: ({ children }: { children: ReactNode }) => <footer>{children}</footer>,
  DialogHeader: ({ children }: { children: ReactNode }) => <header>{children}</header>,
  DialogTitle: ({ children }: { children: ReactNode }) => <h2>{children}</h2>,
}));
vi.mock("@/components/ui/button", () => ({
  Button: ({ children, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement>) => <button {...props}>{children}</button>,
}));
vi.mock("@/components/ui/input", () => ({ Input: (props: React.InputHTMLAttributes<HTMLInputElement>) => <input {...props} /> }));
vi.mock("@/components/ui/textarea", () => ({ Textarea: (props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) => <textarea {...props} /> }));
vi.mock("@/components/ui/label", () => ({ Label: ({ children, ...props }: React.LabelHTMLAttributes<HTMLLabelElement>) => <label {...props}>{children}</label> }));
vi.mock("@/components/ui/select", () => ({
  Select: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SelectContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SelectItem: ({ children, value }: { children: ReactNode; value: string }) => <option value={value}>{children}</option>,
  SelectTrigger: ({ children }: { children: ReactNode }) => <button type="button">{children}</button>,
  SelectValue: () => null,
}));
vi.mock("lucide-react", () => ({ Loader2: () => null }));

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("C08 GroupFormDialog organization read outcome", () => {
  afterEach(() => {
    cleanup();
    mockListOrganizations.mockReset();
  });

  it("keeps a failed organization read unknown and exposes a visible retry", async () => {
    const first = deferred<unknown[]>();
    const retry = deferred<unknown[]>();
    mockListOrganizations.mockReturnValueOnce(first.promise).mockReturnValueOnce(retry.promise);

    try {
      render(
        <GroupFormDialogForCandidate
          open
          mode="create"
          onOpenChange={vi.fn()}
          onSubmit={vi.fn().mockResolvedValue(undefined)}
        />,
      );

      await waitFor(() => expect(mockListOrganizations).toHaveBeenCalledTimes(1));
      await act(async () => {
        first.reject(new Error("organization read failed"));
        await first.promise.catch(() => undefined);
      });

      expect(screen.queryByText("No organizations available")).not.toBeInTheDocument();
      const retryButton = screen.getByRole("button", { name: "Retry organizations" });
      fireEvent.click(retryButton);
      await waitFor(() => expect(mockListOrganizations).toHaveBeenCalledTimes(2));

      await act(async () => retry.resolve([{ id: 7, name: "Operations" }]));
      expect(await screen.findByText("Operations")).toBeInTheDocument();
    } finally {
      first.resolve([]);
      retry.resolve([]);
      await Promise.allSettled([first.promise, retry.promise]);
    }
  });
});
