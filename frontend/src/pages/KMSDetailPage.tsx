import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { toast } from "sonner";
import { ArrowLeft, Download, Edit, Save, Trash2, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  deleteKMSEntry,
  downloadDocument,
  getKMSEntry,
  updateKMSEntry,
  type KMSEntry,
} from "@/lib/api";

const STATUS_VALUES = ["draft", "published", "archived"] as const;

export default function KMSDetailPage() {
  const { entryId } = useParams<{ entryId: string }>();
  const navigate = useNavigate();
  const id = Number(entryId);

  const [entry, setEntry] = useState<KMSEntry | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [summary, setSummary] = useState("");
  const [tags, setTags] = useState("");
  const [status, setStatus] = useState<string>("draft");
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    if (!Number.isFinite(id)) {
      setError("Invalid entry id");
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const e = await getKMSEntry(id);
      setEntry(e);
      setTitle(e.title);
      setBody(e.body);
      setSummary(e.summary);
      setTags(e.tags.join(", "));
      setStatus(e.status);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load entry");
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  async function handleSave() {
    if (!entry) return;
    setSaving(true);
    try {
      const updated = await updateKMSEntry(entry.id, {
        title: title.trim(),
        body,
        summary,
        status,
        tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
      });
      setEntry(updated);
      setEditing(false);
      toast.success("Entry saved");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to save entry");
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    if (!entry) return;
    if (!window.confirm(`Delete "${entry.title}"?`)) return;
    try {
      await deleteKMSEntry(entry.id);
      toast.success("Entry deleted");
      navigate("/kms");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to delete entry");
    }
  }

  async function handleDownload() {
    if (!entry?.file_id) return;
    try {
      await downloadDocument(entry.file_id, entry.title);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Download failed");
    }
  }

  if (loading) {
    return (
      <div className="p-6 text-sm text-muted-foreground">Loading…</div>
    );
  }
  if (error || !entry) {
    return (
      <div className="p-6">
        <Button variant="ghost" size="sm" onClick={() => navigate("/kms")}>
          <ArrowLeft className="w-4 h-4 mr-1" /> Back
        </Button>
        <p className="text-sm text-destructive mt-4">{error ?? "Entry not found"}</p>
      </div>
    );
  }

  return (
    <ScrollArea className="h-full">
      <div className="flex flex-col gap-4 p-6 max-w-3xl mx-auto">
        {/* Header */}
        <div className="flex items-start justify-between gap-2">
          <div className="flex items-center gap-2">
            <Button
              variant="ghost"
              size="icon"
              onClick={() => navigate("/kms")}
              aria-label="Back"
            >
              <ArrowLeft className="w-4 h-4" />
            </Button>
            <div>
              {editing ? (
                <Input value={title} onChange={(e) => setTitle(e.target.value)} className="text-lg font-semibold" />
              ) : (
                <h1 className="text-lg font-semibold">{entry.title}</h1>
              )}
              <p className="text-xs text-muted-foreground">{entry.slug}</p>
            </div>
          </div>
          <div className="flex gap-1">
            {entry.source_type === "document" && entry.file_id != null && (
              <Button variant="outline" size="sm" onClick={handleDownload}>
                <Download className="w-4 h-4 mr-1" />
                Source
              </Button>
            )}
            {editing ? (
              <>
                <Button variant="outline" size="sm" onClick={handleSave} disabled={saving}>
                  <Save className="w-4 h-4 mr-1" />
                  {saving ? "Saving…" : "Save"}
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setEditing(false);
                    setTitle(entry.title);
                    setBody(entry.body);
                    setSummary(entry.summary);
                    setTags(entry.tags.join(", "));
                    setStatus(entry.status);
                  }}
                >
                  <X className="w-4 h-4" />
                </Button>
              </>
            ) : (
              <>
                <Button variant="outline" size="sm" onClick={() => setEditing(true)}>
                  <Edit className="w-4 h-4 mr-1" />
                  Edit
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleDelete}
                  className="text-destructive hover:text-destructive"
                >
                  <Trash2 className="w-4 h-4" />
                </Button>
              </>
            )}
          </div>
        </div>

        {/* Meta */}
        <div className="flex gap-2 flex-wrap items-center">
          {editing ? (
            <Select value={status} onValueChange={setStatus}>
              <SelectTrigger className="w-36">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {STATUS_VALUES.map((s) => (
                  <SelectItem key={s} value={s}>
                    {s}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : (
            <Badge variant="outline">{entry.status}</Badge>
          )}
          <Badge variant="secondary" className="capitalize">
            {entry.source_type}
          </Badge>
        </div>

        {/* Tags */}
        {editing ? (
          <div>
            <Label htmlFor="kms-edit-tags">Tags (comma-separated)</Label>
            <Input
              id="kms-edit-tags"
              value={tags}
              onChange={(e) => setTags(e.target.value)}
            />
          </div>
        ) : (
          entry.tags.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {entry.tags.map((tag) => (
                <Badge key={tag} variant="outline">
                  {tag}
                </Badge>
              ))}
            </div>
          )
        )}

        {/* Summary */}
        {editing ? (
          <div>
            <Label htmlFor="kms-edit-summary">Summary</Label>
            <Textarea
              id="kms-edit-summary"
              value={summary}
              onChange={(e) => setSummary(e.target.value)}
              rows={3}
            />
          </div>
        ) : (
          entry.summary && (
            <p className="text-sm text-muted-foreground">{entry.summary}</p>
          )
        )}

        {/* Body */}
        <Card>
          <CardHeader className="pb-2 pt-3 px-4">
            <CardTitle className="text-sm">Content</CardTitle>
          </CardHeader>
          <CardContent className="px-4 pb-3">
            {editing ? (
              <Textarea
                value={body}
                onChange={(e) => setBody(e.target.value)}
                rows={16}
                className="font-mono text-xs"
              />
            ) : entry.body ? (
              <pre className="text-xs whitespace-pre-wrap font-sans">{entry.body}</pre>
            ) : (
              <p className="text-xs text-muted-foreground italic">No content.</p>
            )}
          </CardContent>
        </Card>
      </div>
    </ScrollArea>
  );
}
