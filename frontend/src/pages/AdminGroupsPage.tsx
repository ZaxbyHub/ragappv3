// frontend/src/pages/AdminGroupsPage.tsx

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { JSX } from "react";
import { QueryClient, QueryClientProvider, useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Plus } from "lucide-react";
import { AdminGuard } from "@/components/auth/RoleGuard";
import { Button } from "@/components/ui/button";
import { GroupTable } from "@/components/groups/GroupTable";
import { GroupFormDialog, GroupFormData } from "@/components/groups/GroupFormDialog";
import { ManageMembersSheet } from "@/components/groups/ManageMembersSheet";
import { ManageVaultsSheet } from "@/components/groups/ManageVaultsSheet";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  createGroup,
  updateGroup,
  deleteGroup,
  updateGroupMembers,
  updateGroupVaults,
  type Group,
  type VaultAccessItem,
} from "@/lib/api";
import { PageTitleHeader } from "@/components/layout/PageTitleHeader";
import {
  captureAuthOwner,
  captureAuthPrincipalGeneration,
  isCurrentAuthOwner,
  onAuthOwnerReplacement,
  subscribeAuthPrincipal,
  type AuthOwner,
} from "@/lib/api/auth-lifecycle";

type EditorKind = "create" | "edit" | "delete" | "members" | "vaults";

const isScopedLeaseCurrent = (leaseRef: { current: number }, lease: number): boolean =>
  leaseRef.current === lease;

interface MutationContext {
  openingToken: number;
  groupId?: number;
  owner: AuthOwner;
  principalGeneration: number;
  mutationToken: number;
}

interface GroupCreateMutationVariables extends MutationContext {
  openingToken: number;
  data: GroupFormData;
}

interface GroupEditMutationVariables extends MutationContext {
  groupId: number;
  openingToken: number;
  data: GroupFormData;
}

interface GroupDeleteMutationVariables extends MutationContext {
  groupId: number;
  openingToken: number;
}

interface MembersMutationVariables extends MutationContext {
  groupId: number;
  openingToken: number;
  userIds: number[];
}

interface VaultsMutationVariables extends MutationContext {
  groupId: number;
  openingToken: number;
  vaultAccess: VaultAccessItem[];
}

const subscribeAuthOwner = (listener: () => void): (() => void) =>
  onAuthOwnerReplacement(() => listener());

// ============================================================================
// Main Page Component
// ============================================================================

function AdminGroupsPageContent({ outerQueryClient, authOwner, principalGeneration }: { outerQueryClient: QueryClient; authOwner: AuthOwner; principalGeneration: number }): JSX.Element {
  const queryClient = useQueryClient();
  const mountedRef = useRef(false);
  const editorTokenRef = useRef(0);
  const mutationTokenRef = useRef(0);
  const activeMutationRef = useRef<MutationContext | null>(null);
  const startedMutationsRef = useRef(new WeakSet<MutationContext>());
  const [activeMutation, setActiveMutation] = useState<MutationContext | null>(null);
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [membersSheetOpen, setMembersSheetOpen] = useState(false);
  const [vaultsSheetOpen, setVaultsSheetOpen] = useState(false);
  const [createEditorToken, setCreateEditorToken] = useState(0);
  const [editEditorToken, setEditEditorToken] = useState(0);
  const [deleteEditorToken, setDeleteEditorToken] = useState(0);
  const [membersEditorToken, setMembersEditorToken] = useState(0);
  const [vaultsEditorToken, setVaultsEditorToken] = useState(0);
  const [selectedGroup, setSelectedGroup] = useState<Group | null>(null);
  const selectedGroupRef = useRef<Group | null>(null);
  const openingRef = useRef<{ kind: EditorKind; openingToken: number; groupId: number | null } | null>(null);

  const isCurrentPageContext = useCallback((owner: AuthOwner, generation: number): boolean => (
    mountedRef.current && isCurrentAuthOwner(owner) && captureAuthPrincipalGeneration() === generation &&
    owner === authOwner && generation === principalGeneration
  ), [authOwner, principalGeneration]);
  const isCurrentOpening = useCallback((kind: EditorKind, token: number, groupId?: number): boolean => {
    const opening = openingRef.current;
    return isCurrentPageContext(authOwner, principalGeneration) && opening?.kind === kind &&
      opening.openingToken === token && (kind === "create" || (
        groupId !== undefined && opening.groupId === groupId && selectedGroupRef.current?.id === groupId
      ));
  }, [authOwner, isCurrentPageContext, principalGeneration]);
  const isCurrentMutation = useCallback((variables: MutationContext): boolean => (
    isCurrentPageContext(variables.owner, variables.principalGeneration) &&
    activeMutationRef.current?.mutationToken === variables.mutationToken &&
    activeMutationRef.current.openingToken === variables.openingToken
  ), [isCurrentPageContext]);
  const releaseMutation = useCallback((variables: MutationContext): void => {
    if (!isCurrentMutation(variables)) return;
    activeMutationRef.current = null;
    setActiveMutation((current) => current?.mutationToken === variables.mutationToken ? null : current);
  }, [isCurrentMutation]);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      openingRef.current = null;
      selectedGroupRef.current = null;
      activeMutationRef.current = null;
    };
  }, []);

  const openEditor = useCallback((kind: EditorKind, group?: Group): void => {
    if (!isCurrentPageContext(authOwner, principalGeneration) || (kind !== "create" && !group)) return;
    const openingToken = ++editorTokenRef.current;
    openingRef.current = { kind, openingToken, groupId: group?.id ?? null };
    selectedGroupRef.current = group ?? null;
    // Retire A logically; its in-flight API call never owns the new B opening.
    activeMutationRef.current = null;
    setActiveMutation(null);
    setSelectedGroup(group ?? null);
    setCreateDialogOpen(kind === "create");
    setEditDialogOpen(kind === "edit");
    setDeleteDialogOpen(kind === "delete");
    setMembersSheetOpen(kind === "members");
    setVaultsSheetOpen(kind === "vaults");
    if (kind === "create") setCreateEditorToken(openingToken);
    if (kind === "edit") setEditEditorToken(openingToken);
    if (kind === "delete") setDeleteEditorToken(openingToken);
    if (kind === "members") setMembersEditorToken(openingToken);
    if (kind === "vaults") setVaultsEditorToken(openingToken);
  }, [authOwner, isCurrentPageContext, principalGeneration]);
  const closeEditor = useCallback((kind: EditorKind, token: number, groupId?: number): void => {
    if (!isCurrentOpening(kind, token, groupId)) return;
    openingRef.current = null;
    selectedGroupRef.current = null;
    activeMutationRef.current = null;
    setActiveMutation(null);
    setSelectedGroup(null);
    if (kind === "create") setCreateDialogOpen(false);
    if (kind === "edit") setEditDialogOpen(false);
    if (kind === "delete") setDeleteDialogOpen(false);
    if (kind === "members") setMembersSheetOpen(false);
    if (kind === "vaults") setVaultsSheetOpen(false);
  }, [isCurrentOpening]);
  const beginMutation = useCallback((kind: EditorKind, openingToken: number, groupId?: number): MutationContext | null => {
    if (!isCurrentOpening(kind, openingToken, groupId) || activeMutationRef.current?.openingToken === openingToken) return null;
    const variables: MutationContext = { owner: authOwner, principalGeneration, openingToken, groupId, mutationToken: ++mutationTokenRef.current };
    activeMutationRef.current = variables;
    setActiveMutation(variables);
    return variables;
  }, [authOwner, isCurrentOpening, principalGeneration]);

  function mutationOptions<T extends MutationContext>(kind: EditorKind, request: (variables: T) => Promise<unknown>, success: string, failure: string) {
    const canPublish = (variables: T): boolean => isCurrentMutation(variables) && isCurrentOpening(kind, variables.openingToken, variables.groupId);
    return {
      mutationFn: (variables: T): Promise<unknown> => {
        if (!canPublish(variables)) return Promise.reject(new Error("Group editor was replaced"));
        startedMutationsRef.current.add(variables);
        return request(variables);
      },
      onSuccess: (_result: unknown, variables: T): void => {
        // A successful request may refresh its own session after its editor closes.
        const ownsSession = (): boolean => isCurrentPageContext(variables.owner, variables.principalGeneration);
        if (!ownsSession()) return;
        toast.success(success);
        if (!ownsSession()) return;
        const queryKey = kind === "members" || kind === "vaults" ? ["groups", variables.groupId, kind] : ["groups"];
        void queryClient.invalidateQueries({ queryKey }).catch(() => undefined);
        if (!ownsSession()) return;
        void outerQueryClient.invalidateQueries({ queryKey }).catch(() => undefined);
        if (!ownsSession()) return;
        if (canPublish(variables)) closeEditor(kind, variables.openingToken, variables.groupId);
      },
      onError: (error: Error, variables: T): void => {
        if (!startedMutationsRef.current.has(variables) || !isCurrentPageContext(variables.owner, variables.principalGeneration)) return;
        toast.error(kind === "members" || kind === "vaults" ? error.message || failure : failure);
      },
      onSettled: (_result: unknown, _error: Error | null, variables: T): void => { releaseMutation(variables); },
    };
  }
  const createMutation = useMutation<unknown, Error, GroupCreateMutationVariables>(mutationOptions("create", ({ data }: GroupCreateMutationVariables) => createGroup(data.name, data.description ?? null, data.org_id), "Group created successfully", "Failed to create group"));
  const updateMutation = useMutation<unknown, Error, GroupEditMutationVariables>(mutationOptions("edit", ({ groupId, data }: GroupEditMutationVariables) => updateGroup(groupId, data.name, data.description ?? null), "Group updated successfully", "Failed to update group"));
  const deleteMutation = useMutation<unknown, Error, GroupDeleteMutationVariables>(mutationOptions("delete", ({ groupId }: GroupDeleteMutationVariables) => deleteGroup(groupId), "Group deleted successfully", "Failed to delete group"));
  const membersMutation = useMutation<unknown, Error, MembersMutationVariables>(mutationOptions("members", ({ groupId, userIds }: MembersMutationVariables) => updateGroupMembers(groupId, userIds), "Group members updated", "Failed to update group members"));
  const vaultsMutation = useMutation<unknown, Error, VaultsMutationVariables>(mutationOptions("vaults", ({ groupId, vaultAccess }: VaultsMutationVariables) => updateGroupVaults(groupId, vaultAccess), "Vault access updated", "Failed to update vault access"));

  const handleCreateClick = useCallback(() => openEditor("create"), [openEditor]);
  const handleEditClick = useCallback((group: Group) => openEditor("edit", group), [openEditor]);
  const handleDeleteClick = useCallback((group: Group) => openEditor("delete", group), [openEditor]);
  const handleManageMembersClick = useCallback((group: Group) => openEditor("members", group), [openEditor]);
  const handleManageVaultsClick = useCallback((group: Group) => openEditor("vaults", group), [openEditor]);
  const handleCreateSubmit = async (data: GroupFormData): Promise<void> => {
    const context = beginMutation("create", createEditorToken);
    if (!context) return;
    try { await createMutation.mutateAsync({ ...context, data }); } finally { releaseMutation(context); }
  };
  const handleEditSubmit = async (data: GroupFormData): Promise<void> => {
    if (!selectedGroup) return;
    const context = beginMutation("edit", editEditorToken, selectedGroup.id);
    if (!context) return;
    try { await updateMutation.mutateAsync({ ...context, groupId: selectedGroup.id, data }); } finally { releaseMutation(context); }
  };
  const handleDeleteConfirm = async (): Promise<void> => {
    if (!selectedGroup) return;
    const context = beginMutation("delete", deleteEditorToken, selectedGroup.id);
    if (!context) return;
    try { await deleteMutation.mutateAsync({ ...context, groupId: selectedGroup.id }); } finally { releaseMutation(context); }
  };
  const handleMembersSave = async (userIds: number[]): Promise<void> => {
    if (!selectedGroup) return;
    const context = beginMutation("members", membersEditorToken, selectedGroup.id);
    if (!context) return;
    try { await membersMutation.mutateAsync({ ...context, groupId: selectedGroup.id, userIds }); } finally { releaseMutation(context); }
  };
  const handleVaultsSave = async (vaultAccess: VaultAccessItem[]): Promise<void> => {
    if (!selectedGroup) return;
    const context = beginMutation("vaults", vaultsEditorToken, selectedGroup.id);
    if (!context) return;
    try { await vaultsMutation.mutateAsync({ ...context, groupId: selectedGroup.id, vaultAccess }); } finally { releaseMutation(context); }
  };
  const isOpeningPending = (token: number): boolean => activeMutation?.openingToken === token && isCurrentMutation(activeMutation);

  // ============================================================================
  // Render
  // ============================================================================

  return (
    <div className="space-y-6 animate-in fade-in duration-300 pb-12" role="main" aria-label="Groups Management">
      {/* Header */}
      <PageTitleHeader
        title="Groups"
        description="Manage user groups and their vault access permissions"
        actions={
          <Button onClick={handleCreateClick} aria-label="Create new group">
            <Plus className="mr-2 h-4 w-4" aria-hidden="true" />
            Create Group
          </Button>
        }
      />

      {/* Groups Table */}
      <GroupTable
        onEdit={handleEditClick}
        onDelete={handleDeleteClick}
        onManageMembers={handleManageMembersClick}
        onManageVaults={handleManageVaultsClick}
      />

      {/* Create Group Dialog */}
      <GroupFormDialog
        key={`create:${createEditorToken}`}
        mode="create"
        open={createDialogOpen}
        onOpenChange={(next) => { if (!next) closeEditor("create", createEditorToken); }}
        onSubmit={handleCreateSubmit}
        isLoading={isOpeningPending(createEditorToken)}
      />

      {/* Edit Group Dialog */}
      <GroupFormDialog
        key={`edit:${editEditorToken}`}
        mode="edit"
        group={selectedGroup}
        open={editDialogOpen}
        onOpenChange={(next) => { if (!next) closeEditor("edit", editEditorToken, selectedGroup?.id); }}
        onSubmit={handleEditSubmit}
        isLoading={isOpeningPending(editEditorToken)}
      />

      {/* Manage Members Sheet */}
      <ManageMembersSheet
        group={selectedGroup}
        open={membersSheetOpen}
        editorToken={membersEditorToken}
        onOpenChange={(next) => { if (!next) closeEditor("members", membersEditorToken, selectedGroup?.id); }}
        onSave={handleMembersSave}
      />

      {/* Manage Vaults Sheet */}
      <ManageVaultsSheet
        group={selectedGroup}
        open={vaultsSheetOpen}
        editorToken={vaultsEditorToken}
        onOpenChange={(next) => { if (!next) closeEditor("vaults", vaultsEditorToken, selectedGroup?.id); }}
        onSave={handleVaultsSave}
      />

      {/* Delete Confirmation Dialog */}
      <Dialog open={deleteDialogOpen} onOpenChange={(next) => { if (!next) closeEditor("delete", deleteEditorToken, selectedGroup?.id); }}>
        <DialogContent className="sm:max-w-[400px]" aria-labelledby="delete-title" aria-describedby="delete-desc">
          <DialogHeader>
            <DialogTitle id="delete-title">Delete Group</DialogTitle>
            <DialogDescription id="delete-desc">
              Are you sure you want to delete <strong>{selectedGroup?.name}</strong>?
              This action cannot be undone. Members will lose access to vaults through this group.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="flex-col gap-2 sm:flex-row">
            <Button
              type="button"
              variant="outline"
              onClick={() => closeEditor("delete", deleteEditorToken, selectedGroup?.id)}
              disabled={isOpeningPending(deleteEditorToken)}
              className="w-full sm:w-auto"
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={() => { void handleDeleteConfirm().catch(() => undefined); }}
              disabled={isOpeningPending(deleteEditorToken)}
              className="w-full sm:w-auto"
              aria-label="Confirm delete group"
            >
              {isOpeningPending(deleteEditorToken) ? "Deleting..." : "Delete Group"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function ScopedGroupsPage({ outerQueryClient, authOwner, principalGeneration }: { outerQueryClient: QueryClient; authOwner: AuthOwner; principalGeneration: number }): JSX.Element {
  const [queryClient] = useState(() => new QueryClient({ defaultOptions: outerQueryClient.getDefaultOptions() }));
  const leaseRef = useRef(0);
  useEffect(() => {
    const lease = ++leaseRef.current;
    return () => {
      // StrictMode reattaches the same scope before this microtask.
      void Promise.resolve().then(() => {
        if (!isScopedLeaseCurrent(leaseRef, lease)) return;
        void queryClient.cancelQueries().catch(() => undefined);
        queryClient.clear();
      });
    };
  }, [queryClient]);
  return <QueryClientProvider client={queryClient}><AdminGroupsPageContent outerQueryClient={outerQueryClient} authOwner={authOwner} principalGeneration={principalGeneration} /></QueryClientProvider>;
}

export default function AdminGroupsPage(): JSX.Element {
  const outerQueryClient = useQueryClient();
  const authOwner = useSyncExternalStore(subscribeAuthOwner, captureAuthOwner, captureAuthOwner);
  const principalGeneration = useSyncExternalStore(subscribeAuthPrincipal, captureAuthPrincipalGeneration, captureAuthPrincipalGeneration);
  const scopeKey = `${authOwner.id}:${principalGeneration}`;
  return <AdminGuard><ScopedGroupsPage key={scopeKey} outerQueryClient={outerQueryClient} authOwner={authOwner} principalGeneration={principalGeneration} /></AdminGuard>;
}
