import { useState, useEffect, useRef, useCallback } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Bold, Italic, Heading1, Link, List, Code } from "lucide-react";
import type { WikiPage } from "@/lib/api";

const PAGE_TYPES = [
  {value: "entity", label: "Entity"},
  {value: "procedure", label: "Procedure"},
  {value: "system", label: "System"},
  {value: "acronym", label: "Acronym"},
  {value: "qa", label: "QA"},
  {value: "contradiction", label: "Contradiction"},
  {value: "open_question", label: "Open Question"},
  {value: "overview", label: "Overview"},
  {value: "manual", label: "Manual"},
] as const;

const STATUSES = [
  {value: "draft", label: "Draft"},
  {value: "needs_review", label: "Needs Review"},
  {value: "verified", label: "Verified"},
  {value: "stale", label: "Stale"},
  {value: "archived", label: "Archived"},
] as const;

const TEMPLATES: Record<string, string> = {
  entity: "# {title}\n\n## Overview\n\n## Key Facts\n\n## Related Entities\n",
  procedure: "# {title}\n\n## Purpose\n\n## Steps\n\n1. \n2. \n3. \n\n## Notes\n",
  system: "# {title}\n\n## Architecture\n\n## Components\n\n## Interfaces\n",
  acronym: "# {title}\n\n**Stands for:** \n\n## Context\n\n## Usage\n",
  qa: "# {title}\n\n## Question\n\n## Answer\n\n## Sources\n",
  manual: "# {title}\n\n",
};

function getTemplate(pageType: string, title: string): string {
  const tmpl = TEMPLATES[pageType] ?? "# {title}\n\n";
  return tmpl.replace("{title}", title || "Untitled");
}

/** Returns true if `content` matches any template (with any title). */
function isTemplateContent(content: string): boolean {
  if (!content) return true;
  for (const tmpl of Object.values(TEMPLATES)) {
    // Build a regex from the template: escape special chars, replace {title} with .*
    const escaped = tmpl.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const pattern = escaped.replace("\\{title\\}", ".*");
    if (new RegExp(`^${pattern}$`, "s").test(content)) return true;
  }
  // Also match the fallback
  if (/^# .*\n\n$/.test(content)) return true;
  return false;
}

interface MarkdownToolbarProps {
  textareaRef: React.RefObject<HTMLTextAreaElement | null>;
  value: string;
  onChange: (v: string) => void;
}

function MarkdownToolbar({ textareaRef, value, onChange }: MarkdownToolbarProps) {
  const insertMarkdown = useCallback(
    (before: string, after: string, placeholder: string) => {
      const ta = textareaRef.current;
      if (!ta) return;
      const start = ta.selectionStart;
      const end = ta.selectionEnd;
      const selected = value.slice(start, end);
      const insertion = selected || placeholder;
      const newValue =
        value.slice(0, start) + before + insertion + after + value.slice(end);
      onChange(newValue);
      // Restore focus and select the inserted text after React re-renders
      requestAnimationFrame(() => {
        ta.focus();
        const selStart = start + before.length;
        const selEnd = selStart + insertion.length;
        ta.setSelectionRange(selStart, selEnd);
      });
    },
    [textareaRef, value, onChange],
  );

  const buttons = [
    { icon: Bold, label: "Bold", before: "**", after: "**", placeholder: "bold" },
    { icon: Italic, label: "Italic", before: "_", after: "_", placeholder: "italic" },
    { icon: Heading1, label: "Heading", before: "# ", after: "", placeholder: "heading" },
    { icon: Link, label: "Link", before: "[", after: "](url)", placeholder: "link text" },
    { icon: List, label: "List", before: "- ", after: "", placeholder: "list item" },
    { icon: Code, label: "Code", before: "`", after: "`", placeholder: "code" },
  ] as const;

  return (
    <div className="flex gap-1 border rounded-md p-1 bg-muted/50">
      {buttons.map((btn) => (
        <Button
          key={btn.label}
          type="button"
          variant="ghost"
          size="icon"
          className="h-7 w-7"
          title={btn.label}
          onClick={() => insertMarkdown(btn.before, btn.after, btn.placeholder)}
        >
          <btn.icon className="h-4 w-4" />
        </Button>
      ))}
    </div>
  );
}

interface WikiEditDialogProps {
  open: boolean;
  page?: WikiPage | null;
  vaultId: number;
  onClose: () => void;
  onSave: (data: {
    title: string;
    page_type: string;
    slug?: string;
    markdown: string;
    summary: string;
    status: string;
    confidence: number;
  }) => Promise<void>;
}

export function WikiEditDialog({ open, page, vaultId: _vaultId, onClose, onSave }: WikiEditDialogProps) {
  const [title, setTitle] = useState("");
  const [pageType, setPageType] = useState<string>("entity");
  const [slug, setSlug] = useState("");
  const [markdown, setMarkdown] = useState("");
  const [summary, setSummary] = useState("");
  const [status, setStatus] = useState<string>("draft");
  const [confidence, setConfidence] = useState(0);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  const isCreating = !page;

  useEffect(() => {
    if (page) {
      setTitle(page.title);
      setPageType(page.page_type);
      setSlug(page.slug);
      setMarkdown(page.markdown);
      setSummary(page.summary);
      setStatus(page.status);
      setConfidence(page.confidence);
    } else {
      setTitle("");
      setPageType("entity");
      setSlug("");
      setMarkdown(getTemplate("entity", ""));
      setSummary("");
      setStatus("draft");
      setConfidence(0);
    }
    setError(null);
  }, [page, open]);

  // Auto-fill template when page type changes during creation
  useEffect(() => {
    if (!isCreating) return;
    if (isTemplateContent(markdown)) {
      setMarkdown(getTemplate(pageType, title));
    }
  }, [pageType]); // eslint-disable-line react-hooks/exhaustive-deps

  async function handleSave() {
    if (!title.trim()) {
      setError("Title is required");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await onSave({ title, page_type: pageType, slug: slug || undefined, markdown, summary, status, confidence });
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{page ? "Edit Page" : "New Wiki Page"}</DialogTitle>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <div>
            <Label htmlFor="wiki-title">Title</Label>
            <Input id="wiki-title" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Page title" />
          </div>

          <div className="flex gap-3">
            <div className="flex-1">
              <Label htmlFor="wiki-type">Type</Label>
              <Select value={pageType} onValueChange={setPageType}>
                <SelectTrigger id="wiki-type">
                  <SelectValue className="capitalize" />
                </SelectTrigger>
                <SelectContent>
                  {PAGE_TYPES.map((t) => (
                    <SelectItem key={t.value} value={t.value} className="capitalize" textValue={t.label}>{t.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="flex-1">
              <Label htmlFor="wiki-status">Status</Label>
              <Select value={status} onValueChange={setStatus}>
                <SelectTrigger id="wiki-status">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {STATUSES.map((s) => (
                    <SelectItem key={s.value} value={s.value} className="capitalize" textValue={s.label}>{s.label}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <div>
            <Label htmlFor="wiki-slug">Slug (optional — auto-generated from title)</Label>
            <Input id="wiki-slug" value={slug} onChange={(e) => setSlug(e.target.value)} placeholder="auto" />
          </div>

          <div>
            <Label htmlFor="wiki-summary">Summary</Label>
            <Input id="wiki-summary" value={summary} onChange={(e) => setSummary(e.target.value)} placeholder="One-line summary" />
          </div>

          <div>
            <Label htmlFor="wiki-markdown">Content (Markdown)</Label>
            <MarkdownToolbar
              textareaRef={textareaRef}
              value={markdown}
              onChange={setMarkdown}
            />
            <Textarea
              ref={textareaRef}
              id="wiki-markdown"
              value={markdown}
              onChange={(e) => setMarkdown(e.target.value)}
              placeholder="Page content…"
              rows={6}
              className="font-mono text-sm mt-1"
            />
          </div>

          {error && <p className="text-sm text-destructive">{error}</p>}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={saving}>Cancel</Button>
          <Button onClick={handleSave} disabled={saving}>
            {saving ? "Saving…" : "Save"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
