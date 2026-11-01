import { Profiler, type ProfilerOnRenderCallback, type ReactNode } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider, onlineManager } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ManageMembersSheet } from "./ManageMembersSheet";
import type { Group, GroupMember } from "@/lib/api";

const { eligibleMembersMock, groupMembersMock } = vi.hoisted(() => ({
  eligibleMembersMock: vi.fn(),
  groupMembersMock: vi.fn(),
}));

const activeQueryClients: QueryClient[] = [];

function makeQueryClient(staleTime = 0) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { gcTime: 0, retry: false, staleTime } },
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
  Checkbox: ({
    onCheckedChange,
    ...props
  }: React.InputHTMLAttributes<HTMLInputElement> & {
    onCheckedChange?: (checked: boolean) => void;
  }) => (
    <input
      type="checkbox"
      {...props}
      onChange={(event) => onCheckedChange?.(event.currentTarget.checked)}
    />
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
    org_id: 1,
    organization_name: "Acme",
  };
}

function makeMember(id = 42, username = "alice"): GroupMember {
  return { id, username, full_name: username === "alice" ? "Alice Example" : username };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function renderSheet(
  onSave: (userIds: number[]) => Promise<void>,
  editorToken = 1,
  onProfilerCommit?: ProfilerOnRenderCallback,
  queryClient = makeQueryClient(),
) {
  const sheet = (
    <ManageMembersSheet
      group={makeGroup()}
      open
      editorToken={editorToken}
      onOpenChange={vi.fn()}
      onSave={onSave}
    />
  );
  const view = render(
    <QueryClientProvider client={queryClient}>
      {onProfilerCommit ? (
        <Profiler id="members-sheet" onRender={onProfilerCommit}>
          {sheet}
        </Profiler>
      ) : sheet}
    </QueryClientProvider>,
  );
  return { ...view, queryClient };
}

describe("ManageMembersSheet issue #772 edge coverage", () => {
  beforeEach(() => {
    onlineManager.setOnline(true);
    eligibleMembersMock.mockReset().mockResolvedValue([]);
    groupMembersMock.mockReset().mockResolvedValue([]);
  });

  afterEach(() => {
    onlineManager.setOnline(true);
    cleanup();
    activeQueryClients.splice(0).forEach((queryClient) => queryClient.clear());
  });

  it("keeps Save disabled when the eligible-members read fails", async () => {
    eligibleMembersMock.mockRejectedValue(new Error("eligible members unavailable"));
    const { queryClient } = renderSheet(vi.fn().mockResolvedValue(undefined));

    await waitFor(() =>
      expect(queryClient.getQueryState(["groups", 7, "eligible-members"])?.status).toBe("error"),
    );
    expect(screen.getByText(/unable to load group members/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save member changes" })).toBeDisabled();
  });

  it("allows an empty replacement after both reads succeed", async () => {
    const onSave = vi.fn<(userIds: number[]) => Promise<void>>().mockResolvedValue();
    renderSheet(onSave);

    const save = await screen.findByRole("button", { name: "Save member changes" });
    await waitFor(() => expect(save).not.toBeDisabled());
    fireEvent.click(save);

    await waitFor(() => expect(onSave).toHaveBeenCalledWith([]));
  });

  it("preserves a local selection across a successful current-members refetch", async () => {
    const member = makeMember();
    eligibleMembersMock.mockResolvedValue([member]);
    groupMembersMock.mockResolvedValueOnce([]).mockResolvedValueOnce([member]);
    const queryClient = makeQueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <ManageMembersSheet
          group={makeGroup()}
          open
          editorToken={1}
          onOpenChange={vi.fn()}
          onSave={vi.fn().mockResolvedValue(undefined)}
        />
      </QueryClientProvider>,
    );

    const checkbox = await screen.findByLabelText("Select alice");
    expect(checkbox).not.toBeChecked();
    fireEvent.click(checkbox);
    expect(checkbox).toBeChecked();

    await queryClient.refetchQueries({ queryKey: ["groups", 7, "members"] });
    expect(screen.getByLabelText("Select alice")).toBeChecked();
  });

  it("waits for cached opening reads before enabling edits and preserves a later local edit", async () => {
    const cachedMember = makeMember(101, "cached-member");
    const freshMember = makeMember(202, "fresh-member");
    const eligibleRead = deferred<GroupMember[]>();
    const membersRead = deferred<GroupMember[]>();
    const backgroundRead = deferred<GroupMember[]>();
    const onSave = vi.fn<(userIds: number[]) => Promise<void>>().mockResolvedValue(undefined);
    const queryClient = makeQueryClient();
    const eligibleKey = ["groups", 7, "eligible-members"];
    const membersKey = ["groups", 7, "members"];

    queryClient.setQueryData(eligibleKey, [cachedMember, freshMember]);
    queryClient.setQueryData(membersKey, [cachedMember]);
    eligibleMembersMock.mockReturnValue(eligibleRead.promise);
    groupMembersMock.mockReturnValue(membersRead.promise);

    render(
      <QueryClientProvider client={queryClient}>
        <ManageMembersSheet
          group={makeGroup()}
          open
          editorToken={1}
          onOpenChange={vi.fn()}
          onSave={onSave}
        />
      </QueryClientProvider>,
    );

    const freshCheckbox = await screen.findByLabelText("Select fresh-member");
    const save = screen.getByRole("button", { name: "Save member changes" });
    expect(freshCheckbox).toBeDisabled();
    expect(save).toBeDisabled();
    await waitFor(() => {
      expect(queryClient.getQueryState(eligibleKey)?.fetchStatus).toBe("fetching");
      expect(queryClient.getQueryState(membersKey)?.fetchStatus).toBe("fetching");
    });

    await act(async () => {
      eligibleRead.resolve([cachedMember, freshMember]);
      membersRead.resolve([freshMember]);
      await Promise.all([eligibleRead.promise, membersRead.promise]);
    });
    await waitFor(() => expect(save).not.toBeDisabled());
    expect(freshCheckbox).toBeChecked();

    fireEvent.click(freshCheckbox);
    expect(freshCheckbox).not.toBeChecked();
    groupMembersMock.mockReturnValueOnce(backgroundRead.promise);
    const backgroundRefetch = queryClient.refetchQueries({ queryKey: membersKey });
    await waitFor(() => expect(queryClient.getQueryState(membersKey)?.fetchStatus).toBe("fetching"));
    expect(freshCheckbox).not.toBeDisabled();
    // Once initialization completed, a background fetch keeps the local edit
    // usable while the stale read is in flight.
    expect(save).not.toBeDisabled();

    await act(async () => {
      backgroundRead.resolve([freshMember]);
      await backgroundRefetch;
    });
    await waitFor(() => expect(queryClient.getQueryState(membersKey)?.fetchStatus).toBe("idle"));
    expect(freshCheckbox).not.toBeChecked();
    await waitFor(() => expect(save).not.toBeDisabled());
    fireEvent.click(save);
    await waitFor(() => expect(onSave).toHaveBeenCalledWith([]));
  });

  it("initializes the current same-group reopening when its shared pending read settles", async () => {
    const oldMember = makeMember(101, "old-member");
    const newMember = makeMember(202, "new-member");
    const pendingRead = deferred<GroupMember[]>();
    eligibleMembersMock.mockResolvedValue([oldMember, newMember]);
    groupMembersMock.mockResolvedValueOnce([oldMember]);
    const queryClient = makeQueryClient();
    const view = renderSheet(vi.fn().mockResolvedValue(undefined), 1, undefined, queryClient);

    const oldCheckbox = await screen.findByLabelText("Select old-member");
    await waitFor(() => expect(oldCheckbox).toBeChecked());
    groupMembersMock.mockReturnValueOnce(pendingRead.promise);
    const membersKey = ["groups", 7, "members"];
    const pendingRefetch = queryClient.refetchQueries({ queryKey: membersKey });
    await waitFor(() => expect(queryClient.getQueryState(membersKey)?.fetchStatus).toBe("fetching"));

    view.rerender(
      <QueryClientProvider client={queryClient}>
        <ManageMembersSheet
          group={makeGroup()}
          open={false}
          editorToken={1}
          onOpenChange={vi.fn()}
          onSave={vi.fn().mockResolvedValue(undefined)}
        />
      </QueryClientProvider>,
    );
    view.rerender(
      <QueryClientProvider client={queryClient}>
        <ManageMembersSheet
          group={makeGroup()}
          open
          editorToken={2}
          onOpenChange={vi.fn()}
          onSave={vi.fn().mockResolvedValue(undefined)}
        />
      </QueryClientProvider>,
    );
    expect(screen.getByLabelText("Select old-member")).not.toBeChecked();

    await act(async () => {
      pendingRead.resolve([newMember]);
      await pendingRefetch;
    });
    await waitFor(() => {
      expect(queryClient.getQueryState(membersKey)?.fetchStatus).toBe("idle");
      expect(screen.getByLabelText("Select old-member")).not.toBeChecked();
      expect(screen.getByLabelText("Select new-member")).toBeChecked();
      expect(screen.getByRole("button", { name: "Save member changes" })).not.toBeDisabled();
    });
  });

  it("fails closed for an initial paused read without cache", async () => {
    onlineManager.setOnline(false);
    const { queryClient } = renderSheet(vi.fn().mockResolvedValue(undefined));

    await waitFor(() => expect(queryClient.getQueryState(["groups", 7, "members"])?.fetchStatus).toBe("paused"));
    expect(screen.getByRole("status")).toHaveTextContent(/offline|reconnect/i);
    expect(screen.getByRole("button", { name: "Save member changes" })).toBeDisabled();
  });

  it("fails closed for a cached initial read that is paused", async () => {
    const queryClient = makeQueryClient();
    const member = makeMember(101, "cached-member");
    queryClient.setQueryData(["groups", 7, "eligible-members"], [member]);
    queryClient.setQueryData(["groups", 7, "members"], [member]);
    onlineManager.setOnline(false);
    render(
      <QueryClientProvider client={queryClient}>
        <ManageMembersSheet
          group={makeGroup()}
          open
          editorToken={1}
          onOpenChange={vi.fn()}
          onSave={vi.fn().mockResolvedValue(undefined)}
        />
      </QueryClientProvider>,
    );

    await waitFor(() => expect(queryClient.getQueryState(["groups", 7, "members"])?.fetchStatus).toBe("paused"));
    expect(screen.getByRole("status")).toHaveTextContent(/offline|reconnect/i);
    expect(screen.getByRole("button", { name: "Save member changes" })).toBeDisabled();
  });

  it("keeps a reopened editor ready after an older save resolves", async () => {
    const firstSave = deferred<void>();
    const onSave = vi.fn<(userIds: number[]) => Promise<void>>().mockReturnValue(firstSave.promise);
    let view: ReturnType<typeof renderSheet> | undefined;
    try {
      view = renderSheet(onSave, 1);
      const initialSave = await screen.findByRole("button", { name: "Save member changes" });
      await waitFor(() => expect(initialSave).not.toBeDisabled());
      fireEvent.click(initialSave);
      await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));

      view.rerender(
        <QueryClientProvider client={view.queryClient}>
          <ManageMembersSheet
            group={makeGroup()}
            open
            editorToken={2}
            onOpenChange={vi.fn()}
            onSave={onSave}
          />
        </QueryClientProvider>,
      );
      const reopenedSave = await screen.findByRole("button", { name: "Save member changes" });
      await waitFor(() => expect(reopenedSave).not.toBeDisabled());

      await act(async () => {
        firstSave.resolve();
        await firstSave.promise;
        await Promise.resolve();
      });
      expect(reopenedSave).not.toBeDisabled();
    } finally {
      await act(async () => {
        firstSave.resolve();
        await firstSave.promise;
      });
      view?.unmount();
    }
  });

  it("contains a current save failure so the editor can retry", async () => {
    const onSave = vi
      .fn<(userIds: number[]) => Promise<void>>()
      .mockRejectedValueOnce(new Error("members save unavailable"))
      .mockResolvedValueOnce(undefined);
    renderSheet(onSave);

    const save = await screen.findByRole("button", { name: "Save member changes" });
    await waitFor(() => expect(save).not.toBeDisabled());
    fireEvent.click(save);
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(save).not.toBeDisabled());

    fireEvent.click(save);
    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(2));
  });

  it("does not let an older rejected save clear a newer opening's pending save", async () => {
    const firstSave = deferred<void>();
    const secondSave = deferred<void>();
    const onSave = vi
      .fn<(userIds: number[]) => Promise<void>>()
      .mockReturnValueOnce(firstSave.promise)
      .mockReturnValueOnce(secondSave.promise);
    let view: ReturnType<typeof renderSheet> | undefined;
    try {
      view = renderSheet(onSave, 1);

      const firstButton = await screen.findByRole("button", { name: "Save member changes" });
      await waitFor(() => expect(firstButton).not.toBeDisabled());
      fireEvent.click(firstButton);
      await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));

      view.rerender(
        <QueryClientProvider client={view.queryClient}>
          <ManageMembersSheet
            group={makeGroup()}
            open
            editorToken={2}
            onOpenChange={vi.fn()}
            onSave={onSave}
          />
        </QueryClientProvider>,
      );
      const reopenedButton = await screen.findByRole("button", { name: "Save member changes" });
      await waitFor(() => expect(reopenedButton).not.toBeDisabled());
      fireEvent.click(reopenedButton);
      await waitFor(() => expect(onSave).toHaveBeenCalledTimes(2));
      expect(reopenedButton).toBeDisabled();

      await act(async () => {
        firstSave.reject(new Error("older members save failed"));
        await Promise.allSettled([firstSave.promise]);
        await Promise.resolve();
      });
      expect(reopenedButton).toBeDisabled();

      await act(async () => {
        secondSave.resolve();
        await secondSave.promise;
      });
      expect(reopenedButton).not.toBeDisabled();
    } finally {
      await act(async () => {
        firstSave.reject(new Error("older members save failed"));
        secondSave.resolve();
        await Promise.allSettled([firstSave.promise, secondSave.promise]);
      });
      view?.unmount();
    }
  });

  it("bounds renders after the members read rejects", async () => {
    const membersRead = deferred<GroupMember[]>();
    const commitCounts = { pending: 0, afterRejection: 0 };
    const commitBudget = 12;
    let phase: "pending" | "after-rejection" = "pending";
    const onProfilerCommit: ProfilerOnRenderCallback = () => {
      commitCounts[phase === "pending" ? "pending" : "afterRejection"] += 1;
      const count = commitCounts[phase === "pending" ? "pending" : "afterRejection"];
      expect(count, `unbounded members renders ${phase}`).toBeLessThanOrEqual(commitBudget);
    };
    const onSave = vi.fn<(userIds: number[]) => Promise<void>>().mockResolvedValue(undefined);
    let view: ReturnType<typeof renderSheet> | undefined;

    groupMembersMock.mockReturnValue(membersRead.promise);
    try {
      view = renderSheet(onSave, 1, onProfilerCommit);

      const membersQueryKey = ["groups", 7, "members"];
      await waitFor(() =>
        expect(view?.queryClient.getQueryState(membersQueryKey)?.status).toBe("pending"),
      );
      expect(commitCounts.pending).toBeGreaterThan(0);

      phase = "after-rejection";
      await act(async () => {
        membersRead.reject(new Error("members read unavailable"));
        await Promise.allSettled([membersRead.promise]);
      });

      await waitFor(() =>
        expect(view?.queryClient.getQueryState(membersQueryKey)?.status).toBe("error"),
      );
      expect(screen.getByText(/unable to load group members/i)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Save member changes" })).toBeDisabled();
      await waitFor(() => expect(commitCounts.afterRejection).toBeGreaterThan(0));

      expect(commitCounts.pending).toBeLessThanOrEqual(commitBudget);
      expect(commitCounts.afterRejection).toBeLessThanOrEqual(commitBudget);
    } finally {
      await act(async () => {
        membersRead.reject(new Error("members read cleanup"));
        await Promise.allSettled([membersRead.promise]);
      });
      view?.unmount();
    }
  });
});
