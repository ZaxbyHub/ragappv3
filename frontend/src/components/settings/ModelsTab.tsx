/**
 * Settings → Models tab.
 *
 * Replaces the legacy "AI" + "Advanced" tabs that contradicted each other
 * (AI claimed read-only via env vars; Advanced edited the same fields).
 *
 * All fields are runtime-editable. Each field shows a source badge derived
 * from ``effective_sources`` so the operator can see whether the displayed
 * value came from a settings_kv override, an env var, or the Pydantic
 * default. Per actual lifespan order in the backend (kv > env > default),
 * saving here will shadow any env var at runtime — that's documented in
 * the help text rather than enforced by disabling inputs.
 */
import { useEffect, useState } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import {
  REINDEX_REQUIRED_FIELDS,
  type SettingsErrors,
  type SettingsFormData,
} from "@/stores/useSettingsStore";
import { ReindexFieldWarning } from "./ReindexFieldWarning";

export interface ModelsTabProps {
  formData: SettingsFormData;
  errors: SettingsErrors;
  onChange: <K extends keyof SettingsFormData>(
    field: K,
    value: SettingsFormData[K],
  ) => void;
  effectiveSources: Record<string, "kv" | "env" | "default">;
}

function SourceBadge({
  source,
}: {
  source?: "kv" | "env" | "default";
}) {
  if (!source) return null;
  const label =
    source === "kv"
      ? "Runtime override"
      : source === "env"
      ? "From env"
      : "Default";
  const variant: "secondary" | "outline" =
    source === "kv" ? "secondary" : "outline";
  return (
    <Badge variant={variant} className="text-[10px] uppercase">
      {label}
    </Badge>
  );
}

interface FieldProps {
  field: keyof SettingsFormData;
  label: string;
  placeholder: string;
  description: string;
  type?: string;
  formData: SettingsFormData;
  errors: SettingsErrors;
  onChange: ModelsTabProps["onChange"];
  source?: "kv" | "env" | "default";
}

function StringField({
  field,
  label,
  placeholder,
  description,
  type = "text",
  formData,
  errors,
  onChange,
  source,
}: FieldProps) {
  const value = (formData as unknown as Record<string, string>)[field as string] ?? "";
  const err = (errors as Record<string, string | undefined>)[field as string];
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <Label htmlFor={String(field)} className="text-sm font-medium">
          {label}
        </Label>
        <SourceBadge source={source} />
      </div>
      <Input
        id={String(field)}
        type={type}
        placeholder={placeholder}
        value={value}
        onChange={(e) =>
          onChange(field, e.target.value as SettingsFormData[typeof field])
        }
        aria-invalid={err ? true : undefined}
        className={err ? "border-destructive" : undefined}
      />
      {err && (
        <p className="text-xs text-destructive" role="alert">
          {err}
        </p>
      )}
      <p className="text-xs text-muted-foreground">{description}</p>
      {REINDEX_REQUIRED_FIELDS.has(field) && <ReindexFieldWarning />}
    </div>
  );
}

interface NumberFieldProps {
  field: keyof SettingsFormData;
  label: string;
  description: string;
  min?: number;
  max?: number;
  formData: SettingsFormData;
  errors: SettingsErrors;
  onChange: ModelsTabProps["onChange"];
  source?: "kv" | "env" | "default";
}

function NumberField({
  field,
  label,
  description,
  min,
  max,
  formData,
  errors,
  onChange,
  source,
}: NumberFieldProps) {
  const value =
    (formData as unknown as Record<string, number>)[field as string] ?? "";
  const [draftValue, setDraftValue] = useState(String(value));
  const err = (errors as Record<string, string | undefined>)[field as string];

  useEffect(() => {
    setDraftValue(String(value));
  }, [value]);

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <Label htmlFor={String(field)} className="text-sm font-medium">
          {label}
        </Label>
        <SourceBadge source={source} />
      </div>
      <Input
        id={String(field)}
        type="number"
        min={min}
        max={max}
        step={1}
        value={draftValue}
        onChange={(e) => {
          const nextValue = e.target.value;
          setDraftValue(nextValue);
          if (nextValue === "") {
            return;
          }
          const coercedValue = Number(nextValue);
          if (Number.isFinite(coercedValue)) {
            onChange(
              field,
              coercedValue as SettingsFormData[typeof field],
            );
          }
        }}
        onBlur={() => {
          const coercedValue = Number(draftValue);
          if (draftValue === "" || !Number.isFinite(coercedValue)) {
            setDraftValue(String(value));
          }
        }}
        aria-invalid={err ? true : undefined}
        className={err ? "border-destructive" : undefined}
      />
      {err && (
        <p className="text-xs text-destructive" role="alert">
          {err}
        </p>
      )}
      <p className="text-xs text-muted-foreground">{description}</p>
    </div>
  );
}

function DefaultModeField({
  formData,
  errors,
  onChange,
  source,
}: {
  formData: SettingsFormData;
  errors: SettingsErrors;
  onChange: ModelsTabProps["onChange"];
  source?: "kv" | "env" | "default";
}) {
  const err = errors.default_chat_mode;
  const modes: Array<{ value: "instant" | "thinking"; label: string }> = [
    { value: "thinking", label: "Thinking" },
    { value: "instant", label: "Instant" },
  ];
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <Label id="default_chat_mode_label" className="text-sm font-medium">
          Default chat mode
        </Label>
        <SourceBadge source={source} />
      </div>
      <div
        id="default_chat_mode"
        role="radiogroup"
        aria-labelledby="default_chat_mode_label"
        aria-describedby="default_chat_mode-desc"
        className="inline-grid grid-cols-2 rounded-md border border-input bg-background p-1"
      >
        {modes.map((mode) => (
          <button
            key={mode.value}
            type="button"
            role="radio"
            aria-checked={formData.default_chat_mode === mode.value}
            onClick={() => onChange("default_chat_mode", mode.value)}
            className={cn(
              "rounded-sm px-3 py-1.5 text-sm font-medium transition-colors",
              formData.default_chat_mode === mode.value
                ? "bg-primary text-primary-foreground"
                : "text-muted-foreground hover:bg-accent hover:text-foreground",
            )}
          >
            {mode.label}
          </button>
        ))}
      </div>
      {err && (
        <p className="text-xs text-destructive" role="alert">
          {err}
        </p>
      )}
      <p id="default_chat_mode-desc" className="text-xs text-muted-foreground">
        New chats use this mode unless the composer mode picker is pinned.
      </p>
    </div>
  );
}

export function ModelsTab({
  formData,
  errors,
  onChange,
  effectiveSources,
}: ModelsTabProps) {
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>Model endpoints</CardTitle>
          <CardDescription>
            All fields are runtime-editable. Saving overrides any env value
            at runtime; env values seed defaults at startup but do not
            enforce precedence.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <StringField
            field="ollama_embedding_url"
            label="Embedding service URL"
            placeholder="http://localhost:11434"
            description="OpenAI-compatible / Ollama / TEI URL for embeddings."
            type="url"
            formData={formData}
            errors={errors}
            onChange={onChange}
            source={effectiveSources.ollama_embedding_url}
          />
          <StringField
            field="ollama_chat_url"
            label="Thinking chat service URL"
            placeholder="http://localhost:11434"
            description="Endpoint for the larger Thinking chat model. For Docker, use http://host.docker.internal:11434 to reach a host Ollama."
            type="url"
            formData={formData}
            errors={errors}
            onChange={onChange}
            source={effectiveSources.ollama_chat_url}
          />
          <StringField
            field="instant_chat_url"
            label="Instant chat service URL"
            placeholder="http://host.docker.internal:1234"
            description="Endpoint for the smaller/faster Instant chat model, such as LM Studio on the host."
            type="url"
            formData={formData}
            errors={errors}
            onChange={onChange}
            source={effectiveSources.instant_chat_url}
          />
          <StringField
            field="reranker_url"
            label="Reranker service URL"
            placeholder="http://localhost:8082"
            description="Reranker endpoint. Leave empty to use local sentence-transformers."
            type="url"
            formData={formData}
            errors={errors}
            onChange={onChange}
            source={effectiveSources.reranker_url}
          />
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Model names</CardTitle>
          <CardDescription>
            Names are passed verbatim to the configured endpoints.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <StringField
            field="embedding_model"
            label="Embedding model"
            placeholder="microsoft/harrier-oss-v1-0.6b"
            description="Used for document embeddings."
            formData={formData}
            errors={errors}
            onChange={onChange}
            source={effectiveSources.embedding_model}
          />
          <StringField
            field="chat_model"
            label="Thinking chat model"
            placeholder="gemma-4-26b-a4b-it-apex"
            description="Larger model used for Thinking mode responses."
            formData={formData}
            errors={errors}
            onChange={onChange}
            source={effectiveSources.chat_model}
          />
          <StringField
            field="instant_chat_model"
            label="Instant chat model"
            placeholder="nvidia/nemotron-3-nano-4b"
            description="Smaller/faster model used for Instant mode responses."
            formData={formData}
            errors={errors}
            onChange={onChange}
            source={effectiveSources.instant_chat_model}
          />
          <DefaultModeField
            formData={formData}
            errors={errors}
            onChange={onChange}
            source={effectiveSources.default_chat_mode}
          />
          <StringField
            field="reranker_model"
            label="Reranker model"
            placeholder="BAAI/bge-reranker-v2-m3"
            description="Cross-encoder model used by the reranker."
            formData={formData}
            errors={errors}
            onChange={onChange}
            source={effectiveSources.reranker_model}
          />
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Instant mode tuning</CardTitle>
          <CardDescription>
            Smaller retrieval and output budgets keep Instant mode responsive.
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-6 md:grid-cols-2">
          <NumberField
            field="instant_initial_retrieval_top_k"
            label="Initial retrieval top-k"
            description="Number of candidates retrieved before instant-mode reranking."
            min={1}
            formData={formData}
            errors={errors}
            onChange={onChange}
            source={effectiveSources.instant_initial_retrieval_top_k}
          />
          <NumberField
            field="instant_reranker_top_n"
            label="Reranker top-n"
            description="Number of reranked documents kept for Instant mode."
            min={1}
            formData={formData}
            errors={errors}
            onChange={onChange}
            source={effectiveSources.instant_reranker_top_n}
          />
          <NumberField
            field="instant_memory_context_top_k"
            label="Memory context top-k"
            description="Number of memories included in Instant mode context."
            min={1}
            formData={formData}
            errors={errors}
            onChange={onChange}
            source={effectiveSources.instant_memory_context_top_k}
          />
          <NumberField
            field="instant_max_tokens"
            label="Max output tokens"
            description="Maximum completion size for Instant mode."
            min={1}
            formData={formData}
            errors={errors}
            onChange={onChange}
            source={effectiveSources.instant_max_tokens}
          />
          <NumberField
            field="thinking_max_tokens"
            label="Thinking max output tokens"
            description="Maximum completion size for Thinking mode."
            min={1}
            formData={formData}
            errors={errors}
            onChange={onChange}
            source={effectiveSources.thinking_max_tokens}
          />
        </CardContent>
      </Card>
    </div>
  );
}
