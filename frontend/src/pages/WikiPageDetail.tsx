import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import { ArrowLeft, ChevronDown, ChevronRight, Edit, FileText, Link2, Trash2, History } from "lucide-react";
import type {
  WikiPage,
  WikiClaim,
  WikiLintFinding,
  WikiPageVersion,
  WikiPageFile,
  WikiPageLink,
} from "@/lib/api";
import { getWikiPage, getWikiPageVersions, getWikiPageFiles, getWikiPageBacklinks } from "@/lib/api";

interface WikiPageDetailProps {
  page: WikiPage;
  onBack: () => void;
  onEdit: () => void;
  onDelete: () => void;
}

const SEVERITY_COLORS: Record<string, string> = {
  low: "bg-muted text-muted-foreground",
  medium: "bg-warning/10 text-warning",
  high: "bg-warning/10 text-warning",
  critical: "bg-destructive/10 text-destructive",
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
          {claim.sources.map((src) => {
            // AC44 (#515): origin chips LINK to the surface the claim came
            // from so provenance is one click away. document → the document
            // detail route; memory → the memories page. PRR-006 (#531):
            // client-side <Link> navigation (not origin <a href>) so the
            // chips stay inside the SPA router.
            if (src.source_kind === "document" && src.file_id != null) {
              return (
                <Link
                  key={src.id}
                  to={`/documents/${src.file_id}`}
                  title={`Open source document ${src.file_id}`}
                  className="inline-flex items-center rounded-full border px-2 py-0.5 text-xs text-muted-foreground hover:text-foreground hover:underline"
                >
                  document{src.source_label ? ` · ${src.source_label}` : ` · file ${src.file_id}`}
                </Link>
              );
            }
            if (src.source_kind === "memory" && src.memory_id != null) {
              return (
                <Link
                  key={src.id}
                  to="/memory"
                  title={`Open memories (source memory ${src.memory_id})`}
                  className="inline-flex items-center rounded-full border px-2 py-0.5 text-xs text-muted-foreground hover:text-foreground hover:underline"
                >
                  memory{src.source_label ? ` · ${src.source_label}` : ` #${src.memory_id}`}
                </Link>
              );
            }
            return (
              <Badge key={src.id} variant="outline" className="text-xs">
                {src.source_kind}
                {src.memory_id != null && ` #${src.memory_id}`}
                {src.file_id != null && ` file:${src.file_id}`}
              </Badge>
            );
          })}
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

function VersionHistorySection({ pageId, vaultId }: { pageId: number; vaultId: number }) {
  const [open, setOpen] = useState(false);
  const [versions, setVersions] = useState<WikiPageVersion[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    getWikiPageVersions(pageId, vaultId)
      .then((data) => setVersions(Array.isArray(data) ? data : data.versions ?? []))
      .catch(() => setVersions([]))
      .finally(() => setLoading(false));
  }, [open, pageId, vaultId]);

  return (
    <Card>
      <CardHeader
        className="pb-2 pt-3 px-4 cursor-pointer select-none"
        onClick={() => setOpen((v) => !v)}
      >
        <CardTitle className="text-sm flex items-center gap-1">
          {open ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
          <History className="w-4 h-4" />
          Version History
        </CardTitle>
      </CardHeader>
      {open && (
        <CardContent className="px-4 pb-3">
          {loading && <p className="text-xs text-muted-foreground">Loading...</p>}
          {!loading && versions.length === 0 && (
            <p className="text-xs text-muted-foreground">No version history available.</p>
          )}
          {!loading &&
            versions.map((v) => (
              <div key={v.id} className="border-b border-border pb-2 mb-2 last:border-0 last:mb-0 last:pb-0">
                <div className="flex items-center gap-2 text-xs">
                  {/* AC40 (#515): the backend version row identifies itself by
                      its id (no separate version number column). */}
                  <Badge variant="outline" className="text-[10px]">v{v.id}</Badge>
                  <span className="text-muted-foreground">{new Date(v.created_at).toLocaleString()}</span>
                  {v.edited_by != null && (
                    <span className="text-muted-foreground">by user {v.edited_by}</span>
                  )}
                </div>
              </div>
            ))}
        </CardContent>
      )}
    </Card>
  );
}

function AttachmentsSection({ pageId, vaultId }: { pageId: number; vaultId: number }) {
  const [open, setOpen] = useState(false);
  const [files, setFiles] = useState<WikiPageFile[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    getWikiPageFiles(pageId, vaultId)
      .then((data) => setFiles(Array.isArray(data) ? data : data.files ?? []))
      .catch(() => setFiles([]))
      .finally(() => setLoading(false));
  }, [open, pageId, vaultId]);

  return (
    <Card>
      <CardHeader
        className="pb-2 pt-3 px-4 cursor-pointer select-none"
        onClick={() => setOpen((v) => !v)}
      >
        <CardTitle className="text-sm flex items-center gap-1">
          {open ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
          <FileText className="w-4 h-4" />
          Attachments
        </CardTitle>
      </CardHeader>
      {open && (
        <CardContent className="px-4 pb-3">
          {loading && <p className="text-xs text-muted-foreground">Loading...</p>}
          {!loading && files.length === 0 && (
            <p className="text-xs text-muted-foreground">No attachments.</p>
          )}
          {!loading &&
            files.map((f) => (
              <div key={f.id} className="flex items-center gap-2 text-xs border-b border-border pb-2 mb-2 last:border-0 last:mb-0 last:pb-0">
                <FileText className="w-3 h-3 text-muted-foreground" />
                {/* AC40 (#515): filename comes joined from the backend; fall
                    back to an identifiable file-id label when the join missed
                    (deleted file / legacy payload). */}
                <span className="truncate">{f.filename ?? `File #${f.file_id}`}</span>
                <span className="text-muted-foreground ml-auto">{new Date(f.created_at).toLocaleDateString()}</span>
              </div>
            ))}
        </CardContent>
      )}
    </Card>
  );
}

function BacklinksSection({ pageId, vaultId }: { pageId: number; vaultId: number }) {
  const [open, setOpen] = useState(false);
  const [backlinks, setBacklinks] = useState<WikiPageLink[]>([]);
  const [resolvedSources, setResolvedSources] = useState<Record<number, { title: string; slug: string } | null>>({});
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    // PRR-013 (#531): pageId/vaultId changed — drop the legacy-source
    // resolution cache so stale titles/slugs from the previous page can't
    // bleed into this page's backlinks while the new fetch is in flight.
    setResolvedSources({});
    let cancelled = false;
    getWikiPageBacklinks(pageId, vaultId)
      .then((data) => {
        const rows = Array.isArray(data) ? data : data.backlinks ?? [];
        if (cancelled) return;
        setBacklinks(rows);
        // AC40 (#515): rows normally carry source_title/source_slug from the
        // backend's JOIN onto wiki_pages; resolve legacy rows (null fields)
        // via a page fetch so every backlink still shows its source page.
        const unresolved = rows
          .filter((bl) => bl.source_title == null && bl.source_page_id != null)
          .map((bl) => bl.source_page_id);
        if (unresolved.length === 0) return;
        Promise.all(
          unresolved.map(async (sourcePageId) => {
            try {
              const page = await getWikiPage(sourcePageId);
              return [sourcePageId, { title: page.title, slug: page.slug }] as const;
            } catch {
              return [sourcePageId, null] as const;
            }
          }),
        ).then((entries) => {
          if (cancelled) return;
          setResolvedSources((prev) => ({ ...prev, ...Object.fromEntries(entries) }));
        });
      })
      .catch(() => {
        if (!cancelled) setBacklinks([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, pageId, vaultId]);

  return (
    <Card>
      <CardHeader
        className="pb-2 pt-3 px-4 cursor-pointer select-none"
        onClick={() => setOpen((v) => !v)}
      >
        <CardTitle className="text-sm flex items-center gap-1">
          {open ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
          <Link2 className="w-4 h-4" />
          Backlinks
        </CardTitle>
      </CardHeader>
      {open && (
        <CardContent className="px-4 pb-3">
          {loading && <p className="text-xs text-muted-foreground">Loading...</p>}
          {!loading && backlinks.length === 0 && (
            <p className="text-xs text-muted-foreground">No pages link to this page.</p>
          )}
          {!loading &&
            backlinks.map((bl) => {
              const title =
                bl.source_title ?? resolvedSources[bl.source_page_id]?.title ?? `Page ${bl.source_page_id}`;
              const slug =
                bl.source_slug ?? resolvedSources[bl.source_page_id]?.slug ?? "";
              return (
                <div key={bl.id} className="flex items-center gap-2 text-xs border-b border-border pb-2 mb-2 last:border-0 last:mb-0 last:pb-0">
                  <Link2 className="w-3 h-3 text-muted-foreground" />
                  <span className="truncate font-medium">{title}</span>
                  {slug && <span className="text-muted-foreground ml-auto">{slug}</span>}
                </div>
              );
            })}
        </CardContent>
      )}
    </Card>
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
              <h1 className="text-lg font-semibold">{page.title}</h1>
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

        {/* Claims — always rendered (AC29, issue #515): a page with zero
            claims gets an explicit empty state instead of silently hiding the
            section, so "compiled but claimless" is distinguishable. */}
        <Card>
          <CardHeader className="pb-2 pt-3 px-4">
            <CardTitle className="text-sm">
              Claims{page.claims?.length ? ` (${page.claims.length})` : ""}
            </CardTitle>
          </CardHeader>
          <CardContent className="px-4 pb-3">
            {page.claims && page.claims.length > 0 ? (
              page.claims.map((claim) => (
                <ClaimRow key={claim.id} claim={claim} />
              ))
            ) : (
              <p className="text-xs text-muted-foreground">No claims extracted yet.</p>
            )}
          </CardContent>
        </Card>

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

        {/* Version History */}
        <VersionHistorySection pageId={page.id} vaultId={page.vault_id} />

        {/* Attachments */}
        <AttachmentsSection pageId={page.id} vaultId={page.vault_id} />

        {/* Backlinks */}
        <BacklinksSection pageId={page.id} vaultId={page.vault_id} />
      </div>
    </ScrollArea>
  );
}
