import type { HealthStatus } from "@/types/health";
import type { ComponentType } from "react";

export type NavItemId = "chat" | "chatNew" | "documents" | "memory" | "vaults" | "wiki" | "kms" | "draftRoom" | "settings" | "groups" | "users" | "organizations" | "profile";

export interface NavItem {
  id: NavItemId;
  label: string;
  icon: ComponentType<{ className?: string }>;
}

export interface NavigationProps {
  // null = no nav item owns the current route (e.g. /search); only the
  // mobile bottom nav consumes this — the desktop rail derives its own
  // NavItemId | null from the location (issue #779 / UI-R1-08).
  activeItem: NavItemId | null;
  onItemSelect: (id: NavItemId) => void;
  healthStatus: HealthStatus;
}
