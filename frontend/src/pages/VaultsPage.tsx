import { useState, useEffect } from "react";
import { toast } from "sonner";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Database, Plus, Pencil, Trash2, FileText, Brain, MessageSquare, Loader2 } from "lucide-react";
import { EmptyState } from "@/components/EmptyState";
import { useVaultStore } from "@/stores/useVaultStore";
import { listOrganizations } from "@/lib/api";
import type { Vault, Organization } from "@/lib/api";
import { useTestMode } from "@/fixtures/TestModeContext";
import { mockVaults, mockOrganizations } from "@/fixtures/vaults";
import { PageTitleHeader } from "@/components/layout/PageTitleHeader";

export default function VaultsPage() {
  const testMode = useTestMode();
  const { vaults, loading, fetchVaults, addVault, editVault, removeVault, activeVaultId, setActiveVault } = useVaultStore();
  const [createDialogOpen, setCreateDialogOpen] = useState(false);
  const [editDialogOpen, setEditDialogOpen] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [selectedVault, setSelectedVault] = useState<Vault | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [orgId, setOrgId] = useState<number | null>(null);
  const [orgs, setOrgs] = useState<Organization[]>([]);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    if (testMode) {
      useVaultStore.setState({ vaults: mockVaults, loading: false, error: null });
      setOrgs(mockOrganizations);
      if (mockOrganizations.length === 1) setOrgId(mockOrganizations[0].id);
      return;
    }
    fetchVaults();
    listOrganizations().then((data) => {
      setOrgs(data);
      // Auto-select if user has exactly one org
      if (data.length === 1) setOrgId(data[0].id);
    }).catch(() => {});
  }, [fetchVaults, testMode]);

  function openCreateDialog() {
    setName("");
    setDescription("");
    if (orgs.length === 1) setOrgId(orgs[0].id);
    else setOrgId(null);
    setCreateDialogOpen(true);
  }

  function openEditDialog(vault: Vault) {
    setSelectedVault(vault);
    setName(vault.name);
    setDescription(vault.description);
    setEditDialogOpen(true);
  }

  function openDeleteDialog(vault: Vault) {
    setSelectedVault(vault);
    setDeleteDialogOpen(true);
  }

  async function handleCreate() {
    if (!name.trim()) {
      toast.error("Vault name is required");
      return;
    }
    if (orgs.length > 1 && orgId === null) {
      toast.error("Please select an organization for this vault");
      return;
    }
    setSaving(true);
    try {
      await addVault({ name: name.trim(), description: description.trim(), org_id: orgId });
      toast.success("Vault created successfully");
      setCreateDialogOpen(false);
      setName("");
      setDescription("");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to create vault");
    } finally {
      setSaving(false);
    }
  }

  async function handleEdit() {
    if (!name.trim()) {
      toast.error("Vault name is required");
      return;
    }
    if (!selectedVault) return;
    setSaving(true);
    try {
      await editVault(selectedVault.id, { name: name.trim(), description: description.trim() });
      toast.success("Vault updated successfully");
      setEditDialogOpen(false);
      setSelectedVault(null);
      setName("");
      setDescription("");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to update vault");
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    if (!selectedVault) return;
    setDeleting(true);
    try {
      await removeVault(selectedVault.id);
      toast.success("Vault deleted successfully");
      setDeleteDialogOpen(false);
      setSelectedVault(null);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to delete vault");
    } finally {
      setDeleting(false);
    }
  }

  function handleSetActive(vaultId: number) {
    setActiveVault(vaultId);
    toast.success("Active vault changed");
  }

  if (loading && vaults.length === 0) {
    return (
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
        {[1, 2, 3].map((i) => (
          <Card key={i} className="p-6 space-y-3">
            <Skeleton className="h-5 w-3/4" />
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-2/3" />
            <div className="flex gap-2 pt-2">
              <Skeleton className="h-6 w-16 rounded-full" />
              <Skeleton className="h-6 w-20 rounded-full" />
            </div>
          </Card>
        ))}
      </div>
    );
  }

  const canAdminVault = (vault: Vault) => vault.current_user_permission === "admin";

  return (
    <div className="space-y-6 animate-in fade-in duration-300 pb-12">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <PageTitleHeader
          title="Vaults"
          description="Manage your knowledge vaults"
        />
        <Button onClick={openCreateDialog}>
          <Plus className="mr-2 h-4 w-4" /> New Vault
        </Button>
      </div>

      {/* Vault Cards Grid */}
      {vaults.length === 0 && (
        <EmptyState
          icon={Database}
          title="No vaults yet"
          description="Create a vault to organize your documents into separate knowledge bases."
        />
      )}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        {vaults.map(vault => {
          const canEditVault = canAdminVault(vault);
          const unavailableReason = "Vault admin permission is required";

          return (
          <Card key={vault.id}>
            <CardHeader>
              <div className="flex items-start justify-between">
                <div className="flex items-center gap-2">
                  <Database className="h-5 w-5 text-muted-foreground" />
                  <CardTitle className="text-lg">{vault.name}</CardTitle>
                </div>
                {vault.id === activeVaultId && (
                  <Badge variant="secondary" className="text-xs">Active</Badge>
                )}
              </div>
              <CardDescription className="mt-2">
                {vault.description || <span className="text-muted-foreground italic">No description</span>}
              </CardDescription>
            </CardHeader>
            <CardContent>
              <div className="space-y-4">
                {/* Stat Badges */}
                <div className="flex items-center gap-3 text-sm text-muted-foreground">
                  <div className="flex items-center gap-1">
                    <FileText className="h-3 w-3" />
                    <span>{vault.file_count} files</span>
                  </div>
                  <div className="flex items-center gap-1">
                    <Brain className="h-3 w-3" />
                    <span>{vault.memory_count} memories</span>
                  </div>
                  <div className="flex items-center gap-1">
                    <MessageSquare className="h-3 w-3" />
                    <span>{vault.session_count} sessions</span>
                  </div>
                </div>

                {/* Action Buttons */}
                <div className="flex items-center gap-2 pt-2">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => handleSetActive(vault.id)}
                    disabled={vault.id === activeVaultId}
                    className="flex-1"
                  >
                    Set Active
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => openEditDialog(vault)}
                    disabled={!canEditVault}
                    title={canEditVault ? "Edit vault" : unavailableReason}
                  >
                    <Pencil className="h-4 w-4" />
                  </Button>
                  <Button
                    variant="destructive"
                    size="sm"
                    onClick={() => openDeleteDialog(vault)}
                    disabled={!canEditVault}
                    title={canEditVault ? "Delete vault" : unavailableReason}
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
              </div>
            </CardContent>
          </Card>
          );
        })}
      </div>

      {/* Create Dialog */}
      <Dialog open={createDialogOpen} onOpenChange={setCreateDialogOpen}>
        <DialogContent aria-labelledby="create-vault-title" aria-describedby="create-vault-desc">
          <DialogHeader>
            <DialogTitle id="create-vault-title">Create New Vault</DialogTitle>
            <DialogDescription id="create-vault-desc">
              Add a new knowledge vault to organize your documents and memories.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div>
              <Label htmlFor="vault-name">Vault Name</Label>
              <Input
                id="vault-name"
                placeholder="Vault name"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <div>
              <Label htmlFor="vault-description">Description</Label>
              <Input
                id="vault-description"
                placeholder="Description (optional)"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </div>
            {orgs.length > 0 && (
              <div>
                <Label htmlFor="vault-org">Organization</Label>
                <Select
                  value={orgId !== null ? String(orgId) : ""}
                  onValueChange={(v) => setOrgId(v ? Number(v) : null)}
                >
                  <SelectTrigger id="vault-org">
                    <SelectValue placeholder={orgs.length === 1 ? orgs[0].name : "Select organization…"} />
                  </SelectTrigger>
                  <SelectContent>
                    {orgs.map((org) => (
                      <SelectItem key={org.id} value={String(org.id)}>
                        {org.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                {orgs.length > 1 && (
                  <p className="text-xs text-muted-foreground mt-1">
                    Required: select which organization owns this vault.
                  </p>
                )}
              </div>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setCreateDialogOpen(false)}>
              Cancel
            </Button>
            <Button onClick={handleCreate} disabled={saving}>
              {saving ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Creating...
                </>
              ) : (
                "Create"
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Edit Dialog */}
      <Dialog open={editDialogOpen} onOpenChange={setEditDialogOpen}>
        <DialogContent aria-labelledby="edit-vault-title" aria-describedby="edit-vault-desc">
          <DialogHeader>
            <DialogTitle id="edit-vault-title">Edit Vault</DialogTitle>
            <DialogDescription id="edit-vault-desc">
              Update vault name or description.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div>
              <Label htmlFor="vault-name">Vault Name</Label>
              <Input
                id="vault-name"
                placeholder="Vault name"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <div>
              <Label htmlFor="vault-description">Description</Label>
              <Input
                id="vault-description"
                placeholder="Description (optional)"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditDialogOpen(false)}>
              Cancel
            </Button>
            <Button onClick={handleEdit} disabled={saving}>
              {saving ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Saving...
                </>
              ) : (
                "Save"
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete Confirmation Dialog */}
      <Dialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
<DialogContent aria-labelledby="delete-vault-title" aria-describedby="delete-vault-desc">
        <DialogHeader>
          <DialogTitle id="delete-vault-title">Delete Vault</DialogTitle>
          <DialogDescription id="delete-vault-desc">
            Are you sure you want to delete "{selectedVault?.name}"? This will permanently delete all documents, memories, and chat sessions in this vault. This action cannot be undone.
          </DialogDescription>
        </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteDialogOpen(false)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={handleDelete} disabled={deleting}>
              {deleting ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Deleting...
                </>
              ) : (
                "Delete"
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
