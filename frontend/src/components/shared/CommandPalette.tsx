import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { FileText, MessageSquare, Library, Vault, Settings, User } from "lucide-react";

interface Command {
  id: string;
  /** Short imperative label. Keep each destination word unique per command
   * (e.g. only the Documents row says "Documents") so palette text queries
   * resolve to exactly one command. */
  label: string;
  to: string;
  icon: typeof FileText;
}

// Navigation commands mirror the app shell's primary destinations (the
// PageShell nav). Executing a command closes the palette and navigates.
const NAVIGATION_COMMANDS: Command[] = [
  {
    id: "documents",
    label: "Go to Documents",
    to: "/documents",
    icon: FileText,
  },
  {
    id: "chat",
    label: "Go to Chat",
    to: "/chat",
    icon: MessageSquare,
  },
  {
    id: "vaults",
    label: "Go to Vaults",
    to: "/vaults",
    icon: Vault,
  },
  {
    id: "memory",
    label: "Go to Memory",
    to: "/memory",
    icon: Library,
  },
  {
    id: "settings",
    label: "Go to Settings",
    to: "/settings",
    icon: Settings,
  },
  {
    id: "profile",
    label: "Go to Profile",
    to: "/profile",
    icon: User,
  },
];

/**
 * Global command palette (issue #258 / legacy-14), mounted once at the app
 * shell. Ctrl/Cmd+K opens a dialog-role palette listing navigation commands;
 * executing a command closes the palette and navigates. Escape/outside-click
 * close via the Radix Dialog primitive.
 */
export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const navigate = useNavigate();

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        setOpen((prev) => !prev);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  const filteredCommands = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return NAVIGATION_COMMANDS;
    return NAVIGATION_COMMANDS.filter((command) => command.label.toLowerCase().includes(q));
  }, [query]);

  const executeCommand = (command: Command) => {
    setOpen(false);
    setQuery("");
    navigate(command.to);
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        setOpen(nextOpen);
        if (!nextOpen) setQuery("");
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
          {filteredCommands.length === 0 ? (
            <li className="px-3 py-6 text-center text-sm text-muted-foreground">
              No matching commands
            </li>
          ) : (
            filteredCommands.map((command) => {
              const Icon = command.icon;
              return (
                <li key={command.id}>
                  <button
                    type="button"
                    onClick={() => executeCommand(command)}
                    className="flex w-full items-center gap-3 rounded-sm px-3 py-2 text-left text-sm hover:bg-muted/50 focus-visible:bg-muted/50 focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <Icon className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                    <span className="flex-1 truncate">{command.label}</span>
                  </button>
                </li>
              );
            })
          )}
        </ul>
      </DialogContent>
    </Dialog>
  );
}
