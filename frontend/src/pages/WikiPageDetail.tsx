import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import { ArrowLeft, Edit, Trash2 } from "lucide-react";
import type { WikiPage, WikiClaim, WikiLintFinding } from "@/lib/api";

interface WikiPageDetailProps {
  page: WikiPage;
  onBack: () => void;
  onEdit: () => void;
  onDelete: () => void;
}

const SEVERITY_COLORS: Record<string, string> = {
  low: "bg-muted text-muted-foreground",
  medium: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200",
  high: "bg-orange-100 text-orange-800 dark:bg-orange-900 dark:text-orange-200",
  critical: "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200",
};

function ClaimRow({ claim }: { claim: WikiClaim }) {
  // PR C: surface curator provenance + needs_review state distinctly.
  // ``created_by_kind`` is "deterministic" | "llm_curator" | null.
  // Null = legacy / unknown row; treat as deterministic for display.
  const isCurator = claim.created_by_kind === "llm_curator";
  const isNeedsReview = claim.status === "needs_review";
  const rowBg = isNeedsReview
    ? "bg-blue-50/60 dark:bg-blue-950/20 rounded-sm -sm px-2 -mx-2"
    : "";
  return (
    <div
      className={`border-b border-border pb-2 mb-2 last:border-0 last:mb-0 last:pb-0 ${rowBg}`}
    >
      <p className="text-sm">{claim.claim_text}</p>
      <div className="flex gap-2 mt-1 flex-wrap items-center">
        {claim.subject && (
          <span className="text-xs text-muted-foreground">
            Subject: {claim.subject}
          </span>
        )}
        {claim.predicate && (
          <span className="text-xs text-muted-foreground">· {claim.predicate}</span>
        )}
        {claim.object && (
          <span className="text-xs text-muted-foreground">→ {claim.object}</span>
        )}
        {/* Provenance chip — distinguishes curator output from
            deterministic extraction so reviewers know which claims to
            scrutinise harder. */}
        <Badge
          variant={isCurator ? "secondary" : "outline"}
          className="text-[10px] uppercase"
          title={
            isCurator
              ? "Authored by the optional LLM curator. Active only when source quote verifies."
              : "Authored by deterministic regex/parser extraction."
          }
        >
          {isCurator ? "LLM curator" : "deterministic"}
        </Badge>
        {isNeedsReview && (
          <Badge
            variant="outline"
            className="text-[10px] uppercase border-blue-300 text-blue-700 dark:text-blue-300"
            title="Needs operator review before becoming an active claim."
          >
            Needs review
          </Badge>
        )}
      </div>
      {claim.sources && claim.sources.length > 0 && (
        <div className="flex gap-1 flex-wrap mt-1">
          {claim.sources.map((src) => (
            <Badge key={src.id} variant="outline" className="text-xs">
              {src.source_kind}
              {src.memory_id != null && ` #${src.memory_id}`}
              {src.file_id != null && ` file:${src.file_id}`}
            </Badge>
          ))}
        </div>
      )}
    </div>
  );
}

function LintFindingRow({ finding }: { finding: WikiLintFinding }) {
  return (
    <div className={`rounded-sm px-3 py-2 text-sm ${SEVERITY_COLORS[finding.severity] ?? ""}`}>
      <div className="font-medium">{finding.title}</div>
      {finding.details && <div className="text-xs mt-0.5 opacity-75">{finding.details}</div>}
    </div>
  );
}

export function WikiPageDetail({ page, onBack, onEdit, onDelete }: WikiPageDetailProps) {
  return (
    <ScrollArea className="h-full">
      <div className="flex flex-col gap-4 p-1">
        {/* Header */}
        <div className="flex items-start justify-between gap-2">
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="icon" onClick={onBack} aria-label="Back">
              <ArrowLeft className="w-4 h-4" />
            </Button>
            <div>
              <h2 className="text-lg font-semibold">{page.title}</h2>
              <p className="text-xs text-muted-foreground">{page.slug}</p>
            </div>
          </div>
          <div className="flex gap-1">
            <Button variant="outline" size="sm" onClick={onEdit}>
              <Edit className="w-4 h-4 mr-1" />
              Edit
            </Button>
            <Button variant="destructive" size="sm" onClick={onDelete}>
              <Trash2 className="w-4 h-4" />
            </Button>
          </div>
        </div>

        {/* Meta */}
        <div className="flex gap-2 flex-wrap">
          <Badge variant="default" className="capitalize">{page.page_type}</Badge>
          <Badge variant="default" className="capitalize">{page.status}</Badge>
          {page.confidence > 0 && (
            <Badge variant="default" className="capitalize">confidence: {(page.confidence * 100).toFixed(0)}%</Badge>
          )}
        </div>

        {/* Summary */}
        {page.summary && (
          <p className="text-sm text-muted-foreground">{page.summary}</p>
        )}

        {/* Markdown */}
        {page.markdown && (
          <Card>
            <CardHeader className="pb-2 pt-3 px-4">
              <CardTitle className="text-sm">Content</CardTitle>
            </CardHeader>
            <CardContent className="px-4 pb-3">
              <pre className="text-xs whitespace-pre-wrap font-sans">{page.markdown}</pre>
            </CardContent>
          </Card>
        )}

        {/* Claims */}
        {page.claims && page.claims.length > 0 && (
          <Card>
            <CardHeader className="pb-2 pt-3 px-4">
              <CardTitle className="text-sm">Claims ({page.claims.length})</CardTitle>
            </CardHeader>
            <CardContent className="px-4 pb-3">
              {page.claims.map((claim) => (
                <ClaimRow key={claim.id} claim={claim} />
              ))}
            </CardContent>
          </Card>
        )}

        {/* Related Entities */}
        {page.entities && page.entities.length > 0 && (
          <Card>
            <CardHeader className="pb-2 pt-3 px-4">
              <CardTitle className="text-sm">Entities ({page.entities.length})</CardTitle>
            </CardHeader>
            <CardContent className="px-4 pb-3 flex flex-wrap gap-2">
              {page.entities.map((entity) => (
                <Badge key={entity.id} variant="secondary">
                  {entity.canonical_name}
                  {entity.entity_type !== "unknown" && (
                    <span className="ml-1 opacity-60 text-xs">({entity.entity_type})</span>
                  )}
                </Badge>
              ))}
            </CardContent>
          </Card>
        )}

        {/* Lint Findings */}
        {page.lint_findings && page.lint_findings.length > 0 && (
          <Card>
            <CardHeader className="pb-2 pt-3 px-4">
              <CardTitle className="text-sm">Lint Findings ({page.lint_findings.length})</CardTitle>
            </CardHeader>
            <CardContent className="px-4 pb-3 flex flex-col gap-2">
              {page.lint_findings.map((f) => (
                <LintFindingRow key={f.id} finding={f} />
              ))}
            </CardContent>
          </Card>
        )}
      </div>
    </ScrollArea>
  );
}
