import {
  ChevronLeft,
  ChevronRight,
  Library,
} from "lucide-react";
import { useThemeStore } from "@/stores/useThemeStore";
import { useAuthStore } from "@/stores/useAuthStore";
import { cn } from "@/lib/utils";
import { NavLink, useLocation } from "react-router-dom";
import type { NavItemId, NavigationProps } from "./navigationTypes";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useState, useEffect } from "react";
import { HugeiconsIcon, type IconSvgElement } from "@hugeicons/react";
import { MessageMultiple01Icon, AiBrain01Icon, Files01Icon, Database02Icon, BookOpenTextIcon, UserSettings01Icon, UserCircleIcon, UserGroupIcon, Setting07Icon, Building03Icon, Sun02Icon, MoonIcon, Logout01Icon } from "@hugeicons/core-free-icons";
import { MeridianLogo } from "../icons/MeridianLogo";

// Re-export types for backward compatibility
export type { NavItemId, NavigationProps };

type NavSection = "workspace" | "admin" | "account";

interface NavConfigItem {
  id: NavItemId;
  label: string;
  icon: React.ComponentType<{ className?: string }> | IconSvgElement;
  to: string;
  section: NavSection;
  adminOnly?: boolean;
}

const navItems: NavConfigItem[] = [
  { id: "chat", label: "Chat", icon: MessageMultiple01Icon, to: "/chat", section: "workspace" },
  { id: "documents", label: "Documents", icon: Files01Icon, to: "/documents", section: "workspace" },
  { id: "memory", label: "Memory", icon: AiBrain01Icon, to: "/memory", section: "workspace" },
  { id: "wiki", label: "Wiki", icon: BookOpenTextIcon, to: "/wiki", section: "workspace" },
  { id: "kms", label: "KMS", icon: Library, to: "/kms", section: "workspace" },
  { id: "vaults", label: "Vaults", icon: Database02Icon, to: "/vaults", section: "workspace" },
  { id: "groups", label: "Groups", icon: UserGroupIcon, to: "/admin/groups", section: "admin", adminOnly: true },
  { id: "users", label: "Users", icon: UserSettings01Icon, to: "/admin/users", section: "admin", adminOnly: true },
  { id: "organizations", label: "Organizations", icon: Building03Icon, to: "/admin/organizations", section: "admin", adminOnly: true },
  { id: "settings", label: "Settings", icon: Setting07Icon, to: "/settings", section: "account" },
  { id: "profile", label: "Profile", icon: UserCircleIcon, to: "/profile", section: "account" },
];

const sectionLabels: Record<NavSection, string> = {
  workspace: "Workspace",
  admin: "Administration",
  account: "Account",
};

function StatusIndicator({ isUp, label, loading, isExpanded }: { isUp: boolean; label: string; loading?: boolean; isExpanded?: boolean }) {
  return (
    <div className="flex items-center min-h-4 gap-2.5">
      <div
        className={cn(
          "rounded-full flex-shrink-0 size-3",
          loading ? "bg-warning animate-pulse" : isUp ? "bg-success" : "bg-destructive"
        )}
      />
      {isExpanded && (
        <>
          <span className="text-[11px] text-muted-foreground truncate whitespace-nowrap">
            {loading ? "Checking" : label}
          </span>
          <span className="sr-only">{label}: {isUp ? "online" : "offline"}</span>
        </>
      )}
    </div>
  );
}

interface NavigationRailProps {
  healthStatus: NavigationProps["healthStatus"];
}

const SIDEBAR_EXPANDED_KEY = "sidebar-expanded";

export function NavigationRail({ healthStatus }: NavigationRailProps) {
  const location = useLocation();
  const pathname = location.pathname;
  const { theme, setTheme } = useThemeStore();
  const userRole = useAuthStore((state) => state.user?.role);
  const logout = useAuthStore((state) => state.logout);
  const isAdmin = userRole === "admin" || userRole === "superadmin";

  const [isExpanded, setIsExpanded] = useState(() => {
    const saved = localStorage.getItem(SIDEBAR_EXPANDED_KEY);
    return saved === null ? true : saved === "true";
  });

  useEffect(() => {
    localStorage.setItem(SIDEBAR_EXPANDED_KEY, String(isExpanded));
  }, [isExpanded]);

  const getActiveItem = (): NavItemId | null => {
    for (const item of navItems) {
      if (item.to === pathname) return item.id;
    }
    if (pathname.startsWith("/chat/")) return "chat";
    if (pathname.startsWith("/admin/groups")) return "groups";
    if (pathname.startsWith("/admin/users")) return "users";
    if (pathname.startsWith("/admin/organizations")) return "organizations";
    if (pathname.startsWith("/profile")) return "profile";
    return null;
  };

  const activeItem = getActiveItem();

  const visibleItems = navItems.filter((item) => !item.adminOnly || isAdmin);

  const workspaceItems = visibleItems.filter((i) => i.section === "workspace");
  const adminItems = visibleItems.filter((i) => i.section === "admin");
  const accountItems = visibleItems.filter((i) => i.section === "account");
  return (
    <nav
      className={cn(
        "h-screen bg-card border-r border-border flex flex-col flex-shrink-0 z-50 transition-all duration-300 ease-in-out relative shadow-md",
        isExpanded ? "w-60" : "w-14"
      )}
      aria-label="Main navigation"
    >
      {/* Header / Brand */}
      <div className="flex items-center px-4 pt-5 pb-2 min-h-[76px]">
        <div className="flex items-center mb-4 w-full gap-2">
          <div className={cn("flex items-center justify-center flex-shrink-0 transition-all duration-200 ease-in-out", isExpanded ? "size-8" : "size-6")}>
            <MeridianLogo />
          </div>
          <span
            className={cn(
              "font-bold text-md text-primary transition-all duration-200 ease-in-out font-electrolize uppercase tracking-tighter",
              isExpanded ? "opacity-100 blur-none" : "opacity-0 w-0 overflow-hidden blur-sm"
            )}
          >
            Meridian
          </span>
        </div>
      </div>

      {/* Scrollable nav content */}
      <ScrollArea className="flex-1 min-h-0 justify-between">
        <div className={cn("pb-2 px-2 overflow-hidden transition-all duration-300 ease-in-out", isExpanded ? "w-full" : "w-14")}>
          {/* Workspace Section */}
          <SectionHeader label={sectionLabels.workspace} isExpanded={isExpanded} />
          <div className="flex flex-col gap-2 pt-2">
            {workspaceItems.map((item) => (
              <NavRow key={item.id} item={item} isActive={activeItem === item.id} isExpanded={isExpanded} />
            ))}
          </div>

          {/* Admin Section */}
          {adminItems.length > 0 && (
            <>
              <SectionHeader label={sectionLabels.admin} isExpanded={isExpanded} />
              <div className="flex flex-col gap-2 pt-2">
                {adminItems.map((item) => (
                  <NavRow key={item.id} item={item} isActive={activeItem === item.id} isExpanded={isExpanded} />
                ))}
              </div>
            </>
          )}

          {/* Account Section */}
          <SectionHeader label={sectionLabels.account} isExpanded={isExpanded} />
          <div className="flex flex-col gap-2 pt-2">
            {accountItems.map((item) => (
              <NavRow key={item.id} item={item} isActive={activeItem === item.id} isExpanded={isExpanded} />
            ))}
          </div>
        </div>
      </ScrollArea>

      {/* Footer */}
      <div className={cn("py-3 px-2 border-t border-border transition-all duration-300 ease-in-out overflow-hidden", isExpanded ? "w-full" : "w-14")}>
        {/* Theme Toggle */}
        <button
          type="button"
          onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
          className="flex items-center rounded-sm text-muted-foreground hover:bg-muted hover:text-foreground text-sm transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring w-full gap-2 px-3 py-1.5 mb-3 h-8"
          aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
        >
          {theme === "dark" ? (
            <HugeiconsIcon strokeWidth={1.2} icon={Sun02Icon} size={16} className="flex-shrink-0" />
          ) : (
            <HugeiconsIcon strokeWidth={1.2} icon={MoonIcon} size={16} className="flex-shrink-0" />
          )}
          <span className={cn("text-[11px] opacity-0 blur-sm transition-all duration-200 ease-in-out whitespace-nowrap", !isExpanded && "sr-only", isExpanded && "opacity-100 blur-none")}>{theme === "dark" ? "Light mode" : "Dark mode"}</span>
        </button>

        {/* Logout */}
        <button
          type="button"
          onClick={() => logout()}
          className="flex items-center rounded-sm text-muted-foreground hover:bg-muted hover:text-foreground text-sm transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring w-full gap-2 px-3 py-1.5 mb-3 h-8"
          aria-label="Log out"
        >
          <HugeiconsIcon strokeWidth={1.2} icon={Logout01Icon} size={16} className="flex-shrink-0" />
          <span className={cn("text-[11px] opacity-0 blur-sm transition-all duration-200 ease-in-out whitespace-nowrap", !isExpanded && "sr-only", isExpanded && "opacity-100 blur-none")}>Log out</span>
        </button>

        {/* Health Status */}
        <div className="flex flex-col gap-2 rounded-sm bg-muted/50 py-2 px-3.5 border border-border h-[82px]">
          <StatusIndicator isUp={healthStatus.backend} label="API" loading={healthStatus.loading} isExpanded={isExpanded} />
          <StatusIndicator isUp={healthStatus.embeddings} label="Embeddings" loading={healthStatus.loading} isExpanded={isExpanded} />
          <StatusIndicator isUp={healthStatus.chat} label="Chat" loading={healthStatus.loading} isExpanded={isExpanded} />
        </div>
      </div>

      {/* Expand button */}
      <button
        type="button"
        onClick={() => setIsExpanded(!isExpanded)}
        className="size-8 flex items-center justify-center p-1.5 mb-2 rounded-sm text-muted-foreground bg-card hover:text-foreground transition-colors absolute top-1/2 -translate-y-1/2 border border-border -right-4"
        aria-label="Expand sidebar"
      >
        {isExpanded ? <ChevronLeft className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
      </button>
    </nav>
  );
}

function SectionHeader({ label, isExpanded = false }: { label: string; isExpanded: boolean }) {
  return (
    <h3 className="w-full px-2 pt-4 pb-1 text-[11px] font-semibold text-muted-foreground uppercase tracking-wider border-b border-border overflow-hidden">
      <span className={cn("opacity-0 transition-all duration-200 ease-in-out blur-sm whitespace-nowrap", isExpanded && "opacity-100 blur-none")}>{label}</span>
    </h3>
  );
}

function NavRow({
  item,
  isActive,
  isExpanded,
}: {
  item: NavConfigItem;
  isActive: boolean;
  isExpanded: boolean;
}) {
  const Icon = item.icon;
  return (
    <NavLink
      to={item.to}
      className={cn(
        "flex items-center rounded-sm text-sm transition-colors gap-2.5 px-3 py-1.5 min-h-[32px]",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        isActive
          ? "bg-accent text-accent-foreground font-medium hover:bg-accent/90"
          : "text-muted-foreground hover:text-foreground hover:bg-muted",
      )}
      aria-current={isActive ? "page" : undefined}
      title={isExpanded ? undefined : item.label}
    >
      {typeof Icon === "function" ? (
        <Icon className="w-4 h-4 flex-shrink-0" />
      ) : (
        <HugeiconsIcon strokeWidth={1.2} icon={Icon} size={16} className="flex-shrink-0" />
      )}
      {isExpanded && <span className="truncate whitespace-nowrap">{item.label}</span>}
    </NavLink>
  );
}
