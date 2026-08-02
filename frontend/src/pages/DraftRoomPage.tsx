import { useEffect, useMemo, useState } from "react";
import { useNavigate, Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { FolderOpen, Plus } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/EmptyState";
import { PageTitleHeader } from "@/components/layout/PageTitleHeader";
import { Pagination } from "@/components/ui/pagination";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";

import { DraftCreateDialog } from "@/components/draft-room/DraftCreateDialog";
import {
  DRAFT_LIST_FILTERS,
  DRAFT_ROOM_DISABLED_MESSAGE,
  DRAFT_ROOM_PAGE_DESCRIPTION,
  DRAFT_STATUS_LABELS,
  NEW_DRAFT_CTA,
} from "@/components/draft-room/labels";
import { useDraftRoomCapabilities } from "@/hooks/useDraftRoomCapabilities";
import { listAccessibleVaults } from "@/lib/api";
import {
  draftRoomKeys,
  listDrafts,
  parseDraftRoomError,
  type DraftStatus,
  type DraftSummary,
} from "@/lib/api/draftRoom";
import { formatDate } from "@/lib/formatters";

const DEFAULT_PER_PAGE = 20;

/**
 * Draft Room keeps source files and manuscripts private to the project until
 * a human explicitly promotes something into the vault (`PROMOTE_CONSEQUENCE`
 * in labels.ts) — not itself a normative label, so kept local to this page
 * rather than added to the shared `labels.ts` file this worker does not own.
 */
const DRAFT_ROOM_PRIVACY_NOTE =
  "Source files and drafts stay private to this project. Nothing is added to the vault until you explicitly promote it.";

function statusBadgeClass(status: DraftStatus): string {
  switch (status) {
    case "ready":
      return "border-success/50 bg-success/10 text-success";
    case "failed":
      return "border-destructive/50 bg-destructive/10 text-destructive";
    case "needs_review":
    case "queued":
    case "running":
      return "border-warning/50 bg-warning/10 text-warning";
    case "archived":
      return "border-border bg-muted text-muted-foreground";
    default:
      return "border-border bg-muted text-foreground";
  }
}

function DraftRow({ draft, vaultName }: { draft: DraftSummary; vaultName: string }) {
  return (
    <li className="grid grid-cols-1 gap-1 border-b border-border p-3 last:border-b-0 lg:grid-cols-[2fr_1fr_1fr_1fr_90px_90px_80px] lg:items-center lg:gap-4">
      <div className="min-w-0">
        <Link to={`/draft-room/${draft.id}`} className="truncate font-medium text-foreground hover:underline">
          {draft.title}
        </Link>
      </div>
      <div className="text-sm text-muted-foreground">
        <span className="font-medium lg:hidden">Vault: </span>
        {vaultName}
      </div>
      <div className="text-sm">
        <span className="font-medium lg:hidden">Status: </span>
        <span
          className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium ${statusBadgeClass(draft.status)}`}
        >
          {DRAFT_STATUS_LABELS[draft.status] ?? draft.status}
        </span>
      </div>
      <div className="text-sm text-muted-foreground">
        <span className="font-medium lg:hidden">Updated: </span>
        {formatDate(draft.updated_at)}
      </div>
      <div className="text-sm text-muted-foreground">
        <span className="font-medium lg:hidden">Sources: </span>
        {draft.input_count}
      </div>
      <div className="text-sm text-muted-foreground">
        <span className="font-medium lg:hidden">Blockers: </span>
        {draft.open_blocker_count}
      </div>
      <div>
        <Button asChild size="sm" variant="outline">
          <Link to={`/draft-room/${draft.id}`}>Open</Link>
        </Button>
      </div>
    </li>
  );
}

function DraftListSkeleton() {
  return (
    <ul className="divide-y divide-border rounded-md border border-border" data-testid="draft-list-skeleton">
      {Array.from({ length: 5 }).map((_, index) => (
        <li key={index} className="flex flex-col gap-2 p-3 lg:flex-row lg:items-center lg:gap-4">
          <Skeleton className="h-5 w-48" />
          <Skeleton className="h-4 w-24" />
          <Skeleton className="h-4 w-20" />
          <Skeleton className="h-4 w-24" />
        </li>
      ))}
    </ul>
  );
}

export default function DraftRoomPage() {
  const navigate = useNavigate();
  const capabilitiesQuery = useDraftRoomCapabilities();

  const vaultsQuery = useQuery({
    queryKey: ["vaults", "accessible"],
    queryFn: () => listAccessibleVaults(),
  });
  const vaults = useMemo(() => vaultsQuery.data?.vaults ?? [], [vaultsQuery.data]);
  const vaultNameById = useMemo(() => {
    const map = new Map<number, string>();
    for (const vault of vaults) map.set(vault.id, vault.name);
    return map;
  }, [vaults]);

  const [vaultFilterId, setVaultFilterId] = useState<number | null>(null);
  const [statusFilterKey, setStatusFilterKey] = useState<string>("all");
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(DEFAULT_PER_PAGE);
  const [createOpen, setCreateOpen] = useState(false);

  const activeStatus = DRAFT_LIST_FILTERS.find((f) => f.id === statusFilterKey)?.status ?? null;

  useEffect(() => {
    setPage(1);
  }, [vaultFilterId, statusFilterKey]);

  const listParams = {
    vault_id: vaultFilterId ?? undefined,
    status: (activeStatus ?? undefined) as DraftStatus | undefined,
    page,
    per_page: perPage,
  };
  const draftsQuery = useQuery({
    queryKey: draftRoomKeys.list(listParams),
    queryFn: () => listDrafts(listParams),
  });

  const isLoading = vaultsQuery.isLoading || draftsQuery.isLoading;
  const capabilitiesKnown = !capabilitiesQuery.isLoading;
  const capabilityDisabled = capabilitiesKnown && capabilitiesQuery.data?.enabled === false;

  const noAccessibleVault = vaultsQuery.isSuccess && vaults.length === 0;
  const items = draftsQuery.data?.items ?? [];
  const total = draftsQuery.data?.total ?? 0;
  const filtersActive = vaultFilterId != null || statusFilterKey !== "all";
  const noProjectsAtAll = draftsQuery.isSuccess && !filtersActive && total === 0;

  const newDraftDisabled = capabilityDisabled || noAccessibleVault;
  const newDraftDisabledReason = capabilityDisabled
    ? DRAFT_ROOM_DISABLED_MESSAGE
    : noAccessibleVault
      ? "You need read access to a vault before creating a drafting project."
      : undefined;

  return (
    <div className="animate-in fade-in space-y-6 pb-12 duration-300">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <PageTitleHeader title="Draft Room" description={DRAFT_ROOM_PAGE_DESCRIPTION} />
        <Button type="button" onClick={() => setCreateOpen(true)} disabled={newDraftDisabled} title={newDraftDisabledReason}>
          <Plus className="mr-1 h-4 w-4" aria-hidden="true" />
          {NEW_DRAFT_CTA}
        </Button>
      </div>

      {capabilityDisabled && (
        <Alert variant="warning">
          <AlertDescription>{DRAFT_ROOM_DISABLED_MESSAGE}</AlertDescription>
        </Alert>
      )}

      {isLoading ? (
        <DraftListSkeleton />
      ) : noAccessibleVault ? (
        <EmptyState
          icon={FolderOpen}
          title="You need vault access to use Draft Room"
          description="Draft Room projects always belong to a vault. Ask an admin to grant you read access to a vault, then come back here."
          action={{ label: "Go to Vaults", onClick: () => navigate("/vaults") }}
        />
      ) : noProjectsAtAll ? (
        <EmptyState
          icon={FolderOpen}
          title="Start your first drafting project"
          description={`${DRAFT_ROOM_PAGE_DESCRIPTION} ${DRAFT_ROOM_PRIVACY_NOTE}`}
          action={{ label: NEW_DRAFT_CTA, onClick: () => setCreateOpen(true) }}
        />
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-3">
            <div className="w-56">
              <Select
                value={vaultFilterId != null ? String(vaultFilterId) : "all"}
                onValueChange={(value) => setVaultFilterId(value === "all" ? null : Number(value))}
              >
                <SelectTrigger aria-label="Filter by vault">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">All vaults</SelectItem>
                  {vaults.map((vault) => (
                    <SelectItem key={vault.id} value={String(vault.id)}>
                      {vault.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <Tabs value={statusFilterKey} onValueChange={setStatusFilterKey}>
              <TabsList className="h-auto flex-wrap justify-start">
                {DRAFT_LIST_FILTERS.map((filter) => (
                  <TabsTrigger key={filter.id} value={filter.id}>
                    {filter.label}
                  </TabsTrigger>
                ))}
              </TabsList>
            </Tabs>
          </div>

          {draftsQuery.isError ? (
            <Alert variant="destructive">
              <AlertDescription>{parseDraftRoomError(draftsQuery.error).detail}</AlertDescription>
            </Alert>
          ) : items.length === 0 ? (
            <EmptyState
              icon={FolderOpen}
              title="No projects match these filters"
              description="Try a different vault or status filter."
              action={{
                label: "Clear filters",
                onClick: () => {
                  setVaultFilterId(null);
                  setStatusFilterKey("all");
                },
              }}
            />
          ) : (
            <>
              <div className="hidden border-b border-border px-3 pb-2 text-xs font-medium text-muted-foreground lg:grid lg:grid-cols-[2fr_1fr_1fr_1fr_90px_90px_80px] lg:gap-4">
                <span>Title</span>
                <span>Vault</span>
                <span>Status</span>
                <span>Updated</span>
                <span>Sources</span>
                <span>Blockers</span>
                <span />
              </div>
              <ul className="divide-y divide-border rounded-md border border-border">
                {items.map((draft) => (
                  <DraftRow key={draft.id} draft={draft} vaultName={vaultNameById.get(draft.vault_id) ?? `Vault #${draft.vault_id}`} />
                ))}
              </ul>
              <Pagination
                page={page}
                limit={perPage}
                total={total}
                onPageChange={setPage}
                onLimitChange={setPerPage}
                isLoading={draftsQuery.isFetching}
                itemName="projects"
              />
            </>
          )}
        </>
      )}

      <DraftCreateDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        defaultVaultId={vaultFilterId}
        onCreated={(draft) => navigate(`/draft-room/${draft.id}`)}
      />
    </div>
  );
}
