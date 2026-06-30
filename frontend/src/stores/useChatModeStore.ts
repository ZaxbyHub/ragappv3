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
    }),
    { name: "ragapp_chat_mode" }
  )
);
