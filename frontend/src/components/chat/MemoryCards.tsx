import { useState } from "react";
import { Brain, Tag, ChevronDown, ChevronUp } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { UsedMemory } from "@/lib/api";

interface MemoryCardProps {
  memory: UsedMemory;
}

function MemoryCard({ memory }: MemoryCardProps) {
  const [expanded, setExpanded] = useState(false);
  const isLong = memory.content.length > 160;
  const display = expanded || !isLong ? memory.content : memory.content.slice(0, 160) + "…";
  const tagsList = parseTagList(memory.tags);

  return (
    <div
      className={cn(
        "rounded-sm border border-amber-500/30 bg-amber-500/5 p-3 text-sm",
        "hover:border-amber-500/50 hover:bg-amber-500/10 transition-colors duration-150",
      )}
      role="article"
      aria-label={`Memory ${memory.memory_label}`}
      data-memory-label={memory.memory_label}
    >
      <div className="flex items-start gap-2">
        <span
          className="flex-shrink-0 inline-flex items-center justify-center min-w-[1.5rem] h-5 px-1.5 rounded-full bg-amber-500/20 text-amber-700 dark:text-amber-300 text-[10px] font-bold"
          aria-label={`Memory label ${memory.memory_label}`}
        >
          {memory.memory_label}
        </span>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-1.5 text-xs text-amber-700/80 dark:text-amber-300/80 mb-1">
            <Brain className="h-3 w-3" aria-hidden />
            <span className="font-medium">Memory</span>
            {memory.category && (
              <>
                <span aria-hidden>·</span>
                <span>{memory.category}</span>
              </>
            )}
            {memory.source && (
              <>
                <span aria-hidden>·</span>
                <span className="italic">{memory.source}</span>
              </>
            )}
          </div>
          <p className="text-xs leading-relaxed text-foreground/90">{display}</p>
          {isLong && (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="mt-1 text-[10px] text-amber-700 dark:text-amber-300 hover:underline flex items-center gap-0.5"
              aria-expanded={expanded}
            >
              {expanded ? (
                <>
                  <ChevronUp className="h-3 w-3" /> Less
                </>
              ) : (
                <>
                  <ChevronDown className="h-3 w-3" /> More
                </>
              )}
            </button>
          )}
          {tagsList.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1">
              {tagsList.slice(0, 6).map((t) => (
                <Badge
                  key={t}
                  variant="outline"
                  className="text-[10px] px-1.5 py-0 border-amber-500/30 text-amber-700 dark:text-amber-300"
                >
                  <Tag className="h-2.5 w-2.5 mr-0.5" aria-hidden /> {t}
                </Badge>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function parseTagList(tags: string | null | undefined): string[] {
  if (!tags) return [];
  try {
    const parsed = JSON.parse(tags);
    if (Array.isArray(parsed)) return parsed.filter((x): x is string => typeof x === "string");
  } catch {
    // not JSON — fall through
  }
  return tags
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

interface MemoryCardsProps {
  memories: UsedMemory[];
}

export function MemoryCards({ memories }: MemoryCardsProps) {
  if (!memories || memories.length === 0) return null;
  return (
    <div className="mt-3 space-y-2" data-testid="memory-cards">
      <div className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
        <Brain className="h-3 w-3" aria-hidden />
        Memories used:
      </div>
      <div className="space-y-1.5">
        {memories.map((m) => (
          <MemoryCard key={m.id || m.memory_label} memory={m} />
        ))}
      </div>
    </div>
  );
}

export { MemoryCard };
