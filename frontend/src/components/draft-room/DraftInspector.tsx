import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { DraftFindingsPanel } from "./DraftFindingsPanel";
import { DraftClaimsPanel } from "./DraftClaimsPanel";
import { DraftEvidencePanel } from "./DraftEvidencePanel";
import { DraftStageArtifact } from "./DraftStageArtifact";
import { useDraftRoomUiStore, type InspectorTab } from "@/stores/useDraftRoomUiStore";
import type { DraftRevisionSummary, DraftStage, DraftTier } from "@/lib/api/draftRoom";

export interface DraftInspectorProps {
  draftId: number;
  revisionId: number | null;
  jobId: number | null;
  stage: DraftStage | null;
  lockVersion: number;
  canDispose: boolean;
  /**
   * SHIPPED ADDITION (contract-components.md): `DraftFindingsPanel` requires a
   * `tier` prop to compute its tier-specific waive consequence copy, but the
   * `DraftInspectorProps` sketch in the contract predates that addition and
   * does not list it. `DraftInspector` is owned solely by W-SHELL and has no
   * other caller, so `tier` is threaded through here rather than editing the
   * shipped `DraftFindingsPanel`.
   */
  tier: DraftTier;
  onRevisionCreated?(revision: DraftRevisionSummary): void;
  onEditDraft?(): void;
}

/**
 * Right-hand (or, below `lg`, bottom-tab) complementary region: Findings,
 * Claims, Evidence, and the selected pipeline stage's artifact. Each panel
 * fetches its own data from `draftId`/`revisionId`/`jobId` — this component
 * only threads ids and switches the active tab via `useDraftRoomUiStore`.
 *
 * `onEditDraft` is accepted for the contract's documented signature. The one
 * "Edit draft" affordance actually rendered today lives inside
 * `DraftClaimsPanel`, which already switches to the draft workspace tab
 * itself via `useDraftRoomUiStore.setWorkspaceTab("draft")` — it does not
 * need this callback. It is forwarded here (as a no-op default) so a future
 * inspector panel can raise "edit draft" through the same channel without a
 * signature change.
 */
export function DraftInspector({
  draftId,
  revisionId,
  jobId,
  stage,
  lockVersion,
  canDispose,
  tier,
  onRevisionCreated,
}: DraftInspectorProps) {
  const inspectorTab = useDraftRoomUiStore((s) => s.inspectorTab);
  const setInspectorTab = useDraftRoomUiStore((s) => s.setInspectorTab);

  return (
    <aside aria-label="Draft inspector" className="min-w-0">
      <Tabs value={inspectorTab} onValueChange={(value) => setInspectorTab(value as InspectorTab)}>
        <TabsList className="h-auto w-full flex-wrap justify-start">
          <TabsTrigger value="findings">Findings</TabsTrigger>
          <TabsTrigger value="claims">Claims</TabsTrigger>
          <TabsTrigger value="evidence">Evidence</TabsTrigger>
          <TabsTrigger value="artifact">Stage artifact</TabsTrigger>
        </TabsList>
        <TabsContent value="findings">
          <DraftFindingsPanel
            draftId={draftId}
            revisionId={revisionId}
            lockVersion={lockVersion}
            baseRevisionId={revisionId}
            canDispose={canDispose}
            tier={tier}
            onRevisionCreated={onRevisionCreated}
          />
        </TabsContent>
        <TabsContent value="claims">
          <DraftClaimsPanel draftId={draftId} revisionId={revisionId} />
        </TabsContent>
        <TabsContent value="evidence">
          <DraftEvidencePanel draftId={draftId} jobId={jobId} />
        </TabsContent>
        <TabsContent value="artifact">
          <DraftStageArtifact stage={stage} />
        </TabsContent>
      </Tabs>
    </aside>
  );
}
