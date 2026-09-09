import { useCallback, useState } from "react";

/**
 * Generic bulk-selection state for a list of string-id items.
 *
 * Ids MUST be canonical strings at every call site (`String(document.id)`) so
 * selections made on different surfaces (desktop rows, mobile cards) agree —
 * a numeric 5 and a string "5" would otherwise live in the set as two
 * distinct entries.
 *
 * Mutations are gated by `enabled`: when false, selection changes are ignored
 * (callers clear selection separately when permissions drop).
 */
export function useBulkSelection(enabled: boolean) {
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  const clear = useCallback(() => setSelectedIds(new Set()), []);

  const selectMany = useCallback(
    (ids: string[], checked: boolean) => {
      if (!enabled) return;
      setSelectedIds((prev) => {
        const next = new Set(prev);
        for (const id of ids) {
          if (checked) next.add(id);
          else next.delete(id);
        }
        return next;
      });
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

  return { selectedIds, setSelectedIds, clear, selectMany, selectOne };
}
