import type { ReactNode } from "react";
import { Profiler } from "react";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ManageMembersSheet } from "./ManageMembersSheet";
import type { Group } from "@/lib/api";

const { eligibleMembersMock, groupMembersMock } = vi.hoisted(() => ({
  eligibleMembersMock: vi.fn(),
  groupMembersMock: vi.fn(),
}));

const activeQueryClients: QueryClient[] = [];

function makeQueryClient() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { gcTime: 0, retry: false } },
  });
  activeQueryClients.push(queryClient);
  return queryClient;
}

vi.mock("@/lib/api", () => ({
  getEligibleGroupMembers: (...args: unknown[]) => eligibleMembersMock(...args),
  getGroupMembers: (...args: unknown[]) => groupMembersMock(...args),
}));

vi.mock("@/components/ui/sheet", () => ({
  Sheet: ({ children, open }: { children: ReactNode; open: boolean }) =>
    open ? <div>{children}</div> : null,
  SheetContent: ({ children, ...props }: { children: ReactNode }) => (
    <div {...props}>{children}</div>
  ),
  SheetDescription: ({ children }: { children: ReactNode }) => <p>{children}</p>,
  SheetFooter: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SheetHeader: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SheetTitle: ({ children }: { children: ReactNode }) => <h2>{children}</h2>,
}));
vi.mock("@/components/ui/button", () => ({
  Button: ({ children, ...props }: { children: ReactNode }) => (
    <button {...props}>{children}</button>
  ),
}));
vi.mock("@/components/ui/input", () => ({
  Input: (props: React.InputHTMLAttributes<HTMLInputElement>) => <input {...props} />,
}));
vi.mock("@/components/ui/checkbox", () => ({
  Checkbox: (props: React.InputHTMLAttributes<HTMLInputElement>) => (
    <input type="checkbox" {...props} />
  ),
}));
vi.mock("@/components/ui/label", () => ({
  Label: ({ children, ...props }: { children: ReactNode }) => <label {...props}>{children}</label>,
}));
vi.mock("@/components/ui/scroll-area", () => ({
  ScrollArea: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}));
vi.mock("@/components/ui/skeleton", () => ({ Skeleton: () => <div /> }));
vi.mock("lucide-react", () => ({
  Loader2: () => <span />,
  Search: () => <span />,
  Users: () => <span />,
}));

function makeGroup(): Group {
  return {
    id: 7,
    name: "Editors",
    description: null,
    created_at: "2024-01-01T00:00:00Z",
    org_id: null,
    organization_name: null,
  };
}

function renderSheet() {
  const queryClient = makeQueryClient();
  queryClient.setQueryData(["groups", 7, "eligible-members"], []);
  queryClient.setQueryData(["groups", 7, "members"], null);
  const view = render(
    <QueryClientProvider client={queryClient}>
      <ManageMembersSheet
        group={makeGroup()}
        open
        onOpenChange={vi.fn()}
        onSave={vi.fn().mockResolvedValue(undefined)}
      />
    </QueryClientProvider>,
  );
  return { ...view, queryClient };
}

describe("ManageMembersSheet issue #772 acceptance checks", () => {
  beforeEach(() => {
    eligibleMembersMock.mockReset().mockResolvedValue([]);
    groupMembersMock.mockReset().mockResolvedValue([]);
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    activeQueryClients.splice(0).forEach((queryClient) => queryClient.clear());
  });

  it("disables Save when the group members read fails", async () => {
    groupMembersMock.mockRejectedValue(new Error("members unavailable"));
    const { queryClient } = renderSheet();

    await waitFor(() => expect(groupMembersMock).toHaveBeenCalledWith(7));
    await waitFor(() => expect(queryClient.getQueryState(["groups", 7, "members"])?.status).toBe("error"));
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Save member changes" })).toBeDisabled();
    });
  });

  it("does not re-render continuously while the members query has no data", async () => {
    groupMembersMock.mockImplementation(() => new Promise<never>(() => undefined));
    let commits = 0;
    const queryClient = makeQueryClient();
    queryClient.setQueryData(["groups", 7, "eligible-members"], []);
    let renderFailure: unknown;
    try {
      render(
        <QueryClientProvider client={queryClient}>
          <Profiler
            id="members-sheet"
            onRender={() => {
              commits += 1;
              if (commits > 10) throw new Error("unbounded members renders");
            }}
          >
            <ManageMembersSheet
              group={makeGroup()}
              open
              onOpenChange={vi.fn()}
              onSave={vi.fn().mockResolvedValue(undefined)}
            />
          </Profiler>
        </QueryClientProvider>,
      );
    } catch (error) {
      renderFailure = error;
    }

    await waitFor(() => expect(groupMembersMock).toHaveBeenCalledWith(7));
    expect(renderFailure).toBeUndefined();
    expect(commits).toBeLessThanOrEqual(10);
    expect(screen.getByRole("button", { name: "Save member changes" })).toBeDisabled();
  });
});
