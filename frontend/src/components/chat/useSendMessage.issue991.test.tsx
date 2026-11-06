/**
 * Issue #991 acceptance checks — C4 (AC4), C5 (AC5), C6 (AC6), all
 * DISCRIMINATING. This file is FROZEN for trace
 * 1040-991-health-401-chat-cap-sync.
 *
 * All checks drive the REAL production surfaces (harness precedents:
 * TranscriptPane.retryDurability.test.tsx for the transcript/retry wiring,
 * Composer.large-input.test.tsx for the composer input/send affordances):
 * TranscriptPane renders the real Composer wired to the real useSendMessage
 * handleSend, and its retry affordance drives the real handleRetry (which
 * truncates server history BEFORE sendDirect — so the over-cap gate must fire
 * BEFORE the truncate).
 *
 * - C4: with an explicit current-default settings response
 *   ({ chat_message_max_chars: 20000 }), a 25_000-char input (over the server
 *   default cap of 20_000) must surface an error naming the exact bound
 *   (matches /20[ ,]?000/, not just "maximum length") on BOTH composer
 *   surfaces — the change feedback AND the submit gate — and the submit gate
 *   must neither start a stream NOR create a chat session (no orphan session
 *   for a send that can never stream). At base the client gate is
 *   MAX_INPUT_LENGTH = 100_000, so 25_000 sails through → RED at base, GREEN
 *   after the fix.
 * - C5: the settings store does NOT pre-satisfy the test (reset to null);
 *   getSettings (the @/lib/api seam, frontend/src/lib/api/settings.ts:225)
 *   resolves chat_message_max_chars = 5000; the composer's fetch flow must
 *   actually CALL getSettings (no pre-seeded store) and a 6_000-char input
 *   must be rejected naming 5000 exactly. At base nothing in the composer
 *   path fetches settings → RED at base, GREEN after the fix.
 * - C6 (pre-truncate validation): retrying a persisted 25_000-char last user
 *   turn through the REAL retry affordance must be refused BEFORE any server
 *   truncate (truncateChatSession NOT called), with no local rows removed, no
 *   stream started, and an actionable error naming the bound surfaced. At
 *   base handleRetry truncates first and ships the over-cap turn → RED.
 * - C6 (deferred settings): a retry whose cap loads LATE still never
 *   truncates — getSettings resolves chat_message_max_chars = 5000 only after
 *   a controlled delay (deferred promise); while the settings promise is
 *   still pending the retry must NOT truncate (an unknown cap is not license
 *   to cut server history), and once the cap lands the 6_000-char turn is
 *   refused with no truncate, no row removal, no stream, and an error naming
 *   5000. At base the retry truncates immediately, ignorant of settings →
 *   RED.
 * - C6 (sendDirect backstop): sendDirect called directly with over-cap
 *   content must set the named-bound error and never reach the stream layer.
 *   At base sendDirect has no gate → RED at base, GREEN after the fix.
 *
 * Round-4 amendments (plan rev 4 semantics), as SUPERSEDED in one place by
 * the round-5 rev below: (1) the fetch fails, or the field is present but
 * null / non-integer / non-finite / < 1 → acquisition FAILURE → sends and
 * retries BLOCK with an actionable "message limit unavailable" error; (2) a
 * valid integer >= 1 is the AUTHORITATIVE cap with NO client-side ceiling
 * clamp. Composer cleanup policy: handleSubmit's synchronous
 * clearDraftNow()/attachment-detach is DEFERRED until the send is admitted,
 * so drafts and attachments survive a blocked send. Round-4 legs (all
 * DISCRIMINATING — RED at base, GREEN after the fix):
 * - no active chat: an over-cap submit never CREATES a session (at base the
 *   25_000 send proceeds under the 100_000 gate and creates + streams).
 * - failed settings acquisition blocks the send and preserves the composer
 *   artifacts (draft value in the textarea, the persisted draft key, and a
 *   completed attachment chip's chat association). At base the 15_000-char
 *   input ships (gate 100_000, no fetch dependency) → chatStream called,
 *   draft cleared, chip detached → RED.
 * - a present-but-NULL chat_message_max_chars BLOCKS the send instead of
 *   silently defaulting (at base 15_000 < 100_000 ships → RED).
 * - no ceiling clamp: a server cap of 150_000 admits a 120_000-char send
 *   as-is (at base the stale 100_000 gate blocks it → chatStream never
 *   called → RED).
 * - a runtime cap change re-validates the composer (error and counter name
 *   the new 5000 cap) without wiping the 6_000-char draft (at base no cap
 *   ever arrives → RED).
 * - edit loads an over-cap persisted 25_000-char turn into the composer for
 *   shortening, with named-bound change feedback that clears once the user
 *   shortens (at base there is no named-bound error for 25_000 → RED).
 *
 * Round-5 amendments (plan rev 5 semantics — this round). Cap acquisition is
 * FOUR states: (1) the settings fetch FAILS → block (limit-unavailable);
 * (2) the fetch SUCCEEDS with the field ABSENT (legacy `{}` response) → the
 * cap is UNKNOWABLE — a legacy server may be configured BELOW the default —
 * → block (limit-unavailable), never a silently-assumed 20_000; (3) the
 * field is present but null / malformed / non-finite / < 1 → block; (4) a
 * valid integer >= 1 → AUTHORITATIVE (no client-side clamp). The advisory
 * 20_000 applies to DISPLAY/change-feedback ONLY while no fetch has
 * completed. The cap loader is keyed by the auth OWNER (auth-lifecycle):
 * a replacement owner (reserveReplacementAuthOwner) discards the in-flight
 * result and re-fetches fresh WITHOUT requiring another send. Draft
 * persistence stays DECOUPLED from the cap (drafts keep the legacy 100_000
 * never-persist-beyond ceiling; the restore/send-time gate enforces the real
 * cap with the named error). BOTH Composer send closures — the normal submit
 * tail AND the "Send without these files" toast action — route through ONE
 * awaited admission helper, so artifacts (draft, attachment links) are
 * touched only AFTER admission. Five amended/new legs this round (all
 * DISCRIMINATING — RED at base, GREEN after the fix, with disclosed
 * GREEN-at-base sub-assertions inside the owner-race and draft-survival
 * legs):
 * - the round-4 default-config legs (C4 change-feedback/submit, the C6
 *   retry, sendDirect, no-active-chat) now feed an EXPLICIT current-default
 *   response { chat_message_max_chars: 20000 } via the beforeEach default —
 *   "default server config" is no longer simulated by `{}` (that shape is
 *   now the BLOCKING legacy case below). Still RED at base through the same
 *   vectors (base ignores settings entirely).
 * - legacy absent-field response (`{}`) BLOCKS with limit-unavailable and
 *   never claims a 20_000-named bound from `{}` (at base the 15_000 input
 *   ships → chatStream called → RED).
 * - REWRITTEN owner race: the REAL owner replacement
 *   (reserveReplacementAuthOwner from @/lib/api/auth-lifecycle) is driven
 *   while the cap fetch is pending; the stale result is discarded, the
 *   loader RE-ARMS so the new owner's own fetch happens without another
 *   send, and that fresh 5000 gates a 6_000 input (RED at base via the
 *   getSettings-never-called waitFor — base has no loader).
 * - the "Send without these files" toast action ALSO defers artifact
 *   cleanup on failed acquisition: no stream, no session, draft storage and
 *   attachment associations survive (at base the action detaches + clears +
 *   sends → RED).
 * - an ADMITTED send still performs artifact cleanup, but only AFTER
 *   admission: while the deferred settings fetch is pending the draft is
 *   still persisted (at base the submit tail clears it synchronously → RED
 *   via draft-cleared-early), then the send proceeds and cleanup completes.
 * - a persisted draft SURVIVES runtime cap changes (5000 arrival then a
 *   20000 arrival through the loader's owner-keyed re-fetch) — the survival
 *   sub-assertions are GREEN at base (base never touches the key after
 *   restore) and are disclosed as such; the leg's RED vector is the missing
 *   5000-named bound feedback for the restored 6_000-char draft.
 *
 * Stores under test are REAL (useChatStore/useSettingsStore/useChatModeStore/
 * useLlmHealthStore/useChatShellStore); ambient modules (router, chat
 * history, vault/auth stores, sonner, framer-motion, MessageBubble/
 * AssistantMessage) are mocked per the TranscriptPane retry-durability
 * pattern. The mocked AssistantMessage exposes the Retry affordance
 * (aria-label "Retry <id>") so the production handleRetry runs for real.
 */
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { TranscriptPane } from "./TranscriptPane";
import { useSendMessage } from "@/hooks/useSendMessage";
// Optional-guard import pattern: the cap-loader reset seam ships WITH the
// fix; at the pre-fix tree the export does not exist and the guarded call is
// a no-op, so the suite keeps failing for behavioral reasons, not import
// errors.
import * as useSendMessageModule from "@/hooks/useSendMessage";
import { useChatStore } from "@/stores/useChatStore";
import { useChatModeStore } from "@/stores/useChatModeStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useLlmHealthStore } from "@/stores/useLlmHealthStore";
import { useSettingsStore } from "@/stores/useSettingsStore";
import { useUploadStore, type UploadFile } from "@/stores/useUploadStore";
import { useAuthStore } from "@/stores/useAuthStore";
import { reserveReplacementAuthOwner } from "@/lib/api/auth-lifecycle";
import { getDraftKey } from "@/lib/chatDrafts";
import { mapSessionMessage } from "@/lib/chatMessageMapper";
import type { SettingsResponse } from "@/lib/api";

const apiMocks = vi.hoisted(() => ({
  createChatSession: vi.fn(),
  chatStream: vi.fn(),
  getSettings: vi.fn(),
  getLlmModeHealth: vi.fn(),
}));
const truncateChatSessionMock = vi.hoisted(() => vi.fn());
const addChatMessagesBatchMock = vi.hoisted(() => vi.fn());
const forkChatSessionMock = vi.hoisted(() => vi.fn());
const getChatSessionMock = vi.hoisted(() => vi.fn());
const mockRefreshHistory = vi.hoisted(() => vi.fn());
const mockNavigate = vi.hoisted(() => vi.fn());
const mockGetActiveVault = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({
  createChatSession: (...args: unknown[]) => apiMocks.createChatSession(...args),
  addChatMessagesBatch: (...args: unknown[]) => addChatMessagesBatchMock(...args),
  addChatMessagesBatchKeepalive: vi.fn(),
  chatStream: (...args: unknown[]) => apiMocks.chatStream(...args),
  truncateChatSession: truncateChatSessionMock,
  forkChatSession: forkChatSessionMock,
  getChatSession: getChatSessionMock,
  getSettings: (...args: unknown[]) => apiMocks.getSettings(...args),
  getLlmModeHealth: (...args: unknown[]) => apiMocks.getLlmModeHealth(...args),
}));

vi.mock("@/stores/useVaultStore");
vi.mock("@/stores/useAuthStore", () => ({
  useAuthStore: vi.fn(() => ({ user: null })),
}));
vi.mock("@/hooks/useChatHistory", () => ({
  useChatHistory: () => ({
    refreshHistory: mockRefreshHistory,
    chatHistory: [],
    isChatLoading: false,
    chatHistoryError: null,
  }),
}));
vi.mock("react-router-dom", () => ({
  useNavigate: () => mockNavigate,
  useInRouterContext: () => false,
}));
vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  },
}));
vi.mock("./MessageBubble", () => ({
  MessageBubble: ({
    message,
    onEdit,
  }: {
    message: { id: string; role: string; content: string };
    onEdit?: (messageId: string, content: string) => void;
  }) => (
    <div data-testid="message-bubble" data-message-id={message.id}>
      {message.content}
      {onEdit && (
        <button
          type="button"
          aria-label={`Edit ${message.id}`}
          onClick={() => onEdit(message.id, message.content)}
        >
          Edit
        </button>
      )}
    </div>
  ),
}));
vi.mock("./AssistantMessage", () => ({
  AssistantMessage: ({
    message,
    onRetry,
    onFork,
  }: {
    message: { id: string; role: string; content: string };
    onRetry?: () => void;
    onFork?: () => void;
  }) => (
    <div data-testid="message-bubble" data-message-id={message.id}>
      {message.content}
      {onRetry && (
        <button type="button" aria-label={`Retry ${message.id}`} onClick={onRetry}>
          Retry
        </button>
      )}
      {onFork && (
        <button type="button" aria-label={`Fork ${message.id}`} onClick={onFork}>
          Fork
        </button>
      )}
    </div>
  ),
}));
vi.mock("framer-motion", () => ({
  motion: {
    div: ({ children, ...props }: { children: React.ReactNode }) => (
      <div data-testid="motion-div" {...props}>
        {children}
      </div>
    ),
  },
  AnimatePresence: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useReducedMotion: () => false,
}));

import { useVaultStore } from "@/stores/useVaultStore";

// JSDOM lacks these; TranscriptPane's scroll area and Composer rely on them.
Element.prototype.scrollIntoView = vi.fn();
Element.prototype.scrollTo = vi.fn();

/** The global test setup (src/test/setup.ts) replaces localStorage with a
 * NO-OP vi.fn() double — getItem always returns null. The round-5 legs
 * assert REAL draft persistence (seeded restore, survive-refused-send,
 * cleared-after-admission), so each test installs a working Map-backed
 * storage surface (repo idiom: Composer.issue823.draftBoundary.test.tsx's
 * installLocalStorage). */
function installLocalStorage(): Map<string, string> {
  const store = new Map<string, string>();
  const impl = {
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
    key: (i: number) => Array.from(store.keys())[i] ?? null,
    get length() {
      return store.size;
    },
  };
  Object.defineProperty(window, "localStorage", { value: impl, configurable: true });
  return store;
}

/** Server-row shape the seeded persisted session rows use (the retry test
 * loads them through the real mapSessionMessage so store rows carry seqs). */
function seedRow(
  id: number,
  role: "user" | "assistant",
  content: string,
  seq: number,
) {
  return {
    id,
    role,
    content,
    created_at: `2026-05-12T00:00:0${seq}:00Z`,
    seq,
    sources: null,
  };
}

/** "The surfaced error names the bound": the number appears standalone (never
 * as a longer digit run like 100000/50000), with an optional thousands
 * separator — bare "maximum length" phrasing does NOT match. */
function namesBound(bound: string): RegExp {
  return new RegExp(`(^|[^0-9])${bound}([^0-9]|$)`);
}
const NAMES_20_000 = namesBound("20[ ,]?000");
const NAMES_5_000 = namesBound("5[ ,]?000");

/** Every user-visible error string the retry surface can show: the composer
 * input error plus each sonner toast.error first argument. */
function gatheredErrors(): string[] {
  const toastErrors = vi
    .mocked(toast.error)
    .mock.calls.map((call) => String(call[0] ?? ""));
  const inputError = useChatStore.getState().inputError;
  return [...(typeof inputError === "string" ? [inputError] : []), ...toastErrors];
}

/** Plain wall-clock settle (NOT a wait on any fix-produced state): lets any
 * mount-time settings fetch resolve and apply before the test drives the
 * composer, so the checks are deterministic once the cap arrives
 * asynchronously. */
async function settle(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 25));
  });
}

/** Point the MOCKED auth store at a different principal — the harness's
 * owner-bump idiom for the (unmocked) useAuthStore module double. The
 * Composer reads this user for its draft principal, and the fixed cap
 * acquisition may key on it, on the auth-lifecycle principal generation, or
 * on both; the owner-discard leg bumps every surface this harness can
 * drive. */
function setMockAuthUser(
  user: { id: number; username: string; full_name: string } | null,
): void {
  (useAuthStore as unknown as ReturnType<typeof vi.fn>).mockImplementation(
    (selector?: (s: { user: typeof user }) => unknown) => {
      const state = { user };
      return selector ? selector(state) : state;
    },
  );
}

describe("useSendMessage issue #991 — the composer gate tracks the server's chat cap", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // The Composer persists real drafts per (principal, session) key; the
    // round-4/5 legs type through the real persist path and must not observe
    // a previous test's leftover draft (mount-time restore would seed it). A
    // FRESH Map-backed surface is installed per test (the global double is a
    // no-op — see installLocalStorage above).
    installLocalStorage();
    // Reinstall the default mocked principal (clearAllMocks keeps a
    // mockImplementation a previous test installed; the owner-discard leg
    // swaps the user mid-test).
    setMockAuthUser(null);
    // mockReset (not just clearMocks) drains any once-queues a previous test
    // left behind before the per-test defaults are installed.
    apiMocks.createChatSession.mockReset().mockResolvedValue({ id: 42 });
    apiMocks.chatStream.mockReset().mockImplementation(
      (
        _messages: unknown,
        handlers: {
          onMessage: (chunk: string) => void;
          onComplete: () => Promise<void>;
        },
      ) => {
        handlers.onMessage("hello");
        void handlers.onComplete();
        return vi.fn();
      },
    );
    // Default server config (round-5): the settings fetch succeeds with an
    // EXPLICIT current-default cap of 20_000 — every leg whose premise is
    // "default server config" (C4 change-feedback/submit, the C6 retry and
    // sendDirect legs, the no-active-chat leg) gets this response. The `{}`
    // shape is no longer the default-config stand-in: under plan rev 5 an
    // absent field is the BLOCKING legacy case (its own leg below).
    apiMocks.getSettings.mockReset().mockResolvedValue({
      chat_message_max_chars: 20000,
    } as unknown as SettingsResponse);
    apiMocks.getLlmModeHealth.mockReset().mockResolvedValue({ thinking: true, instant: true });
    truncateChatSessionMock.mockReset().mockResolvedValue({ remaining_count: 0, tail_seq: 0 });
    addChatMessagesBatchMock.mockReset().mockResolvedValue([]);
    forkChatSessionMock.mockReset().mockResolvedValue({ id: 20, messages: [] });
    getChatSessionMock.mockReset().mockResolvedValue({ id: 42, messages: [] });
    mockRefreshHistory.mockReset().mockResolvedValue(undefined);

    // Reset the REAL chat store between tests (retry-durability pattern).
    useChatStore.setState({
      messageIds: [],
      messagesById: {},
      streamingMessageId: null,
      input: "",
      isStreaming: false,
      abortFn: null,
      inputError: null,
      expandedSources: new Set(),
      activeChatId: null,
      pendingTurnPersist: null,
      messageEditVersions: {},
      activeEditVersion: {},
    });
    useLlmHealthStore.setState({ thinking: true, instant: true, refreshing: false });
    useChatShellStore.setState({ sessionListRefreshToken: 0 });
    useChatModeStore.setState({ scopeDocumentIds: null });
    // C5's precondition: the settings store must never pre-satisfy a check.
    useSettingsStore.getState().resetState();
    useSettingsStore.setState({ settings: null });
    // Loader-memo reset (plan-critic round 6): the per-owner cap memo must
    // not leak between cases — the suite stays order-independent. No-op at
    // the pre-fix tree (the seam ships with the fix); NOT driven through
    // reserveReplacementAuthOwner, whose immediate re-arm would fetch the
    // default before a test installs its custom response.
    (useSendMessageModule as { __resetChatInputCapForTests?: () => void })
      .__resetChatInputCapForTests?.();
    useUploadStore.setState({
      uploads: [],
      chatAttachmentIds: [],
      isProcessing: false,
      activeVaultId: null,
    });

    // Vault store mock with selector support (mirrors the revision suite).
    (useVaultStore as unknown as ReturnType<typeof vi.fn>).mockImplementation(
      (selector?: (s: unknown) => unknown) => {
        const state = {
          vaults: [{ id: 1, name: "Test Vault", file_count: 5 }],
          activeVaultId: 1,
          getActiveVault: mockGetActiveVault,
        };
        return selector ? selector(state) : state;
      },
    );
    mockGetActiveVault.mockReturnValue({ id: 1, name: "Test Vault", file_count: 5 });
  });

  afterEach(() => {
    cleanup();
  });

  it("C4: typing a 25_000-char message with default server config surfaces an error naming the 20_000 bound on the change-feedback surface (RED at base, GREEN after the fix)", async () => {
    render(<TranscriptPane />);
    const textarea = screen.getByLabelText("Message input");
    await settle();

    await act(async () => {
      fireEvent.change(textarea, { target: { value: "a".repeat(25_000) } });
    });

    await waitFor(
      () => {
        expect(
          useChatStore.getState().inputError ?? "",
          "a 25_000-char input is over the effective server cap (20_000 default): the composer's change feedback must surface an error naming that exact bound, not just 'maximum length'",
        ).toMatch(NAMES_20_000);
      },
      { timeout: 2_000 },
    );

    // The surfaced error names the SERVER bound — not the stale 100_000 gate.
    expect(useChatStore.getState().inputError ?? "").not.toMatch(/100[ ,]?000/);
  });

  it("C4: the submit gate refuses a 25_000-char input — no stream starts and the error names the 20_000 bound (RED at base, GREEN after the fix)", async () => {
    // Seed the session BEFORE render so the composer's draft-restore effect
    // settles at mount; seeding the over-cap input after render then sticks
    // (a post-render activeChatId change would re-run the restore and wipe
    // it). This isolates the submit gate — the change surface is covered by
    // the test above — and an existing session means a base-tree send that
    // slips past the gate reaches the stream immediately.
    useChatStore.setState({ activeChatId: "42" });
    render(<TranscriptPane />);
    act(() => {
      useChatStore.setState({ input: "b".repeat(25_000) });
    });
    const textarea = screen.getByLabelText("Message input");
    await settle();

    await act(async () => {
      // Enter is the real user path (a disabled Send button cannot be
      // clicked, but keydown still reaches the composer's submit handler).
      fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });
    });

    expect(
      apiMocks.chatStream,
      "the submit gate must refuse an over-cap input before any transport — never ship it to a guaranteed 422",
    ).not.toHaveBeenCalled();
    expect(
      apiMocks.createChatSession,
      "the refused submit must not create a chat session either — an over-cap send that never streams must not leave an orphan session behind",
    ).not.toHaveBeenCalled();

    await waitFor(
      () => {
        expect(
          useChatStore.getState().inputError ?? "",
          "the refused send must tell the user the actual bound (20_000), not a generic maximum-length line",
        ).toMatch(NAMES_20_000);
      },
      { timeout: 2_000 },
    );
  });

  it("C5: a 6_000-char input is rejected naming the server-configured 5000 bound, and the composer actually fetched the settings (RED at base, GREEN after the fix)", async () => {
    // Precondition: the settings store must NOT pre-satisfy the check — the
    // bound has to arrive through the fetch flow.
    expect(useSettingsStore.getState().settings).toBeNull();
    apiMocks.getSettings.mockReset().mockResolvedValue({
      chat_message_max_chars: 5000,
    } as unknown as SettingsResponse);

    render(<TranscriptPane />);

    // (a) The fetch flow must run: getSettings (the @/lib/api seam) is
    // actually CALLED — a pre-seeded store is not a fix.
    await waitFor(
      () => {
        expect(
          apiMocks.getSettings.mock.calls.length,
          "the composer must fetch the server settings (getSettings) so its gate tracks the configured cap — at base no fetch informs the composer",
        ).toBeGreaterThanOrEqual(1);
      },
      { timeout: 2_000 },
    );
    // Let the fetched cap apply before driving the gate.
    await settle();

    const textarea = screen.getByLabelText("Message input");
    await act(async () => {
      fireEvent.change(textarea, { target: { value: "c".repeat(6_000) } });
    });

    // (b) The surfaced error names 5000 exactly — not the 20_000 default, not
    // the stale 100_000 gate, not "maximum length" alone.
    await waitFor(
      () => {
        const error = useChatStore.getState().inputError ?? "";
        expect(
          error,
          "the gate must derive its bound from the server setting: a 6_000-char input is rejected naming 5000",
        ).toMatch(NAMES_5_000);
        expect(error).not.toMatch(NAMES_20_000);
        expect(error).not.toMatch(/100[ ,]?000/);
      },
      { timeout: 2_000 },
    );
  });

  it("C6: retrying a persisted 25_000-char turn is refused BEFORE the server truncate — rows intact, no stream, named-bound error surfaced (RED at base, GREEN after the fix)", async () => {
    // A persisted session whose LAST user turn is over the server cap, loaded
    // through the real mapper so store rows carry server ids/seqs.
    const rows = [
      seedRow(1, "user", "question-1", 1),
      seedRow(2, "assistant", "answer-1", 2),
      seedRow(3, "user", "d".repeat(25_000), 3),
      seedRow(4, "assistant", "answer-2", 4),
    ];
    act(() => {
      useChatStore.getState().loadChat("42", rows.map(mapSessionMessage));
    });

    render(<TranscriptPane />);
    await settle();

    // The REAL retry entry: the production handleRetry (which today truncates
    // the server history BEFORE sendDirect) driven through its affordance.
    await act(async () => {
      await userEvent.click(screen.getByLabelText("Retry 4"));
    });
    // Let any in-flight revision work settle before asserting the refusal.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 25));
    });

    expect(
      truncateChatSessionMock,
      "an over-cap retry must be refused BEFORE truncating the server history — at base the retry truncates first and then ships the over-cap turn",
    ).not.toHaveBeenCalled();

    // No local rows removed: the over-cap user message is still in the store.
    const state = useChatStore.getState();
    expect(
      state.messageIds.length,
      "the pre-truncate refusal must leave the transcript untouched",
    ).toBe(4);
    const overCapRow = state.messagesById[state.messageIds[2]];
    expect(overCapRow?.role).toBe("user");
    expect(overCapRow?.content).toHaveLength(25_000);

    expect(
      apiMocks.chatStream,
      "an over-cap retry must never start a stream",
    ).not.toHaveBeenCalled();

    // The refusal is actionable: a named-bound error IS surfaced (toast or
    // input error).
    expect(
      gatheredErrors().some((message) => NAMES_20_000.test(message)),
      "the refused retry must surface an error naming the 20_000 bound so the user knows why",
    ).toBe(true);
  });

  it("C6: deferred settings — a retry whose cap loads LATE still never truncates (RED at base, GREEN after the fix)", async () => {
    // The settings fetch resolves chat_message_max_chars = 5000 only when the
    // test releases it (deferred promise on the getSettings seam). While the
    // promise is pending the effective cap is UNKNOWN, and an unknown cap
    // must never be guessed as the default. Every call gets the SAME gate so
    // any number of fetches in the fixed flow are held uniformly.
    let releaseSettings: ((settings: SettingsResponse) => void) | undefined;
    const settingsGate = new Promise<SettingsResponse>((resolve) => {
      releaseSettings = resolve;
    });
    apiMocks.getSettings.mockReset().mockImplementation(() => settingsGate);

    // A persisted session whose LAST user turn is 6_000 chars — under the
    // 20_000 default but over the 5_000 cap that is still in flight.
    const rows = [
      seedRow(1, "user", "question-1", 1),
      seedRow(2, "assistant", "answer-1", 2),
      seedRow(3, "user", "f".repeat(6_000), 3),
      seedRow(4, "assistant", "answer-2", 4),
    ];
    act(() => {
      useChatStore.getState().loadChat("42", rows.map(mapSessionMessage));
    });

    render(<TranscriptPane />);
    await settle();

    // The REAL retry affordance drives the production handleRetry.
    await act(async () => {
      await userEvent.click(screen.getByLabelText("Retry 4"));
    });
    // Let any in-flight revision work settle before asserting.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 25));
    });

    // While the settings promise is STILL pending: no truncate. The retry
    // must not run its server-side history cut on an unknown cap.
    expect(
      truncateChatSessionMock,
      "while the settings fetch is still pending the retry must not truncate — an unknown cap is not license to cut server history",
    ).not.toHaveBeenCalled();

    // Release the deferred cap (5000) and let the refusal land.
    expect(releaseSettings).toBeDefined();
    await act(async () => {
      releaseSettings?.({ chat_message_max_chars: 5000 } as unknown as SettingsResponse);
      await new Promise((resolve) => setTimeout(resolve, 25));
    });

    // Once the cap lands: STILL no truncate (the 6_000-char turn is over the
    // now-known 5_000 cap and must be refused BEFORE the truncate).
    expect(
      truncateChatSessionMock,
      "once the 5000 cap arrives the over-cap retry is refused BEFORE the server truncate — never any history cut",
    ).not.toHaveBeenCalled();

    // No local rows removed: the 6_000-char user message is still in the store.
    const state = useChatStore.getState();
    expect(
      state.messageIds.length,
      "the pre-truncate refusal must leave the transcript untouched",
    ).toBe(4);
    const overCapRow = state.messagesById[state.messageIds[2]];
    expect(overCapRow?.role).toBe("user");
    expect(overCapRow?.content).toHaveLength(6_000);

    expect(
      apiMocks.chatStream,
      "an over-cap retry must never start a stream",
    ).not.toHaveBeenCalled();

    // The refusal is actionable and names the bound that arrived late.
    await waitFor(
      () => {
        expect(
          gatheredErrors().some((message) => NAMES_5_000.test(message)),
          "once the 5000 cap lands the refusal must name it so the user knows why",
        ).toBe(true);
      },
      { timeout: 2_000 },
    );
  });

  it("C6: sendDirect with over-cap content sets the named-bound error and never reaches the stream layer (backstop) (RED at base, GREEN after the fix)", async () => {
    useChatStore.setState({ activeChatId: "42" });
    const { result } = renderHook(() => useSendMessage(7, mockRefreshHistory));

    await act(async () => {
      await result.current.sendDirect("e".repeat(25_000), []);
    });

    expect(
      apiMocks.chatStream,
      "the sendDirect pre-gate must refuse over-cap content before the stream layer — at base the resend reaches the transport",
    ).not.toHaveBeenCalled();

    expect(
      gatheredErrors().some((message) => NAMES_20_000.test(message)),
      "the refused direct send must surface an error naming the 20_000 bound",
    ).toBe(true);
  });

  it("C4: no active chat — an over-cap submit never creates a session (RED at base, GREEN after the fix)", async () => {
    // NO activeChatId is seeded and the single accessible vault is mocked
    // (the beforeEach default), so the base send path WOULD proceed: it
    // creates a session for the over-cap turn and then streams it (the
    // 25_000 input sails through the base 100_000 gate). The pre-send gate
    // must refuse BEFORE any session creation — an over-cap send that can
    // never stream must not leave an orphan session behind.
    render(<TranscriptPane />);
    const textarea = screen.getByLabelText("Message input");
    await settle();

    await act(async () => {
      fireEvent.change(textarea, { target: { value: "a".repeat(25_000) } });
    });
    await act(async () => {
      // Enter is the real user path (a disabled Send button cannot be
      // clicked, but keydown still reaches the composer's submit handler).
      fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });
    });

    expect(
      apiMocks.createChatSession,
      "an over-cap submit with no active chat must not create a chat session — at base the send proceeds under the 100_000 gate and creates the session anyway",
    ).not.toHaveBeenCalled();
    expect(
      apiMocks.chatStream,
      "an over-cap submit with no active chat must never start a stream either — at base the created session is streamed into",
    ).not.toHaveBeenCalled();

    await waitFor(
      () => {
        expect(
          gatheredErrors().some((message) => NAMES_20_000.test(message)),
          "the refused first-message send must still tell the user the actual bound (20_000 default)",
        ).toBe(true);
      },
      { timeout: 2_000 },
    );
  });

  it("C4/C5: failed settings acquisition blocks the send and preserves the composer artifacts — draft and attachment chip survive (RED at base, GREEN after the fix)", async () => {
    // Plan rev 4, acquisition state (2): the settings fetch FAILS, so the
    // cap cannot be acquired and the send must be refused with an
    // actionable limit-unavailable error. Because the composer's cleanup is
    // deferred until a send is admitted, the draft and the attached files
    // must survive the refused submit.
    apiMocks.getSettings.mockReset().mockRejectedValue(new Error("Failed to fetch"));
    // A COMPLETED (indexed) attachment chip in the real upload store — the
    // tray renders it, and an admitted send would detach it.
    const upload: UploadFile = {
      id: "u1",
      file: new File(["x"], "notes.txt", { type: "text/plain" }),
      uploadProgress: 100,
      progress: 100,
      status: "indexed",
      statusSeen: true,
      chunkCount: 5,
    };
    useUploadStore.setState({ uploads: [upload], chatAttachmentIds: ["u1"] });

    render(<TranscriptPane />);
    const textarea = screen.getByLabelText("Message input");
    await settle();

    const input = "b".repeat(15_000); // under the 20_000 advisory display
    await act(async () => {
      fireEvent.change(textarea, { target: { value: input } });
    });
    await act(async () => {
      fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });
    });

    expect(
      apiMocks.chatStream,
      "a send whose cap could not be acquired must be refused before the transport — at base the 15_000-char input ships (gate 100_000, no fetch dependency)",
    ).not.toHaveBeenCalled();
    expect(
      apiMocks.createChatSession,
      "a cap-unavailable send must not create a chat session either",
    ).not.toHaveBeenCalled();

    await waitFor(
      () => {
        expect(
          gatheredErrors().some((message) => /unavailable|try again|limit/i.test(message)),
          "the blocked send must surface an actionable message-limit-unavailable-style error, not a silent no-op",
        ).toBe(true);
      },
      { timeout: 2_000 },
    );

    // Composer artifacts survive the blocked send: the textarea still holds
    // the input...
    expect(
      (textarea as HTMLTextAreaElement).value,
      "the draft must not be cleared by a refused send — cleanup is deferred until a send is admitted",
    ).toBe(input);
    // ...the persisted draft storage still holds it (the send path's
    // synchronous clearDraftNow must not run for a refused send)...
    expect(
      localStorage.getItem(getDraftKey(null, null)),
      "the persisted draft key must survive a refused send (at base the send path synchronously removes it)",
    ).toBe(input);
    // ...and the completed attachment's chat association is NOT detached
    // (an admitted send would have detached the used-up chip).
    expect(
      screen.getByTestId("attachment-indexed"),
      "the completed attachment chip must stay in the composer after a refused send",
    ).toBeInTheDocument();
    expect(
      useUploadStore.getState().chatAttachmentIds,
      "the completed attachment's chat association must survive a refused send",
    ).toContain("u1");
  });

  it("C5: legacy absent-field response blocks with limit-unavailable (RED at base, GREEN after the fix)", async () => {
    // Plan rev 5, acquisition state (2): the fetch SUCCEEDS but the response
    // is the legacy `{}` — no chat_message_max_chars field. The cap is then
    // UNKNOWABLE (a legacy server may be configured BELOW the default), so
    // the send must block with an actionable limit-unavailable error and
    // must NOT claim a 20_000-named bound out of thin air.
    apiMocks.getSettings.mockReset().mockResolvedValue({} as SettingsResponse);

    render(<TranscriptPane />);
    const textarea = screen.getByLabelText("Message input");
    await settle();

    const input = "b".repeat(15_000); // under the advisory 20_000 display cap
    await act(async () => {
      fireEvent.change(textarea, { target: { value: input } });
    });
    await act(async () => {
      fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });
    });

    expect(
      apiMocks.chatStream,
      "a send whose cap is unknowable (legacy absent-field response) must be refused before the transport — at base the 15_000-char input ships under the 100_000 gate",
    ).not.toHaveBeenCalled();
    expect(
      apiMocks.createChatSession,
      "an unknowable-cap send must not create a chat session either",
    ).not.toHaveBeenCalled();

    await waitFor(
      () => {
        expect(
          gatheredErrors().some((message) => /unavailable|try again|limit/i.test(message)),
          "the legacy-{} refusal must surface an actionable message-limit-unavailable-style error",
        ).toBe(true);
      },
      { timeout: 2_000 },
    );

    // The 20_000-named bound must NOT be claimed from `{}`: an absent field
    // never carries the default cap, so naming 20_000 here would be a
    // fabricated bound (the rev-4 backward-compat default is superseded).
    expect(
      gatheredErrors().some((message) => NAMES_20_000.test(message)),
      "no surfaced error may name 20_000 for a legacy absent-field response — the cap is unknowable, not defaulted",
    ).toBe(false);
  });

  it("C4/C5: \"Send without these files\" also defers artifact cleanup on failed acquisition (RED at base, GREEN after the fix)", async () => {
    // Plan rev 5: BOTH Composer send closures route through ONE awaited
    // admission helper. This leg drives the SECOND closure — the
    // "Send without these files" action on the indexing toast
    // (Composer.tsx handleSubmit: an indexing/uploaded chip makes the plain
    // submit path return after toast.warning, and the action's onClick is
    // the alternate send tail). With cap acquisition FAILING, that action
    // must block before any transport AND before ANY artifact cleanup.
    apiMocks.getSettings.mockReset().mockRejectedValue(new Error("Failed to fetch"));
    // An INDEXING attachment chip — no documentId, so the shared upload
    // monitor has nothing to poll; the chip status drives the toast path.
    const upload: UploadFile = {
      id: "u1",
      file: new File(["x"], "notes.txt", { type: "text/plain" }),
      uploadProgress: 100,
      progress: 100,
      status: "indexing",
      statusSeen: true,
      chunkCount: 5,
    };
    useUploadStore.setState({ uploads: [upload], chatAttachmentIds: ["u1"] });

    render(<TranscriptPane />);
    const textarea = screen.getByLabelText("Message input");
    await settle();

    const input = "s".repeat(15_000); // under the advisory 20_000 display cap
    await act(async () => {
      fireEvent.change(textarea, { target: { value: input } });
    });
    await act(async () => {
      fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });
    });

    // The toast affordance fired with the real action (sonner is mocked, so
    // the captured options object carries the production onClick closure).
    const warningCalls = vi.mocked(toast.warning).mock.calls;
    expect(warningCalls.length, "an indexing chip must make the submit surface the still-indexing warning").toBeGreaterThan(0);
    const lastCall = warningCalls[warningCalls.length - 1];
    const action = (
      lastCall?.[1] as unknown as
        | { action?: { label: unknown; onClick: () => void } }
        | undefined
    )?.action;
    expect(action?.label, "the toast action is the Send-without-these-files affordance").toBe("Send without these files");
    expect(action?.onClick, "the captured affordance must expose its onClick so the harness drives the real closure").toBeTypeOf("function");

    // Trigger the REAL alternate send closure.
    await act(async () => {
      action?.onClick();
    });

    expect(
      apiMocks.chatStream,
      "the toast action's send must also refuse on failed cap acquisition — at base the action ships the 15_000-char input straight to the transport",
    ).not.toHaveBeenCalled();
    expect(
      apiMocks.createChatSession,
      "the toast action's send must not create a chat session on failed cap acquisition either",
    ).not.toHaveBeenCalled();

    await waitFor(
      () => {
        expect(
          gatheredErrors().some((message) => /unavailable|try again|limit/i.test(message)),
          "the blocked toast-action send must surface an actionable limit-unavailable-style error",
        ).toBe(true);
      },
      { timeout: 2_000 },
    );

    // Artifact cleanup is DEFERRED: the draft storage survives...
    expect(
      localStorage.getItem(getDraftKey(null, null)),
      "the toast action must not clear the persisted draft on a refused send — at base its onClick clears it synchronously",
    ).toBe(input);
    // ...the textarea still holds the draft...
    expect(
      (textarea as HTMLTextAreaElement).value,
      "the draft must not be cleared from the composer by the refused toast-action send",
    ).toBe(input);
    // ...and the indexing chip's chat association is NOT detached.
    expect(
      screen.getByTestId("attachment-indexing"),
      "the indexing attachment chip must stay in the composer after the refused toast-action send",
    ).toBeInTheDocument();
    expect(
      useUploadStore.getState().chatAttachmentIds,
      "the toast action's detach must not run for a refused send — at base the action detaches the indexing chip before sending",
    ).toContain("u1");
  });

  it("C5: an admitted send still performs artifact cleanup — only AFTER admission (RED at base, GREEN after the fix)", async () => {
    // Plan rev 5: artifacts are touched only after admission, and an
    // ADMITTED send must still clean them up. The settings fetch is held
    // open (deferred gate resolving the explicit 20_000 default), so the
    // submit sits at the admission boundary: while the fetch is pending the
    // draft is STILL persisted (at base the submit tail clears it
    // synchronously → RED via draft-cleared-early), and once the cap lands
    // the 10-char send proceeds and the cleanup completes.
    let releaseSettings: ((settings: SettingsResponse) => void) | undefined;
    const settingsGate = new Promise<SettingsResponse>((resolve) => {
      releaseSettings = resolve;
    });
    apiMocks.getSettings.mockReset().mockImplementation(() => settingsGate);
    // A COMPLETED (indexed) attachment chip — the plain submit path (no
    // toast), and an admitted send detaches it.
    const upload: UploadFile = {
      id: "u1",
      file: new File(["x"], "notes.txt", { type: "text/plain" }),
      uploadProgress: 100,
      progress: 100,
      status: "indexed",
      statusSeen: true,
      chunkCount: 5,
    };
    useUploadStore.setState({ uploads: [upload], chatAttachmentIds: ["u1"] });

    render(<TranscriptPane />);
    const textarea = screen.getByLabelText("Message input");
    await settle();

    const input = "hello cap!"; // 10 chars — well under any bound
    await act(async () => {
      fireEvent.change(textarea, { target: { value: input } });
    });
    await act(async () => {
      fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });
    });

    // While the admission fetch is STILL pending: no transport has started
    // and no artifact has been touched. At base the submit tail runs
    // synchronously — the draft key is already gone and the send is already
    // at the stream layer → both assertions below are the RED vectors.
    expect(
      localStorage.getItem(getDraftKey(null, null)),
      "while admission is pending the persisted draft must survive — at base the submit tail clears it synchronously (draft-cleared-early)",
    ).toBe(input);
    expect(
      apiMocks.chatStream,
      "while admission is pending no stream may start — at base the 10-char send is already at the transport",
    ).not.toHaveBeenCalled();
    expect(
      apiMocks.createChatSession,
      "while admission is pending no session may be created — at base the first-message send creates one immediately",
    ).not.toHaveBeenCalled();

    // Release the cap (explicit current-default 20_000): the send is within
    // bound, admission passes, and the cleanup runs POST-admission.
    await act(async () => {
      releaseSettings?.({ chat_message_max_chars: 20000 } as unknown as SettingsResponse);
      await new Promise((resolve) => setTimeout(resolve, 25));
    });

    await waitFor(
      () => {
        expect(
          apiMocks.chatStream,
          "once the 20_000 cap lands the within-bound send must proceed to the transport",
        ).toHaveBeenCalled();
      },
      { timeout: 2_000 },
    );
    expect(
      apiMocks.createChatSession,
      "the admitted first-message send proceeds through session creation as usual",
    ).toHaveBeenCalled();
    await waitFor(
      () => {
        expect(
          localStorage.getItem(getDraftKey(null, null)),
          "an admitted send must clear the persisted draft once admission has passed",
        ).toBeNull();
      },
      { timeout: 2_000 },
    );
    expect(
      useUploadStore.getState().chatAttachmentIds,
      "an admitted send must detach the used-up completed chip after admission",
    ).not.toContain("u1");
  });

  it("C5: a present-but-null chat_message_max_chars BLOCKS the send instead of silently defaulting (RED at base, GREEN after the fix)", async () => {
    // Plan rev 4, acquisition state (2): the field is PRESENT but null — a
    // malformed cap is an acquisition failure, never license to fall back
    // to the 20_000 default and ship.
    apiMocks.getSettings.mockReset().mockResolvedValue({
      chat_message_max_chars: null,
    } as unknown as SettingsResponse);

    render(<TranscriptPane />);
    const textarea = screen.getByLabelText("Message input");
    await settle();

    const input = "c".repeat(15_000); // under the 20_000 default
    await act(async () => {
      fireEvent.change(textarea, { target: { value: input } });
    });
    await act(async () => {
      fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });
    });

    expect(
      apiMocks.chatStream,
      "a malformed (null) cap must block the send — at base the 15_000-char input ships under the 100_000 gate",
    ).not.toHaveBeenCalled();
    expect(
      apiMocks.createChatSession,
      "a malformed-cap send must not create a chat session either",
    ).not.toHaveBeenCalled();

    await waitFor(
      () => {
        expect(
          gatheredErrors().some((message) => /unavailable|try again|limit/i.test(message)),
          "the malformed-cap refusal must surface an actionable limit-unavailable-style error",
        ).toBe(true);
      },
      { timeout: 2_000 },
    );
  });

  it("C5: no ceiling clamp — a server cap above the legacy bound is served as-is (RED at base, GREEN after the fix)", async () => {
    // Plan rev 4, acquisition state (3): a valid integer >= 1 is the
    // AUTHORITATIVE cap; a server cap of 150_000 must admit a 120_000-char
    // send with no clamp back to the legacy 100_000 bound. At base the
    // 120_000 input exceeds MAX_INPUT_LENGTH → the submit is blocked and no
    // stream ever starts → RED via chatStream-not-called.
    apiMocks.getSettings.mockReset().mockResolvedValue({
      chat_message_max_chars: 150000,
    } as unknown as SettingsResponse);

    render(<TranscriptPane />);
    const textarea = screen.getByLabelText("Message input");
    await settle();

    await act(async () => {
      fireEvent.change(textarea, { target: { value: "d".repeat(120_000) } });
    });
    await act(async () => {
      fireEvent.keyDown(textarea, { key: "Enter", shiftKey: false });
    });

    await waitFor(
      () => {
        expect(
          apiMocks.chatStream,
          "a 120_000-char input is within the server's 150_000 cap: the send must proceed — the client must not clamp the authoritative cap to the legacy 100_000 bound",
        ).toHaveBeenCalled();
      },
      { timeout: 2_000 },
    );
    expect(
      apiMocks.createChatSession,
      "the within-cap first message proceeds through session creation as usual",
    ).toHaveBeenCalled();

    // And the refusal surface never fired for the within-cap input: no
    // over-cap error was surfaced anywhere (at base the submit leaves
    // 'Input exceeds maximum length of 100000 characters' behind).
    expect(
      gatheredErrors().some((message) =>
        /exceeds (the )?maximum length|exceeds maximum|too long/i.test(message),
      ),
      "a within-cap input must not surface any over-cap error (and never the stale 100_000 bound)",
    ).toBe(false);
  });

  it("C5: a REAL owner replacement mid-load discards the in-flight cap and re-arms the loader — the new owner's own fetch gates (RED at base, GREEN after the fix)", async () => {
    // Round-5 rewrite: the owner replacement is driven through the REAL
    // auth-lifecycle seam — reserveReplacementAuthOwner (auth-lifecycle.ts:
    // replaces the module's current owner, aborts the previous owner's
    // signal, and fires the replacement listeners). Plan rev 5 keys the cap
    // loader on that owner: a replacement discards the in-flight result and
    // re-fetches fresh, WITHOUT requiring another send.
    //
    // Two deferred gates on the getSettings seam: every fetch started
    // BEFORE the replacement resolves through gateA (the STALE 5000
    // payload), every fetch started AFTER through gateB (the same server
    // value, acquired by the NEW owner).
    let ownerReplaced = false;
    let releaseA: ((settings: SettingsResponse) => void) | undefined;
    let releaseB: ((settings: SettingsResponse) => void) | undefined;
    const gateA = new Promise<SettingsResponse>((resolve) => {
      releaseA = resolve;
    });
    const gateB = new Promise<SettingsResponse>((resolve) => {
      releaseB = resolve;
    });
    apiMocks
      .getSettings.mockReset()
      .mockImplementation(() => (ownerReplaced ? gateB : gateA));

    // RESTORE note: auth-lifecycle has NO test-reset export (verified by
    // reading the module — capture/isCurrent/reserve/subscribe/enqueue
    // exports only). reserveReplacementAuthOwner mutates the module-local
    // currentOwner and aborts the previous owner's controller; the pre-test
    // owner object itself is unrecoverable. The closest restoration — used
    // in the finally below — is ONE more reserveReplacementAuthOwner() so
    // the module's current owner is a FRESH, un-aborted owner for any later
    // capture (later consumers call captureAuthOwner() at their own use
    // time, so a fresh owner is behaviorally equivalent to the module-init
    // one). At base the test aborts before any replacement (no loader), so
    // the restore only ever runs post-fix.
    let replacedForTeardown = false;
    try {
      render(<TranscriptPane />);
      // At base nothing in the composer path fetches settings, so the test
      // is already RED here; the legs below pin the post-fix semantics.
      await waitFor(
        () => {
          expect(
            apiMocks.getSettings.mock.calls.length,
            "the mount-time cap fetch must have started before the owner is replaced",
          ).toBeGreaterThanOrEqual(1);
        },
        { timeout: 2_000 },
      );
      const preReplacementFetches = apiMocks.getSettings.mock.calls.length;

      // The REAL owner replacement, while the mount-time cap fetch (gateA)
      // is still pending. The flag flips BEFORE the call: replacement
      // listeners run synchronously inside reserveReplacementAuthOwner, so
      // gateB's refetch must already see the replacement mock routing.
      ownerReplaced = true;
      reserveReplacementAuthOwner();
      replacedForTeardown = true;

      // (b, fetch side) The loader RE-ARMS at the owner boundary: the new
      // owner's own fetch (gateB) starts WITHOUT another send being driven.
      await waitFor(
        () => {
          expect(
            apiMocks.getSettings.mock.calls.length,
            "an owner replacement must re-arm the cap loader: the new owner's own fetch starts with no second send",
          ).toBeGreaterThan(preReplacementFetches);
        },
        { timeout: 2_000 },
      );

      const textarea = screen.getByLabelText("Message input");

      // (a) Discard: release the STALE fetch's payload (5000). It settled
      // after the owner was replaced, so its result must be discarded at
      // the owner boundary — it must NOT gate the new owner's composer.
      // NOTE (disclosed): this sub-assertion alone is GREEN at base — base
      // never acquires a cap, so no error ever names 5000. The test's
      // RED-at-base comes from the two getSettings waitFors above/below and
      // the fresh-fetch gate leg, so the overall test is RED at base. The
      // assertion the harness can make honestly is behavioral (the stale
      // value never surfaces): the plan does not fix WHERE the cap is
      // stored, so asserting a specific store write would over-specify.
      await act(async () => {
        releaseA?.({ chat_message_max_chars: 5000 } as unknown as SettingsResponse);
        await new Promise((resolve) => setTimeout(resolve, 25));
      });
      const input = "e".repeat(6_000);
      await act(async () => {
        fireEvent.change(textarea, { target: { value: input } });
      });
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 50));
      });
      expect(
        gatheredErrors().some((message) => NAMES_5_000.test(message)),
        "the pre-replacement fetch's 5000 settled after the owner was replaced: applying it to the new owner would enforce the previous owner's server view",
      ).toBe(false);

      // (b, gate side) The NEW owner's own fetch resolves the SAME server
      // value through gateB — and NOW the gate must enforce it (the
      // discard must not permanently disable cap acquisition).
      await act(async () => {
        releaseB?.({ chat_message_max_chars: 5000 } as unknown as SettingsResponse);
        await new Promise((resolve) => setTimeout(resolve, 25));
      });
      // Re-drive the change surface so validation runs against the arrived
      // cap even without reactive re-validation (reactive re-validation is
      // pinned separately by the runtime-cap-change leg below).
      await act(async () => {
        fireEvent.change(textarea, { target: { value: input } });
      });
      await waitFor(
        () => {
          expect(
            gatheredErrors().some((message) => NAMES_5_000.test(message)),
            "after the owner replacement the new owner's own fetch must arm the 5000 cap: a 6_000-char input is rejected naming 5000",
          ).toBe(true);
        },
        { timeout: 2_000 },
      );
    } finally {
      if (replacedForTeardown) {
        // (c) RESTORE — see the RESTORE note above: leave a fresh,
        // un-aborted owner behind so later tests are unaffected.
        reserveReplacementAuthOwner();
      }
    }
  });

  it("C5: a runtime cap change re-validates the composer without wiping the draft (RED at base, GREEN after the fix)", async () => {
    // The cap arrives LATE through the fetch flow the app uses: the
    // mount-time settings fetch is held open until the test releases it
    // (the deferred idiom of the C6 deferred-settings leg).
    let releaseSettings: ((settings: SettingsResponse) => void) | undefined;
    const settingsGate = new Promise<SettingsResponse>((resolve) => {
      releaseSettings = resolve;
    });
    apiMocks.getSettings.mockReset().mockImplementation(() => settingsGate);

    render(<TranscriptPane />);
    const textarea = screen.getByLabelText("Message input");
    await settle();

    const input = "f".repeat(6_000); // under the 20_000 default — no error yet
    await act(async () => {
      fireEvent.change(textarea, { target: { value: input } });
    });
    // While the cap is still in flight the input is under the default (and
    // under any hold semantics): no bound-naming error.
    expect(
      gatheredErrors().some((message) => NAMES_5_000.test(message) || NAMES_20_000.test(message)),
      "a 6_000-char input under the 20_000 default must not error while the cap is still in flight",
    ).toBe(false);

    // The cap lands at runtime: 5000.
    await act(async () => {
      releaseSettings?.({ chat_message_max_chars: 5000 } as unknown as SettingsResponse);
      await new Promise((resolve) => setTimeout(resolve, 25));
    });

    // The composer re-validates the EXISTING input against the new cap...
    await waitFor(
      () => {
        expect(
          gatheredErrors().some((message) => NAMES_5_000.test(message)),
          "a late-arriving cap must re-validate the composer: the 6_000-char draft is now over the 5_000 cap and the error must name 5000",
        ).toBe(true);
      },
      { timeout: 2_000 },
    );
    // ...the character counter tracks the runtime cap (denominator 5000,
    // never the stale 100_000 gate)...
    expect(
      document.body.textContent ?? "",
      "the character counter must name the runtime cap (6000/5000), not the stale 100_000 gate",
    ).toMatch(/6[ ,]?000\s*\/\s*5[ ,]?000/);
    // ...and the draft was NOT wiped by the re-validation.
    expect(
      (textarea as HTMLTextAreaElement).value,
      "re-validation must not wipe the draft — the 6_000 chars stay in the composer for shortening",
    ).toBe(input);
  });

  it("C5: a persisted draft survives runtime cap changes (RED at base, GREEN after the fix)", async () => {
    // Plan rev 5: draft persistence is DECOUPLED from the cap — the persisted
    // draft key/content must be unchanged by cap arrivals, whatever the cap
    // does to validation. Seeded BEFORE render so the composer's real
    // mount-time draft restore loads it into the textarea.
    const draft = "h".repeat(6_000);
    localStorage.setItem(getDraftKey(null, null), draft);
    // SEAM (disclosed): both cap arrivals flow through the app's OWN fetch
    // path — the getSettings seam resolves 5000 on the first fetch and
    // 20000 on every later one, and the SECOND arrival is triggered by the
    // loader's owner-keyed re-fetch (plan rev 5: a replacement owner
    // re-fetches fresh) driven through the real reserveReplacementAuthOwner.
    // No store is hand-written: whatever the loader does with each fetched
    // response is the fix's own bookkeeping.
    let settingsCall = 0;
    apiMocks.getSettings.mockReset().mockImplementation(() => {
      settingsCall += 1;
      return Promise.resolve(
        (settingsCall === 1
          ? { chat_message_max_chars: 5000 }
          : { chat_message_max_chars: 20000 }) as unknown as SettingsResponse,
      );
    });

    let replacedForTeardown = false;
    try {
      render(<TranscriptPane />);
      const textarea = screen.getByLabelText("Message input");
      await waitFor(
        () => {
          expect(
            (textarea as HTMLTextAreaElement).value.length,
            "the seeded persisted draft must be restored into the composer at mount",
          ).toBe(6_000);
        },
        { timeout: 2_000 },
      );

      // First cap arrival (5000) through the app's fetch flow.
      await waitFor(
        () => {
          expect(
            apiMocks.getSettings.mock.calls.length,
            "the app's own fetch flow must acquire the cap — the first arrival is 5000",
          ).toBeGreaterThanOrEqual(1);
        },
        { timeout: 2_000 },
      );
      await settle();

      // RED vector (same as the round-4 runtime-cap-change leg): at base the
      // composer shows NO bound feedback for the 6_000-char draft even while
      // settings resolve 5000 — base never fetches settings at all.
      await act(async () => {
        fireEvent.change(textarea, { target: { value: draft } });
      });
      await waitFor(
        () => {
          expect(
            gatheredErrors().some((message) => NAMES_5_000.test(message)),
            "a restored 6_000-char draft must show bound feedback naming 5000 once the settings fetch resolves 5000",
          ).toBe(true);
        },
        { timeout: 2_000 },
      );

      // Survival, arrival 1 (disclosed GREEN at base — base never touches
      // the key after the restore, so the draft trivially survives there;
      // the leg's RED vector is the missing 5000-named feedback above).
      expect(
        localStorage.getItem(getDraftKey(null, null)),
        "the persisted draft must survive the 5000 cap arrival untouched (draft persistence is decoupled from the cap)",
      ).toBe(draft);

      // Second cap arrival (20000) through the loader's owner-keyed
      // re-fetch — no store hand-written, no new send required.
      reserveReplacementAuthOwner();
      replacedForTeardown = true;
      await waitFor(
        () => {
          expect(
            apiMocks.getSettings.mock.calls.length,
            "the owner-keyed loader must re-fetch after the owner replacement — the second settings arrival (20000) flows through the app's fetch path",
          ).toBeGreaterThanOrEqual(2);
        },
        { timeout: 2_000 },
      );
      await settle();

      // 6_000 is within the relaxed 20_000 cap: the 5000-named feedback must
      // clear, and the persisted draft must STILL be untouched.
      await act(async () => {
        fireEvent.change(textarea, { target: { value: draft } });
      });
      await waitFor(
        () => {
          expect(
            gatheredErrors().some((message) => NAMES_5_000.test(message)),
            "once the cap relaxes to 20_000 the 6_000-char draft is within bound and the 5000-named feedback must clear",
          ).toBe(false);
        },
        { timeout: 2_000 },
      );
      expect(
        localStorage.getItem(getDraftKey(null, null)),
        "the persisted draft must survive the 20000 cap arrival untouched too",
      ).toBe(draft);
    } finally {
      if (replacedForTeardown) {
        // RESTORE — same note as the owner-race leg: auth-lifecycle has no
        // test-reset export; one more reserveReplacementAuthOwner() leaves a
        // fresh, un-aborted owner for later tests. At base the test aborts
        // before any replacement (no loader), so this only runs post-fix.
        reserveReplacementAuthOwner();
      }
    }
  });

  it("C6: edit loads an over-cap persisted turn into the composer for shortening — the named-bound error surfaces and shortening clears it (RED at base, GREEN after the fix)", async () => {
    // A persisted session whose LAST user turn is over the server cap,
    // loaded through the real mapper so store rows carry server ids/seqs.
    const rows = [
      seedRow(1, "user", "question-1", 1),
      seedRow(2, "assistant", "answer-1", 2),
      seedRow(3, "user", "g".repeat(25_000), 3),
    ];
    act(() => {
      useChatStore.getState().loadChat("42", rows.map(mapSessionMessage));
    });

    render(<TranscriptPane />);
    await settle();

    // The REAL edit affordance: the production handleEdit (which truncates
    // the server history, then restores the content into the composer)
    // driven through the mocked MessageBubble's Edit control.
    await act(async () => {
      await userEvent.click(screen.getByLabelText("Edit 3"));
    });

    const textarea = screen.getByLabelText("Message input");
    await waitFor(
      () => {
        expect(
          (textarea as HTMLTextAreaElement).value.length,
          "edit must load the persisted 25_000-char turn into the composer so the user can shorten it",
        ).toBe(25_000);
      },
      { timeout: 2_000 },
    );

    // Change feedback names the bound for the loaded over-cap content.
    // (Re-drive the change surface with the loaded content: whether the fix
    // validates at edit-load or on change, the named-bound feedback must
    // appear for content the user can see is too long.) At base there is no
    // named-bound error for 25_000 chars (gate 100_000) → RED.
    await act(async () => {
      fireEvent.change(textarea, { target: { value: "g".repeat(25_000) } });
    });
    await waitFor(
      () => {
        expect(
          gatheredErrors().some((message) => NAMES_20_000.test(message)),
          "an over-cap turn loaded into the composer by edit must surface the named-bound (20_000) change feedback",
        ).toBe(true);
      },
      { timeout: 2_000 },
    );

    // The recovery path completes: shortening below the bound clears the
    // error.
    await act(async () => {
      fireEvent.change(textarea, { target: { value: "short enough now" } });
    });
    await waitFor(
      () => {
        expect(
          gatheredErrors().some((message) => NAMES_20_000.test(message)),
          "once the user shortens the draft below the bound the over-cap error must clear",
        ).toBe(false);
      },
      { timeout: 2_000 },
    );
  });
});
