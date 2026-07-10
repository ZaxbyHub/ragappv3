import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import type { SettingsFormData, SettingsErrors } from "@/stores/useSettingsStore";
import { ReindexFieldWarning } from "./ReindexFieldWarning";

interface DocumentProcessingSettingsProps {
  formData: SettingsFormData;
  errors: SettingsErrors;
  onChange: (field: keyof SettingsFormData, value: string | boolean) => void;
}

export function DocumentProcessingSettings({
  formData,
  errors,
  onChange,
}: DocumentProcessingSettingsProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Document Processing</CardTitle>
        <CardDescription>Configure document chunking and retrieval parameters</CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        {/* Chunk Size */}
        <div className="space-y-2">
          <Label htmlFor="chunk-size">Chunk Size (characters)</Label>
          <Input
            id="chunk-size"
            type="number"
            min={1}
            value={formData.chunk_size_chars}
            onChange={(e) => onChange("chunk_size_chars", e.target.value)}
            className={errors.chunk_size_chars ? "border-destructive" : ""}
          />
          {errors.chunk_size_chars && (
            <p role="alert" className="text-xs text-destructive">{errors.chunk_size_chars}</p>
          )}
          <p className="text-xs text-muted-foreground">
            Number of characters per document chunk
          </p>
          <ReindexFieldWarning />
        </div>

        {/* Chunk Overlap */}
        <div className="space-y-2">
          <Label htmlFor="chunk-overlap">Chunk Overlap (characters)</Label>
          <Input
            id="chunk-overlap"
            type="number"
            min={1}
            value={formData.chunk_overlap_chars}
            onChange={(e) => onChange("chunk_overlap_chars", e.target.value)}
            className={errors.chunk_overlap_chars ? "border-destructive" : ""}
          />
          {errors.chunk_overlap_chars && (
            <p role="alert" className="text-xs text-destructive">{errors.chunk_overlap_chars}</p>
          )}
          <p className="text-xs text-muted-foreground">
            Number of overlapping characters between chunks (must be less than chunk size)
          </p>
          <ReindexFieldWarning />
        </div>

        {/* Retrieval Top-K */}
        <div className="space-y-2">
          <Label htmlFor="retrieval-top-k">Retrieval Top-K</Label>
          <Input
            id="retrieval-top-k"
            type="number"
            min={1}
            value={formData.retrieval_top_k}
            onChange={(e) => onChange("retrieval_top_k", e.target.value)}
            className={errors.retrieval_top_k ? "border-destructive" : ""}
          />
          {errors.retrieval_top_k && (
            <p role="alert" className="text-xs text-destructive">{errors.retrieval_top_k}</p>
          )}
          <p className="text-xs text-muted-foreground">
            Maximum number of chunks to retrieve and include in context
          </p>
        </div>

        {/* Auto Scan Enabled */}
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <Checkbox
              id="auto-scan-enabled"
              checked={!!formData.auto_scan_enabled}
              onCheckedChange={(checked) => onChange("auto_scan_enabled", checked as boolean)}
            />
            <Label htmlFor="auto-scan-enabled">
              Enable Auto Scan
            </Label>
          </div>
          <p className="text-xs text-muted-foreground">
            Automatically scan for new documents at regular intervals
          </p>
        </div>

        {/* Auto Scan Interval */}
        {formData.auto_scan_enabled && (
          <div className="space-y-2">
            <Label htmlFor="auto-scan-interval">Auto Scan Interval (minutes)</Label>
            <Input
              id="auto-scan-interval"
              type="number"
              min={1}
              value={formData.auto_scan_interval_minutes}
              onChange={(e) => onChange("auto_scan_interval_minutes", e.target.value)}
              className={errors.auto_scan_interval_minutes ? "border-destructive" : ""}
            />
            {errors.auto_scan_interval_minutes && (
              <p role="alert" className="text-xs text-destructive">{errors.auto_scan_interval_minutes}</p>
            )}
            <p className="text-xs text-muted-foreground">
              How often to scan for new documents (in minutes)
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
