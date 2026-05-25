import { useRef } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { DocumentCard } from "@/components/shared/DocumentCard";
import type { Document } from "@/lib/api";

interface DocumentCardsListProps {
  documents: Document[];
  canMutateDocuments: boolean;
  selectedIds: Set<string>;
  onSelectOne: (docId: string, checked: boolean) => void;
  onDelete: (docId: string) => void;
  onDownload: (doc: Document) => void;
}

export function DocumentCardsList({
  documents,
  canMutateDocuments,
  selectedIds,
  onSelectOne,
  onDelete,
  onDownload,
}: DocumentCardsListProps) {
  const mobileScrollRef = useRef<HTMLDivElement>(null);

  const mobileVirtualizer = useVirtualizer({
    count: documents.length,
    getScrollElement: () => mobileScrollRef.current,
    estimateSize: () => 200,
    overscan: 3,
    measureElement: (el) => el?.getBoundingClientRect().height ?? 0,
  });

  return (
    <div ref={mobileScrollRef} className="sm:hidden" style={{ maxHeight: "70vh", overflowY: "auto" }}>
      <div style={{ height: mobileVirtualizer.getTotalSize(), position: "relative" }}>
        {mobileVirtualizer.getVirtualItems().map((virtualItem) => {
          const doc = documents[virtualItem.index];
          const docId = String(doc.id);
          return (
            <div
              key={docId}
              data-index={virtualItem.index}
              ref={mobileVirtualizer.measureElement}
              style={{
                position: "absolute",
                top: virtualItem.start,
                left: 0,
                width: "100%",
                paddingTop: "0.75rem",
              }}
            >
              <DocumentCard
                document={doc}
                onDelete={(id) => onDelete(String(id))}
                onDownload={() => onDownload(doc)}
                canDelete={canMutateDocuments}
                isSelected={selectedIds.has(doc.id)}
                onSelectionChange={canMutateDocuments ? onSelectOne : undefined}
              />
            </div>
          );
        })}
      </div>
    </div>
  );
}
