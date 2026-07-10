import { useState } from "react";
import { MessageSquare, FileText, Brain, MoreHorizontal, Database, Settings, Users, X, User, Building2, UserCog, BookOpen, Library, LogOut } from "lucide-react";
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
import { useNavigate } from "react-router-dom";
import type { NavItemId } from "./navigationTypes";

interface MobileBottomNavProps {
  activeItem: NavItemId;
  onItemSelect: (id: NavItemId) => void;
}

// Primary tabs shown on bottom nav
const primaryNavItems = [
  { id: "chat" as const, label: "Chat", icon: MessageSquare },
  { id: "documents" as const, label: "Documents", icon: FileText },
  { id: "memory" as const, label: "Memory", icon: Brain },
];

// Secondary items shown in "More" drawer
const moreNavItems: { id: NavItemId; label: string; icon: React.ComponentType<{ className?: string }>; adminOnly?: boolean }[] = [
  { id: "wiki", label: "Wiki", icon: BookOpen },
  { id: "kms" as const, label: "KMS", icon: Library },
  { id: "vaults", label: "Vaults", icon: Database },
  { id: "settings", label: "Settings", icon: Settings },
  { id: "groups", label: "Groups", icon: Users, adminOnly: true },
  { id: "users", label: "Users", icon: UserCog, adminOnly: true },
  { id: "profile", label: "Profile", icon: User },
  { id: "organizations", label: "Orgs", icon: Building2, adminOnly: true },
];

export function MobileBottomNav({ activeItem, onItemSelect }: MobileBottomNavProps) {
  const [moreOpen, setMoreOpen] = useState(false);
  const navigate = useNavigate();
  const userRole = useAuthStore((state) => state.user?.role);
  const logout = useAuthStore((state) => state.logout);
  const isAdmin = userRole === "admin" || userRole === "superadmin";

  const handleLogout = async () => {
    setMoreOpen(false);
    await logout();
    navigate("/login", { replace: true });
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
                "hover:bg-secondary focus:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                isActive && "bg-primary/10"
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
                  isActive ? "text-primary" : "text-muted-foreground"
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
                "hover:bg-secondary focus:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                moreOpen && "bg-primary/10"
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
                  moreOpen ? "text-primary" : "text-muted-foreground"
                )}
              >
                More
              </span>
            </button>
          </SheetTrigger>
          <SheetContent side="bottom" className="h-[50vh] rounded-t-2xl" aria-describedby="mobile-more-desc">
            <SheetHeader className="mb-6">
              <div className="flex items-center justify-between">
                <SheetTitle id="mobile-more-title" className="text-xl font-semibold">More</SheetTitle>
                <button
                  onClick={() => setMoreOpen(false)}
                  className="p-2 rounded-sm hover:bg-secondary focus:outline-none focus-visible:ring-2 focus-visible:ring-ring min-w-[44px] min-h-[44px]"
                  aria-label="Close"
                >
                  <X className="w-5 h-5" />
                </button>
</div>
              <SheetDescription id="mobile-more-desc">Access settings, help, and other options</SheetDescription>
            </SheetHeader>

            <div className="grid grid-cols-2 gap-3">
              {moreNavItems.filter((item) => !item.adminOnly || isAdmin).map((item) => {
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
                      "hover:bg-secondary focus:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                      isActive && "bg-primary/10 border-primary/20"
                    )}
                    aria-label={item.label}
                  >
                    <Icon
                      className={cn(
                        "w-6 h-6 transition-colors",
                        isActive ? "text-primary" : "text-muted-foreground"
                      )}
                    />
                    <span
                      className={cn(
                        "text-sm font-medium transition-colors",
                        isActive ? "text-primary" : "text-foreground"
                      )}
                    >
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
                  "hover:bg-secondary focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
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
