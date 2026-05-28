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
 */
import { useEffect, useRef, useState } from "react";
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
import { testCuratorConnection } from "@/lib/api";

/**
 * Tiny "always reflect the latest value" ref helper. Used by handleTest
 * so the post-await guard can compare the URL/model the test was
 * launched with against whatever the operator has now typed.
 */
function useLatestRef<T>(value: T) {
  const ref = useRef(value);
  ref.current = value;
  return ref;
}
import type { CuratorTestResult } from "@/lib/api";
import type { SettingsErrors, SettingsFormData } from "@/stores/useSettingsStore";

export interface WikiCuratorSettingsProps {
  formData: SettingsFormData;
  errors: SettingsErrors;
  onChange: <K extends keyof SettingsFormData>(
    field: K,
    value: SettingsFormData[K],
  ) => void;
}

export function WikiCuratorSettings({
  formData,
  errors,
  onChange,
}: WikiCuratorSettingsProps) {
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<CuratorTestResult | null>(null);

  // Clear any stale test result when the operator toggles curator off,
  // changes the URL, or changes the model — the previous OK / error no
  // longer reflects the current configuration and would mislead.
  useEffect(() => {
    setTestResult(null);
  }, [
    formData.wiki_llm_curator_enabled,
    formData.wiki_llm_curator_url,
    formData.wiki_llm_curator_model,
  ]);

  const handleTest = async () => {
    // Capture the URL/model snapshot we are testing. If the operator
    // edits the URL/model while the request is in flight, the
    // useEffect below will null out testResult; we additionally
    // guard the post-await write so a late response can't repaint
    // a stale OK label against the now-changed config.
    const requestedUrl = formData.wiki_llm_curator_url;
    const requestedModel = formData.wiki_llm_curator_model;
    setTesting(true);
    setTestResult(null);
    try {
      const out = await testCuratorConnection(requestedUrl, requestedModel);
      // Latest formData read AFTER await — guard against in-flight edits.
      const latest = useFormDataSnapshot.current;
      if (
        latest.url === requestedUrl &&
        latest.model === requestedModel
      ) {
        setTestResult(out);
      }
    } catch (e) {
      const latest = useFormDataSnapshot.current;
      if (
        latest.url === requestedUrl &&
        latest.model === requestedModel
      ) {
        setTestResult({
          ok: false,
          model: requestedModel,
          latency_ms: null,
          error: e instanceof Error ? e.message : "Test failed",
        });
      }
    } finally {
      setTesting(false);
    }
  };

  // Live snapshot of the URL/model used by the test handler's
  // post-await guard. We need a ref so the closure inside handleTest
  // sees the latest values without re-binding the callback.
  const useFormDataSnapshot = useLatestRef({
    url: formData.wiki_llm_curator_url,
    model: formData.wiki_llm_curator_model,
  });

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
              <p className="text-xs text-destructive">
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
              <p className="text-xs text-destructive">
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
                    ? "flex items-center gap-1 text-xs text-emerald-600"
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
