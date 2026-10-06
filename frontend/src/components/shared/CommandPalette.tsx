import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { HugeiconsIcon } from "@hugeicons/react";
import { Keyboard, SunMoon, Monitor, Contrast, Link2, Search } from "lucide-react";
import {
  navItems,
  isAdminRole,
  isNavItemVisible,
  isHugeicon,
} from "@/components/layout/NavigationRail";
import { useAuthStore } from "@/stores/useAuthStore";
import { useDraftRoomVisible } from "@/hooks/useDraftRoomCapabilities";
import { useThemeStore } from "@/stores/useThemeStore";
import { bindingFor } from "@/components/shared/KeyboardShortcuts";
import {
  comboFromEvent,
  comboToKeyboardEventInit,
  PALETTE_TOGGLE_COMBOS,
} from "@/lib/shortcutBindings";
import { unifiedSearch, type UnifiedSearchResult } from "@/lib/api/search";

type PaletteIcon = React.ComponentType<{ className?: string }> | Parameters<typeof HugeiconsIcon>[0]["icon"];

interface NavCommand {
  id: string;
  kind: "nav";
  /** Short imperative label. Keep each destination word unique per command
   * (e.g. only the Documents row says "Documents") so palette text queries
   * resolve to exactly one command. */
  label: string;
  to: string;
  icon: PaletteIcon;
}

interface ActionCommand {
  id: string;
  kind: "action";
  label: string;
  run: () => void;
  icon: PaletteIcon;
}

const ENTITY_SEARCH_DEBOUNCE_MS = 300;
const ENTITY_SEARCH_MIN_CHARS = 2;
const ENTITY_SEARCH_LIMIT = 5;

function renderIcon(icon: PaletteIcon, className: string) {
  if (isHugeicon(icon as Parameters<typeof isHugeicon>[0])) {
    return <HugeiconsIcon strokeWidth={1.2} icon={icon as Parameters<typeof HugeiconsIcon>[0]["icon"]} size={16} className={className} aria-hidden="true" />;
  }
  const Icon = icon as React.ComponentType<{ className?: string }>;
  return <Icon className={className} aria-hidden="true" />;
}

/**
 * Global command palette (issue #258 / legacy-14; v2 per issue #775), mounted
 * once at the app shell. Ctrl/Cmd+K (PALETTE_TOGGLE_COMBOS — the single
 * shared definition) opens a dialog-role palette listing EVERY nav
 * destination (derived from NavigationRail's canonical navItems under the
 * same adminOnly/capabilityGated visibility rule), five non-navigating
 * actions, and entity hits from the existing unified search API (#515).
 * Executing a command closes the palette and navigates / runs the action.
 * Escape/outside-click close via the Radix Dialog primitive.
 *
 * One owner per combo (issue #775): the handler yields to any earlier
 * claimant via e.defaultPrevented (e.g. the chat rail's focusSearch on
 * /chat), and the rebind capture refuses combos in PALETTE_TOGGLE_COMBOS so
 * nothing can be persisted that this handler would shadow.
 */
export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<UnifiedSearchResult[]>([]);
  const hitSeqRef = useRef(0);
  const navigate = useNavigate();

  const userRole = useAuthStore((state) => state.user?.role);
  const draftRoomVisible = useDraftRoomVisible();
  const theme = useThemeStore((state) => state.theme);
  const setTheme = useThemeStore((state) => state.setTheme);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // Issue #775: exactly one owner per combo — if an earlier listener
      // already claimed this keydown (focusSearch's document-level handler
      // on /chat, or any capture-phase claimant), the palette yields.
      if (e.defaultPrevented) return;
      if (e.isComposing) return;
      const combo = comboFromEvent(e);
      if (combo === null || !PALETTE_TOGGLE_COMBOS.has(combo)) return;
      e.preventDefault();
      setOpen((prev) => !prev);
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  // Entity search (issue #775 / #515): queries of 2+ characters hit the
  // existing global search API after a debounce; a request-sequence guard
  // drops stale responses, and failures are swallowed (static filtering
  // always remains available).
  useEffect(() => {
    const q = query.trim();
    if (q.length < ENTITY_SEARCH_MIN_CHARS) {
      hitSeqRef.current += 1;
      setHits([]);
      return;
    }
    const seq = ++hitSeqRef.current;
    const timer = window.setTimeout(() => {
      unifiedSearch({ q, limit: ENTITY_SEARCH_LIMIT })
        .then((response) => {
          if (hitSeqRef.current === seq) setHits(response.results);
        })
        .catch(() => {
          if (hitSeqRef.current === seq) setHits([]);
        });
    }, ENTITY_SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [query]);

  const closePalette = () => {
    setOpen(false);
    setQuery("");
    setHits([]);
    hitSeqRef.current += 1;
  };

  // Navigation commands derive from the rail's canonical destination list
  // under the rail's own visibility rule (issue #775: no hand-copied subset,
  // no second gating definition). RENDER ORDER CONTRACT: every navigation
  // command precedes every action and entity hit.
  const navCommands: NavCommand[] = useMemo(() => {
    const isAdmin = isAdminRole(userRole);
    return navItems
      .filter((item) => isNavItemVisible(item, isAdmin, draftRoomVisible))
      .map((item) => ({
        id: item.id,
        kind: "nav" as const,
        label: `Go to ${item.label}`,
        to: item.to,
        icon: item.icon,
      }));
  }, [userRole, draftRoomVisible]);

  // Non-navigating actions (issue #775): executing one never changes the
  // route. The shortcuts action drives the SINGLE existing shortcut
  // dispatcher — it dispatches the bound showShortcuts combo instead of
  // opening the dialog through a second channel.
  const actionCommands: ActionCommand[] = useMemo(() => {
    return [
      {
        id: "action-show-shortcuts",
        kind: "action" as const,
        label: "Show keyboard shortcuts",
        icon: Keyboard,
        run: () => {
          closePalette();
          window.dispatchEvent(
            new KeyboardEvent("keydown", comboToKeyboardEventInit(bindingFor("showShortcuts")))
          );
        },
      },
      {
        id: "action-toggle-theme",
        kind: "action" as const,
        label: "Toggle light/dark theme",
        icon: SunMoon,
        run: () => {
          const systemDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
          const resolvedDark = theme === "dark" || (theme === "system" && systemDark);
          setTheme(resolvedDark ? "light" : "dark");
          closePalette();
        },
      },
      {
        id: "action-system-theme",
        kind: "action" as const,
        label: "Use system theme",
        icon: Monitor,
        run: () => {
          setTheme("system");
          closePalette();
        },
      },
      {
        id: "action-high-contrast-theme",
        kind: "action" as const,
        label: "Use high contrast theme",
        icon: Contrast,
        run: () => {
          setTheme("high-contrast");
          closePalette();
        },
      },
      {
        id: "action-copy-link",
        kind: "action" as const,
        label: "Copy page link",
        icon: Link2,
        run: () => {
          void navigator.clipboard?.writeText(window.location.href)?.catch(() => {
            // Clipboard access is best-effort; the action is still complete.
          });
          closePalette();
        },
      },
    ];
  }, [theme, setTheme]);

  const q = query.trim().toLowerCase();
  const filteredNav = useMemo(
    () => (q ? navCommands.filter((c) => c.label.toLowerCase().includes(q)) : navCommands),
    [navCommands, q]
  );
  const filteredActions = useMemo(
    () => (q ? actionCommands.filter((c) => c.label.toLowerCase().includes(q)) : actionCommands),
    [actionCommands, q]
  );

  const executeNav = (command: NavCommand) => {
    closePalette();
    navigate(command.to);
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        setOpen(nextOpen);
        if (!nextOpen) {
          setQuery("");
          setHits([]);
          hitSeqRef.current += 1;
        }
      }}
    >
      <DialogContent className="sm:max-w-md" aria-describedby="command-palette-desc">
        <DialogHeader>
          <DialogTitle className="sr-only">Command palette</DialogTitle>
          <DialogDescription id="command-palette-desc" className="sr-only">
            Search destinations, actions and entities
          </DialogDescription>
        </DialogHeader>
        <input
          // eslint-disable-next-line jsx-a11y-x/no-autofocus -- the palette is a modal surface; moving focus into its filter input on open is the standard palette interaction.
          autoFocus
          type="text"
          aria-label="Search commands"
          placeholder="Type a command or search..."
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          className="w-full rounded-sm border border-border bg-transparent px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-ring"
        />
        <ul aria-label="Commands" className="max-h-72 overflow-auto">
          {filteredNav.length === 0 && filteredActions.length === 0 && hits.length === 0 ? (
            <li className="px-3 py-6 text-center text-sm text-muted-foreground">
              No matching commands
            </li>
          ) : (
            <>
              {filteredNav.map((command) => (
                <li key={command.id}>
                  <button
                    type="button"
                    onClick={() => executeNav(command)}
                    className="flex w-full items-center gap-3 rounded-sm px-3 py-2 text-left text-sm hover:bg-muted/50 focus-visible:bg-muted/50 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    {renderIcon(command.icon, "h-4 w-4 shrink-0 text-muted-foreground")}
                    <span className="flex-1 truncate">{command.label}</span>
                  </button>
                </li>
              ))}
              {filteredActions.map((command) => (
                <li key={command.id}>
                  <button
                    type="button"
                    onClick={() => command.run()}
                    className="flex w-full items-center gap-3 rounded-sm px-3 py-2 text-left text-sm hover:bg-muted/50 focus-visible:bg-muted/50 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    {renderIcon(command.icon, "h-4 w-4 shrink-0 text-muted-foreground")}
                    <span className="flex-1 truncate">{command.label}</span>
                  </button>
                </li>
              ))}
              {hits.map((hit) => (
                <li key={`${hit.type}-${hit.id}`}>
                  <button
                    type="button"
                    onClick={() => {
                      closePalette();
                      navigate(hit.url_hint);
                    }}
                    className="flex w-full items-center gap-3 rounded-sm px-3 py-2 text-left text-sm hover:bg-muted/50 focus-visible:bg-muted/50 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <Search className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                    <span className="flex-1 truncate">{hit.title}</span>
                    <span className="shrink-0 text-xs uppercase text-muted-foreground">{hit.type}</span>
                  </button>
                </li>
              ))}
            </>
          )}
        </ul>
      </DialogContent>
    </Dialog>
  );
}
