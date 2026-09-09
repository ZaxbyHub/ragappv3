import { create } from "zustand";
import { persist } from "zustand/middleware";

export type ChatMode = "instant" | "thinking";

export type RetrievalMode = "auto" | "semantic" | "keyword";
export type CitationMode = "enabled" | "disabled" | "required";

export interface ComposerControlsState {
  /** User-pinned mode. `null` means "use the settings default". */
  chatMode: ChatMode | null;
  setChatMode: (mode: ChatMode) => void;
  clearChatMode: () => void;
  /** Temperature for generation (0–2, default 0.7). */
  temperature: number;
  setTemperature: (t: number) => void;
  /** Retrieval strategy. */
  retrievalMode: RetrievalMode;
  setRetrievalMode: (m: RetrievalMode) => void;
  /** Citation enforcement level. */
  citationMode: CitationMode;
  setCitationMode: (m: CitationMode) => void;
  /**
   * Metadata filter (issue #510 AC-16). Inclusive retrieval date bounds as
   * ISO yyyy-mm-dd strings; empty string means unset. Session-scoped query
   * context — intentionally not persisted (see `partialize` below).
   */
  metadataFilterDateFrom: string;
  setMetadataFilterDateFrom: (v: string) => void;
  metadataFilterDateTo: string;
  setMetadataFilterDateTo: (v: string) => void;
  /** Comma-separated tag input; split/trimmed at send time. */
  metadataFilterTags: string;
  setMetadataFilterTags: (v: string) => void;
  metadataFilterAuthor: string;
  setMetadataFilterAuthor: (v: string) => void;
  /** Clear every metadata-filter field. */
  resetMetadataFilter: () => void;
  /**
   * Document scope for the NEXT question (issue #514 AC-23). Set from a
   * document's "Ask about this document" control; consumed and cleared by the
   * send path so the scope applies to exactly one question. One-shot query
   * context — intentionally not persisted (see `partialize`).
   */
  scopeDocumentIds: number[] | null;
  setScopeDocumentIds: (ids: number[]) => void;
  clearScopeDocumentIds: () => void;
}

export const useChatModeStore = create<ComposerControlsState>()(
  persist(
    (set) => ({
      chatMode: null,
      setChatMode: (chatMode) => set({ chatMode }),
      clearChatMode: () => set({ chatMode: null }),
      temperature: 0.7,
      setTemperature: (temperature) => set({ temperature }),
      retrievalMode: "auto",
      setRetrievalMode: (retrievalMode) => set({ retrievalMode }),
      citationMode: "enabled",
      setCitationMode: (citationMode) => set({ citationMode }),
      metadataFilterDateFrom: "",
      setMetadataFilterDateFrom: (metadataFilterDateFrom) =>
        set({ metadataFilterDateFrom }),
      metadataFilterDateTo: "",
      setMetadataFilterDateTo: (metadataFilterDateTo) =>
        set({ metadataFilterDateTo }),
      metadataFilterTags: "",
      setMetadataFilterTags: (metadataFilterTags) => set({ metadataFilterTags }),
      metadataFilterAuthor: "",
      setMetadataFilterAuthor: (metadataFilterAuthor) =>
        set({ metadataFilterAuthor }),
      resetMetadataFilter: () =>
        set({
          metadataFilterDateFrom: "",
          metadataFilterDateTo: "",
          metadataFilterTags: "",
          metadataFilterAuthor: "",
        }),
      scopeDocumentIds: null,
      setScopeDocumentIds: (scopeDocumentIds) => set({ scopeDocumentIds }),
      clearScopeDocumentIds: () => set({ scopeDocumentIds: null }),
    }),
    {
      name: "ragapp_chat_mode",
      // The metadata filter is per-query context, not a durable preference —
      // a stale date/author filter surviving a reload would silently hide
      // results. Pin persistence to the original preference fields only.
      partialize: (state) => ({
        chatMode: state.chatMode,
        temperature: state.temperature,
        retrievalMode: state.retrievalMode,
        citationMode: state.citationMode,
      }),
    }
  )
);
