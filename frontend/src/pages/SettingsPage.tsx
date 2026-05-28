/**
 * Settings page — task-based 6-tab redesign (PR B).
 *
 * Tabs: Overview · Models · Documents · Retrieval · Wiki & Curator · Maintenance.
 * The legacy "AI" + "Advanced" tabs were contradictory (AI claimed
 * read-only via env vars; Advanced edited the same fields) and have been
 * removed. Models is now the single source of truth, with a per-field
 * source badge sourced from the backend's ``effective_sources`` map.
 *
 * Numeric inputs use the NumberInput primitive (draft-string semantics)
 * so blanks no longer collapse to 0 mid-edit. The sticky SaveDiscardFooter
 * persists across tab switches and shows the count of unsaved changes
 * plus per-tab dot indicators.
 *
 * Discard restores the snapshot taken at load. Save persists via PUT
 * /settings; the backend rejects (422) curator-enabled bodies missing
 * url/model.
 */
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
import { AlertTriangle } from "lucide-react";
import {
  getSettings,
  updateSettings,
  testConnections,
} from "@/lib/api";
import type { ConnectionTestResult, UpdateSettingsRequest } from "@/lib/api";
import { useTestMode } from "@/fixtures/TestModeContext";
import { mockSettings } from "@/fixtures/settings";
import {
  useSettingsStore,
  type SettingsFormData,
  type SettingsTab,
} from "@/stores/useSettingsStore";
import { handleSettingsInputChange } from "@/components/settings/handleInputChange";
import { ConnectionStatusBadges } from "@/components/shared/ConnectionStatusBadges";
import type { HealthStatus } from "@/types/health";
import { useHealthCheck } from "@/hooks/useHealthCheck";
import { ConnectionSettings } from "@/components/settings/ConnectionSettings";
import { DocumentProcessingSettings } from "@/components/settings/DocumentProcessingSettings";
import { RAGSettings } from "@/components/settings/RAGSettings";
import { RetrievalSettings } from "@/components/settings/RetrievalSettings";
import { ModelsTab } from "@/components/settings/ModelsTab";
import { OverviewTab } from "@/components/settings/OverviewTab";
import { WikiCuratorSettings } from "@/components/settings/WikiCuratorSettings";
import { MaintenanceSettings } from "@/components/settings/MaintenanceSettings";
import { SaveDiscardFooter } from "@/components/settings/SaveDiscardFooter";
import { PageTitleHeader } from "@/components/layout/PageTitleHeader";
import { ReindexConfirmDialog } from "@/components/settings/ReindexConfirmDialog";
import { REINDEX_REQUIRED_FIELDS } from "@/stores/useSettingsStore";
import { useVaultStore } from "@/stores/useVaultStore";

function pickDirtyPayload(
  formData: SettingsFormData,
  loaded: SettingsFormData,
): UpdateSettingsRequest {
  const out: Record<string, unknown> = {};
  (Object.keys(formData) as Array<keyof SettingsFormData>).forEach((k) => {
    if (formData[k] !== loaded[k]) {
      out[k as string] = formData[k];
    }
  });
  return out as UpdateSettingsRequest;
}

function SettingsPageContent({
  health,
  connectionResult,
  isTestingConnections,
  onTestConnections,
}: {
  health: HealthStatus;
  connectionResult: ConnectionTestResult | null;
  isTestingConnections: boolean;
  onTestConnections: () => Promise<void>;
}) {
  const testMode = useTestMode();
  const {
    settings,
    formData,
    loadedFormData,
    loading,
    saving,
    error,
    errors,
    reindexRequired,
    setSettings,
    initializeForm,
    setSaving,
    setError,
    setReindexRequired,
    updateFormField,
    validateForm,
    discard,
    dirtyFields,
    dirtyByTab,
  } = useSettingsStore();

  const activeVaultId = useVaultStore((s) => s.activeVaultId);
  const [activeTab, setActiveTab] = useState<SettingsTab>("overview");
  const [reindexDialogOpen, setReindexDialogOpen] = useState(false);

  useEffect(() => {
    if (testMode) {
      setSettings(mockSettings);
      initializeForm(mockSettings);
      return;
    }
    let mounted = true;
    getSettings()
      .then((data) => {
        if (mounted) {
          setSettings(data);
          initializeForm(data);
        }
      })
      .catch((err) => {
        if (mounted) {
          setError(err instanceof Error ? err.message : "Failed to load settings");
        }
      });
    return () => {
      mounted = false;
    };
  }, [setSettings, initializeForm, setError, testMode]);

  const dirtySet = dirtyFields();
  const dirtyCount = dirtySet.size;
  const tabDots = dirtyByTab();
  const effectiveSources = settings?.effective_sources ?? {};

  // Wrapper that the legacy components call with a loose signature.
  // Implementation lives in components/settings/handleInputChange.ts so
  // it can be imported and exercised by tests directly. The legacy
  // components surface `e.target.value` strings; the helper coerces
  // numeric fields and intentionally drops blank input (preserves last
  // good value rather than overwriting with NaN/0). Known legacy
  // limitation: trailing-garbage input ("12abc") commits the partial
  // parse silently — the legacy components don't surface invalid state.
  // The Wiki & Curator tab uses NumberInput which owns its own draft
  // state and surfaces invalid via data-invalid.
  const handleInputChange = (
    field: keyof SettingsFormData,
    value: string | boolean | number,
  ) => {
    handleSettingsInputChange(field, value, updateFormField);
  };

  // Fields that are both dirty AND in the re-index-required set. Listed in
  // the confirmation dialog so the user sees exactly what triggered it.
  const dirtyReindexFields = useMemo(
    () =>
      Array.from(dirtySet).filter((f) =>
        REINDEX_REQUIRED_FIELDS.has(f),
      ) as string[],
    [dirtySet],
  );

  const persistSave = async () => {
    setSaving(true);
    setError(null);
    try {
      const payload = pickDirtyPayload(formData, loadedFormData);
      const updated = await updateSettings(payload);
      setSettings(updated);
      // Re-initialize so the snapshot reflects the new persisted state
      // and dirtyFields drops to zero.
      initializeForm(updated);
      if (dirtyReindexFields.length > 0) {
        setReindexRequired(true);
        toast.warning(
          "Settings saved. Existing document embeddings are stale — reindex required.",
        );
      } else {
        toast.success("Settings saved");
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to save settings";
      setError(msg);
      toast.error(msg);
    } finally {
      setSaving(false);
      setReindexDialogOpen(false);
    }
  };

  const handleSave = async () => {
    if (!validateForm()) {
      toast.error("Please fix the highlighted errors before saving.");
      return;
    }
    // If any re-index-required field is dirty, gate the save behind an
    // explicit confirmation so the consequence is acknowledged.
    if (dirtyReindexFields.length > 0) {
      setReindexDialogOpen(true);
      return;
    }
    await persistSave();
  };

  const handleDiscard = () => {
    discard();
    toast.info("Discarded unsaved changes");
  };

  const validationFailed = useMemo(() => {
    return Object.keys(errors).length > 0;
  }, [errors]);

  if (loading) {
    return (
      <div className="space-y-4">
        <Card>
          <CardHeader>
            <Skeleton className="h-6 w-[180px]" />
            <Skeleton className="h-4 w-[250px]" />
          </CardHeader>
          <CardContent className="space-y-6">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
          </CardContent>
        </Card>
      </div>
    );
  }

  if (error && !settings) {
    return (
      <Card>
        <CardContent className="py-8">
          <p className="text-destructive text-center">Error: {error}</p>
        </CardContent>
      </Card>
    );
  }

  const tabTrigger = (value: SettingsTab, label: string) => (
    <TabsTrigger value={value} className="text-xs">
      <span className="flex items-center gap-1">
        {label}
        {tabDots[value] > 0 && (
          <span
            className="h-1.5 w-1.5 rounded-full bg-primary"
            aria-label={`${tabDots[value]} unsaved changes in ${label}`}
          />
        )}
      </span>
    </TabsTrigger>
  );

  return (
    <>
      {reindexRequired && (
        <div className="flex items-start gap-3 rounded-sm border border-amber-300 bg-amber-50 dark:bg-amber-950/20 dark:border-amber-800 p-4">
          <AlertTriangle
            className="h-5 w-5 text-amber-600 flex-shrink-0 mt-0.5"
            aria-hidden
          />
          <div className="flex-1 min-w-0">
            <p className="text-sm font-medium text-amber-800 dark:text-amber-200">
              Reindex required
            </p>
            <p className="text-xs text-amber-700 dark:text-amber-300 mt-0.5">
              Changes to embedding model, chunk size, or vector settings
              invalidate existing document embeddings. Re-process documents
              from the Documents page or run a wiki recompile from the
              Maintenance tab.
            </p>
          </div>
          <button
            onClick={() => setReindexRequired(false)}
            className="text-amber-600 hover:text-amber-800 text-xs underline flex-shrink-0"
          >
            Dismiss
          </button>
        </div>
      )}

      <Tabs
        value={activeTab}
        onValueChange={(v) => setActiveTab(v as SettingsTab)}
        className="w-full"
      >
        <TabsList className="flex-wrap h-auto gap-1 mb-4">
          {tabTrigger("overview", "Overview")}
          {tabTrigger("models", "Models")}
          {tabTrigger("documents", "Documents")}
          {tabTrigger("retrieval", "Retrieval")}
          {tabTrigger("wiki", "Wiki & Curator")}
          {tabTrigger("maintenance", "Maintenance")}
        </TabsList>

        <TabsContent value="overview">
          <OverviewTab
            health={health}
            connectionResult={connectionResult}
            isTestingConnections={isTestingConnections}
            onTestConnections={() => {
              void onTestConnections();
            }}
            curatorEnabled={formData.wiki_llm_curator_enabled}
            wikiEnabled={formData.wiki_enabled}
          />
        </TabsContent>

        <TabsContent value="models">
          <ModelsTab
            formData={formData}
            errors={errors}
            onChange={(f, v) =>
              updateFormField(f, v as SettingsFormData[typeof f])
            }
            effectiveSources={
              effectiveSources as Record<string, "kv" | "env" | "default">
            }
          />
        </TabsContent>

        <TabsContent value="documents" className="space-y-4">
          <DocumentProcessingSettings
            formData={formData}
            errors={errors}
            onChange={handleInputChange}
          />
        </TabsContent>

        <TabsContent value="retrieval" className="space-y-4">
          <RAGSettings
            formData={formData}
            errors={errors}
            onChange={handleInputChange}
          />
          <RetrievalSettings
            formData={formData}
            errors={errors}
            onChange={handleInputChange}
          />
        </TabsContent>

        <TabsContent value="wiki" className="space-y-4">
          <WikiCuratorSettings
            formData={formData}
            errors={errors}
            onChange={(f, v) =>
              updateFormField(f, v as SettingsFormData[typeof f])
            }
          />
        </TabsContent>

        <TabsContent value="maintenance" className="space-y-4">
          <MaintenanceSettings vaultId={activeVaultId} />
        </TabsContent>
      </Tabs>

      <SaveDiscardFooter
        dirtyCount={dirtyCount}
        invalid={validationFailed}
        saving={saving}
        onSave={handleSave}
        onDiscard={handleDiscard}
      />

      <ReindexConfirmDialog
        open={reindexDialogOpen}
        onOpenChange={(open) => {
          if (!saving) setReindexDialogOpen(open);
        }}
        dirtyReindexFields={dirtyReindexFields}
        onConfirm={persistSave}
        saving={saving}
      />
    </>
  );
}

function SettingsPageWithStatus({ health }: { health: HealthStatus }) {
  const formatLastChecked = (date: Date | null) => {
    if (!date) return "Not checked";
    return `Last checked: ${date.toLocaleTimeString()}`;
  };

  const [connectionResult, setConnectionResult] =
    useState<ConnectionTestResult | null>(null);
  const [isTestingConnections, setIsTestingConnections] = useState(false);

  const handleConnectionTest = async () => {
    setIsTestingConnections(true);
    try {
      const result = await testConnections();
      setConnectionResult(result);
      toast.success("Connection test completed");
    } catch (err) {
      const message = err instanceof Error ? err.message : "Connection test failed";
      toast.error(message);
      setConnectionResult(null);
    } finally {
      setIsTestingConnections(false);
    }
  };

  // Hide the unused-warning for dropped ConnectionSettings consumer.
  void ConnectionSettings;

  return (
    <div className="space-y-6 animate-in fade-in duration-300 pb-12">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <PageTitleHeader
          title="Settings"
          description="Configure your application preferences"
        />
        <div className="flex flex-col items-end gap-1">
          <ConnectionStatusBadges health={health} />
          <span className="text-xs text-muted-foreground">
            {formatLastChecked(health.lastChecked)}
          </span>
        </div>
      </div>
      <SettingsPageContent
        health={health}
        connectionResult={connectionResult}
        isTestingConnections={isTestingConnections}
        onTestConnections={handleConnectionTest}
      />
    </div>
  );
}

export default function SettingsPage() {
  const health = useHealthCheck();
  return <SettingsPageWithStatus health={health} />;
}
