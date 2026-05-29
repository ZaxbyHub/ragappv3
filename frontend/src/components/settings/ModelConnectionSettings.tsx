import { AlertCircle } from "lucide-react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { SettingsFormData, SettingsErrors } from "@/stores/useSettingsStore";

export interface ModelConnectionSettingsProps {
  formData: SettingsFormData;
  errors: SettingsErrors;
  onChange: (field: keyof SettingsFormData, value: string | boolean | number) => void;
}

export function ModelConnectionSettings({
  formData,
  errors,
  onChange,
}: ModelConnectionSettingsProps) {
  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle>Model Endpoints</CardTitle>
          <CardDescription>
            Configure the URLs for embedding, chat, and reranking services
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <div className="space-y-2">
            <Label htmlFor="ollama_embedding_url" className="text-sm font-medium">
              Embedding Service URL
            </Label>
            <Input
              id="ollama_embedding_url"
              type="url"
              placeholder="http://localhost:11434"
              value={formData.ollama_embedding_url || ""}
              onChange={(e) => onChange("ollama_embedding_url", e.target.value)}
              aria-describedby="ollama_embedding_url-desc"
              aria-invalid={!!errors.ollama_embedding_url}
              className={errors.ollama_embedding_url ? "border-destructive" : ""}
            />
            {errors.ollama_embedding_url && (
              <p className="text-xs text-destructive" role="alert">
                {errors.ollama_embedding_url}
              </p>
            )}
            <p id="ollama_embedding_url-desc" className="text-xs text-muted-foreground">
              URL for the embedding model service (e.g., HuggingFace TEI, Ollama, OpenAI-compatible).
              Examples: http://harrier-embed:8080/v1/embeddings (TEI) or http://localhost:11434 (Ollama)
            </p>
          </div>

          <div className="space-y-2">
            <Label htmlFor="ollama_chat_url" className="text-sm font-medium">
              Chat/LLM Service URL
            </Label>
            <Input
              id="ollama_chat_url"
              type="url"
              placeholder="http://localhost:11434"
              value={formData.ollama_chat_url || ""}
              onChange={(e) => onChange("ollama_chat_url", e.target.value)}
              aria-describedby="ollama_chat_url-desc"
              aria-invalid={!!errors.ollama_chat_url}
              className={errors.ollama_chat_url ? "border-destructive" : ""}
            />
            {errors.ollama_chat_url && (
              <p className="text-xs text-destructive" role="alert">
                {errors.ollama_chat_url}
              </p>
            )}
            <p id="ollama_chat_url-desc" className="text-xs text-muted-foreground">
              URL for the chat/LLM service. For Docker, use http://host.docker.internal:11434 to
              connect to the host&apos;s Ollama instance.
            </p>
          </div>

          <div className="space-y-2">
            <Label htmlFor="instant_chat_url" className="text-sm font-medium">
              Instant LLM Service URL
            </Label>
            <Input
              id="instant_chat_url"
              type="url"
              placeholder="http://host.docker.internal:1234"
              value={formData.instant_chat_url || ""}
              onChange={(e) => onChange("instant_chat_url", e.target.value)}
              aria-describedby="instant_chat_url-desc"
              aria-invalid={!!errors.instant_chat_url}
              className={errors.instant_chat_url ? "border-destructive" : ""}
            />
            {errors.instant_chat_url && (
              <p className="text-xs text-destructive" role="alert">
                {errors.instant_chat_url}
              </p>
            )}
            <p id="instant_chat_url-desc" className="text-xs text-muted-foreground">
              URL for the Instant chat backend (e.g., LM Studio on the host machine
              listening on port 1234). Changes apply immediately.
            </p>
          </div>

          <div className="space-y-2">
            <Label htmlFor="reranker_url" className="text-sm font-medium">
              Reranker Service URL
            </Label>
            <Input
              id="reranker_url"
              type="url"
              placeholder="http://localhost:8082"
              value={formData.reranker_url || ""}
              onChange={(e) => onChange("reranker_url", e.target.value)}
              aria-describedby="reranker_url-desc"
              aria-invalid={!!errors.reranker_url}
              className={errors.reranker_url ? "border-destructive" : ""}
            />
            {errors.reranker_url && (
              <p className="text-xs text-destructive" role="alert">
                {errors.reranker_url}
              </p>
            )}
            <p id="reranker_url-desc" className="text-xs text-muted-foreground">
              URL for the reranking service (e.g., a hosted cross-encoder). Leave empty to use local
              reranking.
            </p>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Model Names</CardTitle>
          <CardDescription>
            Specify which models to use for embeddings, chat, and reranking
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <div className="rounded-sm border border-warning/30 bg-warning/10 p-3 flex items-start gap-2">
            <AlertCircle className="w-4 h-4 text-warning mt-0.5 shrink-0" aria-hidden="true" />
            <p className="text-sm text-foreground/90">
              Changing model names takes effect immediately. Ensure the specified models are
              available at the configured endpoints.
            </p>
          </div>

          <div className="space-y-2">
            <Label htmlFor="embedding_model" className="text-sm font-medium">
              Embedding Model
            </Label>
            <Input
              id="embedding_model"
              type="text"
              placeholder="nomic-embed-text"
              value={formData.embedding_model || ""}
              onChange={(e) => onChange("embedding_model", e.target.value)}
              aria-describedby="embedding_model-desc"
              aria-invalid={!!errors.embedding_model}
              className={errors.embedding_model ? "border-destructive" : ""}
            />
            {errors.embedding_model && (
              <p className="text-xs text-destructive" role="alert">
                {errors.embedding_model}
              </p>
            )}
            <p id="embedding_model-desc" className="text-xs text-muted-foreground">
              Model used for generating document embeddings. Examples: microsoft/harrier-oss-v1-0.6b,
              nomic-embed-text, mxbai-embed-large
            </p>
          </div>

          <div className="space-y-2">
            <Label htmlFor="chat_model" className="text-sm font-medium">
              Chat Model
            </Label>
            <Input
              id="chat_model"
              type="text"
              placeholder="llama3"
              value={formData.chat_model || ""}
              onChange={(e) => onChange("chat_model", e.target.value)}
              aria-describedby="chat_model-desc"
              aria-invalid={!!errors.chat_model}
              className={errors.chat_model ? "border-destructive" : ""}
            />
            {errors.chat_model && (
              <p className="text-xs text-destructive" role="alert">
                {errors.chat_model}
              </p>
            )}
            <p id="chat_model-desc" className="text-xs text-muted-foreground">
              Model used for chat responses. Examples: llama3, mistral, openai/gpt-oss-20b
            </p>
          </div>

          <div className="space-y-2">
            <Label htmlFor="instant_chat_model" className="text-sm font-medium">
              Instant Chat Model
            </Label>
            <Input
              id="instant_chat_model"
              type="text"
              placeholder="nvidia/nemotron-3-nano-4b"
              value={formData.instant_chat_model || ""}
              onChange={(e) => onChange("instant_chat_model", e.target.value)}
              aria-describedby="instant_chat_model-desc"
              aria-invalid={!!errors.instant_chat_model}
              className={errors.instant_chat_model ? "border-destructive" : ""}
            />
            {errors.instant_chat_model && (
              <p className="text-xs text-destructive" role="alert">
                {errors.instant_chat_model}
              </p>
            )}
            <p id="instant_chat_model-desc" className="text-xs text-muted-foreground">
              Small/fast model loaded in LM Studio for Instant mode. Changes apply
              immediately.
            </p>
          </div>

          <div className="space-y-2">
            <Label htmlFor="default_chat_mode" className="text-sm font-medium">
              Default Chat Mode
            </Label>
            <div
              id="default_chat_mode"
              role="radiogroup"
              aria-describedby="default_chat_mode-desc"
              className="flex gap-2"
            >
              <button
                type="button"
                role="radio"
                aria-checked={formData.default_chat_mode === "instant"}
                onClick={() => onChange("default_chat_mode", "instant")}
                className={
                  "rounded-sm border px-3 py-1.5 text-sm transition-colors " +
                  (formData.default_chat_mode === "instant"
                    ? "border-primary bg-primary text-primary-foreground"
                    : "border-input bg-background text-foreground hover:bg-accent")
                }
              >
                Instant
              </button>
              <button
                type="button"
                role="radio"
                aria-checked={formData.default_chat_mode === "thinking"}
                onClick={() => onChange("default_chat_mode", "thinking")}
                className={
                  "rounded-sm border px-3 py-1.5 text-sm transition-colors " +
                  (formData.default_chat_mode === "thinking"
                    ? "border-primary bg-primary text-primary-foreground"
                    : "border-input bg-background text-foreground hover:bg-accent")
                }
              >
                Thinking
              </button>
            </div>
            <p id="default_chat_mode-desc" className="text-xs text-muted-foreground">
              Default mode for new chats. Users can still override per message
              via the composer toggle.
            </p>
          </div>

          <div className="space-y-2">
            <Label htmlFor="reranker_model" className="text-sm font-medium">
              Reranker Model
            </Label>
            <Input
              id="reranker_model"
              type="text"
              placeholder="BAAI/bge-reranker-v2-m3"
              value={formData.reranker_model || ""}
              onChange={(e) => onChange("reranker_model", e.target.value)}
              aria-describedby="reranker_model-desc"
              aria-invalid={!!errors.reranker_model}
              className={errors.reranker_model ? "border-destructive" : ""}
            />
            {errors.reranker_model && (
              <p className="text-xs text-destructive" role="alert">
                {errors.reranker_model}
              </p>
            )}
            <p id="reranker_model-desc" className="text-xs text-muted-foreground">
              Model used for reranking retrieved documents. Examples: BAAI/bge-reranker-v2-m3,
              cross-encoder/ms-marco-MiniLM-L-12-v2
            </p>
          </div>
        </CardContent>
      </Card>
    </>
  );
}
