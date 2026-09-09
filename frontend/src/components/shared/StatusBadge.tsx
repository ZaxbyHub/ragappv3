import { Badge } from "@/components/ui/badge";
import { CheckCircle, Loader2, Clock, AlertCircle, AlertTriangle } from "lucide-react";
import type { LucideIcon } from "lucide-react";

export const FILE_STATUS_LABELS: Record<string, string> = {
  pending: "Pending",
  processing: "Processing",
  indexed: "Indexed",
  /** Pipeline finished with embedding failures (LIVE-03) — completed-with-failures, never pending. */
  partial: "Partial",
  error: "Error",
};

export const FILE_STATUS_COLORS: Record<string, { variant: "default" | "secondary" | "outline" | "destructive"; className: string; icon: LucideIcon }> = {
  pending: { variant: "outline", className: "", icon: Clock },
  processing: { variant: "secondary", className: "", icon: Loader2 },
  indexed: { variant: "default", className: "bg-success", icon: CheckCircle },
  partial: { variant: "default", className: "bg-warning", icon: AlertTriangle },
  error: { variant: "destructive", className: "", icon: AlertCircle },
};

interface StatusBadgeProps {
  status?: string;
  /** Chunks dropped due to embedding failures; >0 on an indexed doc renders "Partially indexed". */
  chunksFailed?: number;
}

/** Renders a color-coded badge for document processing status. */
export function StatusBadge({ status, chunksFailed }: StatusBadgeProps) {
  if (!status) {
    return <Badge variant="outline">Unknown</Badge>;
  }
  const config = FILE_STATUS_COLORS[status];
  if (!config) {
    return <Badge variant="outline">Unknown</Badge>;
  }
  if (status === "indexed" && (chunksFailed ?? 0) > 0) {
    return (
      <Badge
        variant="default"
        className="bg-warning"
        title={`${chunksFailed} chunks failed to embed — retrieval may miss content`}
      >
        <AlertTriangle className="w-3 h-3 mr-1" />
        Partially indexed
      </Badge>
    );
  }
  const Icon = config.icon;
  const label = FILE_STATUS_LABELS[status] ?? status;
  return (
    <Badge variant={config.variant} className={config.className}>
      <Icon className={`w-3 h-3 mr-1 ${status === "processing" ? "animate-spin" : ""}`} />
      {label}
    </Badge>
  );
}
