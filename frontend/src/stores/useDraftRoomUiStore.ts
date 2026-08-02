import { create } from "zustand";

// ============================================================================
// Draft Room — UI-only Zustand store.
//
// Holds ONLY ephemeral view state: active tab, panel visibility,
// compare-from/compare-to revision ids, unsaved editor text, inspector
// filters. Server state (drafts/jobs/revisions/claims/findings/evidence)
// belongs to React Query, never here.
// ============================================================================

export type WorkspaceTab =
  | "assignment"
  | "sources"
  | "research"
  | "outline"
  | "draft"
  | "copy"
  | "standards"
  | "fact";

export type EditorTab = "editor" | "preview" | "compare";

export type InspectorTab = "findings" | "claims" | "evidence" | "artifact";

interface DraftRoomUiState {
  workspaceTab: WorkspaceTab;
  editorTab: EditorTab;
  inspectorTab: InspectorTab;
  selectedStage: string | null;
  compareFromRevisionId: number | null;
  compareToRevisionId: number | null;
  sourcesSheetOpen: boolean;
  inspectorOpen: boolean;
  draftText: Record<number, string>;
  findingSeverityFilter: string | null;
  findingStatusFilter: string | null;
  claimStatusFilter: string | null;
  setWorkspaceTab(t: WorkspaceTab): void;
  setEditorTab(t: EditorTab): void;
  setInspectorTab(t: InspectorTab): void;
  setSelectedStage(s: string | null): void;
  setCompareRevisions(from: number | null, to: number | null): void;
  setSourcesSheetOpen(open: boolean): void;
  setInspectorOpen(open: boolean): void;
  setDraftText(draftId: number, text: string): void;
  clearDraftText(draftId: number): void;
  setFindingSeverityFilter(v: string | null): void;
  setFindingStatusFilter(v: string | null): void;
  setClaimStatusFilter(v: string | null): void;
  resetForDraft(draftId: number): void;
}

/**
 * Default view-selection state. Deliberately excludes `draftText`, which is
 * keyed per-draft and must survive `resetForDraft` — that's the whole point
 * of keeping it in a `Record<draftId, text>` instead of a single field.
 */
const DEFAULT_SELECTION_STATE = {
  workspaceTab: "assignment" as WorkspaceTab,
  editorTab: "editor" as EditorTab,
  inspectorTab: "findings" as InspectorTab,
  selectedStage: null as string | null,
  compareFromRevisionId: null as number | null,
  compareToRevisionId: null as number | null,
  sourcesSheetOpen: false,
  inspectorOpen: true,
  findingSeverityFilter: null as string | null,
  findingStatusFilter: null as string | null,
  claimStatusFilter: null as string | null,
};

export const useDraftRoomUiStore = create<DraftRoomUiState>((set) => ({
  ...DEFAULT_SELECTION_STATE,
  draftText: {},
  setWorkspaceTab: (t) => set({ workspaceTab: t }),
  setEditorTab: (t) => set({ editorTab: t }),
  setInspectorTab: (t) => set({ inspectorTab: t }),
  setSelectedStage: (s) => set({ selectedStage: s }),
  setCompareRevisions: (from, to) =>
    set({ compareFromRevisionId: from, compareToRevisionId: to }),
  setSourcesSheetOpen: (open) => set({ sourcesSheetOpen: open }),
  setInspectorOpen: (open) => set({ inspectorOpen: open }),
  setDraftText: (draftId, text) =>
    set((state) => ({ draftText: { ...state.draftText, [draftId]: text } })),
  clearDraftText: (draftId) =>
    set((state) => {
      if (!(draftId in state.draftText)) return state;
      const next = { ...state.draftText };
      delete next[draftId];
      return { draftText: next };
    }),
  setFindingSeverityFilter: (v) => set({ findingSeverityFilter: v }),
  setFindingStatusFilter: (v) => set({ findingStatusFilter: v }),
  setClaimStatusFilter: (v) => set({ claimStatusFilter: v }),
  resetForDraft: () => set({ ...DEFAULT_SELECTION_STATE }),
}));
