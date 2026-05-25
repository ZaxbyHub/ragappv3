import { useCallback, useState } from "react";

/**
 * Generic bulk-selection state for a list of string-id items.
 *
 * Mutations are gated by `enabled`: when false, selection changes are ignored
 * (callers clear selection separately when permissions drop).
 */
export function useBulkSelection(enabled: boolean) {
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  const clear = useCallback(() => setSelectedIds(new Set()), []);

  const selectAll = useCallback(
    (ids: string[]) => {
      if (!enabled) return;
      setSelectedIds(new Set(ids));
    },
    [enabled]
  );

  const selectOne = useCallback(
    (id: string, checked: boolean) => {
      if (!enabled) return;
      setSelectedIds((prev) => {
        const next = new Set(prev);
        if (checked) next.add(id);
        else next.delete(id);
        return next;
      });
    },
    [enabled]
  );

  return { selectedIds, setSelectedIds, clear, selectAll, selectOne };
}
