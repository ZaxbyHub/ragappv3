import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  probeModelEndpoint,
  updateSettings,
  type ProbeEndpointResult,
} from "@/lib/api";
import { Loader2, PlugZap } from "lucide-react";

/**
 * First-setup step 2 (issue #622): choose chat model endpoints.
 *
 * Presets prefill URL shapes only — nothing here prescribes a model name.
 * Persistence rides the admin PUT /api/settings pipeline, whose hot rebind
 * activates a just-configured endpoint live (no restart). The instant
 * endpoint is optional: "Skip for now" finishes setup leaving instant
 * unconfigured (the app degrades per the instant-routing contract), and
 * "Skip setup" finishes with no configuration at all.
 */

const PROVIDER_PRESETS = [
  { value: "ollama", label: "Ollama", placeholder: "http://localhost:11434" },
  { value: "lmstudio", label: "LM Studio", placeholder: "http://localhost:1234" },
  { value: "vllm", label: "vLLM", placeholder: "http://localhost:8000" },
  { value: "other", label: "Other", placeholder: "https://your-endpoint.example.com" },
] as const;

const PROBE_STATUS_COPY: Record<ProbeEndpointResult["status"], string> = {
  ok: "Connection successful - the model is available.",
  unreachable: "Could not reach the endpoint.",
  model_mismatch: "Endpoint reachable, but the model is not served.",
};

interface ModelEndpointStepProps {
  onFinish: () => void;
}

export default function ModelEndpointStep({ onFinish }: ModelEndpointStepProps) {
  const [provider, setProvider] = useState<string>("ollama");
  const [thinking, setThinking] = useState({ base_url: "", model: "", api_key: "" });
  const [instant, setInstant] = useState({ base_url: "", model: "", api_key: "" });
  const [probeStatus, setProbeStatus] = useState<ProbeEndpointResult | null>(null);
  const [probing, setProbing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const preset =
    PROVIDER_PRESETS.find((p) => p.value === provider) ?? PROVIDER_PRESETS[0];

  const testConnection = async () => {
    setError("");
    setProbeStatus(null);
    setProbing(true);
    try {
      const result = await probeModelEndpoint({
        target: "thinking",
        base_url: thinking.base_url,
        model: thinking.model,
        api_key: thinking.api_key || undefined,
      });
      setProbeStatus(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Probe request failed.");
    } finally {
      setProbing(false);
    }
  };

  const finish = async (includeInstant: boolean) => {
    setError("");
    setSaving(true);
    try {
      const payload: Record<string, string> = {};
      if (thinking.base_url || thinking.model) {
        payload.ollama_chat_url = thinking.base_url;
        payload.chat_model = thinking.model;
        if (thinking.api_key) payload.chat_api_key = thinking.api_key;
      }
      if (includeInstant && (instant.base_url || instant.model)) {
        payload.instant_chat_url = instant.base_url;
        payload.instant_chat_model = instant.model;
        if (instant.api_key) payload.instant_api_key = instant.api_key;
      }
      if (Object.keys(payload).length > 0) {
        await updateSettings(payload);
      }
      onFinish();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the configuration.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <Label htmlFor="wizard-provider">Provider</Label>
        <Select value={provider} onValueChange={setProvider}>
          <SelectTrigger id="wizard-provider">
            <SelectValue placeholder="Provider" />
          </SelectTrigger>
          <SelectContent>
            {PROVIDER_PRESETS.map((p) => (
              <SelectItem key={p.value} value={p.value}>
                {p.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <p className="text-xs text-muted-foreground">
          Presets only shape the Base URL placeholder — you bring the model.
        </p>
      </div>

      <div className="space-y-2">
        <Label htmlFor="wizard-base-url">Base URL</Label>
        <Input
          id="wizard-base-url"
          type="text"
          placeholder={preset.placeholder}
          value={thinking.base_url}
          onChange={(e) =>
            setThinking((prev) => ({ ...prev, base_url: e.target.value }))
          }
          disabled={saving}
          aria-required="true"
        />
      </div>

      <div className="space-y-2">
        <Label htmlFor="wizard-model">Model name</Label>
        <Input
          id="wizard-model"
          type="text"
          placeholder="e.g. qwen3:32b"
          value={thinking.model}
          onChange={(e) => setThinking((prev) => ({ ...prev, model: e.target.value }))}
          disabled={saving}
          aria-required="true"
        />
      </div>

      <div className="space-y-2">
        <Label htmlFor="wizard-api-key">API key (optional)</Label>
        <Input
          id="wizard-api-key"
          type="password"
          placeholder="For remote providers that require one"
          value={thinking.api_key}
          onChange={(e) =>
            setThinking((prev) => ({ ...prev, api_key: e.target.value }))
          }
          disabled={saving}
        />
      </div>

      <div className="space-y-2">
        <Button
          type="button"
          variant="outline"
          className="w-full"
          onClick={testConnection}
          disabled={probing}
        >
          {probing ? (
            <>
              <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
              Testing...
            </>
          ) : (
            <>
              <PlugZap className="mr-2 h-4 w-4" aria-hidden="true" />
              Test connection
            </>
          )}
        </Button>
        {probeStatus && (
          <p
            role="status"
            aria-live="polite"
            className={
              probeStatus.status === "ok"
                ? "text-sm text-green-600 dark:text-green-400"
                : "text-sm text-destructive"
            }
          >
            {PROBE_STATUS_COPY[probeStatus.status]}
          </p>
        )}
      </div>

      <div className="space-y-3 rounded-md border border-border p-3">
        <p className="font-medium">Instant endpoint (optional)</p>
        <div className="space-y-2">
          <Label htmlFor="wizard-instant-base-url">Instant base URL</Label>
          <Input
            id="wizard-instant-base-url"
            type="text"
            placeholder={preset.placeholder}
            value={instant.base_url}
            onChange={(e) =>
              setInstant((prev) => ({ ...prev, base_url: e.target.value }))
            }
            disabled={saving}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="wizard-instant-model">Instant model name</Label>
          <Input
            id="wizard-instant-model"
            type="text"
            value={instant.model}
            onChange={(e) =>
              setInstant((prev) => ({ ...prev, model: e.target.value }))
            }
            disabled={saving}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="wizard-instant-api-key">
            Instant API key (optional)
          </Label>
          <Input
            id="wizard-instant-api-key"
            type="password"
            placeholder="Defaults to the thinking endpoint's key"
            value={instant.api_key}
            onChange={(e) =>
              setInstant((prev) => ({ ...prev, api_key: e.target.value }))
            }
            disabled={saving}
          />
        </div>
        <Button
          type="button"
          variant="ghost"
          className="w-full"
          onClick={() => finish(false)}
          disabled={saving}
        >
          Skip for now
        </Button>
      </div>

      {error && (
        <p role="alert" className="text-sm text-destructive text-center">
          {error}
        </p>
      )}

      <Button
        type="button"
        className="w-full"
        onClick={() => finish(true)}
        disabled={saving}
      >
        {saving ? (
          <>
            <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden="true" />
            Saving...
          </>
        ) : (
          "Save and continue"
        )}
      </Button>

      <Button
        type="button"
        variant="ghost"
        className="w-full"
        onClick={onFinish}
        disabled={saving}
      >
        Skip setup
      </Button>
    </div>
  );
}
