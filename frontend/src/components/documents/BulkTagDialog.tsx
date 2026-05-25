import { useState } from "react";
import { toast } from "sonner";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import { Loader2, Plus } from "lucide-react";
import { assignTags, createTag, type Tag } from "@/lib/api";

interface BulkTagDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  vaultId: number;
  selectedFileIds: number[];
  tags: Tag[];
  onTagsChanged: () => void;
  onAssigned: () => void;
}

export function BulkTagDialog({
  open,
  onOpenChange,
  vaultId,
  selectedFileIds,
  tags,
  onTagsChanged,
  onAssigned,
}: BulkTagDialogProps) {
  const [checked, setChecked] = useState<Set<number>>(new Set());
  const [newTagName, setNewTagName] = useState("");
  const [creating, setCreating] = useState(false);
  const [assigning, setAssigning] = useState(false);

  const toggle = (id: number, on: boolean) => {
    setChecked((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  };

  const handleCreateTag = async () => {
    const name = newTagName.trim();
    if (!name) return;
    setCreating(true);
    try {
      const tag = await createTag(vaultId, name);
      setNewTagName("");
      setChecked((prev) => new Set(prev).add(tag.id));
      onTagsChanged();
      toast.success(`Created tag "${tag.name}"`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to create tag");
    } finally {
      setCreating(false);
    }
  };

  const handleAssign = async () => {
    if (checked.size === 0 || selectedFileIds.length === 0) return;
    setAssigning(true);
    try {
      const result = await assignTags(vaultId, selectedFileIds, Array.from(checked));
      toast.success(
        `Assigned ${result.assigned} tag${result.assigned === 1 ? "" : "s"} across ${
          selectedFileIds.length
        } document${selectedFileIds.length === 1 ? "" : "s"}`
      );
      setChecked(new Set());
      onAssigned();
      onOpenChange(false);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to assign tags");
    } finally {
      setAssigning(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Assign tags</DialogTitle>
          <DialogDescription>
            Add tags to {selectedFileIds.length} selected document
            {selectedFileIds.length === 1 ? "" : "s"}.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="flex items-center gap-2">
            <Input
              placeholder="New tag name"
              value={newTagName}
              onChange={(e) => setNewTagName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  handleCreateTag();
                }
              }}
            />
            <Button
              variant="outline"
              size="sm"
              onClick={handleCreateTag}
              disabled={creating || !newTagName.trim()}
            >
              {creating ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
            </Button>
          </div>

          <div className="max-h-60 overflow-y-auto space-y-2">
            {tags.length === 0 ? (
              <p className="text-sm text-muted-foreground">No tags yet. Create one above.</p>
            ) : (
              tags.map((tag) => (
                <label
                  key={tag.id}
                  className="flex items-center gap-2 text-sm cursor-pointer"
                >
                  <Checkbox
                    checked={checked.has(tag.id)}
                    onCheckedChange={(c) => toggle(tag.id, !!c)}
                    aria-label={`Select tag ${tag.name}`}
                  />
                  <span>{tag.name}</span>
                  <span className="text-muted-foreground">({tag.document_count})</span>
                </label>
              ))
            )}
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={handleAssign} disabled={assigning || checked.size === 0}>
            {assigning ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : null}
            Assign
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
