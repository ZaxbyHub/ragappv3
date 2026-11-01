/**
 * Settings → Wiki & Curator tab.
 *
 * Wires every wiki + curator field defined in the backend config to the
 * settings store. Curator inputs are visually grouped and explain that
 * "claims become active only when source quotes are verified", so the
 * operator never thinks the curator can write authoritative claims
 * without provenance.
 *
 * The "Test connection" button is wired to POST /settings/curator/test.
 * The local-model UX hint is rendered inline so operators know the
 * ALLOW_LOCAL_CURATOR=1 opt-in exists.
 *
 * When vaultId is provided, a per-vault enrichment override toggle is
 * also shown (admin vault members only).
 */
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Loader2, ShieldCheck, AlertTriangle, CheckCircle2, XCircle } from "lucide-react";
import { NumberInput } from "./NumberInput";
import {
  testCuratorConnection,
  toggleVaultEnrichment,
  getVault,
} from "@/lib/api";
import type { CuratorTestResult } from "@/lib/api";
import type { SettingsErrors, SettingsFormData } from "@/stores/useSettingsStore";
import { useAuthOwner } from "@/hooks/useAuthOwner";
import {
  captureAuthPrincipalGeneration,
  isCurrentAuthOwner,
  subscribeAuthPrincipal,
  type AuthOwner,
} from "@/lib/api/auth-lifecycle";

export interface WikiCuratorSettingsProps {
  formData: SettingsFormData;
  errors: SettingsErrors;
  vaultId?: number | null;
  onChange: <K extends keyof SettingsFormData>(
    field: K,
    value: SettingsFormData[K],
  ) => void;
}

type OwnerScope = { owner: AuthOwner; generation: number; vaultId: number | null };
type VaultEnrichment = {
  enrichment_enabled: boolean | null;
  effective_enrichment_enabled: boolean;
  current_user_permission: string | null;
};

function isVaultEnrichment(value: unknown): value is VaultEnrichment {
  if (!value || typeof value !== "object") return false;
  const vault = value as Record<string, unknown>;
  return (
    (vault.enrichment_enabled === null || typeof vault.enrichment_enabled === "boolean") &&
    typeof vault.effective_enrichment_enabled === "boolean" &&
    (vault.current_user_permission === null || typeof vault.current_user_permission === "string")
  );
}

export function WikiCuratorSettings(props: WikiCuratorSettingsProps) {
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
  return <WikiCuratorSettingsContent key={`curator-scope-${scopeSerial.current}`} {...props} scope={scope} />;
}

function WikiCuratorSettingsContent({
  formData,
  errors,
  vaultId,
  onChange: originalOnChange,
  scope,
}: WikiCuratorSettingsProps & { scope: OwnerScope }) {
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<CuratorTestResult | null>(null);

  // Per-vault enrichment override state
  const [vaultEnrichment, setVaultEnrichment] = useState<VaultEnrichment | null>(null);
  const [togglingEnrichment, setTogglingEnrichment] = useState(false);
  const [vaultReadState, setVaultReadState] = useState<"idle" | "loading" | "ready" | "error">(vaultId ? "loading" : "idle");
  const [vaultReadError, setVaultReadError] = useState<string | null>(null);
  const scopeRef = useRef(scope);
  const mountedLeaseRef = useRef<symbol | null>(null);
  const readIntentRef = useRef<symbol | null>(null);
  const readPendingRef = useRef<symbol | null>(null);
  const toggleIntentRef = useRef<symbol | null>(null);
  const togglePendingRef = useRef<symbol | null>(null);
  const testIntentRef = useRef<symbol | null>(null);
  const testPendingRef = useRef<{ intent: symbol; config: { enabled: boolean; url: string; model: string } } | null>(null);
  scopeRef.current = scope;

  useEffect(() => {
    const lease = Symbol("curator-mounted");
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
      if (!isVaultEnrichment(vault)) throw new Error("Malformed vault response");
      setVaultEnrichment((current) => currentRead() ? vault : current);
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

  const handleToggleEnrichment = async (checked: boolean): Promise<void> => {
    if (!vaultId || !canToggleEnrichment || togglePendingRef.current || !isLive()) return;
    const lease = mountedLeaseRef.current;
    const intent = Symbol("vault-toggle");
    toggleIntentRef.current = intent;
    togglePendingRef.current = intent;
    // A mutation supersedes an older Retry before the physical write starts.
    readIntentRef.current = null;
    readPendingRef.current = null;
    const currentToggle = () => isLive(lease) && toggleIntentRef.current === intent;
    setTogglingEnrichment((current) => currentToggle() ? true : current);
    try {
      try {
        const updated = await toggleVaultEnrichment(vaultId, { enabled: checked });
        if (!currentToggle()) return;
        if (!isVaultEnrichment(updated)) throw new Error("Malformed vault response");
        setVaultEnrichment((current) => currentToggle() ? updated : current);
      } catch {
        if (!currentToggle()) return;
      }
      if (currentToggle()) await loadVault(lease, true);
    } finally {
      if (togglePendingRef.current === intent) togglePendingRef.current = null;
      setTogglingEnrichment((current) => currentToggle() ? false : current);
    }
  };

  const canToggleEnrichment = vaultEnrichment?.current_user_permission === "admin";

  const testConfig = useMemo(
    () => ({ enabled: formData.wiki_llm_curator_enabled, url: formData.wiki_llm_curator_url, model: formData.wiki_llm_curator_model }),
    [formData.wiki_llm_curator_enabled, formData.wiki_llm_curator_model, formData.wiki_llm_curator_url],
  );
  const testConfigRef = useRef(testConfig);
  testConfigRef.current = testConfig;

  // Clear any stale test result when the operator toggles curator off,
  // changes the URL, or changes the model — the previous OK / error no
  // longer reflects the current configuration and would mislead.
  useEffect(() => {
    const pending = testPendingRef.current;
    // Do not clear a replacement test dispatched between render and this
    // effect. Only the pending test that belongs to an older config retires.
    if (pending && pending.config !== testConfig) {
      if (testIntentRef.current === pending.intent) testIntentRef.current = null;
      if (testPendingRef.current === pending) testPendingRef.current = null;
    }
    if ((!pending || pending.config !== testConfig) && isLive()) {
      setTesting(false);
      setTestResult(null);
    }
  }, [isLive, testConfig, scope]);

  const handleTest = async () => {
    // A retained callback from an earlier URL/model render must not begin a
    // request after the current config has changed.
    if (testPendingRef.current || testConfigRef.current !== testConfig || !isLive()) return;
    const lease = mountedLeaseRef.current;
    const intent = Symbol("curator-test");
    const config = testConfig;
    testIntentRef.current = intent;
    testPendingRef.current = { intent, config };
    // Capture the URL/model snapshot we are testing. If the operator
    // edits the URL/model while the request is in flight, the
    // useEffect below will null out testResult; we additionally
    // guard the post-await write so a late response can't repaint
    // a stale OK label against the now-changed config.
    const requestedUrl = config.url;
    const requestedModel = config.model;
    setTesting(true);
    setTestResult(null);
    try {
      const out = await testCuratorConnection(requestedUrl, requestedModel);
      if (isLive(lease) && testIntentRef.current === intent && testConfigRef.current === config) {
        setTestResult((current) => isLive(lease) && testIntentRef.current === intent && testConfigRef.current === config ? out : current);
      }
    } catch (e) {
      if (isLive(lease) && testIntentRef.current === intent && testConfigRef.current === config) {
        const errorResult: CuratorTestResult = {
          ok: false,
          model: requestedModel,
          latency_ms: null,
          error: e instanceof Error ? e.message : "Test failed",
        };
        setTestResult((current) => isLive(lease) && testIntentRef.current === intent && testConfigRef.current === config ? errorResult : current);
      }
    } finally {
      if (isLive(lease) && testIntentRef.current === intent) {
        if (testPendingRef.current?.intent === intent) testPendingRef.current = null;
        setTesting((current) => isLive(lease) && testIntentRef.current === intent ? false : current);
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
      <Card>
        <CardHeader>
          <CardTitle>Wiki / Knowledge Compiler</CardTitle>
          <CardDescription>
            Toggle when the wiki compiler runs and which sources it processes.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex items-center gap-2">
            <Checkbox
              id="wiki-enabled"
              checked={formData.wiki_enabled}
              onCheckedChange={(v) => onChange("wiki_enabled", Boolean(v))}
            />
            <Label htmlFor="wiki-enabled" className="text-sm font-normal">Wiki enabled</Label>
          </div>
          <div className="flex items-center gap-2">
            <Checkbox
              id="wiki-compile-on-ingest"
              checked={formData.wiki_compile_on_ingest}
              onCheckedChange={(v) =>
                onChange("wiki_compile_on_ingest", Boolean(v))
              }
            />
            <Label htmlFor="wiki-compile-on-ingest" className="text-sm font-normal">Compile on document ingest</Label>
          </div>
          <div className="flex items-center gap-2">
            <Checkbox
              id="wiki-compile-on-query"
              checked={formData.wiki_compile_on_query}
              onCheckedChange={(v) =>
                onChange("wiki_compile_on_query", Boolean(v))
              }
            />
            <Label htmlFor="wiki-compile-on-query" className="text-sm font-normal">Compile on chat query</Label>
          </div>
          <div className="flex items-center gap-2">
            <Checkbox
              id="wiki-compile-after-indexing"
              checked={formData.wiki_compile_after_indexing}
              onCheckedChange={(v) =>
                onChange("wiki_compile_after_indexing", Boolean(v))
              }
            />
            <Label htmlFor="wiki-compile-after-indexing" className="text-sm font-normal">Compile after document indexing finishes</Label>
          </div>
          <div className="flex items-center gap-2">
            <Checkbox
              id="wiki-lint-enabled"
              checked={formData.wiki_lint_enabled}
              onCheckedChange={(v) => onChange("wiki_lint_enabled", Boolean(v))}
            />
            <Label htmlFor="wiki-lint-enabled" className="text-sm font-normal">Wiki lint enabled</Label>
          </div>
          {vaultId && vaultReadState === "loading" && !vaultEnrichment && (
            <p role="status">Loading vault enrichment</p>
          )}
          {vaultId && vaultReadState === "error" && (
            <div role="alert" className="space-y-2">
              <p>{vaultReadError}</p>
              {!togglingEnrichment && (<Button type="button" variant="outline" onClick={retryVaultRead}>
                Retry
              </Button>)}
            </div>
          )}
          {vaultId && vaultEnrichment && (
            <div className="flex items-center gap-2 pt-2 border-t mt-3">
              <Checkbox
                id="vault-enrichment-enabled"
                checked={vaultEnrichment?.effective_enrichment_enabled ?? false}
                onCheckedChange={(v) => handleToggleEnrichment(Boolean(v))}
                disabled={togglingEnrichment || !canToggleEnrichment}
              />
              <Label
                htmlFor="vault-enrichment-enabled"
                className="text-sm font-normal flex flex-col gap-0.5"
              >
                <span>Per-vault document enrichment</span>
                {!canToggleEnrichment && vaultEnrichment && (
                  <span className="text-xs text-muted-foreground">
                    {vaultEnrichment.enrichment_enabled === null
                      ? `Inherits global setting (${vaultEnrichment.effective_enrichment_enabled ? "on" : "off"})`
                      : vaultEnrichment.enrichment_enabled
                        ? "Vault override: on"
                        : "Vault override: off"}
                  </span>
                )}
                {canToggleEnrichment && vaultEnrichment && vaultEnrichment.enrichment_enabled === null && (
                  <span className="text-xs text-muted-foreground">
                    No vault override — inherits global
                  </span>
                )}
              </Label>
              {togglingEnrichment && (
                <Loader2 className="w-3 h-3 animate-spin text-muted-foreground" />
              )}
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ShieldCheck className="w-4 h-4" />
            LLM Wiki Curator (optional)
          </CardTitle>
          <CardDescription>
            The curator proposes wiki updates from a small local model.
            Claims become active only when source quotes are verified —
            unsupported output never becomes an active claim.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex items-center gap-2">
            <Checkbox
              id="wiki-llm-curator-enabled"
              checked={formData.wiki_llm_curator_enabled}
              onCheckedChange={(v) =>
                onChange("wiki_llm_curator_enabled", Boolean(v))
              }
            />
            <Label htmlFor="wiki-llm-curator-enabled" className="text-sm font-normal">Enable LLM curator</Label>
          </div>

          <div className="space-y-1">
            <Label htmlFor="curator-url" className="block">
              Endpoint URL
            </Label>
            <Input
              id="curator-url"
              type="url"
              placeholder="https://localhost:11434"
              value={formData.wiki_llm_curator_url}
              onChange={(e) =>
                onChange("wiki_llm_curator_url", e.target.value)
              }
              disabled={!formData.wiki_llm_curator_enabled}
              data-invalid={errors.wiki_llm_curator_url ? "true" : undefined}
              aria-invalid={errors.wiki_llm_curator_url ? true : undefined}
            />
            {errors.wiki_llm_curator_url && (
              <p role="alert" className="text-xs text-destructive">
                {errors.wiki_llm_curator_url}
              </p>
            )}
            <p className="text-xs text-muted-foreground">
              OpenAI-compatible /v1/chat/completions base URL. Local
              endpoints (loopback / RFC1918) require{" "}
              <code className="rounded-sm bg-muted px-1">ALLOW_LOCAL_CURATOR=1</code>.
            </p>
          </div>

          <div className="space-y-1">
            <Label htmlFor="curator-model" className="block">
              Model name
            </Label>
            <Input
              id="curator-model"
              type="text"
              placeholder="qwen2.5:1.5b"
              value={formData.wiki_llm_curator_model}
              onChange={(e) =>
                onChange("wiki_llm_curator_model", e.target.value)
              }
              disabled={!formData.wiki_llm_curator_enabled}
              data-invalid={errors.wiki_llm_curator_model ? "true" : undefined}
              aria-invalid={errors.wiki_llm_curator_model ? true : undefined}
            />
            {errors.wiki_llm_curator_model && (
              <p role="alert" className="text-xs text-destructive">
                {errors.wiki_llm_curator_model}
              </p>
            )}
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div className="space-y-1">
              <Label htmlFor="curator-temp" className="block">
                Temperature (0.0–1.0)
              </Label>
              <NumberInput
                id="curator-temp"
                value={formData.wiki_llm_curator_temperature}
                onCommit={(v) =>
                  onChange("wiki_llm_curator_temperature", v ?? 0)
                }
                error={errors.wiki_llm_curator_temperature}
                disabled={!formData.wiki_llm_curator_enabled}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="curator-max-input" className="block">
                Max input chars (1000–24000)
              </Label>
              <NumberInput
                id="curator-max-input"
                parseAs="int"
                value={formData.wiki_llm_curator_max_input_chars}
                onCommit={(v) =>
                  onChange("wiki_llm_curator_max_input_chars", v ?? 6000)
                }
                error={errors.wiki_llm_curator_max_input_chars}
                disabled={!formData.wiki_llm_curator_enabled}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="curator-max-output" className="block">
                Max output tokens
              </Label>
              <NumberInput
                id="curator-max-output"
                parseAs="int"
                value={formData.wiki_llm_curator_max_output_tokens}
                onCommit={(v) =>
                  onChange("wiki_llm_curator_max_output_tokens", v ?? 2048)
                }
                error={errors.wiki_llm_curator_max_output_tokens}
                disabled={!formData.wiki_llm_curator_enabled}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="curator-timeout" className="block">
                Timeout seconds (10–600)
              </Label>
              <NumberInput
                id="curator-timeout"
                value={formData.wiki_llm_curator_timeout_sec}
                onCommit={(v) =>
                  onChange("wiki_llm_curator_timeout_sec", v ?? 120)
                }
                error={errors.wiki_llm_curator_timeout_sec}
                disabled={!formData.wiki_llm_curator_enabled}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="curator-concurrency" className="block">
                Concurrency (1–4)
              </Label>
              <NumberInput
                id="curator-concurrency"
                parseAs="int"
                value={formData.wiki_llm_curator_concurrency}
                onCommit={(v) =>
                  onChange("wiki_llm_curator_concurrency", v ?? 1)
                }
                error={errors.wiki_llm_curator_concurrency}
                disabled={!formData.wiki_llm_curator_enabled}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="curator-mode" className="block">
                Mode
              </Label>
              <Select
                value={formData.wiki_llm_curator_mode}
                onValueChange={(v) =>
                  onChange("wiki_llm_curator_mode", v)
                }
                disabled={!formData.wiki_llm_curator_enabled}
              >
                <SelectTrigger id="curator-mode">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="draft">draft (always needs review)</SelectItem>
                  <SelectItem value="active_if_verified">active if verified</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <Checkbox
              id="wiki-llm-curator-require-quote-match"
              checked={formData.wiki_llm_curator_require_quote_match}
              onCheckedChange={(v) =>
                onChange(
                  "wiki_llm_curator_require_quote_match",
                  Boolean(v),
                )
              }
              disabled={!formData.wiki_llm_curator_enabled}
            />
            <Label htmlFor="wiki-llm-curator-require-quote-match" className="text-sm font-normal">Require source-quote match</Label>
          </div>
          <div className="flex items-center gap-2">
            <Checkbox
              id="wiki-llm-curator-require-chunk-id"
              checked={formData.wiki_llm_curator_require_chunk_id}
              onCheckedChange={(v) =>
                onChange("wiki_llm_curator_require_chunk_id", Boolean(v))
              }
              disabled={!formData.wiki_llm_curator_enabled}
            />
            <Label htmlFor="wiki-llm-curator-require-chunk-id" className="text-sm font-normal">Require chunk ID</Label>
          </div>
          <div className="flex items-center gap-2">
            <Checkbox
              id="wiki-llm-curator-run-on-ingest"
              checked={formData.wiki_llm_curator_run_on_ingest}
              onCheckedChange={(v) =>
                onChange("wiki_llm_curator_run_on_ingest", Boolean(v))
              }
              disabled={!formData.wiki_llm_curator_enabled}
            />
            <Label htmlFor="wiki-llm-curator-run-on-ingest" className="text-sm font-normal">Run on ingest</Label>
          </div>
          <div className="flex items-center gap-2">
            <Checkbox
              id="wiki-llm-curator-run-on-query"
              checked={formData.wiki_llm_curator_run_on_query}
              onCheckedChange={(v) =>
                onChange("wiki_llm_curator_run_on_query", Boolean(v))
              }
              disabled={!formData.wiki_llm_curator_enabled}
            />
            <Label htmlFor="wiki-llm-curator-run-on-query" className="text-sm font-normal">Run on query (default off)</Label>
          </div>
          <div className="flex items-center gap-2">
            <Checkbox
              id="wiki-llm-curator-run-on-manual"
              checked={formData.wiki_llm_curator_run_on_manual}
              onCheckedChange={(v) =>
                onChange("wiki_llm_curator_run_on_manual", Boolean(v))
              }
              disabled={!formData.wiki_llm_curator_enabled}
            />
            <Label htmlFor="wiki-llm-curator-run-on-manual" className="text-sm font-normal">Run on manual / recompile</Label>
          </div>

          <div className="flex items-center gap-3 pt-2 border-t">
            <Button
              size="sm"
              variant="outline"
              onClick={handleTest}
              disabled={
                testing ||
                !formData.wiki_llm_curator_url.trim() ||
                !formData.wiki_llm_curator_model.trim()
              }
            >
              {testing && <Loader2 className="w-4 h-4 mr-1 animate-spin" />}
              Test curator connection
            </Button>
            {testResult && (
              <div
                className={
                  testResult.ok
                    ? "flex items-center gap-1 text-xs text-success"
                    : "flex items-center gap-1 text-xs text-destructive"
                }
                role="status"
              >
                {testResult.ok ? (
                  <>
                    <CheckCircle2 className="w-3 h-3" />
                    OK · {testResult.latency_ms}ms
                  </>
                ) : (
                  <>
                    <XCircle className="w-3 h-3" />
                    {testResult.error ?? "Failed"}
                  </>
                )}
              </div>
            )}
          </div>
          <p className="flex items-start gap-1 text-xs text-muted-foreground">
            <AlertTriangle className="w-3 h-3 mt-0.5" />
            The curator proposes wiki updates. Claims become active only
            when source quotes are verified.
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
