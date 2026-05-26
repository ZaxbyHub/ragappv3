import { useMemo, useState } from "react";
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
import { Folder as FolderIcon, Loader2 } from "lucide-react";
import { moveDocumentsToFolder, type Folder } from "@/lib/api";

interface MoveToFolderDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  vaultId: number;
  selectedFileIds: number[];
  folders: Folder[];
  onMoved: () => void;
}

interface FolderOption {
  id: number | null;
  name: string;
  depth: number;
}

/** Flatten the folder tree into a depth-ordered list for the picker. */
function flattenForPicker(folders: Folder[]): FolderOption[] {
  const childrenByParent = new Map<number | null, Folder[]>();
  folders.forEach((f) => {
    const key = f.parent_folder_id ?? null;
    const list = childrenByParent.get(key) ?? [];
    list.push(f);
    childrenByParent.set(key, list);
  });
  // Orphans (parent not present) are treated as roots so they stay reachable.
  const ids = new Set(folders.map((f) => f.id));
  const result: FolderOption[] = [];
  const visit = (parentId: number | null, depth: number) => {
    const children = (childrenByParent.get(parentId) ?? []).slice().sort((a, b) =>
      a.name.localeCompare(b.name)
    );
    for (const child of children) {
      result.push({ id: child.id, name: child.name, depth });
      visit(child.id, depth + 1);
    }
  };
  visit(null, 0);
  // Append orphaned folders whose parent id isn't in the set.
  folders
    .filter((f) => f.parent_folder_id != null && !ids.has(f.parent_folder_id))
    .forEach((f) => {
      if (!result.some((o) => o.id === f.id)) {
        result.push({ id: f.id, name: f.name, depth: 0 });
      }
    });
  return result;
}

export function MoveToFolderDialog({
  open,
  onOpenChange,
  vaultId,
  selectedFileIds,
  folders,
  onMoved,
}: MoveToFolderDialogProps) {
  const [target, setTarget] = useState<number | null>(null);
  const [moving, setMoving] = useState(false);
  const options = useMemo(() => flattenForPicker(folders), [folders]);

  const handleMove = async () => {
    if (selectedFileIds.length === 0 || moving) return;
    setMoving(true);
    try {
      const result = await moveDocumentsToFolder(vaultId, selectedFileIds, target);
      toast.success(
        `Moved ${result.moved} document${result.moved === 1 ? "" : "s"}`
      );
      onMoved();
      onOpenChange(false);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to move documents");
    } finally {
      setMoving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Move to folder</DialogTitle>
          <DialogDescription>
            Move {selectedFileIds.length} selected document
            {selectedFileIds.length === 1 ? "" : "s"} into a folder.
          </DialogDescription>
        </DialogHeader>

        <div className="max-h-72 space-y-1 overflow-y-auto" role="radiogroup" aria-label="Target folder">
          <button
            type="button"
            role="radio"
            aria-checked={target === null}
            className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm ${
              target === null ? "bg-accent" : "hover:bg-accent/50"
            }`}
            onClick={() => setTarget(null)}
          >
            <FolderIcon className="h-4 w-4 text-muted-foreground" />
            <span>No folder (root)</span>
          </button>
          {options.map((opt) => (
            <button
              key={opt.id}
              type="button"
              role="radio"
              aria-checked={target === opt.id}
              className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm ${
                target === opt.id ? "bg-accent" : "hover:bg-accent/50"
              }`}
              style={{ paddingLeft: 8 + opt.depth * 16 }}
              onClick={() => setTarget(opt.id)}
            >
              <FolderIcon className="h-4 w-4 text-muted-foreground" />
              <span className="truncate">{opt.name}</span>
            </button>
          ))}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={handleMove} disabled={moving}>
            {moving ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
            Move
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
