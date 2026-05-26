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
import { updateFolder, type Folder } from "@/lib/api";

interface MoveFolderDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The folder being reparented. */
  folder: Folder;
  /** All folders in the vault (used to build the picker). */
  folders: Folder[];
  /** Called after a successful move so the parent can refresh its folder list. */
  onMoved: () => void;
}

interface FolderOption {
  id: number | null;
  name: string;
  depth: number;
}

/**
 * Flatten the folder tree into a depth-ordered list for the picker,
 * excluding the folder being moved (moving it into itself is meaningless, and
 * deeper cycle cases are caught by the backend with a clear error toast).
 */
function flattenForPicker(folders: Folder[], excludeId: number): FolderOption[] {
  const visible = folders.filter((f) => f.id !== excludeId);

  const childrenByParent = new Map<number | null, Folder[]>();
  visible.forEach((f) => {
    const key = f.parent_folder_id ?? null;
    const list = childrenByParent.get(key) ?? [];
    list.push(f);
    childrenByParent.set(key, list);
  });

  const ids = new Set(visible.map((f) => f.id));
  const result: FolderOption[] = [];

  const visit = (parentId: number | null, depth: number) => {
    const children = (childrenByParent.get(parentId) ?? [])
      .slice()
      .sort((a, b) => a.name.localeCompare(b.name));
    for (const child of children) {
      result.push({ id: child.id, name: child.name, depth });
      visit(child.id, depth + 1);
    }
  };
  visit(null, 0);

  // Append orphaned folders whose parent is not in the visible set.
  visible
    .filter((f) => f.parent_folder_id != null && !ids.has(f.parent_folder_id))
    .forEach((f) => {
      if (!result.some((o) => o.id === f.id)) {
        result.push({ id: f.id, name: f.name, depth: 0 });
      }
    });

  return result;
}

export function MoveFolderDialog({
  open,
  onOpenChange,
  folder,
  folders,
  onMoved,
}: MoveFolderDialogProps) {
  // Default to the folder's current parent so the picker reflects where it already lives.
  const [target, setTarget] = useState<number | null>(
    folder.parent_folder_id ?? null
  );
  const [moving, setMoving] = useState(false);
  const options = useMemo(
    () => flattenForPicker(folders, folder.id),
    [folders, folder.id]
  );

  const handleMove = async () => {
    if (moving) return;
    setMoving(true);
    try {
      await updateFolder(folder.id, { parent_folder_id: target });
      toast.success(`Moved folder "${folder.name}"`);
      onMoved();
      onOpenChange(false);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to move folder");
    } finally {
      setMoving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Move folder</DialogTitle>
          <DialogDescription>
            Choose a new parent for &ldquo;{folder.name}&rdquo;.
          </DialogDescription>
        </DialogHeader>

        <div
          className="max-h-72 space-y-1 overflow-y-auto"
          role="radiogroup"
          aria-label="Target parent folder"
        >
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
            <span>No parent (root)</span>
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
              onClick={() => setTarget(opt.id ?? null)}
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
            {moving ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : null}
            Move
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
