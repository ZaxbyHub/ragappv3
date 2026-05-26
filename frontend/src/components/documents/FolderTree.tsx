import { useMemo, useState } from "react";
import {
  ChevronRight,
  ChevronDown,
  Folder as FolderIcon,
  FolderInput,
  FolderPlus,
  Library,
  MoreVertical,
  Pencil,
  Trash2,
  Check,
  X,
  Loader2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { Folder } from "@/lib/api";

interface FolderTreeProps {
  folders: Folder[];
  selectedFolderId: number | null;
  onSelect: (folderId: number | null) => void;
  canMutate: boolean;
  onCreate: (name: string, parentFolderId: number | null) => Promise<void>;
  onRename: (folderId: number, name: string) => Promise<void>;
  onDelete: (folder: Folder) => void;
  onMove?: (folder: Folder) => void;
}

interface FolderNodeData extends Folder {
  children: FolderNodeData[];
}

/** Build a parent→children tree from the flat folder list. */
function buildTree(folders: Folder[]): FolderNodeData[] {
  const byId = new Map<number, FolderNodeData>();
  folders.forEach((f) => byId.set(f.id, { ...f, children: [] }));
  const roots: FolderNodeData[] = [];
  byId.forEach((node) => {
    if (node.parent_folder_id != null && byId.has(node.parent_folder_id)) {
      byId.get(node.parent_folder_id)!.children.push(node);
    } else {
      roots.push(node);
    }
  });
  return roots;
}

/** Inline text field used for both creating and renaming folders. */
function FolderNameInput({
  initial,
  placeholder,
  onSubmit,
  onCancel,
}: {
  initial: string;
  placeholder: string;
  onSubmit: (name: string) => Promise<void>;
  onCancel: () => void;
}) {
  const [value, setValue] = useState(initial);
  const [saving, setSaving] = useState(false);

  const submit = async () => {
    const name = value.trim();
    if (!name || saving) return;
    setSaving(true);
    try {
      await onSubmit(name);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex items-center gap-1 py-1">
      <Input
        autoFocus
        className="h-7 text-sm"
        placeholder={placeholder}
        value={value}
        disabled={saving}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            submit();
          } else if (e.key === "Escape") {
            e.preventDefault();
            onCancel();
          }
        }}
        aria-label={placeholder}
      />
      <Button
        size="icon"
        variant="ghost"
        className="h-7 w-7"
        onClick={submit}
        disabled={saving || !value.trim()}
        aria-label="Confirm folder name"
      >
        {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
      </Button>
      <Button
        size="icon"
        variant="ghost"
        className="h-7 w-7"
        onClick={onCancel}
        disabled={saving}
        aria-label="Cancel"
      >
        <X className="h-3.5 w-3.5" />
      </Button>
    </div>
  );
}

function FolderNode({
  node,
  depth,
  selectedFolderId,
  expanded,
  toggleExpanded,
  onSelect,
  canMutate,
  renamingId,
  setRenamingId,
  addingChildId,
  setAddingChildId,
  onCreate,
  onRename,
  onDelete,
  onMove,
}: {
  node: FolderNodeData;
  depth: number;
  selectedFolderId: number | null;
  expanded: Set<number>;
  toggleExpanded: (id: number) => void;
  onSelect: (folderId: number | null) => void;
  canMutate: boolean;
  renamingId: number | null;
  setRenamingId: (id: number | null) => void;
  addingChildId: number | null;
  setAddingChildId: (id: number | null) => void;
  onCreate: (name: string, parentFolderId: number | null) => Promise<void>;
  onRename: (folderId: number, name: string) => Promise<void>;
  onDelete: (folder: Folder) => void;
  onMove?: (folder: Folder) => void;
}) {
  const hasChildren = node.children.length > 0;
  const isOpen = expanded.has(node.id);
  const isSelected = selectedFolderId === node.id;

  if (renamingId === node.id) {
    return (
      <div style={{ paddingLeft: depth * 16 }}>
        <FolderNameInput
          initial={node.name}
          placeholder="Folder name"
          onCancel={() => setRenamingId(null)}
          onSubmit={async (name) => {
            await onRename(node.id, name);
            setRenamingId(null);
          }}
        />
      </div>
    );
  }

  return (
    <div>
      <div
        className={`group flex items-center gap-1 rounded-md pr-1 ${
          isSelected ? "bg-accent" : "hover:bg-accent/50"
        }`}
        style={{ paddingLeft: depth * 16 }}
      >
        <button
          type="button"
          className="flex h-6 w-6 shrink-0 items-center justify-center text-muted-foreground"
          onClick={() => hasChildren && toggleExpanded(node.id)}
          aria-label={hasChildren ? (isOpen ? "Collapse folder" : "Expand folder") : undefined}
          tabIndex={hasChildren ? 0 : -1}
        >
          {hasChildren ? (
            isOpen ? (
              <ChevronDown className="h-4 w-4" />
            ) : (
              <ChevronRight className="h-4 w-4" />
            )
          ) : null}
        </button>
        <button
          type="button"
          className="flex min-w-0 flex-1 items-center gap-2 py-1.5 text-left text-sm"
          onClick={() => onSelect(node.id)}
        >
          <FolderIcon className="h-4 w-4 shrink-0 text-muted-foreground" />
          <span className="truncate">{node.name}</span>
          <span className="ml-auto shrink-0 text-xs text-muted-foreground">
            {node.document_count}
          </span>
        </button>
        {canMutate && (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                size="icon"
                variant="ghost"
                className="h-6 w-6 shrink-0 opacity-0 group-hover:opacity-100 data-[state=open]:opacity-100"
                aria-label={`Folder actions for ${node.name}`}
              >
                <MoreVertical className="h-4 w-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem
                onClick={() => {
                  setAddingChildId(node.id);
                  if (!isOpen) toggleExpanded(node.id);
                }}
              >
                <FolderPlus className="mr-2 h-4 w-4" />
                New subfolder
              </DropdownMenuItem>
              <DropdownMenuItem onClick={() => setRenamingId(node.id)}>
                <Pencil className="mr-2 h-4 w-4" />
                Rename
              </DropdownMenuItem>
              {onMove && (
                <DropdownMenuItem onClick={() => onMove(node)}>
                  <FolderInput className="mr-2 h-4 w-4" />
                  Move to...
                </DropdownMenuItem>
              )}
              <DropdownMenuItem
                className="text-destructive focus:text-destructive"
                onClick={() => onDelete(node)}
              >
                <Trash2 className="mr-2 h-4 w-4" />
                Delete
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        )}
      </div>

      {isOpen && (
        <div>
          {node.children.map((child) => (
            <FolderNode
              key={child.id}
              node={child}
              depth={depth + 1}
              selectedFolderId={selectedFolderId}
              expanded={expanded}
              toggleExpanded={toggleExpanded}
              onSelect={onSelect}
              canMutate={canMutate}
              renamingId={renamingId}
              setRenamingId={setRenamingId}
              addingChildId={addingChildId}
              setAddingChildId={setAddingChildId}
              onCreate={onCreate}
              onRename={onRename}
              onDelete={onDelete}
              onMove={onMove}
            />
          ))}
          {addingChildId === node.id && (
            <div style={{ paddingLeft: (depth + 1) * 16 }}>
              <FolderNameInput
                initial=""
                placeholder="New subfolder name"
                onCancel={() => setAddingChildId(null)}
                onSubmit={async (name) => {
                  await onCreate(name, node.id);
                  setAddingChildId(null);
                }}
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function FolderTree({
  folders,
  selectedFolderId,
  onSelect,
  canMutate,
  onCreate,
  onRename,
  onDelete,
  onMove,
}: FolderTreeProps) {
  const tree = useMemo(() => buildTree(folders), [folders]);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [renamingId, setRenamingId] = useState<number | null>(null);
  const [addingChildId, setAddingChildId] = useState<number | null>(null);
  const [addingRoot, setAddingRoot] = useState(false);

  const toggleExpanded = (id: number) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <div className="w-60 shrink-0 space-y-1" aria-label="Folders">
      <div className="flex items-center justify-between px-1">
        <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Folders
        </span>
        {canMutate && (
          <Button
            size="icon"
            variant="ghost"
            className="h-6 w-6"
            onClick={() => setAddingRoot(true)}
            aria-label="New folder"
          >
            <FolderPlus className="h-4 w-4" />
          </Button>
        )}
      </div>

      <button
        type="button"
        className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm ${
          selectedFolderId == null ? "bg-accent" : "hover:bg-accent/50"
        }`}
        onClick={() => onSelect(null)}
      >
        <Library className="h-4 w-4 shrink-0 text-muted-foreground" />
        <span>All documents</span>
      </button>

      {tree.map((node) => (
        <FolderNode
          key={node.id}
          node={node}
          depth={0}
          selectedFolderId={selectedFolderId}
          expanded={expanded}
          toggleExpanded={toggleExpanded}
          onSelect={onSelect}
          canMutate={canMutate}
          renamingId={renamingId}
          setRenamingId={setRenamingId}
          addingChildId={addingChildId}
          setAddingChildId={setAddingChildId}
          onCreate={onCreate}
          onRename={onRename}
          onDelete={onDelete}
          onMove={onMove}
        />
      ))}

      {addingRoot && (
        <FolderNameInput
          initial=""
          placeholder="New folder name"
          onCancel={() => setAddingRoot(false)}
          onSubmit={async (name) => {
            await onCreate(name, null);
            setAddingRoot(false);
          }}
        />
      )}

      {tree.length === 0 && !addingRoot && (
        <p className="px-2 py-1 text-xs text-muted-foreground">
          {canMutate ? "No folders yet. Create one above." : "No folders yet."}
        </p>
      )}
    </div>
  );
}
