import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import type { SettingsFormData, SettingsErrors } from "@/stores/useSettingsStore";

export interface RetrievalSettingsProps {
  formData: SettingsFormData;
  errors: SettingsErrors;
  onChange: (field: keyof SettingsFormData, value: string | boolean) => void;
}

export function RetrievalSettings({
  formData,
  errors,
  onChange,
}: RetrievalSettingsProps) {
  return (
    <>
      {/* Reranking Settings */}
      <Card>
        <CardHeader>
          <CardTitle>Reranking</CardTitle>
          <CardDescription>Configure document reranking for improved retrieval quality</CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          {/* Enable Reranking Toggle */}
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Checkbox
                id="reranking-enabled"
                checked={!!formData.reranking_enabled}
                onCheckedChange={(checked) => onChange("reranking_enabled", checked as boolean)}
              />
              <Label htmlFor="reranking-enabled">
                Enable Reranking
              </Label>
            </div>
            <p className="text-xs text-muted-foreground">
              Apply reranking to improve the relevance of retrieved documents
            </p>
          </div>

          {/* Reranker URL */}
          <div className="space-y-2">
            <Label htmlFor="reranker-url">Reranker URL</Label>
            <Input
              id="reranker-url"
              type="text"
              value={formData.reranker_url || ""}
              onChange={(e) => onChange("reranker_url", e.target.value)}
              placeholder="http://host.docker.internal:8082 or leave empty for local"
              className={errors.reranker_url ? "border-destructive" : ""}
            />
            {errors.reranker_url && (
              <p role="alert" className="text-xs text-destructive">{errors.reranker_url}</p>
            )}
            <p className="text-xs text-muted-foreground">
              URL to the reranking service (leave empty to use local reranker)
            </p>
          </div>

          {/* Reranker Model */}
          <div className="space-y-2">
            <Label htmlFor="reranker-model">Reranker Model</Label>
            <Input
              id="reranker-model"
              type="text"
              value={formData.reranker_model || ""}
              onChange={(e) => onChange("reranker_model", e.target.value)}
              placeholder="BAAI/bge-reranker-v2-m3"
              className={errors.reranker_model ? "border-destructive" : ""}
            />
            {errors.reranker_model && (
              <p role="alert" className="text-xs text-destructive">{errors.reranker_model}</p>
            )}
            <p className="text-xs text-muted-foreground">
              Model name for the reranking service
            </p>
          </div>

          {/* Initial Retrieval Top-K */}
          <div className="space-y-2">
            <Label htmlFor="initial-retrieval-top-k">Initial Retrieval Top-K</Label>
            <Input
              id="initial-retrieval-top-k"
              type="number"
              min={5}
              max={100}
              value={formData.initial_retrieval_top_k || 20}
              onChange={(e) => onChange("initial_retrieval_top_k", e.target.value)}
              className={errors.initial_retrieval_top_k ? "border-destructive" : ""}
            />
            {errors.initial_retrieval_top_k && (
              <p role="alert" className="text-xs text-destructive">{errors.initial_retrieval_top_k}</p>
            )}
            <p className="text-xs text-muted-foreground">
              Number of initial documents to retrieve before reranking (5-100)
            </p>
          </div>

          {/* Reranker Top-N */}
          <div className="space-y-2">
            <Label htmlFor="reranker-top-n">Reranker Top-N</Label>
            <Input
              id="reranker-top-n"
              type="number"
              min={1}
              max={20}
              value={formData.reranker_top_n || 5}
              onChange={(e) => onChange("reranker_top_n", e.target.value)}
              className={errors.reranker_top_n ? "border-destructive" : ""}
            />
            {errors.reranker_top_n && (
              <p role="alert" className="text-xs text-destructive">{errors.reranker_top_n}</p>
            )}
            <p className="text-xs text-muted-foreground">
              Number of top documents to keep after reranking (1-20)
            </p>
          </div>
        </CardContent>
      </Card>

      {/* Hybrid Search Settings */}
      <Card>
        <CardHeader>
          <CardTitle>Hybrid Search</CardTitle>
          <CardDescription>Configure hybrid search combining vector and keyword search</CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          {/* Enable Hybrid Search Toggle */}
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <Checkbox
                id="hybrid-search-enabled"
                checked={!!formData.hybrid_search_enabled}
                onCheckedChange={(checked) => onChange("hybrid_search_enabled", checked as boolean)}
              />
              <Label htmlFor="hybrid-search-enabled">
                Enable Hybrid Search
              </Label>
            </div>
            <p className="text-xs text-muted-foreground">
              Combine vector similarity search with keyword-based search
            </p>
          </div>

          {/* Hybrid Alpha */}
          <div className="space-y-2">
            <Label htmlFor="hybrid-alpha">Hybrid Alpha</Label>
            <div className="flex items-center gap-4">
              <Input
                id="hybrid-alpha"
                type="number"
                min={0}
                max={1}
                step={0.1}
                value={formData.hybrid_alpha || 0.5}
                onChange={(e) => onChange("hybrid_alpha", e.target.value)}
                className={`w-24 ${errors.hybrid_alpha ? "border-destructive" : ""}`}
              />
              <input
                type="range"
                min={0}
                max={1}
                step={0.1}
                value={formData.hybrid_alpha || 0.5}
                onChange={(e) => onChange("hybrid_alpha", e.target.value)}
                aria-label="Hybrid Alpha"
                className="flex-1"
              />
            </div>
            {errors.hybrid_alpha && (
              <p role="alert" className="text-xs text-destructive">{errors.hybrid_alpha}</p>
            )}
            <p className="text-xs text-muted-foreground">
              Weight for vector search vs keyword search (0 = keyword only, 1 = vector only)
            </p>
          </div>
        </CardContent>
      </Card>
    </>
  );
}