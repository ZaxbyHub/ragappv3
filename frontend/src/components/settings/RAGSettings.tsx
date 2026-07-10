import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import type { SettingsFormData, SettingsErrors } from "@/stores/useSettingsStore";
import { ReindexFieldWarning } from "./ReindexFieldWarning";

interface RAGSettingsProps {
  formData: SettingsFormData;
  errors: SettingsErrors;
  onChange: (field: keyof SettingsFormData, value: string | boolean) => void;
}

export function RAGSettings({
  formData,
  errors,
  onChange,
}: RAGSettingsProps) {
  return (
    <>
      {/* Max Distance Threshold */}
      <Card>
        <CardHeader>
          <CardTitle>RAG Retrieval Settings</CardTitle>
          <CardDescription>Configure RAG retrieval behavior and filtering</CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <div className="space-y-2">
            <Label htmlFor="max-distance-threshold">Max Distance Threshold</Label>
            <div className="flex items-center gap-4">
              <Input
                id="max-distance-threshold"
                type="number"
                min={0}
                max={1}
                step={0.01}
                value={formData.max_distance_threshold}
                onChange={(e) => onChange("max_distance_threshold", e.target.value)}
                className={`w-24 ${errors.max_distance_threshold ? "border-destructive" : ""}`}
              />
              <input
                type="range"
                min={0}
                max={1}
                step={0.01}
                value={formData.max_distance_threshold}
                onChange={(e) => onChange("max_distance_threshold", e.target.value)}
                aria-label="Max Distance Threshold"
                className="flex-1"
              />
            </div>
            {errors.max_distance_threshold && (
              <p role="alert" className="text-xs text-destructive">{errors.max_distance_threshold}</p>
            )}
            <p className="text-xs text-muted-foreground">
              Maximum distance (1-0) for chunks to be included in context (lower = more strict)
            </p>
          </div>

          {/* Retrieval Window */}
          <div className="space-y-2">
            <Label htmlFor="retrieval-window">Retrieval Window</Label>
            <Input
              id="retrieval-window"
              type="number"
              min={0}
              max={3}
              value={formData.retrieval_window}
              onChange={(e) => onChange("retrieval_window", e.target.value)}
              className={errors.retrieval_window ? "border-destructive" : ""}
            />
            {errors.retrieval_window && (
              <p role="alert" className="text-xs text-destructive">{errors.retrieval_window}</p>
            )}
            <p className="text-xs text-muted-foreground">
              Number of adjacent chunks to include (0-3)
            </p>
          </div>

          {/* Vector Metric */}
          <div className="space-y-2">
            <Label htmlFor="vector-metric">Vector Metric</Label>
            <Select
              value={formData.vector_metric}
              onValueChange={(value) => onChange("vector_metric", value)}
            >
              <SelectTrigger id="vector-metric" className={errors.vector_metric ? "border-destructive" : ""}>
                <SelectValue placeholder="Select metric" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="cosine">Cosine Similarity</SelectItem>
                <SelectItem value="euclidean">Euclidean Distance</SelectItem>
                <SelectItem value="dot_product">Dot Product</SelectItem>
              </SelectContent>
            </Select>
            {errors.vector_metric && (
              <p role="alert" className="text-xs text-destructive">{errors.vector_metric}</p>
            )}
            <p className="text-xs text-muted-foreground">
              Distance metric used for vector similarity search
            </p>
            <ReindexFieldWarning />
          </div>

          {/* Embedding Batch Size */}
          <div className="space-y-2">
            <Label htmlFor="embedding-batch-size">Embedding Batch Size</Label>
            <Input
              id="embedding-batch-size"
              type="number"
              min={64}
              max={2048}
              step={64}
              value={formData.embedding_batch_size}
              onChange={(e) => onChange("embedding_batch_size", e.target.value)}
              className={errors.embedding_batch_size ? "border-destructive" : ""}
            />
            {errors.embedding_batch_size && (
              <p role="alert" className="text-xs text-destructive">{errors.embedding_batch_size}</p>
            )}
            <p className="text-xs text-muted-foreground">
              Number of chunks to embed per API request. Higher values = better GPU utilization.
            </p>
          </div>
        </CardContent>
      </Card>

      {/* Advanced Embedding Settings */}
      <Card>
        <CardHeader>
          <CardTitle>Advanced Embedding Settings</CardTitle>
          <CardDescription>Configure embedding prefixes for different content types</CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <div className="space-y-2">
            <Label htmlFor="embedding-doc-prefix">Embedding Document Prefix</Label>
            <Textarea
              id="embedding-doc-prefix"
              value={formData.embedding_doc_prefix}
              onChange={(e) => onChange("embedding_doc_prefix", e.target.value)}
              className={`min-h-[80px] ${errors.embedding_doc_prefix ? "border-destructive" : ""}`}
              placeholder="Passage: "
            />
            {errors.embedding_doc_prefix && (
              <p role="alert" className="text-xs text-destructive">{errors.embedding_doc_prefix}</p>
            )}
            <p className="text-xs text-muted-foreground">
              Prefix added to document chunks before embedding (default: &quot;Passage: &quot;)
            </p>
            <ReindexFieldWarning />
          </div>

          <div className="space-y-2">
            <Label htmlFor="embedding-query-prefix">Embedding Query Prefix</Label>
            <Textarea
              id="embedding-query-prefix"
              value={formData.embedding_query_prefix}
              onChange={(e) => onChange("embedding_query_prefix", e.target.value)}
              className={`min-h-[80px] ${errors.embedding_query_prefix ? "border-destructive" : ""}`}
              placeholder="Query: "
            />
            {errors.embedding_query_prefix && (
              <p role="alert" className="text-xs text-destructive">{errors.embedding_query_prefix}</p>
            )}
            <p className="text-xs text-muted-foreground">
              Prefix added to queries before embedding (default: &quot;Query: &quot;)
            </p>
            <ReindexFieldWarning />
          </div>
        </CardContent>
      </Card>
    </>
  );
}
