import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { BookOpen, ChevronDown, ChevronUp, ExternalLink } from "lucide-react";
import { cn } from "@/lib/utils";
import type { WikiReference } from "@/lib/api";

interface WikiCardProps {
  wikiRef: WikiReference;
}

function WikiCard({ wikiRef }: WikiCardProps) {
  const [expanded, setExpanded] = useState(false);
  const navigate = useNavigate();
  const body = wikiRef.claim_text ?? wikiRef.excerpt ?? "";
  const isLong = body.length > 160;
  const display = expanded || !isLong ? body : body.slice(0, 160) + "…";
  const statusLabel = wikiRef.claim_status ?? wikiRef.page_status ?? "";
  const conf = wikiRef.confidence != null ? `${Math.round(wikiRef.confidence * 100)}%` : null;

  const handleNavigate = () => {
    if (wikiRef.page_id != null) {
      navigate(`/wiki?page=${wikiRef.page_id}`);
    } else if (wikiRef.slug) {
      navigate(`/wiki?page=${encodeURIComponent(wikiRef.slug)}`);
    }
  };

  return (
    <div
      className={cn(
        "rounded-sm border border-indigo-500/30 bg-indigo-500/5 p-3 text-sm",
        "hover:border-indigo-500/50 hover:bg-indigo-500/10 transition-colors duration-150",
      )}
      role="article"
      aria-label={`Wiki ${wikiRef.wiki_label}: ${wikiRef.title}`}
      data-wiki-label={wikiRef.wiki_label}
    >
      <div className="flex items-start gap-2">
        <span
          className="flex-shrink-0 inline-flex items-center justify-center min-w-[1.5rem] h-5 px-1.5 rounded-full bg-indigo-500/20 text-indigo-700 dark:text-indigo-300 text-[10px] font-bold"
          aria-label={`Wiki label ${wikiRef.wiki_label}`}
        >
          {wikiRef.wiki_label}
        </span>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-1.5 flex-wrap text-xs text-indigo-700/80 dark:text-indigo-300/80 mb-1">
            <BookOpen className="h-3 w-3 flex-shrink-0" aria-hidden />
            <span className="font-semibold truncate">{wikiRef.title}</span>
            {wikiRef.page_type && (
              <>
                <span aria-hidden className="text-indigo-400">·</span>
                <span className="capitalize">{wikiRef.page_type}</span>
              </>
            )}
            {conf && (
              <>
                <span aria-hidden className="text-indigo-400">·</span>
                <span>{conf}</span>
              </>
            )}
            {statusLabel && (
              <>
                <span aria-hidden className="text-indigo-400">·</span>
                <span className="capitalize">{statusLabel}</span>
              </>
            )}
            {(wikiRef.page_id != null || wikiRef.slug) && (
              <button
                type="button"
                onClick={handleNavigate}
                className="ml-auto flex-shrink-0 inline-flex items-center gap-0.5 text-[10px] text-indigo-600 dark:text-indigo-400 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                aria-label={`Open wiki page ${wikiRef.title}`}
              >
                <ExternalLink className="h-2.5 w-2.5" aria-hidden />
              </button>
            )}
          </div>
          {body && (
            <>
              <p className="text-xs leading-relaxed text-foreground/90">{display}</p>
              {isLong && (
                <button
                  type="button"
                  onClick={() => setExpanded((v) => !v)}
                  className="mt-1 text-[10px] text-indigo-600 dark:text-indigo-400 hover:underline flex items-center gap-0.5"
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
            </>
          )}
          {wikiRef.provenance_summary && (
            <p className="mt-1 text-[10px] text-indigo-500/80 dark:text-indigo-400/60 italic truncate">
              {wikiRef.provenance_summary}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

interface WikiCardsProps {
  wikiRefs: WikiReference[];
}

export function WikiCards({ wikiRefs }: WikiCardsProps) {
  if (!wikiRefs || wikiRefs.length === 0) return null;
  return (
    <div className="mt-3 space-y-2" data-testid="wiki-cards">
      <div className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
        <BookOpen className="h-3 w-3" aria-hidden />
        Wiki knowledge:
      </div>
      <div className="space-y-1.5">
        {wikiRefs.map((w) => (
          <WikiCard key={w.wiki_label} wikiRef={w} />
        ))}
      </div>
    </div>
  );
}

export { WikiCard };
