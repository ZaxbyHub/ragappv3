import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  ARCHIVED_READ_ONLY_WARNING,
  DRAFT_COMPLETE_REVIEW_REQUIRED,
  DRAFT_NOT_FACT_CHECKED,
  EVIDENCE_INVALIDATED_WARNING,
  RETRIEVAL_PARTIAL_WARNING,
  SOURCE_ONLY_WARNING,
  VAULT_ACCESS_REVOKED_WARNING,
} from "@/components/draft-room/labels";
import type { DraftDetail, DraftFactStatus, DraftSummary, DraftVaultAccess } from "@/lib/api/draftRoom";
import { cn } from "@/lib/utils";
import { AlertOctagon, AlertTriangle, Archive } from "lucide-react";
import type { LucideIcon } from "lucide-react";

export interface DraftStatusBannerProps {
  draft: DraftSummary;
  detail?: DraftDetail;
  factStatus: DraftFactStatus | null;
  sourceOnly: boolean;
  retrievalPartial: boolean;
  evidenceInvalidated: boolean;
  vaultAccess: DraftVaultAccess;
  onRerunResearch?: () => void;
  onRerunNewsroom?: () => void;
}

type AlertVariant = "destructive" | "warning" | "default";

interface BannerSpec {
  key: string;
  variant: AlertVariant;
  icon: LucideIcon;
  message: string;
  className?: string;
  cta?: { label: string; onClick: () => void };
}

/**
 * Zero or more blocking/warning banners, in the fixed priority order required by SPEC 16.6:
 * vault revoked > evidence invalidated > retrieval partial > source-only > archived (read-only)
 * > fact-not-current > needs-review. All applicable banners render (not just the top one);
 * blocking states are persistent and offer no dismiss control.
 */
export function DraftStatusBanner({
  draft,
  factStatus,
  sourceOnly,
  retrievalPartial,
  evidenceInvalidated,
  vaultAccess,
  onRerunResearch,
  onRerunNewsroom,
}: DraftStatusBannerProps) {
  const banners: BannerSpec[] = [];

  if (vaultAccess === "revoked") {
    banners.push({
      key: "vault-revoked",
      variant: "destructive",
      icon: AlertOctagon,
      message: VAULT_ACCESS_REVOKED_WARNING,
    });
  }

  if (evidenceInvalidated) {
    banners.push({
      key: "evidence-invalidated",
      variant: "destructive",
      icon: AlertTriangle,
      message: EVIDENCE_INVALIDATED_WARNING,
      cta: onRerunNewsroom ? { label: "Rerun newsroom", onClick: onRerunNewsroom } : undefined,
    });
  }

  if (retrievalPartial) {
    banners.push({
      key: "retrieval-partial",
      variant: "destructive",
      icon: AlertTriangle,
      message: RETRIEVAL_PARTIAL_WARNING,
      cta: onRerunResearch ? { label: "Rerun research", onClick: onRerunResearch } : undefined,
    });
  }

  if (sourceOnly) {
    banners.push({
      key: "source-only",
      variant: "warning",
      icon: AlertTriangle,
      message: SOURCE_ONLY_WARNING,
    });
  }

  if (draft.status === "archived") {
    banners.push({
      key: "archived",
      variant: "default",
      icon: Archive,
      message: ARCHIVED_READ_ONLY_WARNING,
      className: "border-muted bg-muted/30 text-muted-foreground [&>svg]:text-muted-foreground",
    });
  }

  if (factStatus === "not_run" || factStatus === "running") {
    banners.push({
      key: "fact-not-current",
      variant: "warning",
      icon: AlertTriangle,
      message: DRAFT_NOT_FACT_CHECKED,
    });
  }

  if (draft.status === "needs_review") {
    banners.push({
      key: "needs-review",
      variant: "warning",
      icon: AlertTriangle,
      message: DRAFT_COMPLETE_REVIEW_REQUIRED,
    });
  }

  if (banners.length === 0) return null;

  return (
    <div className="flex flex-col gap-2">
      {banners.map(({ key, variant, icon: Icon, message, className, cta }) => (
        <Alert key={key} variant={variant} className={cn(className)}>
          <Icon className="h-4 w-4" aria-hidden="true" />
          <AlertDescription className="flex flex-wrap items-center justify-between gap-2">
            <span>{message}</span>
            {cta && (
              <button
                type="button"
                onClick={cta.onClick}
                className="shrink-0 rounded-sm border border-current px-2 py-1 text-xs font-medium underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
              >
                {cta.label}
              </button>
            )}
          </AlertDescription>
        </Alert>
      ))}
    </div>
  );
}
