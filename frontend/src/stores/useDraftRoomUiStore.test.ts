import { beforeEach, describe, expect, it } from "vitest";
import { useDraftRoomUiStore } from "./useDraftRoomUiStore";

const DEFAULTS = {
  workspaceTab: "assignment",
  editorTab: "editor",
  inspectorTab: "findings",
  selectedStage: null,
  compareFromRevisionId: null,
  compareToRevisionId: null,
  sourcesSheetOpen: false,
  inspectorOpen: true,
  findingSeverityFilter: null,
  findingStatusFilter: null,
  claimStatusFilter: null,
} as const;

function resetStore() {
  useDraftRoomUiStore.setState({ ...DEFAULTS, draftText: {} });
}

describe("useDraftRoomUiStore", () => {
  beforeEach(() => {
    resetStore();
  });

  it("has the documented initial state", () => {
    const state = useDraftRoomUiStore.getState();
    expect(state.workspaceTab).toBe("assignment");
    expect(state.editorTab).toBe("editor");
    expect(state.inspectorTab).toBe("findings");
    expect(state.selectedStage).toBeNull();
    expect(state.compareFromRevisionId).toBeNull();
    expect(state.compareToRevisionId).toBeNull();
    expect(state.sourcesSheetOpen).toBe(false);
    expect(state.inspectorOpen).toBe(true);
    expect(state.draftText).toEqual({});
    expect(state.findingSeverityFilter).toBeNull();
    expect(state.findingStatusFilter).toBeNull();
    expect(state.claimStatusFilter).toBeNull();
  });

  it("setWorkspaceTab updates workspaceTab", () => {
    useDraftRoomUiStore.getState().setWorkspaceTab("sources");
    expect(useDraftRoomUiStore.getState().workspaceTab).toBe("sources");
  });

  it("setEditorTab updates editorTab", () => {
    useDraftRoomUiStore.getState().setEditorTab("compare");
    expect(useDraftRoomUiStore.getState().editorTab).toBe("compare");
  });

  it("setInspectorTab updates inspectorTab", () => {
    useDraftRoomUiStore.getState().setInspectorTab("claims");
    expect(useDraftRoomUiStore.getState().inspectorTab).toBe("claims");
  });

  it("setSelectedStage sets and clears the selected stage", () => {
    useDraftRoomUiStore.getState().setSelectedStage("research");
    expect(useDraftRoomUiStore.getState().selectedStage).toBe("research");
    useDraftRoomUiStore.getState().setSelectedStage(null);
    expect(useDraftRoomUiStore.getState().selectedStage).toBeNull();
  });

  it("setCompareRevisions sets both from and to together", () => {
    useDraftRoomUiStore.getState().setCompareRevisions(3, 5);
    expect(useDraftRoomUiStore.getState().compareFromRevisionId).toBe(3);
    expect(useDraftRoomUiStore.getState().compareToRevisionId).toBe(5);

    useDraftRoomUiStore.getState().setCompareRevisions(null, null);
    expect(useDraftRoomUiStore.getState().compareFromRevisionId).toBeNull();
    expect(useDraftRoomUiStore.getState().compareToRevisionId).toBeNull();
  });

  it("setSourcesSheetOpen toggles the sources sheet flag", () => {
    useDraftRoomUiStore.getState().setSourcesSheetOpen(true);
    expect(useDraftRoomUiStore.getState().sourcesSheetOpen).toBe(true);
    useDraftRoomUiStore.getState().setSourcesSheetOpen(false);
    expect(useDraftRoomUiStore.getState().sourcesSheetOpen).toBe(false);
  });

  it("setInspectorOpen toggles the inspector visibility flag", () => {
    useDraftRoomUiStore.getState().setInspectorOpen(false);
    expect(useDraftRoomUiStore.getState().inspectorOpen).toBe(false);
    useDraftRoomUiStore.getState().setInspectorOpen(true);
    expect(useDraftRoomUiStore.getState().inspectorOpen).toBe(true);
  });

  it("setFindingSeverityFilter/setFindingStatusFilter/setClaimStatusFilter set independent filters", () => {
    useDraftRoomUiStore.getState().setFindingSeverityFilter("blocker");
    useDraftRoomUiStore.getState().setFindingStatusFilter("open");
    useDraftRoomUiStore.getState().setClaimStatusFilter("unsupported");

    const state = useDraftRoomUiStore.getState();
    expect(state.findingSeverityFilter).toBe("blocker");
    expect(state.findingStatusFilter).toBe("open");
    expect(state.claimStatusFilter).toBe("unsupported");
  });

  it("setDraftText stores unsaved text per draft id", () => {
    useDraftRoomUiStore.getState().setDraftText(1, "hello draft one");
    expect(useDraftRoomUiStore.getState().draftText).toEqual({ 1: "hello draft one" });
  });

  it("keeps draftText isolated per draft id — writing one draft never touches another", () => {
    const { setDraftText } = useDraftRoomUiStore.getState();
    setDraftText(1, "draft one text");
    setDraftText(2, "draft two text");

    const state = useDraftRoomUiStore.getState();
    expect(state.draftText[1]).toBe("draft one text");
    expect(state.draftText[2]).toBe("draft two text");

    // Overwriting draft 1 must not disturb draft 2.
    useDraftRoomUiStore.getState().setDraftText(1, "draft one revised");
    const next = useDraftRoomUiStore.getState();
    expect(next.draftText[1]).toBe("draft one revised");
    expect(next.draftText[2]).toBe("draft two text");
  });

  it("clearDraftText removes only the given draft id's unsaved text", () => {
    const { setDraftText, clearDraftText } = useDraftRoomUiStore.getState();
    setDraftText(1, "draft one text");
    setDraftText(2, "draft two text");

    clearDraftText(1);

    const state = useDraftRoomUiStore.getState();
    expect(1 in state.draftText).toBe(false);
    expect(state.draftText[2]).toBe("draft two text");
  });

  it("clearDraftText is a no-op when the draft id has no unsaved text", () => {
    useDraftRoomUiStore.getState().setDraftText(2, "draft two text");
    useDraftRoomUiStore.getState().clearDraftText(999);
    expect(useDraftRoomUiStore.getState().draftText).toEqual({ 2: "draft two text" });
  });

  it("resetForDraft restores every selection field to its default", () => {
    const store = useDraftRoomUiStore.getState();
    store.setWorkspaceTab("outline");
    store.setEditorTab("preview");
    store.setInspectorTab("evidence");
    store.setSelectedStage("draft");
    store.setCompareRevisions(1, 2);
    store.setSourcesSheetOpen(true);
    store.setInspectorOpen(false);
    store.setFindingSeverityFilter("warning");
    store.setFindingStatusFilter("open");
    store.setClaimStatusFilter("stale");

    useDraftRoomUiStore.getState().resetForDraft(42);

    const state = useDraftRoomUiStore.getState();
    expect(state.workspaceTab).toBe("assignment");
    expect(state.editorTab).toBe("editor");
    expect(state.inspectorTab).toBe("findings");
    expect(state.selectedStage).toBeNull();
    expect(state.compareFromRevisionId).toBeNull();
    expect(state.compareToRevisionId).toBeNull();
    expect(state.sourcesSheetOpen).toBe(false);
    expect(state.inspectorOpen).toBe(true);
    expect(state.findingSeverityFilter).toBeNull();
    expect(state.findingStatusFilter).toBeNull();
    expect(state.claimStatusFilter).toBeNull();
  });

  it("resetForDraft never clears unsaved draftText for any draft", () => {
    useDraftRoomUiStore.getState().setDraftText(7, "unsaved manuscript edits");
    useDraftRoomUiStore.getState().setWorkspaceTab("draft");

    useDraftRoomUiStore.getState().resetForDraft(7);

    expect(useDraftRoomUiStore.getState().draftText[7]).toBe("unsaved manuscript edits");
  });
});
