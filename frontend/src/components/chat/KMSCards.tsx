import { useState } from "react";
import { Library, ChevronDown, ChevronUp, ExternalLink } from "lucide-react";
import { cn } from "@/lib/utils";
import type { KMSReference } from "@/lib/api";

interface KMSCardProps {
  kmsRef: KMSReference;
}

function KMSCard({ kmsRef }: KMSCardProps) {
  const [expanded, setExpanded] = useState(false);
  const body = kmsRef.excerpt ?? kmsRef.summary ?? "";
  const isLong = body.length > 160;
  const display = expanded || !isLong ? body : body.slice(0, 160) + "…";
  const statusLabel = kmsRef.status ?? "";

  const handleNavigate = () => {
    window.open(`/kms/${kmsRef.entry_id}`, "_blank", "noopener");
  };

  return (
    <div
      className={cn(
        "rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-3 text-sm",
        "hover:border-emerald-500/50 hover:bg-emerald-500/10 transition-colors duration-150",
      )}
      role="article"
      aria-label={`Knowledge ${kmsRef.kms_label}: ${kmsRef.title}`}
      data-kms-label={kmsRef.kms_label}
    >
      <div className="flex items-start gap-2">
        <span
          className="flex-shrink-0 inline-flex items-center justify-center min-w-[1.5rem] h-5 px-1.5 rounded-full bg-emerald-500/20 text-emerald-700 dark:text-emerald-300 text-[10px] font-bold"
          aria-label={`Knowledge label ${kmsRef.kms_label}`}
        >
          {kmsRef.kms_label}
        </span>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-1.5 flex-wrap text-xs text-emerald-700/80 dark:text-emerald-300/80 mb-1">
            <Library className="h-3 w-3 flex-shrink-0" aria-hidden />
            <span className="font-semibold truncate">{kmsRef.title}</span>
            {kmsRef.source_type && (
              <>
                <span aria-hidden className="text-emerald-400">·</span>
                <span className="capitalize">{kmsRef.source_type}</span>
              </>
            )}
            {statusLabel && (
              <>
                <span aria-hidden className="text-emerald-400">·</span>
                <span className="capitalize">{statusLabel}</span>
              </>
            )}
            <button
              type="button"
              onClick={handleNavigate}
              className="ml-auto flex-shrink-0 inline-flex items-center gap-0.5 text-[10px] text-emerald-600 dark:text-emerald-400 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              aria-label={`Open knowledge entry ${kmsRef.title}`}
            >
              <ExternalLink className="h-2.5 w-2.5" aria-hidden />
            </button>
          </div>
          {body && (
            <>
              <p className="text-xs leading-relaxed text-foreground/90">{display}</p>
              {isLong && (
                <button
                  type="button"
                  onClick={() => setExpanded((v) => !v)}
                  className="mt-1 text-[10px] text-emerald-600 dark:text-emerald-400 hover:underline flex items-center gap-0.5"
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
          {kmsRef.tags && kmsRef.tags.length > 0 && (
            <div className="mt-1 flex flex-wrap gap-1">
              {kmsRef.tags.map((tag) => (
                <span
                  key={tag}
                  className="inline-flex items-center px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 text-[10px]"
                >
                  {tag}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

interface KMSCardsProps {
  kmsRefs: KMSReference[];
}

export function KMSCards({ kmsRefs }: KMSCardsProps) {
  if (!kmsRefs || kmsRefs.length === 0) return null;
  return (
    <div className="mt-3 space-y-2" data-testid="kms-cards">
      <div className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
        <Library className="h-3 w-3" aria-hidden />
        Knowledge base:
      </div>
      <div className="space-y-1.5">
        {kmsRefs.map((k) => (
          <KMSCard key={k.kms_label} kmsRef={k} />
        ))}
      </div>
    </div>
  );
}

export { KMSCard };
