// frontend/src/pages/AdminGroupsPage.tsx

import { useState, useCallback } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
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

// ============================================================================
// Main Page Component
// ============================================================================

function AdminGroupsPageContent(): JSX.Element {
  const queryClient = useQueryClient();

  // Modal/Sheet states
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [membersSheetOpen, setMembersSheetOpen] = useState(false);
  const [vaultsSheetOpen, setVaultsSheetOpen] = useState(false);

  // Selected group for actions
  const [selectedGroup, setSelectedGroup] = useState<Group | null>(null);

  // Invalidate all groups queries (list + any sub-queries)
  const invalidateGroups = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ["groups"] });
  }, [queryClient]);

  // ============================================================================
  // Mutations
  // ============================================================================

  const createMutation = useMutation({
    mutationFn: (data: GroupFormData) => createGroup(data.name, data.description ?? null, data.org_id),
    onSuccess: () => {
      toast.success("Group created successfully");
      setCreateDialogOpen(false);
      invalidateGroups();
    },
    onError: () => {
      toast.error("Failed to create group");
    },
  });

  const updateMutation = useMutation({
    mutationFn: (data: GroupFormData) => {
      if (!selectedGroup) throw new Error("No group selected");
      return updateGroup(selectedGroup.id, data.name, data.description ?? null);
    },
    onSuccess: () => {
      toast.success("Group updated successfully");
      setEditDialogOpen(false);
      invalidateGroups();
    },
    onError: () => {
      toast.error("Failed to update group");
    },
  });

  const deleteMutation = useMutation({
    mutationFn: () => {
      if (!selectedGroup) throw new Error("No group selected");
      return deleteGroup(selectedGroup.id);
    },
    onSuccess: () => {
      toast.success("Group deleted successfully");
      setDeleteDialogOpen(false);
      invalidateGroups();
    },
    onError: () => {
      toast.error("Failed to delete group");
    },
  });

  const membersMutation = useMutation({
    mutationFn: (userIds: number[]) => {
      if (!selectedGroup) throw new Error("No group selected");
      return updateGroupMembers(selectedGroup.id, userIds);
    },
    onSuccess: () => {
      toast.success("Group members updated");
      setMembersSheetOpen(false);
      queryClient.invalidateQueries({ queryKey: ["groups", selectedGroup?.id, "members"] });
    },
    onError: (error: Error) => {
      toast.error(error.message || "Failed to update group members");
    },
  });

  const vaultsMutation = useMutation({
    mutationFn: (vaultAccess: VaultAccessItem[]) => {
      if (!selectedGroup) throw new Error("No group selected");
      return updateGroupVaults(selectedGroup.id, vaultAccess);
    },
    onSuccess: () => {
      toast.success("Vault access updated");
      setVaultsSheetOpen(false);
      queryClient.invalidateQueries({ queryKey: ["groups", selectedGroup?.id, "vaults"] });
    },
    onError: (error: Error) => {
      toast.error(error.message || "Failed to update vault access");
    },
  });

  // ============================================================================
  // Event Handlers
  // ============================================================================

  const handleCreateClick = useCallback(() => {
    setCreateDialogOpen(true);
  }, []);

  const handleEditClick = useCallback((group: Group) => {
    setSelectedGroup(group);
    setEditDialogOpen(true);
  }, []);

  const handleDeleteClick = useCallback((group: Group) => {
    setSelectedGroup(group);
    setDeleteDialogOpen(true);
  }, []);

  const handleManageMembersClick = useCallback((group: Group) => {
    setSelectedGroup(group);
    setMembersSheetOpen(true);
  }, []);

  const handleManageVaultsClick = useCallback((group: Group) => {
    setSelectedGroup(group);
    setVaultsSheetOpen(true);
  }, []);

  const handleCreateSubmit = useCallback(async (data: GroupFormData) => {
    await createMutation.mutateAsync(data);
  }, [createMutation]);

  const handleEditSubmit = useCallback(async (data: GroupFormData) => {
    await updateMutation.mutateAsync(data);
  }, [updateMutation]);

  const handleDeleteConfirm = useCallback(async () => {
    await deleteMutation.mutateAsync();
  }, [deleteMutation]);

  const handleMembersSave = useCallback(async (userIds: number[]) => {
    await membersMutation.mutateAsync(userIds);
  }, [membersMutation]);

  const handleVaultsSave = useCallback(async (vaultAccess: VaultAccessItem[]) => {
    await vaultsMutation.mutateAsync(vaultAccess);
  }, [vaultsMutation]);

  // ============================================================================
  // Render
  // ============================================================================

  return (
    <div className="space-y-6 animate-in fade-in duration-300 pb-12" role="main" aria-label="Groups Management">
      {/* Header */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <PageTitleHeader
          title="Groups"
          description="Manage user groups and their vault access permissions"
        />
        <Button onClick={handleCreateClick} aria-label="Create new group">
          <Plus className="mr-2 h-4 w-4" aria-hidden="true" />
          Create Group
        </Button>
      </div>

      {/* Groups Table */}
      <GroupTable
        onEdit={handleEditClick}
        onDelete={handleDeleteClick}
        onManageMembers={handleManageMembersClick}
        onManageVaults={handleManageVaultsClick}
      />

      {/* Create Group Dialog */}
      <GroupFormDialog
        mode="create"
        open={createDialogOpen}
        onOpenChange={setCreateDialogOpen}
        onSubmit={handleCreateSubmit}
        isLoading={createMutation.isPending}
      />

      {/* Edit Group Dialog */}
      <GroupFormDialog
        mode="edit"
        group={selectedGroup}
        open={editDialogOpen}
        onOpenChange={setEditDialogOpen}
        onSubmit={handleEditSubmit}
        isLoading={updateMutation.isPending}
      />

      {/* Manage Members Sheet */}
      <ManageMembersSheet
        group={selectedGroup}
        open={membersSheetOpen}
        onOpenChange={setMembersSheetOpen}
        onSave={handleMembersSave}
      />

      {/* Manage Vaults Sheet */}
      <ManageVaultsSheet
        group={selectedGroup}
        open={vaultsSheetOpen}
        onOpenChange={setVaultsSheetOpen}
        onSave={handleVaultsSave}
      />

      {/* Delete Confirmation Dialog */}
      <Dialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
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
              onClick={() => setDeleteDialogOpen(false)}
              disabled={deleteMutation.isPending}
              className="w-full sm:w-auto"
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              onClick={handleDeleteConfirm}
              disabled={deleteMutation.isPending}
              className="w-full sm:w-auto"
              aria-label="Confirm delete group"
            >
              {deleteMutation.isPending ? "Deleting..." : "Delete Group"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

export default function AdminGroupsPage(): JSX.Element {
  return (
    <AdminGuard>
      <AdminGroupsPageContent />
    </AdminGuard>
  );
}
