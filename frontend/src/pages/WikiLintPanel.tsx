import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Play, Check, X } from "lucide-react";
import { resolveWikiLintFinding } from "@/lib/api";
import type { WikiLintFinding } from "@/lib/api";

const SEVERITY_COLORS: Record<string, string> = {
  low: "border-l-muted bg-muted/20",
  medium: "border-l-warning bg-warning/5",
  high: "border-l-warning bg-warning/5",
  critical: "border-l-destructive bg-destructive/5",
};

const SEVERITY_TEXT: Record<string, string> = {
  low: "text-muted-foreground",
  medium: "text-warning",
  high: "text-warning",
  critical: "text-destructive",
};

interface WikiLintPanelProps {
  findings: WikiLintFinding[];
  loading: boolean;
  onRunLint: () => void;
  vaultId: number | null;
}

export function WikiLintPanel({ findings, loading, onRunLint, vaultId }: WikiLintPanelProps) {
  const [resolving, setResolving] = useState<number | null>(null);

  const handleResolve = async (findingId: number, status: "resolved" | "dismissed") => {
    if (!vaultId) return;
    setResolving(findingId);
    try {
      await resolveWikiLintFinding(findingId, vaultId, status);
      onRunLint();
    } finally {
      setResolving(null);
    }
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold">Lint Findings ({findings.length})</h3>
        <Button variant="outline" size="sm" onClick={onRunLint} disabled={loading}>
          <Play className="w-3.5 h-3.5 mr-1" />
          {loading ? "Running…" : "Run Lint"}
        </Button>
      </div>

      {findings.length === 0 && !loading && (
        <p className="text-xs text-muted-foreground text-center py-4">No findings. Run lint to check.</p>
      )}

      {findings.map((finding) => (
        <Card
          key={finding.id}
          className={`border-l-4 ${SEVERITY_COLORS[finding.severity] ?? ""}`}
        >
          <CardContent className="py-2 px-3">
            <div className={`text-sm font-medium ${SEVERITY_TEXT[finding.severity] ?? ""}`}>
              {finding.title}
            </div>
            {finding.details && (
              <div className="text-xs text-muted-foreground mt-0.5">{finding.details}</div>
            )}
            <div className="flex items-center justify-between mt-1">
              <div className="text-xs text-muted-foreground uppercase tracking-wide">
                {finding.severity} · {finding.finding_type.replace(/_/g, " ")}
              </div>
              {vaultId && (
                <div className="flex gap-1">
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-6 px-1.5 text-xs"
                    disabled={resolving === finding.id}
                    onClick={() => handleResolve(finding.id, "resolved")}
                    title="Resolve"
                  >
                    <Check className="w-3 h-3" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-6 px-1.5 text-xs"
                    disabled={resolving === finding.id}
                    onClick={() => handleResolve(finding.id, "dismissed")}
                    title="Dismiss"
                  >
                    <X className="w-3 h-3" />
                  </Button>
                </div>
              )}
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
