import { getJwtAccessToken as getPublishedJwtAccessToken } from "@/lib/api/core";
import { create } from "zustand";
import { persist } from "zustand/middleware";
import axios from "axios";
import {
  API_BASE_URL,
  setJwtAccessToken,
  getJwtAccessToken,
  refreshAccessToken,
  ensureCsrfToken,
  resetCsrfToken,
  resetSubpathRefreshDiagnostic,
  attachCsrfInterceptor,
  ensureCsrfTokenPhysical,
} from "@/lib/api";
import { useVaultStore } from "@/stores/useVaultStore";
import { useChatStore } from "@/stores/useChatStore";
import { useChatShellStore } from "@/stores/useChatShellStore";
import { useUploadStore } from "@/stores/useUploadStore";
import {
  clearAllChatDrafts,
  invalidatePendingDraftWrites,
} from "@/lib/chatDrafts";
import { clearAllFeedbackVotes } from "@/lib/chatFeedbackStorage";
import {
  clearAllCanvasDrafts,
  invalidatePendingCanvasDraftWrites,
} from "@/lib/canvasDrafts";
import { clearAllDraftBriefTemplates } from "@/lib/draftBriefTemplates";
import { queryClient } from "@/lib/queryClient";
import { useDraftRoomUiStore } from "@/stores/useDraftRoomUiStore";
import { resetCitationReport } from "@/lib/api/onboarding";
import { AuthTransportTimeoutError, type AuthOwner, captureAuthOwner, captureAuthPrincipalGeneration, enqueueAuthTransport, isCurrentAuthOwner, publishAuthPrincipal, reserveReplacementAuthOwner, StaleAuthOwnerError, subscribeAuthPrincipal } from "@/lib/api/auth-lifecycle";
import { onJwtAccessTokenPublished } from "@/lib/api/core";

interface User {
  id: number;
  username: string;
  full_name: string;
  role: "superadmin" | "admin" | "member" | "viewer";
  is_active: boolean;
  /** When true, the user must change their password before using the app. */
  must_change_password?: boolean;
}

interface LoginResponse {
  access_token: string;
  user?: User;
}

interface RegisterResponse {
  access_token: string;
  user: User;
}

export interface RegisterPublicationScope {
  readonly admissionOwner: AuthOwner;
  readonly admissionUser: User | null;
  readonly admissionGeneration: number;
  reservedOwner?: AuthOwner;
  receipt?: {
    readonly owner: AuthOwner;
    readonly user: User;
    readonly principalGeneration: number;
  };
}

let armedRegisterPublicationScope: RegisterPublicationScope | undefined;

export function captureRegisterPublicationScope(): RegisterPublicationScope {
  return {
    admissionOwner: captureAuthOwner(),
    admissionUser: useAuthStore.getState().user,
    admissionGeneration: captureAuthPrincipalGeneration(),
  };
}

export function withRegisterPublicationScope<T>(
  scope: RegisterPublicationScope,
  invoke: () => T,
): T {
  const previous = armedRegisterPublicationScope;
  armedRegisterPublicationScope = scope;
  try {
    return invoke();
  } finally {
    // register consumes the armed value synchronously before its first await;
    // nested wrappers still restore the prior scope after that consumption.
    armedRegisterPublicationScope = previous;
  }
}

function takeRegisterPublicationScope(): RegisterPublicationScope | undefined {
  const scope = armedRegisterPublicationScope;
  armedRegisterPublicationScope = undefined;
  return scope;
}

function isSamePrincipal(left: User | null, right: User | null): boolean {
  return left?.id === right?.id && left?.role === right?.role;
}

function isRegisterScopePrincipalCurrent(scope: RegisterPublicationScope): boolean {
  return useAuthStore.getState().user === scope.admissionUser
    && captureAuthPrincipalGeneration() === scope.admissionGeneration;
}

export function isRegisterPublicationAdmissionCurrent(scope: RegisterPublicationScope): boolean {
  return isCurrentAuthOwner(scope.admissionOwner)
    && isRegisterScopePrincipalCurrent(scope);
}

export function canReportRegisterFailure(scope: RegisterPublicationScope): boolean {
  return scope.reservedOwner !== undefined
    && isCurrentAuthOwner(scope.reservedOwner)
    && isRegisterScopePrincipalCurrent(scope);
}

export function canNavigateRegisterPublication(scope: RegisterPublicationScope): boolean {
  const receipt = scope.receipt;
  return receipt !== undefined
    && scope.reservedOwner === receipt.owner
    && isCurrentAuthOwner(receipt.owner)
    && useAuthStore.getState().user === receipt.user
    && captureAuthPrincipalGeneration() === receipt.principalGeneration;
}

interface AuthState {
  // State
  user: User | null;
  accessToken: string | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  isInitialized: boolean;
  initializationFailed: boolean;
  needsSetup: boolean | null;
  authMode: "jwt" | "single_admin" | "unknown";

  // Actions
  login: (username: string, password: string) => Promise<void>;
  register: (username: string, password: string, fullName?: string) => Promise<void>;
  logout: () => Promise<void>;
  refreshToken: () => Promise<string | null>;
  fetchMe: () => Promise<void>;
  checkSetupStatus: (owner?: AuthOwner) => Promise<void>;
  setAuthMode: (mode: "jwt" | "single_admin") => void;
  updateProfile: (data: { full_name?: string }) => Promise<void>;
  init: () => Promise<void>;

  // Internal
  _setLoading: (loading: boolean) => void;
}

// Create a separate axios instance for auth calls to avoid interceptor loops
const authClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: 30000,
  headers: {
    "Content-Type": "application/json",
  },
  withCredentials: true, // Required for httpOnly refresh cookie
});

interface InitScope {
  readonly owner: AuthOwner;
  principalGeneration: number;
}

let initAttemptedScope: InitScope | null = null;
let initPromiseScope: InitScope | null = null;
let initPromise: Promise<void> | null = null;
let vaultsInitializedScope: InitScope | null = null;
let latestMeRead = 0;
let latestProfileMutation = 0;

function sameInitScope(left: InitScope | null, right: InitScope): boolean {
  return left?.owner === right.owner && left.principalGeneration === right.principalGeneration;
}

function isCurrentInitScope(scope: InitScope): boolean {
  return isCurrentAuthOwner(scope.owner) && captureAuthPrincipalGeneration() === scope.principalGeneration;
}

// These ordinary requests own only their local cancellation controller.
// Principal retirement never aborts the shared authentication owner.
function createPrincipalRequest(scope: InitScope): { signal: AbortSignal; dispose(): void } {
  const controller = new AbortController();
  const retire = () => { if (!isCurrentInitScope(scope)) controller.abort(); };
  scope.owner.signal.addEventListener("abort", retire, { once: true });
  const unsubscribe = subscribeAuthPrincipal(retire);
  retire();
  return {
    signal: controller.signal,
    dispose: () => {
      scope.owner.signal.removeEventListener("abort", retire);
      unsubscribe();
    },
  };
}

async function initializeVaultsForScope(scope: InitScope): Promise<void> {
  if (!isCurrentInitScope(scope) || sameInitScope(vaultsInitializedScope, scope)) return;
  vaultsInitializedScope = scope;
  if (!isCurrentInitScope(scope)) return;
  await useVaultStore.getState().fetchVaults();
}

/**
 * Reset init guard state (exported for testing).
 */
export const resetInitState = () => {
  initAttemptedScope = null;
  initPromiseScope = null;
  initPromise = null;
  vaultsInitializedScope = null;
};

/**
 * Issue #823: no per-user chat state may survive an identity boundary.
 * ChatShell's session-load fast path renders whatever `activeChatId` holds
 * without a server re-fetch, so a different user logging in within the same
 * tab could otherwise see the previous user's transcript; the composer's
 * unsent input, its persisted localStorage drafts, the shell's session
 * title/evidence/search/pins, the feedback-vote mirror, and the upload
 * store's attachment registrations and in-flight transfers are the same
 * content class. Called ONLY at real credential boundaries: `logout` (after
 * the auth resets), a different-principal `register`, and a
 * different-principal `login` at its credential-publication instant.
 */
const resetChatStateForIdentitySwitch = () => {
  // Kill delayed writers FIRST so they cannot re-create cleared state:
  // pending draft debounce timers / unmount flushes, and async upload
  // completions / queue continuations of the outgoing identity. Issue
  // #1032: the canvas draft writers are the same delayed-writer class
  // (debounce timer + unmount/artifact-switch flushes in CanvasPage).
  invalidatePendingDraftWrites();
  invalidatePendingCanvasDraftWrites();
  useChatStore.getState().newChat();
  useChatStore.getState().setInput("");
  useChatStore.getState().setInputError(null);
  // Best-effort wipes can partially fail on a degraded storage surface; the
  // principal-scoped key namespaces keep the residue unreadable to the next
  // principal, and the counts are logged so the residual is observable
  // rather than silent (hermes review, final head). Issue #1032 extends the
  // same contract to the canvas draft namespace.
  const draftRemovalFailures = clearAllChatDrafts();
  if (draftRemovalFailures > 0) {
    console.warn(
      "identity_switch_draft_wipe_incomplete failed_keys=%d",
      draftRemovalFailures
    );
  }
  const canvasDraftRemovalFailures = clearAllCanvasDrafts();
  if (canvasDraftRemovalFailures < 0) {
    // Enumeration itself failed: the wipe completeness is unknown, so the
    // residual is unobservable (multi-stage review). Principal-scoped keys
    // keep any residue unreadable to the next principal either way.
    console.warn(
      "identity_switch_canvas_draft_wipe_unknown storage_enumeration_failed"
    );
  } else if (canvasDraftRemovalFailures > 0) {
    console.warn(
      "identity_switch_canvas_draft_wipe_incomplete failed_keys=%d",
      canvasDraftRemovalFailures
    );
  }
  const feedbackRemovalFailures = clearAllFeedbackVotes();
  if (feedbackRemovalFailures > 0) {
    console.warn(
      "identity_switch_feedback_wipe_incomplete failed_keys=%d",
      feedbackRemovalFailures
    );
  }
  // Issue #1036 AC2: saved brief templates are user-authored content in the
  // same class as chat drafts and feedback votes. The keys are
  // principal-scoped, so any residue a degraded storage surface leaves
  // behind stays unreadable to the next principal.
  const briefTemplateRemovalFailures = clearAllDraftBriefTemplates();
  if (briefTemplateRemovalFailures < 0) {
    console.warn(
      "identity_switch_brief_template_wipe_unknown storage_enumeration_failed"
    );
  } else if (briefTemplateRemovalFailures > 0) {
    console.warn(
      "identity_switch_brief_template_wipe_incomplete failed_keys=%d",
      briefTemplateRemovalFailures
    );
  }
  const shell = useChatShellStore.getState();
  shell.setActiveSessionId(null);
  shell.setActiveSessionTitle(null);
  shell.resetEvidenceSelection();
  // Review round 1 (implementation review): the rail's search box holds
  // user-typed text and the pin list is user-scoped persisted metadata —
  // the same content class as the fields above.
  shell.clearIdentityScopedState();
  useUploadStore.getState().resetForIdentitySwitch();
  // Issue #1032: the remaining per-user surfaces outside the chat store's
  // namespace — the app-wide React Query cache (no query key carries a
  // principal segment) and the Draft Room UI store's unsent editor text.
  // resetQueries() FIRST: a mounted QueryObserver keeps rendering its own
  // #currentQuery state after clear() empties the cache map, so clear()
  // alone leaves the previous principal's payload on screen until the next
  // refetch; resetQueries() resets every observer to its initial state
  // (cancelling in-flight fetches), and the subsequent clear() removes the
  // entries outright (blind-lane probe: clear() alone -> observer still
  // renders user A's payload; resetQueries() -> data undefined, pending).
  queryClient.resetQueries();
  queryClient.clear();
  useDraftRoomUiStore.getState().resetForIdentitySwitch();
};

// Wire up CSRF via centralized api.ts utilities
attachCsrfInterceptor(authClient);

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      // Initial state
      user: null,
      accessToken: null,
      isAuthenticated: false,
      isInitialized: false,
      initializationFailed: false,
      isLoading: false,
      needsSetup: null as boolean | null,
      authMode: "unknown",

      _setLoading: (loading: boolean) => set({ isLoading: loading }),

      setAuthMode: (mode: "jwt" | "single_admin") => set({ authMode: mode }),

      init: async () => {
        const scope: InitScope = {
          owner: captureAuthOwner(),
          principalGeneration: captureAuthPrincipalGeneration(),
        };
        if (!isCurrentInitScope(scope)) return;
        if (initPromise && sameInitScope(initPromiseScope, scope)) {
          await initPromise;
          return;
        }
        if (sameInitScope(initAttemptedScope, scope)) return;

        let run: () => void = () => undefined;
        const attempt = new Promise<void>((resolve, reject) => {
          run = () => {
            void (async () => {
              const loadMeForScope = async (): Promise<boolean> => {
                const token = get().accessToken || getJwtAccessToken();
                if (!token) throw new Error("No access token available");
                const response = await authClient.get<User>("/auth/me", {
                  headers: { Authorization: `Bearer ${token}` },
                });
                if (!isCurrentInitScope(scope)) return false;
                const previousGeneration = scope.principalGeneration;
                const currentUser = get().user;
                const willPublishPrincipal = currentUser?.id !== response.data.id || currentUser?.role !== response.data.role;
                scope.principalGeneration = previousGeneration + (willPublishPrincipal ? 1 : 0);
                set({ user: response.data, isAuthenticated: true });
                if (!isCurrentAuthOwner(scope.owner) || get().user !== response.data) return false;
                return captureAuthPrincipalGeneration() === scope.principalGeneration;
              };
              const refreshForScope = async (): Promise<string | null | false> => {
                try {
                  const newToken = await refreshAccessToken(scope.owner);
                  if (!isCurrentInitScope(scope)) return false;
                  if (newToken) {
                    if (getJwtAccessToken() !== newToken) {
                      setJwtAccessToken(newToken);
                      if (!isCurrentInitScope(scope)) return false;
                      if (get().accessToken !== newToken) set({ accessToken: newToken });
                    }
                    return isCurrentInitScope(scope) ? newToken : false;
                  }
                  const previousGeneration = scope.principalGeneration;
                  setJwtAccessToken(null);
                  if (!isCurrentAuthOwner(scope.owner) || captureAuthPrincipalGeneration() !== previousGeneration) return false;
                  const willClearPrincipal = get().user !== null;
                  scope.principalGeneration = previousGeneration + (willClearPrincipal ? 1 : 0);
                  set({ user: null, accessToken: null, isAuthenticated: false });
                  if (!isCurrentAuthOwner(scope.owner) || get().user !== null || captureAuthPrincipalGeneration() !== scope.principalGeneration) return false;
                  return null;
                } catch (error) {
                  if (error instanceof StaleAuthOwnerError || !isCurrentInitScope(scope)) return false;
                  console.warn("Token refresh unavailable (transport failure):", error);
                  return null;
                }
              };
              const checkSetupForScope = async (): Promise<boolean> => {
                const response = await authClient.get<{ needs_setup: boolean; auth_mode?: "jwt" | "single_admin"; users_enabled?: boolean }>(
                  "/auth/setup-status"
                );
                if (!isCurrentInitScope(scope)) return false;
                if (typeof response.data.needs_setup !== "boolean") {
                  throw new Error("Invalid setup-status response");
                }
                set({
                  needsSetup: response.data.needs_setup,
                  authMode: response.data.auth_mode ?? (response.data.users_enabled === false ? "single_admin" : "jwt"),
                });
                return isCurrentInitScope(scope);
              };

              if (!isCurrentInitScope(scope)) return;
              set({ isLoading: true, initializationFailed: false });
              if (!isCurrentInitScope(scope)) return;
              const state = get();
              try {
                if (state.accessToken) {
                  if (!(await loadMeForScope())) return;
                  set({ authMode: "jwt", isAuthenticated: true, isLoading: true, isInitialized: false, initializationFailed: false });
                  if (!isCurrentInitScope(scope)) return;
                  await initializeVaultsForScope(scope);
                } else {
                  const newToken = await refreshForScope();
                  if (newToken === false || !isCurrentInitScope(scope)) return;
                  if (newToken) {
                    if (!(await loadMeForScope())) return;
                    set({ authMode: "jwt", isAuthenticated: true, isLoading: true, isInitialized: false, initializationFailed: false });
                    if (!isCurrentInitScope(scope)) return;
                    await initializeVaultsForScope(scope);
                  }
                }
              } catch (error) {
                if (!isCurrentInitScope(scope)) return;
                const status = (error as { response?: { status?: number } } | null)?.response?.status ?? (error as { status?: number } | null)?.status;
                if (status === 401 || status === 403) {
                  const previousGeneration = scope.principalGeneration;
                  setJwtAccessToken(null);
                  if (!isCurrentAuthOwner(scope.owner) || captureAuthPrincipalGeneration() !== previousGeneration) return;
                  const willClearPrincipal = get().user !== null;
                  scope.principalGeneration = previousGeneration + (willClearPrincipal ? 1 : 0);
                  set({ accessToken: null, user: null, isAuthenticated: false });
                  if (!isCurrentAuthOwner(scope.owner) || get().user !== null || captureAuthPrincipalGeneration() !== scope.principalGeneration) return;
                } else {
                  console.warn("Auth init unavailable (transport failure):", error);
                }
              }
              if (!isCurrentInitScope(scope)) return;
              try {
                if (!(await checkSetupForScope())) return;
              } catch {
                if (!isCurrentInitScope(scope)) return;
                set({ isLoading: false, isInitialized: false, initializationFailed: true, needsSetup: null });
                if (isCurrentInitScope(scope) && initAttemptedScope === scope) initAttemptedScope = null;
                return;
              }
              if (!isCurrentInitScope(scope)) return;
              const authMode = get().authMode === "unknown" ? "jwt" : get().authMode;
              set({ authMode, isLoading: false, isInitialized: true, initializationFailed: false });
            })().then(resolve, reject);
          };
        });
        initAttemptedScope = scope;
        initPromiseScope = scope;
        initPromise = attempt;
        run();
        try {
          await attempt;
        } finally {
          if (initPromise === attempt && initPromiseScope === scope) {
            initPromise = null;
            initPromiseScope = null;
          }
        }
      },

      checkSetupStatus: async (owner: AuthOwner = captureAuthOwner()) => {
        const expectedPrincipalGeneration = captureAuthPrincipalGeneration();
        try {
          const response = await authClient.get<{ needs_setup: boolean; auth_mode?: "jwt" | "single_admin"; users_enabled?: boolean }>(
            "/auth/setup-status"
          );
          if (typeof response.data.needs_setup !== "boolean") {
            throw new Error("Invalid setup-status response");
          }
          if (isCurrentAuthOwner(owner) && captureAuthPrincipalGeneration() === expectedPrincipalGeneration) {
            set({
              needsSetup: response.data.needs_setup,
              authMode: response.data.auth_mode ?? (response.data.users_enabled === false ? "single_admin" : "jwt"),
              initializationFailed: false,
            });
          }
        } catch (error) {
          // Unknown is a real state: a transport failure cannot authoritatively
          // answer setup status, and init must remain retryable.
          if (isCurrentAuthOwner(owner) && captureAuthPrincipalGeneration() === expectedPrincipalGeneration) {
            if (sameInitScope(initAttemptedScope, { owner, principalGeneration: expectedPrincipalGeneration })) {
              initAttemptedScope = null;
            }
            set({ needsSetup: null, isInitialized: false, initializationFailed: true });
          }
          console.error("Failed to check setup status:", error);
          throw error;
        }
      },

      login: async (username: string, password: string) => {
        const owner = reserveReplacementAuthOwner();
        // Issue #823: the identity this tab's chat surface currently belongs
        // to. A login that publishes a DIFFERENT principal — including a
        // user-less response, whose eventual principal is unknown until
        // fetchMe — must reset the chat surface BEFORE the new credentials
        // are published, so no bridge subscriber or request issued on the
        // new token can ever observe the previous user's state (reviews
        // PRR-009/PRR-015).
        const prevUserId = get().user?.id ?? null;
        if (isCurrentAuthOwner(owner)) resetInitState();
        if (isCurrentAuthOwner(owner)) set({ isLoading: true, initializationFailed: false });
        if (!isCurrentAuthOwner(owner)) return;
        try {
          resetCsrfToken();
          const response = await enqueueAuthTransport(owner, async (context) => {
            await ensureCsrfTokenPhysical(false, owner);
            context.assertCurrent();
            return authClient.post<LoginResponse>("/auth/login", { username, password }, { signal: context.signal });
          });
          if (!isCurrentAuthOwner(owner)) return;
          const { access_token, user } = response.data;
          // Identity boundary first: same principal -> provably the same
          // surface, no reset (a fresh boot has prevUserId null and no
          // state to clear, so the unconditional user-less arm costs
          // nothing there). Everything else — different id, or an unknown
          // principal — clears before the credentials become usable.
          if (!user || user.id !== prevUserId) {
            resetChatStateForIdentitySwitch();
          }
          // Publish the holder: bridge subscribers may issue a request
          // synchronously when the store state changes.
          setJwtAccessToken(access_token);
          if (!isCurrentAuthOwner(owner)) return;
          set({ accessToken: access_token, user: user || null, isAuthenticated: true, authMode: "jwt" });
          resetSubpathRefreshDiagnostic();
          if (!user) await get().fetchMe();
          if (!isCurrentAuthOwner(owner)) return;
          const loginPrincipalGeneration = captureAuthPrincipalGeneration();
          resetCsrfToken();
          await ensureCsrfToken(true, owner).catch(() => undefined);
          if (!isCurrentAuthOwner(owner) || captureAuthPrincipalGeneration() !== loginPrincipalGeneration) return;
          const vaultScope: InitScope = {
            owner,
            principalGeneration: loginPrincipalGeneration,
          };
          if (isCurrentInitScope(vaultScope)) await initializeVaultsForScope(vaultScope);
          if (!isCurrentAuthOwner(owner) || captureAuthPrincipalGeneration() !== loginPrincipalGeneration) return;
          set({ isInitialized: true, initializationFailed: false });
        } catch (error) {
          // Existing login callers treat a superseded attempt as a no-op.
          if (!(error instanceof StaleAuthOwnerError)) throw error;
        } finally {
          if (isCurrentAuthOwner(owner)) set({ isLoading: false });
        }
      },

      register: async (username: string, password: string, fullName?: string) => {
        const scope = takeRegisterPublicationScope();
        if (scope && !isRegisterPublicationAdmissionCurrent(scope)) return;

        const admissionUser = scope?.admissionUser ?? useAuthStore.getState().user;
        const admissionGeneration = scope?.admissionGeneration ?? captureAuthPrincipalGeneration();
        const owner = reserveReplacementAuthOwner();
        const registerProfileMutation = latestProfileMutation;
        const registerMeRead = latestMeRead;
        if (scope) scope.reservedOwner = owner;
        const isPrePublicationCurrent = () => isCurrentAuthOwner(owner)
          && useAuthStore.getState().user === admissionUser
          && captureAuthPrincipalGeneration() === admissionGeneration;
        if (isPrePublicationCurrent()) {
          set((current) => isPrePublicationCurrent() ? { ...current, isLoading: true } : current);
        }
        let expectedGeneration = admissionGeneration;
        let publishedUser: User | null = null;
        const hasExactOwnPublication = () => publishedUser !== null
          && isCurrentAuthOwner(owner)
          && useAuthStore.getState().user === publishedUser
          && captureAuthPrincipalGeneration() === expectedGeneration;

        try {
          const response = await enqueueAuthTransport(
            owner,
            (context) => {
              if (!isPrePublicationCurrent()) throw new StaleAuthOwnerError();
              return authClient.post<RegisterResponse>(
                "/auth/register",
                { username, password, full_name: fullName },
                { signal: context.signal },
              );
            },
            undefined,
            isPrePublicationCurrent,
          );
          if (!isPrePublicationCurrent()) return;

          const { access_token, user: userData } = response.data;
          const user: User = {
            id: userData.id,
            username: userData.username,
            full_name: userData.full_name,
            role: userData.role,
            is_active: userData.is_active ?? true,
          };

          // Issue #823 identity boundary (review PRR-002): register is a
          // principal publication like login. When the registered account is
          // a DIFFERENT principal than the one this tab's chat surface
          // belongs to — reachable because the auth-clearing arms
          // (refreshToken failure, init 401/403) null the principal without
          // clearing chat, which also disables RegisterPage's authenticated
          // redirect — clear the surface BEFORE publishing the new
          // credentials.
          if (!isSamePrincipal(admissionUser, user)) {
            resetChatStateForIdentitySwitch();
          }
          setJwtAccessToken(access_token);
          if (!isPrePublicationCurrent()) return;
          resetInitState();
          if (!isPrePublicationCurrent()) return;
          set({
            accessToken: access_token,
            user,
            isAuthenticated: true,
            isInitialized: true,
            initializationFailed: false,
            authMode: "jwt",
            needsSetup: false,
          });

          publishedUser = user;
          expectedGeneration = admissionGeneration
            + (isSamePrincipal(admissionUser, user) ? 0 : 1);
          if (!hasExactOwnPublication()) return;
          if (scope) {
            scope.receipt = { owner, user, principalGeneration: expectedGeneration };
            if (!hasExactOwnPublication()) return;
          }

          if (!hasExactOwnPublication() || (scope && !canNavigateRegisterPublication(scope))) return;
          resetSubpathRefreshDiagnostic();
          if (!hasExactOwnPublication() || (scope && !canNavigateRegisterPublication(scope))) return;
          resetCsrfToken();
          if (!hasExactOwnPublication() || (scope && !canNavigateRegisterPublication(scope))) return;
          await ensureCsrfToken(false, owner).catch(() => undefined);
          if (!hasExactOwnPublication()) return;
        } catch (error) {
          if (!(error instanceof StaleAuthOwnerError)) throw error;
        } finally {
          if (isCurrentAuthOwner(owner) && latestProfileMutation === registerProfileMutation
            && latestMeRead === registerMeRead && (isPrePublicationCurrent() || hasExactOwnPublication())) {
            set((current) => {
              const principalStillBound = isPrePublicationCurrent() || hasExactOwnPublication();
              if (!isCurrentAuthOwner(owner) || latestProfileMutation !== registerProfileMutation
                || latestMeRead !== registerMeRead || !principalStillBound) return current;
              return { ...current, isLoading: false };
            });
          }
        }
      },

      logout: (): Promise<void> => {
        const owner = reserveReplacementAuthOwner();
        if (isCurrentAuthOwner(owner)) set({ isLoading: true });
        const operation = (async () => {
          let requestFailure: unknown = null;
          let timeoutFailure: AuthTransportTimeoutError | null = null;
          try {
            await enqueueAuthTransport(owner, (context) =>
              authClient.post("/auth/logout", {}, { withCredentials: true, signal: context.signal }),
            );
          } catch (error) {
            // A stale/deadline logout must remain observable to its caller.
            if (error instanceof AuthTransportTimeoutError) {
              timeoutFailure = error;
            }
            if (error instanceof StaleAuthOwnerError) throw error;
            requestFailure = error;
            console.error("Logout request failed:", error);
          }
          if (!isCurrentAuthOwner(owner)) throw new StaleAuthOwnerError();
          setJwtAccessToken(null);
          if (!isCurrentAuthOwner(owner)) throw new StaleAuthOwnerError();
          set({ user: null, accessToken: null, isAuthenticated: false, isLoading: false });
          if (!isCurrentAuthOwner(owner)) throw new StaleAuthOwnerError();
          resetCsrfToken();
          resetSubpathRefreshDiagnostic();
          resetCitationReport();
          resetInitState();
          // Issue #823: this tab's chat surface belongs to the identity that
          // just ended — clear it before any other user can log in here.
          resetChatStateForIdentitySwitch();
          if (timeoutFailure) throw timeoutFailure;
          // Preserve the longstanding best-effort server logout for ordinary
          // request failures while keeping lifecycle cancellation rejecting.
          void requestFailure;
        })();
        void operation.catch(() => undefined);
        return operation;
      },

      refreshToken: async (): Promise<string | null> => {
        const owner = captureAuthOwner();
        try {
          const newToken = await refreshAccessToken(owner);
          if (!isCurrentAuthOwner(owner)) return null;
          if (newToken) {
            // Real core refresh already published holder then bridge before this
            // action resumed. Mock/public alternatives still need that bridge.
            if (getJwtAccessToken() !== newToken) {
              setJwtAccessToken(newToken);
              if (!isCurrentAuthOwner(owner)) return null;
              if (get().accessToken !== newToken) set({ accessToken: newToken });
            }
            return isCurrentAuthOwner(owner) ? newToken : null;
          }
          // Null outcome is also holder-first. A reentrant store subscriber may
          // reserve a replacement while the core publication is delivered.
          if (getJwtAccessToken() !== null) setJwtAccessToken(null);
          if (!isCurrentAuthOwner(owner)) return null;
          set({ user: null, accessToken: null, isAuthenticated: false });
          return null;
        } catch (error) {
          if (error instanceof StaleAuthOwnerError || !isCurrentAuthOwner(owner)) return null;
          console.warn("Token refresh unavailable (transport failure):", error);
          return null;
        }
      },

      fetchMe: async () => {
        const scope: InitScope = {
          owner: captureAuthOwner(), principalGeneration: captureAuthPrincipalGeneration(),
        };
        const attempt = ++latestMeRead;
        const request = createPrincipalRequest(scope);
        try {
          const token = get().accessToken || getJwtAccessToken();
          if (!token) throw new Error("No access token available");
          if (!isCurrentInitScope(scope)) return;
          const response = await authClient.get<User>("/auth/me", {
            headers: { Authorization: `Bearer ${token}` },
            signal: request.signal,
          });
          if (isCurrentInitScope(scope) && latestMeRead === attempt) {
            set({ user: response.data, isAuthenticated: true });
          }
        } catch (error) {
          console.error("Failed to fetch user:", error);
          // Preserve rejection for awaiters; the caller owns navigation policy.
          throw error;
        } finally {
          request.dispose();
        }
      },

      updateProfile: async (data: { full_name?: string }) => {
        const scope: InitScope = {
          owner: captureAuthOwner(), principalGeneration: captureAuthPrincipalGeneration(),
        };
        const attempt = ++latestProfileMutation;
        ++latestMeRead;
        const payload = { ...data };
        const token = get().accessToken || getJwtAccessToken();
        if (!token) throw new Error("No access token available");
        const request = createPrincipalRequest(scope);
        try {
          if (!isCurrentInitScope(scope)) return;
          set({ isLoading: true });
          if (!isCurrentInitScope(scope) || latestProfileMutation !== attempt) return;
          const response = await authClient.patch<User>("/auth/me", payload, {
            headers: { Authorization: `Bearer ${token}` },
            signal: request.signal,
          });
          if (isCurrentInitScope(scope) && latestProfileMutation === attempt) {
            ++latestMeRead;
            set({ user: response.data });
          }
        } finally {
          request.dispose();
          // Release only this action's loading state. A newer mutation, or a
          // replacement auth owner, has sole authority over its own busy state.
          if (isCurrentAuthOwner(scope.owner) && latestProfileMutation === attempt) {
            set({ isLoading: false });
          }
        }
      },
    }),
    {
      name: "auth-storage",
      // H-11 fix: Do NOT persist accessToken to localStorage (XSS risk).
      // The httpOnly refresh cookie handles session persistence.
      partialize: (state) => ({
        user: state.user,
        authMode: state.authMode,
        needsSetup: state.needsSetup,
      }),
    }
  )
);

// Publish hydrated principal authority before subscribing to later changes.
publishAuthPrincipal(useAuthStore.getState().user);

// Subscribe to token and principal changes to keep authority in sync
useAuthStore.subscribe((state, prevState) => {
  if (state.accessToken !== prevState.accessToken) {
    // Direct public store replacement (including same-account context) is a
    // session boundary. Internal core-first publications already match the
    // holder and therefore keep the current refresh owner alive.
    if (getPublishedJwtAccessToken() !== state.accessToken) reserveReplacementAuthOwner();
    setJwtAccessToken(state.accessToken);
  }
  publishAuthPrincipal(state.user);
});


onJwtAccessTokenPublished((token) => {
  const state = useAuthStore.getState();
  if (state.accessToken !== token) useAuthStore.setState({ accessToken: token });
}, true);
