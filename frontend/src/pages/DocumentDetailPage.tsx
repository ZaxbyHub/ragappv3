import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { toast } from "sonner";
import { ArrowLeft, Download, Loader2, MessageSquare, Save } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import { FileIcon } from "@/lib/fileIcon";
import { StatusBadge } from "@/components/shared/StatusBadge";
import { ParseQualityPanel } from "@/components/documents/ParseQualityPanel";
import { documentField } from "@/components/documents/documentProgress";
import { formatFileSize, formatDate } from "@/lib/formatters";
import { useChatModeStore } from "@/stores/useChatModeStore";
import {
  getDocument,
  getDocumentRawBlob,
  setDocumentTags,
  downloadDocument,
  listTags,
  type Document,
  type Tag,
} from "@/lib/api";

const TEXT_EXTENSIONS = ["txt", "md", "markdown", "csv", "json", "log", "yaml", "yml"];

function extensionOf(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot + 1).toLowerCase() : "";
}

export default function DocumentDetailPage() {
  const { documentId } = useParams<{ documentId: string }>();
  const navigate = useNavigate();
  const id = Number(documentId);

  const [doc, setDoc] = useState<Document | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [allTags, setAllTags] = useState<Tag[]>([]);
  const [selectedTagIds, setSelectedTagIds] = useState<Set<number>>(new Set());
  const [savingTags, setSavingTags] = useState(false);

  const [previewText, setPreviewText] = useState<string | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);

  const setScopeDocumentIds = useChatModeStore((state) => state.setScopeDocumentIds);

  // Generation token (issue #514 / UI-011): bumped on every load so a late
  // response for a previously-navigated document id can never commit state
  // for the document currently shown. Checked after EVERY await.
  const loadGenRef = useRef(0);
  // Latest in-flight preview request — cancelled on unmount so no preview
  // request outlives the page.
  const previewInFlightRef = useRef<{ controller: AbortController; gen: number } | null>(null);
  useEffect(() => () => previewInFlightRef.current?.controller.abort(), []);

  // The preview follows the most recently RESOLVED document (any generation),
  // so the blob fetch always runs against the payload actually in hand; its
  // COMMITS are generation-guarded below, which is what keeps a stale blob
  // from overwriting the current document's preview (UI-011).
  const [previewSource, setPreviewSource] = useState<{ doc: Document; gen: number } | null>(null);

  const load = useCallback(async () => {
    if (!Number.isFinite(id)) {
      setError("Invalid document id");
      setLoading(false);
      return;
    }
    const gen = ++loadGenRef.current;
    setLoading(true);
    setError(null);
    try {
      const d = await getDocument(id);
      // UI-011: a stale getDocument response (for a document the user
      // navigated away from) must not overwrite the current document, its
      // tags, or the vault tag list.
      if (loadGenRef.current === gen) {
        setDoc(d);
        setSelectedTagIds(new Set((d.tags ?? []).map((t) => t.id)));
      }
      setPreviewSource({ doc: d, gen });
      if (d.vault_id != null) {
        try {
          const tags = await listTags(d.vault_id);
          if (loadGenRef.current === gen) {
            setAllTags(tags);
          }
        } catch {
          // Tag editing is best-effort; ignore load failures.
        }
      }
    } catch (err) {
      if (loadGenRef.current === gen) {
        setError(err instanceof Error ? err.message : "Failed to load document");
      }
    } finally {
      if (loadGenRef.current === gen) {
        setLoading(false);
      }
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  // Inline preview for text-like and PDF documents.
  useEffect(() => {
    if (!previewSource) return;
    const { doc: previewDoc, gen } = previewSource;
    const ext = extensionOf(previewDoc.filename);
    const isText = TEXT_EXTENSIONS.includes(ext);
    const isPdf = ext === "pdf";
    if (!isText && !isPdf) return;

    // Guard against loading huge text files into a JS string (F-004). PDFs are
    // streamed into an object URL by the browser, so only text needs the cap.
    const MAX_TEXT_PREVIEW_BYTES = 5 * 1024 * 1024;
    if (isText && typeof previewDoc.size === "number" && previewDoc.size > MAX_TEXT_PREVIEW_BYTES) {
      if (loadGenRef.current === gen) {
        setPreviewText(
          `File too large to preview (${formatFileSize(previewDoc.size)}). Download to view.`
        );
      }
      return;
    }

    let revoked: string | null = null;
    const controller = new AbortController();
    // Supersede the previous preview request — but only when THIS run is the
    // newer load. When a stale document's late resolution started this run,
    // the current document's in-flight preview must survive (UI-011); its own
    // commit is generation-guarded below anyway.
    const previous = previewInFlightRef.current;
    if (previous && gen > previous.gen) previous.controller.abort();
    previewInFlightRef.current = { controller, gen };
    (async () => {
      try {
        const blob = await getDocumentRawBlob(previewDoc.id, controller.signal);
        // UI-011: check after EVERY await — a blob belonging to a superseded
        // load generation must never commit preview state.
        if (loadGenRef.current !== gen) return;
        if (isText) {
          const text = await blob.text();
          if (loadGenRef.current !== gen) return;
          setPreviewText(text);
        } else {
          const url = URL.createObjectURL(blob);
          if (loadGenRef.current !== gen) {
            URL.revokeObjectURL(url);
            return;
          }
          revoked = url;
          setPreviewUrl(url);
        }
      } catch {
        // Preview is best-effort.
      }
    })();
    return () => {
      if (revoked) URL.revokeObjectURL(revoked);
      setPreviewText(null);
      setPreviewUrl(null);
    };
  }, [previewSource]);

  const tagsDirty = useMemo(() => {
    if (!doc) return false;
    const current = new Set((doc.tags ?? []).map((t) => t.id));
    if (current.size !== selectedTagIds.size) return true;
    for (const tid of selectedTagIds) if (!current.has(tid)) return true;
    return false;
  }, [doc, selectedTagIds]);

  const toggleTag = (tagId: number, on: boolean) => {
    setSelectedTagIds((prev) => {
      const next = new Set(prev);
      if (on) next.add(tagId);
      else next.delete(tagId);
      return next;
    });
  };

  const handleSaveTags = async () => {
    if (!doc || doc.vault_id == null) return;
    setSavingTags(true);
    try {
      const updated = await setDocumentTags(Number(doc.id), doc.vault_id, Array.from(selectedTagIds));
      setDoc((prev) => (prev ? { ...prev, tags: updated } : prev));
      toast.success("Tags updated");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to update tags");
    } finally {
      setSavingTags(false);
    }
  };

  const handleDownload = async () => {
    if (!doc) return;
    try {
      await downloadDocument(doc.id, doc.filename || `document-${doc.id}`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Download failed");
    }
  };

  if (loading) {
    return <div className="p-6 text-sm text-muted-foreground">Loading…</div>;
  }
  if (error || !doc) {
    return (
      <div className="p-6">
        <Button variant="ghost" size="sm" onClick={() => navigate("/documents")}>
          <ArrowLeft className="w-4 h-4 mr-1" /> Back
        </Button>
        <p className="text-sm text-destructive mt-4">{error ?? "Document not found"}</p>
      </div>
    );
  }

  const status = (doc.metadata?.status as string | undefined) ?? "";
  const chunkCount = Number(doc.metadata?.chunk_count ?? 0);
  // Searchable once chunks are embedded: "indexed", or "partial" (LIVE-03 —
  // completed with embedding failures; the embedded chunks still retrieve).
  const isSearchable = status === "indexed" || status === "partial";
  const chunksFailed = Number(doc.metadata?.chunks_failed ?? 0);
  const failureMessage =
    documentField<string>(doc, "error_message") ??
    (chunksFailed > 0 ? `${chunksFailed} chunks failed to embed` : null);
  const partialDescription =
    status === "partial" ? (failureMessage ?? "Completed with failures") : null;

  const handleAskAboutDocument = () => {
    if (!doc || !isSearchable) return;
    // Scope the NEXT chat question to this document (issue #514 AC-23), then
    // route into the chat surface carrying the document id.
    setScopeDocumentIds([Number(doc.id)]);
    navigate({ pathname: "/chat", search: `?document_ids=${doc.id}` });
  };

  return (
    <ScrollArea className="h-full">
      <div className="flex flex-col gap-4 p-6 max-w-3xl mx-auto">
        {/* Header */}
        <div className="flex items-start justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <Button variant="ghost" size="icon" onClick={() => navigate("/documents")} aria-label="Back">
              <ArrowLeft className="w-4 h-4" />
            </Button>
            <FileIcon filename={doc.filename} className="w-5 h-5 shrink-0" />
            <h1 className="text-lg font-semibold truncate" title={doc.filename}>
              {doc.filename}
            </h1>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <Button
              variant="outline"
              size="sm"
              onClick={handleAskAboutDocument}
              disabled={!isSearchable}
              title={
                isSearchable
                  ? "Ask questions scoped to this document"
                  : "Waiting until indexed — available once the document is searchable"
              }
            >
              <MessageSquare className="w-4 h-4 mr-1" />
              Ask about this document
            </Button>
            <Button variant="outline" size="sm" onClick={handleDownload}>
              <Download className="w-4 h-4 mr-1" />
              Download
            </Button>
          </div>
        </div>

        {/* Metadata */}
        <Card>
          <CardHeader className="pb-2 pt-3 px-4">
            <CardTitle className="text-sm">Details</CardTitle>
          </CardHeader>
          <CardContent className="px-4 pb-3 grid grid-cols-2 gap-3 text-sm">
            <div>
              <p className="text-muted-foreground">Status</p>
              <StatusBadge status={status} chunksFailed={Number(doc.metadata?.chunks_failed ?? 0)} />
              {partialDescription && (
                <p className="text-xs text-warning mt-1">{partialDescription}</p>
              )}
            </div>
            <div>
              <p className="text-muted-foreground">Chunks</p>
              <p>{chunkCount}</p>
            </div>
            <div>
              <p className="text-muted-foreground">Size</p>
              <p>{formatFileSize(doc.size)}</p>
            </div>
            <div>
              <p className="text-muted-foreground">Uploaded</p>
              <p>{formatDate(doc.created_at)}</p>
            </div>
          </CardContent>
        </Card>

        {/* Parse quality — extraction diagnostics (issue #514 / PRODUCT-ENH-06) */}
        <ParseQualityPanel doc={doc} />

        {/* Tags */}
        <Card>
          <CardHeader className="pb-2 pt-3 px-4 flex flex-row items-center justify-between">
            <CardTitle className="text-sm">Tags</CardTitle>
            {doc.vault_id != null && (
              <Button size="sm" variant="outline" onClick={handleSaveTags} disabled={!tagsDirty || savingTags}>
                {savingTags ? (
                  <Loader2 className="w-4 h-4 mr-1 animate-spin" />
                ) : (
                  <Save className="w-4 h-4 mr-1" />
                )}
                Save
              </Button>
            )}
          </CardHeader>
          <CardContent className="px-4 pb-3">
            {allTags.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                No tags in this vault yet. Create tags from the Documents page.
              </p>
            ) : (
              <div className="flex flex-wrap gap-3">
                {allTags.map((tag) => (
                  <label key={tag.id} className="flex items-center gap-2 text-sm cursor-pointer">
                    <Checkbox
                      checked={selectedTagIds.has(tag.id)}
                      onCheckedChange={(c) => toggleTag(tag.id, !!c)}
                      aria-label={`Tag ${tag.name}`}
                    />
                    <Badge variant="outline">{tag.name}</Badge>
                  </label>
                ))}
              </div>
            )}
          </CardContent>
        </Card>

        {/* Preview */}
        {(previewText != null || previewUrl != null) && (
          <Card>
            <CardHeader className="pb-2 pt-3 px-4">
              <CardTitle className="text-sm">Preview</CardTitle>
            </CardHeader>
            <CardContent className="px-4 pb-3">
              {previewText != null ? (
                <pre className="text-xs whitespace-pre-wrap font-sans max-h-[60vh] overflow-auto">
                  {previewText}
                </pre>
              ) : previewUrl != null ? (
                <iframe
                  src={previewUrl}
                  title={`Preview of ${doc.filename}`}
                  className="w-full h-[70vh] border rounded"
                />
              ) : null}
            </CardContent>
          </Card>
        )}
      </div>
    </ScrollArea>
  );
}
