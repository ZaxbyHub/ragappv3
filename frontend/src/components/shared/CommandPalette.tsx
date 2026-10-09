import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { HugeiconsIcon } from "@hugeicons/react";
import {
  type UnifiedSearchResult,
  unifiedSearch,
} from "@/lib/api/search";
import {
  comboToKeyboardEventInit,
  PALETTE_TOGGLE_COMBOS,
  comboFromEvent,
} from "@/lib/shortcutBindings";
import type { NavItemId } from "@/components/layout/navigationTypes";
import { useAuthOwner } from "@/hooks/useAuthOwner";
import { useDraftRoomVisible } from "@/hooks/useDraftRoomCapabilities";
import { navItems, isAdminRole, isNavItemVisible, isHugeicon } from "@/components/layout/NavigationRail";
import { bindingFor } from "@/components/shared/KeyboardShortcuts";
import { useThemeStore } from "@/stores/useThemeStore";
import { useNavigationGuardStore } from "@/stores/useNavigationGuardStore";
import { captureAuthPrincipalGeneration, subscribeAuthPrincipal } from "@/lib/api/auth-lifecycle";
import { Keyboard, SunMoon, Monitor, Contrast, Link2 } from "lucide-react";
import { toast } from "sonner";
import { useAuthStore } from "@/stores/useAuthStore";
import { useVaultStore } from "@/stores/useVaultStore";
import { COMMAND_PALETTE_OPEN_EVENT } from "@/lib/commandPaletteEvents";
import {
  getCommandPaletteActionSnapshot,
  invokeCommandPaletteAction,
  subscribeCommandPaletteActions,
  type CommandPaletteActionSnapshot,
} from "@/lib/commandPaletteActions";

interface Command {
  id: NavItemId;
  label: string;
  to: string;
  icon: (typeof navItems)[number]["icon"];
}

interface PaletteContext {
  generation: number;
  owner: ReturnType<typeof useAuthOwner>;
  principalGeneration: number;
  principalId: number | string | null;
  role: string | null;
  vaultId: number | null;
}

interface CommandPaletteProps {
  draftRoomVisible?: boolean;
  /** Retained for callers during the active-vault migration; the store is authoritative. */
  vaultId?: number | string | null;
}

type SearchStatus = "idle" | "loading" | "success" | "error";

interface SearchState {
  status: SearchStatus;
  results: UnifiedSearchResult[];
  error: string | null;
}

interface SearchRequest {
  context: PaletteContext;
  queryGeneration: number;
  query: string;
  vaultId: number | null;
  controller: AbortController | null;
  timer: ReturnType<typeof setTimeout> | null;
  removeOwnerAbortListener: (() => void) | null;
}

const SEARCH_DEBOUNCE_MS = 300;
const SEARCH_MIN_QUERY_LENGTH = 2;
const idleSearchState: SearchState = { status: "idle", results: [], error: null };

function searchErrorMessage(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  return "Search failed. Try again.";
}

function copyPageLink(url: string, isCurrent: () => boolean): void {
  const write = navigator.clipboard?.writeText(url);
  if (write) {
    void write.then(
      () => { if (isCurrent()) toast.success("Page link copied"); },
      () => { if (isCurrent()) toast.error("Couldn't copy — try copying the address bar URL"); }
    );
    return;
  }
  try {
    const textarea = document.createElement("textarea");
    textarea.value = url;
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    const copied = document.execCommand("copy");
    document.body.removeChild(textarea);
    if (!copied) throw new Error("execCommand failed");
    if (isCurrent()) toast.success("Page link copied");
  } catch {
    if (isCurrent()) toast.error("Couldn't copy — try copying the address bar URL");
  }
}

const builtinActions = [
  { id: "action-show-shortcuts", label: "Show keyboard shortcuts", icon: Keyboard },
  { id: "action-toggle-theme", label: "Toggle light/dark theme", icon: SunMoon },
  { id: "action-system-theme", label: "Use system theme", icon: Monitor },
  { id: "action-high-contrast-theme", label: "Use high contrast theme", icon: Contrast },
  { id: "action-copy-link", label: "Copy page link", icon: Link2 },
] as const;

export function CommandPalette({ draftRoomVisible: draftRoomOverride }: CommandPaletteProps = {}) {
  const capabilityDraftRoomVisible = useDraftRoomVisible();
  const draftRoomVisible = draftRoomOverride ?? capabilityDraftRoomVisible;
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [retryNonce, setRetryNonce] = useState(0);
  const [searchState, setSearchState] = useState<SearchState>(idleSearchState);
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const pathnameRef = useRef(pathname);
  pathnameRef.current = pathname;
  const actionDispatchRef = useRef(false);
  const actions = useSyncExternalStore(
    subscribeCommandPaletteActions, getCommandPaletteActionSnapshot, getCommandPaletteActionSnapshot
  );
  const authOwner = useAuthOwner();
  const principalGeneration = useSyncExternalStore(
    subscribeAuthPrincipal, captureAuthPrincipalGeneration, captureAuthPrincipalGeneration
  );
  const theme = useThemeStore((state) => state.theme);
  const setTheme = useThemeStore((state) => state.setTheme);
  const themeRef = useRef(theme);
  themeRef.current = theme;
  const user = useAuthStore((state) => state.user);
  const activeVaultId = useVaultStore((state) => state.activeVaultId);
  const principalId = user?.id ?? null;
  const role = user?.role ?? null;
  const isAdmin = isAdminRole(role ?? undefined);

  const generationRef = useRef(0);
  const queryGenerationRef = useRef(0);
  const contextRef = useRef<PaletteContext | null>(null);
  const activeSearchRef = useRef<SearchRequest | null>(null);
  const resultContextRef = useRef<SearchRequest | null>(null);
  const mountedRef = useRef(false);
  const queryRef = useRef(query);
  const openRef = useRef(open);
  const authOwnerRef = useRef(authOwner);
  const principalRoleCapabilityRef = useRef({
    principalId,
    role,
    isAdmin,
    draftRoomVisible,
    vaultId: activeVaultId,
  });

  authOwnerRef.current = authOwner;
  principalRoleCapabilityRef.current = {
    principalId,
    role,
    isAdmin,
    draftRoomVisible,
    vaultId: activeVaultId,
  };
  queryRef.current = query;
  openRef.current = open;

  const cancelActiveSearch = useCallback(() => {
    const active = activeSearchRef.current;
    if (active && active.timer !== null) {
      clearTimeout(active.timer);
      active.timer = null;
    }
    active?.controller?.abort();
    active?.removeOwnerAbortListener?.();
    if (active) active.removeOwnerAbortListener = null;
    activeSearchRef.current = null;
    resultContextRef.current = null;
  }, []);

  const isCurrentContext = useCallback((context: PaletteContext, command?: Command): boolean => {
    const currentIdentity = principalRoleCapabilityRef.current;
    return (
      mountedRef.current &&
      openRef.current &&
      contextRef.current === context &&
      context.generation === generationRef.current &&
      context.owner === authOwnerRef.current &&
      !context.owner.signal.aborted &&
      context.principalGeneration === captureAuthPrincipalGeneration() &&
      context.principalId === currentIdentity.principalId &&
      context.role === currentIdentity.role &&
      context.vaultId === currentIdentity.vaultId &&
      (!command ||
        navItems.filter((item) => isNavItemVisible(item, currentIdentity.isAdmin, currentIdentity.draftRoomVisible)).some(
          (item) => item.id === command.id && item.to === command.to
        ))
    );
  }, []);

  const isCurrentSearch = useCallback(
    (request: SearchRequest): boolean =>
      isCurrentContext(request.context) &&
      request.queryGeneration === queryGenerationRef.current &&
      request.query === queryRef.current.trim().toLowerCase() &&
      request.vaultId === principalRoleCapabilityRef.current.vaultId &&
      activeSearchRef.current === request,
    [isCurrentContext]
  );

  const retirePalette = useCallback(
    (context?: PaletteContext) => {
      if (context && contextRef.current !== context) return;
      cancelActiveSearch();
      queryGenerationRef.current += 1;
      generationRef.current += 1;
      contextRef.current = null;
      openRef.current = false;
      setOpen(false);
      setQuery("");
      setSearchState(idleSearchState);
    },
    [cancelActiveSearch]
  );

  const openPalette = useCallback(() => {
    if (
      !mountedRef.current ||
      openRef.current ||
      authOwnerRef.current !== authOwner ||
      authOwner.signal.aborted
    ) {
      return;
    }
    const currentIdentity = principalRoleCapabilityRef.current;
    if (
      currentIdentity.principalId !== principalId ||
      currentIdentity.role !== role ||
      currentIdentity.vaultId !== activeVaultId
    ) {
      return;
    }
    const context: PaletteContext = {
      generation: ++generationRef.current,
      owner: authOwner,
      principalGeneration,
      principalId,
      role,
      vaultId: activeVaultId,
    };
    contextRef.current = context;
    openRef.current = true;
    setQuery("");
    setSearchState(idleSearchState);
    setOpen(true);
  }, [activeVaultId, authOwner, principalGeneration, principalId, role]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      contextRef.current = null;
      cancelActiveSearch();
    };
  }, [cancelActiveSearch]);

  useEffect(() => {
    const context = contextRef.current;
    if (
      !context ||
      !openRef.current ||
      (context.owner === authOwner &&
        context.principalGeneration === principalGeneration &&
        context.principalId === principalId &&
        context.role === role &&
        context.vaultId === activeVaultId)
    ) {
      return;
    }

    cancelActiveSearch();
    queryGenerationRef.current += 1;
    const replacement: PaletteContext = {
      generation: ++generationRef.current,
      owner: authOwner,
      principalGeneration,
      principalId,
      role,
      vaultId: activeVaultId,
    };
    contextRef.current = replacement;
    setQuery("");
    setSearchState(idleSearchState);
  }, [activeVaultId, authOwner, cancelActiveSearch, principalGeneration, principalId, role]);

  useEffect(() => {
    const context = contextRef.current;
    const normalizedQuery = query.trim().toLowerCase();
    const queryGeneration = queryGenerationRef.current;
    if (!context || !isCurrentContext(context) || normalizedQuery.length < SEARCH_MIN_QUERY_LENGTH) {
      setSearchState(idleSearchState);
      return;
    }

    const request: SearchRequest = {
      context,
      queryGeneration,
      query: normalizedQuery,
      vaultId: activeVaultId,
      controller: null,
      timer: null,
      removeOwnerAbortListener: null,
    };
    activeSearchRef.current = request;
    resultContextRef.current = request;
    setSearchState({ status: "loading", results: [], error: null });
    request.timer = setTimeout(() => {
      request.timer = null;
      if (!isCurrentSearch(request)) return;

      const controller = new AbortController();
      const abortForOwner = () => controller.abort();
      request.controller = controller;
      request.removeOwnerAbortListener = () =>
        request.context.owner.signal.removeEventListener("abort", abortForOwner);
      request.context.owner.signal.addEventListener("abort", abortForOwner, { once: true });
      if (request.context.owner.signal.aborted) controller.abort();

      void unifiedSearch({
        q: request.query,
        limit: 5,
        ...(request.vaultId === null ? {} : { vault_id: request.vaultId }),
        signal: controller.signal,
      })
        .then((response) => {
          if (!isCurrentSearch(request)) return;
          setSearchState({ status: "success", results: response.results, error: null });
        })
        .catch((error: unknown) => {
          if (!isCurrentSearch(request) || controller.signal.aborted) return;
          setSearchState({ status: "error", results: [], error: searchErrorMessage(error) });
        })
        .finally(() => {
          request.removeOwnerAbortListener?.();
          request.removeOwnerAbortListener = null;
        });
    }, SEARCH_DEBOUNCE_MS);

    return () => {
      if (activeSearchRef.current !== request) return;
      if (request.timer !== null) {
        clearTimeout(request.timer);
        request.timer = null;
      }
      request.controller?.abort();
      request.removeOwnerAbortListener?.();
      request.removeOwnerAbortListener = null;
      activeSearchRef.current = null;
      resultContextRef.current = null;
    };
  }, [activeVaultId, isCurrentContext, isCurrentSearch, query, retryNonce]);

  const retrySearch = useCallback(
    (request: SearchRequest) => {
      if (!isCurrentSearch(request)) return;
      setRetryNonce((value) => value + 1);
    },
    [isCurrentSearch]
  );

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.repeat || event.isComposing) return;
      const combo = comboFromEvent(event);
      if (combo === null || !PALETTE_TOGGLE_COMBOS.has(combo)) return;
      event.preventDefault();
      event.stopPropagation();
      if (openRef.current) retirePalette();
      else openPalette();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [openPalette, retirePalette]);

  useEffect(() => {
    window.addEventListener(COMMAND_PALETTE_OPEN_EVENT, openPalette);
    return () => window.removeEventListener(COMMAND_PALETTE_OPEN_EVENT, openPalette);
  }, [openPalette]);

  const visibleCommands = useMemo<Command[]>(
    () =>
      navItems.filter((item) => isNavItemVisible(item, isAdmin, draftRoomVisible)).map((item) => ({
        id: item.id,
        label: `Go to ${item.label}`,
        to: item.to,
        icon: item.icon,
      })),
    [draftRoomVisible, isAdmin]
  );
  const filteredCommands = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    if (!normalizedQuery) return visibleCommands;
    return visibleCommands.filter((command) => command.label.toLowerCase().includes(normalizedQuery));
  }, [query, visibleCommands]);

  const filteredActions = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return actions.filter((action) => action.label.toLowerCase().includes(normalizedQuery));
  }, [actions, query]);
  const filteredBuiltinActions = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return builtinActions.filter((action) => action.label.toLowerCase().includes(normalizedQuery));
  }, [query]);
  const executeBuiltinAction = useCallback((
    context: PaletteContext,
    queryGeneration: number,
    route: string,
    action: (typeof builtinActions)[number],
  ) => {
    if (actionDispatchRef.current || !isCurrentContext(context) ||
      queryGenerationRef.current !== queryGeneration || pathnameRef.current !== route) return;
    // A physically admitted copy may report its result after this dialog closes.
    // Its publication still belongs to this mounted auth/principal/vault/route.
    const isPublicationCurrent = () => {
      const identity = principalRoleCapabilityRef.current;
      return mountedRef.current && context.owner === authOwnerRef.current &&
        !context.owner.signal.aborted && context.principalGeneration === captureAuthPrincipalGeneration() &&
        context.principalId === identity.principalId && context.role === identity.role &&
        context.vaultId === identity.vaultId && pathnameRef.current === route;
    };
    actionDispatchRef.current = true;
    try {
      switch (action.id) {
        case "action-show-shortcuts":
          retirePalette(context);
          if (isPublicationCurrent()) window.dispatchEvent(new KeyboardEvent(
            "keydown", comboToKeyboardEventInit(bindingFor("showShortcuts"))
          ));
          break;
        case "action-toggle-theme": {
          const systemDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
          const resolvedDark = themeRef.current === "dark" || (themeRef.current === "system" && systemDark);
          if (isPublicationCurrent()) setTheme(resolvedDark ? "light" : "dark");
          retirePalette(context);
          break;
        }
        case "action-system-theme":
          if (isPublicationCurrent()) setTheme("system");
          retirePalette(context);
          break;
        case "action-high-contrast-theme":
          if (isPublicationCurrent()) setTheme("high-contrast");
          retirePalette(context);
          break;
        case "action-copy-link":
          retirePalette(context);
          if (isPublicationCurrent()) copyPageLink(window.location.href, isPublicationCurrent);
          break;
      }
    } finally {
      actionDispatchRef.current = false;
    }
  }, [isCurrentContext, retirePalette, setTheme]);

  const executeAction = useCallback((
    context: PaletteContext,
    queryGeneration: number,
    route: string,
    action: CommandPaletteActionSnapshot,
  ) => {
    if (actionDispatchRef.current) return;
    const guard = { isCurrent: () => isCurrentContext(context) &&
      queryGenerationRef.current === queryGeneration && pathnameRef.current === route };
    if (!guard.isCurrent()) return;
    actionDispatchRef.current = true;
    try {
      if (invokeCommandPaletteAction(action, guard) && guard.isCurrent()) retirePalette(context);
    } finally {
      actionDispatchRef.current = false;
    }
  }, [isCurrentContext, retirePalette]);

  const executeCommand = useCallback(
    (context: PaletteContext, command: Command) => {
      if (!isCurrentContext(context, command)) return;
      const confirmLeave = useNavigationGuardStore.getState().confirmLeave;
      if (confirmLeave && !confirmLeave()) return;
      if (!isCurrentContext(context, command)) return;
      navigate(command.to);
      if (!isCurrentContext(context)) return;
      retirePalette(context);
    },
    [isCurrentContext, navigate, retirePalette]
  );
  const executeSearchResult = useCallback(
    (request: SearchRequest, result: UnifiedSearchResult) => {
      if (!isCurrentSearch(request)) return;
      const confirmLeave = useNavigationGuardStore.getState().confirmLeave;
      if (confirmLeave && !confirmLeave()) return;
      if (!isCurrentSearch(request)) return;
      navigate(result.url_hint);
      if (!isCurrentSearch(request)) return;
      retirePalette(request.context);
    },
    [isCurrentSearch, navigate, retirePalette]
  );
  const handleQueryChange = useCallback(
    (nextQuery: string) => {
      const context = contextRef.current;
      if (!context || !isCurrentContext(context)) return;
      cancelActiveSearch();
      queryGenerationRef.current += 1;
      setSearchState(idleSearchState);
      setQuery(nextQuery);
    },
    [cancelActiveSearch, isCurrentContext]
  );

  const requestForRender = resultContextRef.current;
  const displayedSearchState =
    requestForRender && isCurrentSearch(requestForRender) ? searchState : idleSearchState;
  const openingContext = contextRef.current;
  const renderContext = openingContext && isCurrentContext(openingContext) ? openingContext : null;
  const renderQueryGeneration = queryGenerationRef.current;
  const displayedQuery = renderContext ? query : "";
  const searchingEntities = displayedQuery.trim().length >= SEARCH_MIN_QUERY_LENGTH;

  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (nextOpen) openPalette();
        else if (openingContext) retirePalette(openingContext);
      }}
    >
      <DialogContent className="sm:max-w-md" aria-describedby="command-palette-desc">
        <DialogHeader>
          <DialogTitle className="sr-only">Command palette</DialogTitle>
          <DialogDescription id="command-palette-desc" className="sr-only">
            Search and run a navigation command
          </DialogDescription>
        </DialogHeader>
        <input
          // eslint-disable-next-line jsx-a11y-x/no-autofocus -- modal palette input receives initial focus
          autoFocus
          aria-label="Search commands"
          placeholder="Type a command or search..."
          value={displayedQuery}
          onChange={(event) => handleQueryChange(event.target.value)}
          className="w-full rounded-sm border border-border bg-transparent px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-ring"
        />
        <ul aria-label="Commands" className="max-h-72 overflow-auto">
          {filteredCommands.length === 0 ? (
            <li className="px-3 py-6 text-center text-sm text-muted-foreground">No matching commands</li>
          ) : (
            filteredCommands.map((command) => {
              const Icon = command.icon;
              return (
                <li key={`${renderContext?.generation ?? 0}:${command.id}`}>
                  <button
                    type="button"
                    onClick={() => {
                      if (renderContext) executeCommand(renderContext, command);
                    }}
                    className="flex w-full items-center gap-3 rounded-sm px-3 py-2 text-left text-sm hover:bg-muted/50 focus-visible:bg-muted/50 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    {isHugeicon(Icon) ? <HugeiconsIcon icon={Icon} size={18} aria-hidden="true" /> : <Icon className="h-4 w-4" aria-hidden="true" />}
                    <span>{command.label}</span>
                  </button>
                </li>
              );
            })
          )}
        </ul>
        {searchingEntities ? (
          <div aria-live="polite">
            {displayedSearchState.status === "loading" ? (
              <p className="px-3 py-3 text-center text-sm text-muted-foreground">Searching...</p>
            ) : displayedSearchState.status === "error" && requestForRender ? (
              <div className="px-3 py-3 text-center text-sm text-destructive">
                <p>{displayedSearchState.error}</p>
                <button type="button" onClick={() => retrySearch(requestForRender)} className="mt-2 underline underline-offset-2">
                  Retry
                </button>
              </div>
            ) : displayedSearchState.status === "success" && displayedSearchState.results.length === 0 ? (
              <p className="px-3 py-3 text-center text-sm text-muted-foreground">No search results</p>
            ) : displayedSearchState.status === "success" ? (
              <ul aria-label="Search results" className="max-h-48 overflow-auto">
                {displayedSearchState.results.map((result) => (
                  <li key={`${requestForRender?.context.generation ?? 0}:${requestForRender?.queryGeneration ?? 0}:${result.type}:${result.id}`}>
                    <button
                      type="button"
                      onClick={() => {
                        if (requestForRender) executeSearchResult(requestForRender, result);
                      }}
                      className="flex w-full flex-col items-start rounded-sm px-3 py-2 text-left text-sm hover:bg-muted/50 focus-visible:bg-muted/50 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      <span className="w-full truncate">{result.title}</span>
                      {result.snippet ? <span className="w-full truncate text-xs text-muted-foreground">{result.snippet}</span> : null}
                    </button>
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}
        {renderContext && (filteredActions.length > 0 || filteredBuiltinActions.length > 0) ? (
          <ul aria-label="Actions" className="max-h-48 overflow-auto">
            {filteredBuiltinActions.map((action) => (
              <li key={`${renderContext.generation}:${renderQueryGeneration}:${action.id}`}>
                <button type="button" onClick={() =>
                  executeBuiltinAction(renderContext, renderQueryGeneration, pathname, action)}
                  className="flex w-full items-center gap-3 rounded-sm px-3 py-2 text-left text-sm hover:bg-muted/50 focus-visible:bg-muted/50 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                  <action.icon className="h-4 w-4" aria-hidden="true" />
                  {action.label}
                </button>
              </li>
            ))}
            {filteredActions.map((action) => (
              <li key={`${renderContext.generation}:${renderQueryGeneration}:${action.id}`}>
                <button type="button" onClick={() =>
                  executeAction(renderContext, renderQueryGeneration, pathname, action)}
                  className="flex w-full items-center gap-3 rounded-sm px-3 py-2 text-left text-sm hover:bg-muted/50 focus-visible:bg-muted/50 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                  {action.label}
                </button>
              </li>
            ))}
          </ul>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
