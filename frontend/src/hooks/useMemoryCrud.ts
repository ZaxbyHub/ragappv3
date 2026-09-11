import { useState, useCallback, useRef } from "react";
import { toast } from "sonner";
import { addMemory, deleteMemory, type MemoryResult } from "@/lib/api";

export const MAX_MEMORY_CONTENT_LENGTH = 10000;

export interface NewMemory {
  content: string;
  category: string;
  tags: string;
  source: string;
}

export interface UseMemoryCrudReturn {
  isAddDialogOpen: boolean;
  setIsAddDialogOpen: (open: boolean) => void;
  newMemory: NewMemory;
  setNewMemory: (memory: NewMemory | ((prev: NewMemory) => NewMemory)) => void;
  isSubmitting: boolean;
  isDeleting: string | null;
  contentError: string | null;
  handleContentChange: (e: React.ChangeEvent<HTMLTextAreaElement>) => void;
  handleAddMemory: () => Promise<void>;
  handleKeyDown: (e: React.KeyboardEvent) => void;
  handleDeleteMemory: (id: string) => Promise<void>;
}

/** Manages memory CRUD operations — add, delete, and form state with validation. */
export function useMemoryCrud(
  activeVaultId: number | null,
  refreshMemories: () => Promise<void>
): UseMemoryCrudReturn {
  const [isAddDialogOpen, setIsAddDialogOpen] = useState(false);
  const [newMemory, setNewMemory] = useState<NewMemory>({
    content: "",
    category: "",
    tags: "",
    source: "",
  });
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isDeleting, setIsDeleting] = useState<string | null>(null);
  const [contentError, setContentError] = useState<string | null>(null);

  // In-flight guard (issue #515 / C41): Ctrl+Enter and the Add Memory button
  // both funnel into handleAddMemory; a synchronous ref (not state) blocks the
  // second invocation while the first request is pending, so the API is called
  // exactly once per submit intent.
  const submittingRef = useRef(false);

  const validateContent = useCallback((content: string): boolean => {
    if (content.length > MAX_MEMORY_CONTENT_LENGTH) {
      setContentError(`Content exceeds maximum length of ${MAX_MEMORY_CONTENT_LENGTH} characters`);
      return false;
    }
    setContentError(null);
    return true;
  }, []);

  const handleContentChange = useCallback((e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const value = e.target.value;
    setNewMemory((prev) => ({ ...prev, content: value }));
    if (value.length > MAX_MEMORY_CONTENT_LENGTH) {
      setContentError(`Content exceeds maximum length of ${MAX_MEMORY_CONTENT_LENGTH} characters`);
    } else {
      setContentError(null);
    }
  }, []);

  const handleAddMemory = useCallback(async () => {
    // C28 (issue #515): an empty add must surface user-visible feedback —
    // a silent return leaves keyboard submitters with no explanation.
    if (!newMemory.content.trim()) {
      toast.error("Content cannot be empty");
      return;
    }
    if (!validateContent(newMemory.content)) return;
    // C41: drop repeated submits (Ctrl+Enter spam / double click) while the
    // create request is in flight instead of queueing duplicate memories.
    if (submittingRef.current) return;
    submittingRef.current = true;

    setIsSubmitting(true);
    try {
      await addMemory(
        {
          content: newMemory.content,
          category: newMemory.category || undefined,
          tags: newMemory.tags ? newMemory.tags.split(",").map((t) => t.trim()).filter(Boolean) : [],
          source: newMemory.source || undefined,
        },
        activeVaultId ?? undefined
      );
      toast.success("Memory added successfully");
      // Reset form and close dialog only on success
      setNewMemory({ content: "", category: "", tags: "", source: "" });
      setContentError(null);
      setIsAddDialogOpen(false);
      await refreshMemories();
    } catch (err) {
      console.error("Failed to add memory:", err);
      toast.error(err instanceof Error ? err.message : "Failed to add memory");
    } finally {
      submittingRef.current = false;
      setIsSubmitting(false);
    }
  }, [newMemory, activeVaultId, refreshMemories, validateContent]);

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === "Enter" && e.ctrlKey) {
      e.preventDefault();
      handleAddMemory();
    }
  }, [handleAddMemory]);

  const handleDeleteMemory = useCallback(async (id: string) => {
    setIsDeleting(id);
    try {
      await deleteMemory(id);
      toast.success("Memory deleted successfully");
      await refreshMemories();
    } catch (err) {
      console.error("Failed to delete memory:", err);
      toast.error(err instanceof Error ? err.message : "Failed to delete memory");
    } finally {
      setIsDeleting(null);
    }
  }, [refreshMemories]);

  return {
    isAddDialogOpen,
    setIsAddDialogOpen,
    newMemory,
    setNewMemory,
    isSubmitting,
    isDeleting,
    contentError,
    handleContentChange,
    handleAddMemory,
    handleKeyDown,
    handleDeleteMemory,
  };
}

// Re-export MemoryResult type for convenience
export type { MemoryResult };

// Metadata helper functions (pure functions, not hooks)
/** Extract category string from memory metadata, defaulting to 'Uncategorized'. */
export function getCategoryFromMetadata(metadata?: Record<string, unknown>): string {
  return (metadata?.category as string) || "Uncategorized";
}

/** Extract tags array from memory metadata, defaulting to empty array. */
export function getTagsFromMetadata(metadata?: Record<string, unknown>): string[] {
  const tags = metadata?.tags;
  if (Array.isArray(tags)) return tags;
  return [];
}

/** Extract source string from memory metadata, defaulting to empty string. */
export function getSourceFromMetadata(metadata?: Record<string, unknown>): string {
  return (metadata?.source as string) || "";
}
