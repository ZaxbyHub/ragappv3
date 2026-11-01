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
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Loader2, ShieldAlert } from "lucide-react";
import { cn } from "@/lib/utils";
import {
  REINDEX_REQUIRED_FIELDS,
  type SettingsErrors,
  type SettingsFormData,
} from "@/stores/useSettingsStore";
import { ReindexFieldWarning } from "./ReindexFieldWarning";
import { getVault, toggleVaultMultimodalProvider } from "@/lib/api";
import { useAuthOwner } from "@/hooks/useAuthOwner";
import {
  captureAuthPrincipalGeneration,
  isCurrentAuthOwner,
  subscribeAuthPrincipal,
  type AuthOwner,
} from "@/lib/api/auth-lifecycle";

export interface ModelsTabProps {
  formData: SettingsFormData;
  errors: SettingsErrors;
  /** When provided (per-vault settings surface), shows the multimodal opt-in card. */
  vaultId?: number | null;
  onChange: <K extends keyof SettingsFormData>(
    field: K,
    value: SettingsFormData[K],
  ) => void;
  effectiveSources: Record<string, "kv" | "env" | "default">;
  /**
   * Issue #622: keys are write-only (GET returns ""), so clearing cannot go
   * through the dirty-payload save path — an empty form field equals the
   * loaded "" and is never sent. The Clear control PUTs an explicit empty
   * string through this handler instead.
   */
  onClearKey?: (field: "chat_api_key" | "instant_api_key") => Promise<void>;
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

interface OriginsFieldProps {
  field: "multimodal_allowed_model_origins";
  label: string;
  placeholder: string;
  description: string;
  formData: SettingsFormData;
  errors: SettingsErrors;
  onChange: ModelsTabProps["onChange"];
  source?: "kv" | "env" | "default";
}

function OriginsField({
  field,
  label,
  placeholder,
  description,
  formData,
  errors,
  onChange,
  source,
}: OriginsFieldProps) {
  const value = formData[field];
  const [draft, setDraft] = useState(value.join(", "));
  const err = errors[field];

  useEffect(() => {
    setDraft(value.join(", "));
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
        type="text"
        placeholder={placeholder}
        value={draft}
        onChange={(e) => {
          setDraft(e.target.value);
          const parts = e.target.value
            .split(",")
            .map((p) => p.trim())
            .filter(Boolean);
          onChange(field, parts);
        }}
        onBlur={() => setDraft(value.join(", "))}
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

interface ModeFieldProps {
  field: "multimodal_mode";
  value: "thinking" | "instant";
  disabled?: boolean;
  onChange: (v: "thinking" | "instant") => void;
  source?: "kv" | "env" | "default";
}

function MultimodalModeField({
  field,
  value,
  disabled,
  onChange,
  source,
}: ModeFieldProps) {
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <Label htmlFor={field} className="text-sm font-medium">
          Enrichment mode
        </Label>
        <SourceBadge source={source} />
      </div>
      <Select
        value={value}
        onValueChange={(v) => onChange(v as "thinking" | "instant")}
        disabled={disabled}
      >
        <SelectTrigger id={field}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="thinking">Thinking</SelectItem>
          <SelectItem value="instant">Instant</SelectItem>
        </SelectContent>
      </Select>
      <p className="text-xs text-muted-foreground">
        Larger contexts for artifact description; instant is faster/lower-cost.
      </p>
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

type VaultMultimodal = {
  multimodal_provider_enabled: boolean | null;
  effective_multimodal_enabled: boolean;
  current_user_permission: string | null;
};

type OwnerScope = {
  owner: AuthOwner;
  generation: number;
  vaultId: number | null;
};

function isVaultMultimodal(value: unknown): value is VaultMultimodal {
  if (!value || typeof value !== "object") return false;
  const vault = value as Record<string, unknown>;
  return (
    (vault.multimodal_provider_enabled === null || typeof vault.multimodal_provider_enabled === "boolean") &&
    typeof vault.effective_multimodal_enabled === "boolean" &&
    (vault.current_user_permission === null || typeof vault.current_user_permission === "string")
  );
}

export function ModelsTab(props: ModelsTabProps) {
  const owner = useAuthOwner();
  const generation = useSyncExternalStore(
    subscribeAuthPrincipal,
    captureAuthPrincipalGeneration,
    captureAuthPrincipalGeneration,
  );
  const scope = useMemo<OwnerScope>(
    () => ({ owner, generation, vaultId: props.vaultId ?? null }),
    [generation, owner, props.vaultId],
  );
  const scopeSerial = useRef(0);
  const previousScope = useRef<OwnerScope | null>(null);
  if (
    previousScope.current?.owner !== scope.owner ||
    previousScope.current?.generation !== scope.generation ||
    previousScope.current?.vaultId !== scope.vaultId
  ) {
    scopeSerial.current += 1;
    previousScope.current = scope;
  }

  return <ModelsTabContent key={`models-scope-${scopeSerial.current}`} {...props} scope={scope} />;
}

function ModelsTabContent({
  formData,
  errors,
  onChange: originalOnChange,
  effectiveSources,
  vaultId = null,
  onClearKey,
  scope,
}: ModelsTabProps & { scope: OwnerScope }) {
  // Per-vault multimodal provider opt-in (tri-state: inherit/on/off)
  const [vaultMultimodal, setVaultMultimodal] = useState<{
    multimodal_provider_enabled: boolean | null;
    effective_multimodal_enabled: boolean;
    current_user_permission?: string | null;
  } | null>(null);
  const [vaultReadState, setVaultReadState] = useState<"idle" | "loading" | "ready" | "error">(vaultId ? "loading" : "idle");
  const [vaultReadError, setVaultReadError] = useState<string | null>(null);
  const [togglingMultimodal, setTogglingMultimodal] = useState(false);
  const [clearingKey, setClearingKey] = useState<string | null>(null);
  const [clearError, setClearError] = useState<string | null>(null);
  const scopeRef = useRef(scope);
  const mountedLeaseRef = useRef<symbol | null>(null);
  const readIntentRef = useRef<symbol | null>(null);
  const readPendingRef = useRef<symbol | null>(null);
  const toggleIntentRef = useRef<symbol | null>(null);
  const togglePendingRef = useRef<symbol | null>(null);
  const clearIntentRef = useRef<symbol | null>(null);
  const clearPendingRef = useRef<symbol | null>(null);
  scopeRef.current = scope;

  useEffect(() => {
    const lease = Symbol("models-mounted");
    mountedLeaseRef.current = lease;
    return () => {
      if (mountedLeaseRef.current === lease) mountedLeaseRef.current = null;
    };
  }, []);

  const isLive = useCallback((lease: symbol | null = mountedLeaseRef.current) =>
    lease !== null && mountedLeaseRef.current === lease && scopeRef.current === scope &&
    isCurrentAuthOwner(scope.owner) && captureAuthPrincipalGeneration() === scope.generation,
    [scope]);

  const onChange = <K extends keyof SettingsFormData>(field: K, value: SettingsFormData[K]) => {
    if (isLive()) originalOnChange(field, value);
  };

  const loadVault = useCallback(async (lease: symbol | null, supersede = false) => {
    if (!isLive(lease) || (readPendingRef.current && !supersede)) return;
    if (!vaultId) return;
    const intent = Symbol("vault-read");
    readIntentRef.current = intent;
    readPendingRef.current = intent;
    const currentRead = () => isLive(lease) && readIntentRef.current === intent;
    setVaultReadState((current) => currentRead() ? "loading" : current);
    setVaultReadError((current) => currentRead() ? null : current);
    try {
      const vault = await getVault(vaultId);
      if (!currentRead()) return;
      if (!isVaultMultimodal(vault)) throw new Error("Malformed vault response");
      setVaultMultimodal((current) => currentRead() ? vault : current);
      setVaultReadState((current) => currentRead() ? "ready" : current);
    } catch (error) {
      if (!currentRead()) return;
      const message = error instanceof Error ? error.message : "Could not load vault settings. Retry to continue.";
      setVaultReadState((current) => currentRead() ? "error" : current);
      setVaultReadError((current) => currentRead() ? message : current);
    } finally {
      if (readPendingRef.current === intent) readPendingRef.current = null;
    }
  }, [isLive, vaultId]);

  useEffect(() => {
    const lease = mountedLeaseRef.current;
    void loadVault(lease, true);
    const intent = readIntentRef.current;
    return () => {
      if (readIntentRef.current === intent) readIntentRef.current = null;
      if (readPendingRef.current === intent) readPendingRef.current = null;
    };
  }, [loadVault]);

  const handleVaultMultimodalToggle = async (enabled: boolean | null): Promise<void> => {
    if (!vaultId || !canToggleMultimodal || togglePendingRef.current || !isLive()) return;
    const lease = mountedLeaseRef.current;
    const intent = Symbol("vault-toggle");
    toggleIntentRef.current = intent;
    togglePendingRef.current = intent;
    // A mutation supersedes an older Retry before the physical write starts.
    readIntentRef.current = null;
    readPendingRef.current = null;
    const currentToggle = () => isLive(lease) && toggleIntentRef.current === intent;
    setTogglingMultimodal((current) => currentToggle() ? true : current);
    try {
      try {
        const updated = await toggleVaultMultimodalProvider(vaultId, { enabled });
        if (!currentToggle()) return;
        if (!isVaultMultimodal(updated)) throw new Error("Malformed vault response");
        setVaultMultimodal((current) => currentToggle() ? updated : current);
      } catch {
        if (!currentToggle()) return;
      }
      if (currentToggle()) await loadVault(lease, true);
    } finally {
      if (togglePendingRef.current === intent) togglePendingRef.current = null;
      setTogglingMultimodal((current) => currentToggle() ? false : current);
    }
  };

  const canToggleMultimodal = vaultMultimodal?.current_user_permission === "admin";

  const handleClearKey = async (field: "chat_api_key" | "instant_api_key") => {
    if (!onClearKey || clearingKey || clearPendingRef.current || !isLive()) return;
    const lease = mountedLeaseRef.current;
    const intent = Symbol("models-clear");
    clearIntentRef.current = intent;
    clearPendingRef.current = intent;
    setClearError(null);
    setClearingKey(field);
    try {
      await onClearKey(field);
      if (isLive(lease) && clearIntentRef.current === intent) onChange(field, "");
    } catch {
      if (isLive(lease) && clearIntentRef.current === intent) {
        setClearError((current) => isLive(lease) && clearIntentRef.current === intent ? "Could not clear the API key. Retry to continue." : current);
      }
    } finally {
      if (isLive(lease) && clearIntentRef.current === intent) {
        if (clearPendingRef.current === intent) clearPendingRef.current = null;
        setClearingKey((current) => isLive(lease) && clearIntentRef.current === intent ? null : current);
      }
    }
  };

  const retryVaultRead = () => {
    const lease = mountedLeaseRef.current;
    if (!isLive(lease) || togglePendingRef.current || readPendingRef.current) return;
    void loadVault(lease);
  };

  return (
    <div className="space-y-4">
      {clearError && <p role="alert" className="text-sm text-destructive">{clearError}</p>}
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
            description="Endpoint for the Thinking chat model (any OpenAI-compatible server: Ollama, LM Studio, vLLM, or a remote API)."
            type="url"
            formData={formData}
            errors={errors}
            onChange={onChange}
            source={effectiveSources.ollama_chat_url}
          />
          <StringField
            field="instant_chat_url"
            label="Instant chat service URL"
            placeholder="http://your-inference-host:1234"
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
            placeholder="e.g. gemma4:26b"
            description="Larger model used for Thinking mode responses."
            formData={formData}
            errors={errors}
            onChange={onChange}
            source={effectiveSources.chat_model}
          />
          <div className="space-y-2">
            <Label htmlFor="settings-chat-api-key">Thinking API key</Label>
            <div className="flex items-center gap-2">
              <Input
                id="settings-chat-api-key"
                type="password"
                autoComplete="off"
                placeholder={
                  formData.chat_api_key_set
                    ? "Stored — type a new key to replace it"
                    : "Optional — for remote providers that require one"
                }
                value={formData.chat_api_key}
                onChange={(e) => onChange("chat_api_key", e.target.value)}
              />
              {formData.chat_api_key_set && onClearKey && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={clearingKey !== null}
                  aria-label="Clear thinking API key"
                  onClick={() => void handleClearKey("chat_api_key")}
                >
                  Clear
                </Button>
              )}
            </div>
            <p className="text-xs text-muted-foreground">
              {formData.chat_api_key_set
                ? "A key is stored (write-only — it is never shown). Type a new key to rotate it, or use Clear to remove it."
                : "Sent as a Bearer token to the thinking endpoint. Leave empty for local servers."}
            </p>
          </div>
          <div className="space-y-2">
            <Label htmlFor="settings-instant-api-key">Instant API key</Label>
            <div className="flex items-center gap-2">
              <Input
                id="settings-instant-api-key"
                type="password"
                autoComplete="off"
                placeholder={
                  formData.instant_api_key_set
                    ? "Stored — type a new key to replace it"
                    : "Optional — defaults to the thinking endpoint's key"
                }
                value={formData.instant_api_key}
                onChange={(e) => onChange("instant_api_key", e.target.value)}
              />
              {formData.instant_api_key_set && onClearKey && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={clearingKey !== null}
                  aria-label="Clear instant API key"
                  onClick={() => void handleClearKey("instant_api_key")}
                >
                  Clear
                </Button>
              )}
            </div>
            <p className="text-xs text-muted-foreground">
              {formData.instant_api_key_set
                ? "A key is stored (write-only — it is never shown). Type a new key to rotate it, or use Clear to remove it."
                : "Sent as a Bearer token to the instant endpoint; defaults to the thinking endpoint's key."}
            </p>
          </div>
          <StringField
            field="instant_chat_model"
            label="Instant chat model"
            placeholder="e.g. minicpm5-2b"
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
          <div className="flex items-center space-x-2">
            <Checkbox
              id="instant-enable-thinking"
              checked={formData.instant_enable_thinking}
              onCheckedChange={(checked) => onChange("instant_enable_thinking", checked === true)}
            />
            <Label htmlFor="instant-enable-thinking" className="text-sm font-normal">
              Enable thinking in Instant mode
            </Label>
          </div>
          <p className="text-xs text-muted-foreground">
            Off (default) sends the model-family no-think control (enable_thinking=false for
            Qwen-family models; none for unrecognized families). On sends no control so the
            model template default governs.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ShieldAlert className="w-4 h-4" />
            Multimodal artifact enrichment
          </CardTitle>
          <CardDescription>
            Enrich typed image/chart/table/equation atoms from documents with
            a configured multimodal model. Off by default; provider must be
            exact-origin allowlisted and each vault must opt in.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <Alert variant="warning">
            <AlertTitle>External data egress</AlertTitle>
            <AlertDescription>
              Enabling this sends artifact bytes and surrounding evidence from
              opted-in vaults to the configured provider. Only enable with a
              provider you trust and whose origin appears in the allowlist.
            </AlertDescription>
          </Alert>

          <div className="flex items-center gap-2">
            <Checkbox
              id="multimodal-enrichment-enabled"
              checked={formData.multimodal_enrichment_enabled}
              onCheckedChange={(v) =>
                onChange("multimodal_enrichment_enabled", Boolean(v))
              }
            />
            <Label
              htmlFor="multimodal-enrichment-enabled"
              className="text-sm font-normal"
            >
              Enable multimodal enrichment globally
            </Label>
          </div>

          <div className="flex items-center gap-2">
            <Checkbox
              id="multimodal-query-vision-enabled"
              checked={formData.multimodal_query_vision_enabled}
              onCheckedChange={(v) =>
                onChange("multimodal_query_vision_enabled", Boolean(v))
              }
            />
            <Label
              htmlFor="multimodal-query-vision-enabled"
              className="text-sm font-normal"
            >
              Enable retrieval-first VLM synthesis at query time
            </Label>
            <p className="text-xs text-muted-foreground">
              Runs after normal retrieval/rerank/distill/pack against only the
              retrieved artifact sources. Off by default.
            </p>
          </div>

          <StringField
            field="multimodal_chat_url"
            label="Multimodal provider URL"
            placeholder="https://provider.example.com"
            description="OpenAI-compatible /v1/chat/completions base URL. Must exactly match an allowlisted origin (scheme://host:port)."
            type="url"
            formData={formData}
            errors={errors}
            onChange={onChange}
            source={effectiveSources.multimodal_chat_url}
          />
          <StringField
            field="multimodal_model"
            label="Multimodal model name"
            placeholder="gpt-4o"
            description="Model identifier passed verbatim to the provider."
            formData={formData}
            errors={errors}
            onChange={onChange}
            source={effectiveSources.multimodal_model}
          />
          <OriginsField
            field="multimodal_allowed_model_origins"
            label="Allowed provider origins"
            placeholder="https://provider.example.com, http://localhost:11434"
            description="Comma-separated exact origins (scheme://host:port). Empty = disabled; SSRF guard is enforced independently for allowlisted origins."
            formData={formData}
            errors={errors}
            onChange={onChange}
            source={effectiveSources.multimodal_allowed_model_origins}
          />
          <MultimodalModeField
            field="multimodal_mode"
            value={formData.multimodal_mode}
            onChange={(v) => onChange("multimodal_mode", v)}
            source={effectiveSources.multimodal_mode}
          />

          <div className="grid gap-6 md:grid-cols-2">
            <NumberField
              field="multimodal_timeout_seconds"
              label="Provider timeout (s)"
              description="Per-request timeout for the multimodal provider."
              min={1}
              formData={formData}
              errors={errors}
              onChange={onChange}
              source={effectiveSources.multimodal_timeout_seconds}
            />
            <NumberField
              field="multimodal_concurrency"
              label="Concurrency"
              description="Max concurrent provider requests."
              min={1}
              formData={formData}
              errors={errors}
              onChange={onChange}
              source={effectiveSources.multimodal_concurrency}
            />
            <NumberField
              field="multimodal_max_assets_per_batch"
              label="Max assets per batch"
              description="Max artifact assets sent in one request."
              min={1}
              formData={formData}
              errors={errors}
              onChange={onChange}
              source={effectiveSources.multimodal_max_assets_per_batch}
            />
            <NumberField
              field="multimodal_max_asset_bytes"
              label="Max asset bytes"
              description="Per-asset size cap before decoding."
              min={1}
              formData={formData}
              errors={errors}
              onChange={onChange}
              source={effectiveSources.multimodal_max_asset_bytes}
            />
            <NumberField
              field="multimodal_max_total_payload_bytes"
              label="Max total payload bytes"
              description="Aggregate bytes cap for the whole request."
              min={1}
              formData={formData}
              errors={errors}
              onChange={onChange}
              source={effectiveSources.multimodal_max_total_payload_bytes}
            />
            <NumberField
              field="multimodal_max_pixels"
              label="Max decoded pixels"
              description="Upper bound on decoded image dimensions."
              min={1}
              formData={formData}
              errors={errors}
              onChange={onChange}
              source={effectiveSources.multimodal_max_pixels}
            />
            <NumberField
              field="multimodal_max_attempts"
              label="Max retry attempts"
              description="Retry cap for transient provider failures."
              min={1}
              formData={formData}
              errors={errors}
              onChange={onChange}
              source={effectiveSources.multimodal_max_attempts}
            />
          </div>
        </CardContent>
      </Card>

      {vaultId && (
        <Card>
          <CardHeader>
            <CardTitle>Multimodal provider opt-in (this vault)</CardTitle>
            <CardDescription>
              Decide whether this vault's artifacts may be sent to the
              configured multimodal provider.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <Alert variant="warning">
              <AlertTitle>Vault data egress</AlertTitle>
              <AlertDescription>
                Enabling external providers may receive this vault&apos;s
                artifacts. Only enable when the global feature and allowlist
                are configured.
              </AlertDescription>
            </Alert>
            {vaultReadState === "loading" && !vaultMultimodal && (
              <p role="status">Loading vault multimodal</p>
            )}
            {vaultReadState === "error" && (
              <div role="alert" className="space-y-2">
                <p>{vaultReadError}</p>
                {!togglingMultimodal && (<Button type="button" variant="outline" onClick={retryVaultRead}>
                  Retry
                </Button>)}
              </div>
            )}
            {vaultMultimodal && (
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium">Override</span>
                </div>
                <div className="flex flex-col gap-2">
                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="radio"
                      name="vault-multimodal"
                      checked={
                        vaultMultimodal.multimodal_provider_enabled === null
                      }
                      onChange={() => void handleVaultMultimodalToggle(null)}
                      disabled={togglingMultimodal || !canToggleMultimodal}
                      className="h-4 w-4"
                    />
                    Inherit global
                    {vaultMultimodal.multimodal_provider_enabled === null && (
                      <span className="text-xs text-muted-foreground">
                        (not opted in — fail-closed; select “On” to send)
                      </span>
                    )}
                  </label>
                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="radio"
                      name="vault-multimodal"
                      checked={
                        vaultMultimodal.multimodal_provider_enabled === true
                      }
                      onChange={() => void handleVaultMultimodalToggle(true)}
                      disabled={togglingMultimodal || !canToggleMultimodal}
                      className="h-4 w-4"
                    />
                    On
                  </label>
                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="radio"
                      name="vault-multimodal"
                      checked={
                        vaultMultimodal.multimodal_provider_enabled === false
                      }
                      onChange={() => void handleVaultMultimodalToggle(false)}
                      disabled={togglingMultimodal || !canToggleMultimodal}
                      className="h-4 w-4"
                    />
                    Off
                  </label>
                </div>
              </div>
            )}
            {!canToggleMultimodal && vaultMultimodal && (
              <p className="text-xs text-muted-foreground">
                Only vault admins can change this.
              </p>
            )}
            {togglingMultimodal && (
              <Loader2 className="w-4 h-4 animate-spin text-muted-foreground" />
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
