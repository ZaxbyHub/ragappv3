import { useEffect, useRef, useState } from "react";
import { MessageSquare, FileText, Brain, MoreHorizontal, Database, Settings, Users, User, Building2, UserCog, BookOpen, Library, LogOut, PenLine } from "lucide-react";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import { cn } from "@/lib/utils";
import { useAuthStore } from "@/stores/useAuthStore";
import { captureAuthOwner, isCurrentAuthOwner } from "@/lib/api/auth-lifecycle";
import { useNavigate } from "react-router-dom";
import type { NavItemId } from "./navigationTypes";
import { useDraftRoomVisible } from "@/hooks/useDraftRoomCapabilities";
import { DRAFT_ROOM_NAV_LABEL } from "@/components/draft-room/labels";

interface MobileBottomNavProps {
  // null = no item active (unmapped routes like /search; issue #779/UI-R1-08).
  activeItem: NavItemId | null;
  onItemSelect: (id: NavItemId) => void;
}

// Primary tabs shown on bottom nav
const primaryNavItems = [
  { id: "chat" as const, label: "Chat", icon: MessageSquare },
  { id: "documents" as const, label: "Documents", icon: FileText },
  { id: "memory" as const, label: "Memory", icon: Brain },
];

// Secondary items shown in "More" drawer
const moreNavItems: { id: NavItemId; label: string; icon: React.ComponentType<{ className?: string }>; adminOnly?: boolean; capabilityGated?: boolean }[] = [
  { id: "wiki", label: "Wiki", icon: BookOpen },
  { id: "kms" as const, label: "KMS", icon: Library },
  { id: "draftRoom", label: DRAFT_ROOM_NAV_LABEL, icon: PenLine, capabilityGated: true },
  { id: "vaults", label: "Vaults", icon: Database },
  { id: "settings", label: "Settings", icon: Settings },
  { id: "groups", label: "Groups", icon: Users, adminOnly: true },
  { id: "users", label: "Users", icon: UserCog, adminOnly: true },
  { id: "profile", label: "Profile", icon: User },
  { id: "organizations", label: "Orgs", icon: Building2, adminOnly: true },
];

export function MobileBottomNav({ activeItem, onItemSelect }: MobileBottomNavProps) {
  const [moreOpen, setMoreOpen] = useState(false);
  const isMounted = useRef(true);
  useEffect(() => {
    isMounted.current = true;
    return () => { isMounted.current = false; };
  }, []);
  const navigate = useNavigate();
  const userRole = useAuthStore((state) => state.user?.role);
  const logout = useAuthStore((state) => state.logout);
  const isAdmin = userRole === "admin" || userRole === "superadmin";
  const draftRoomVisible = useDraftRoomVisible();

  const handleLogout = () => {
    setMoreOpen(false);
    // The store deliberately rejects stale/deadline logout to its callers.
    // This UI boundary consumes that rejection so a dropped click never leaks
    // an unhandled promise, and it navigates only after the current success.
    const operation = logout();
    const logoutOwner = captureAuthOwner();
    return operation.then(
      () => {
        if (isMounted.current && isCurrentAuthOwner(logoutOwner)) {
          navigate("/login", { replace: true });
        }
      },
      () => undefined,
    );
  };

  return (
    <nav className="fixed bottom-0 left-0 right-0 bg-card border-t border-border z-50 md:hidden" aria-label="Mobile navigation">
      <div className="flex items-center justify-around px-2 py-2" style={{ paddingBottom: 'env(safe-area-inset-bottom, 0px)' }}>
        {/* Primary Tabs */}
        {primaryNavItems.map((item) => {
          const Icon = item.icon;
          const isActive = activeItem === item.id;

          return (
            <button
              key={item.id}
              onClick={() => onItemSelect(item.id)}
              className={cn(
                "flex flex-col items-center gap-1 min-w-[44px] min-h-[44px] px-3 py-2 rounded-sm transition-all duration-200",
                "hover:bg-secondary focus:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
                // Issue #778: the active label is foreground text + an inset
                // ring cue (the #862 tabs pattern) — text-primary at text-xs
                // on the bg-primary/10 pill measured ~4.4:1 in light theme,
                // under the 4.5:1 AA floor; the pill alone measured ~1.1:1,
                // so the ring restores a >=3:1 non-text state cue.
                isActive && "bg-primary/10 ring-2 ring-inset ring-foreground"
              )}
              aria-label={item.label}
              aria-current={isActive ? "page" : undefined}
            >
              <Icon
                className={cn(
                  "w-5 h-5 transition-colors",
                  isActive ? "text-primary" : "text-muted-foreground"
                )}
              />
              <span
                className={cn(
                  "text-xs font-medium transition-colors",
                  isActive ? "text-foreground" : "text-muted-foreground"
                )}
              >
                {item.label}
              </span>
            </button>
          );
        })}

        {/* More Button with Sheet */}
        <Sheet open={moreOpen} onOpenChange={setMoreOpen}>
          <SheetTrigger asChild>
            <button
              className={cn(
                "flex flex-col items-center gap-1 min-w-[44px] min-h-[44px] px-3 py-2 rounded-sm transition-all duration-200",
                "hover:bg-secondary focus:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
                // Issue #778: same open-state cue contract as the active tab
                // above (foreground label + inset ring, not text-primary).
                moreOpen && "bg-primary/10 ring-2 ring-inset ring-foreground"
              )}
              aria-label="More navigation options"
              aria-expanded={moreOpen}
              aria-haspopup="menu"
            >
              <MoreHorizontal
                className={cn(
                  "w-5 h-5 transition-colors",
                  moreOpen ? "text-primary" : "text-muted-foreground"
                )}
              />
              <span
                className={cn(
                  "text-xs font-medium transition-colors",
                  moreOpen ? "text-foreground" : "text-muted-foreground"
                )}
              >
                More
              </span>
            </button>
          </SheetTrigger>
          {/* Issue #776 (UI-R4-03): admin tile lists exceed 50vh, so the sheet
              content scrolls — the scroll lives on an inner region so the
              built-in close control (absolutely positioned on SheetContent)
              stays pinned and reachable while scrolled (review PRR-002).
              Issue #776 (UI-R4-04): this sheet must not add a second close. */}
          <SheetContent side="bottom" className="flex h-[50vh] flex-col rounded-t-2xl" aria-describedby="mobile-more-desc">
            <SheetHeader className="shrink-0 pb-2">
              <SheetTitle id="mobile-more-title" className="text-xl font-semibold">More</SheetTitle>
              <SheetDescription id="mobile-more-desc">Access settings, help, and other options</SheetDescription>
            </SheetHeader>

            <div className="grid flex-1 grid-cols-2 content-start gap-3 overflow-y-auto">
              {moreNavItems
                .filter((item) => (!item.adminOnly || isAdmin) && (!item.capabilityGated || draftRoomVisible))
                .map((item) => {
                const Icon = item.icon;
                const isActive = activeItem === item.id;

                return (
                  <button
                    key={item.id}
                    onClick={() => {
                      onItemSelect(item.id);
                      setMoreOpen(false);
                    }}
                    className={cn(
                      "flex flex-col items-center gap-3 p-4 rounded-xl border border-border transition-all duration-200",
                      "hover:bg-secondary focus:outline-hidden focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
                      // Issue #778: foreground label + inset ring cue for the
                      // active tile (same contract as the primary tabs).
                      isActive && "bg-primary/10 border-primary/20 ring-2 ring-inset ring-foreground"
                    )}
                    aria-label={item.label}
                  >
                    <Icon
                      className={cn(
                        "w-6 h-6 transition-colors",
                        isActive ? "text-primary" : "text-muted-foreground"
                      )}
                    />
                    <span className="text-sm font-medium transition-colors text-foreground">
                      {item.label}
                    </span>
                  </button>
                );
              })}
              <button
                type="button"
                onClick={handleLogout}
                className={cn(
                  "flex flex-col items-center gap-3 p-4 rounded-xl border border-border transition-all duration-200",
                  "hover:bg-secondary focus:outline-hidden focus-visible:ring-2 focus-visible:ring-ring"
                )}
                aria-label="Log out"
              >
                <LogOut className="w-6 h-6 text-muted-foreground" />
                <span className="text-sm font-medium text-foreground">Log out</span>
              </button>
            </div>
          </SheetContent>
        </Sheet>
      </div>
    </nav>
  );
}
