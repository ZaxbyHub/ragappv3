export type UserRole = "superadmin" | "admin" | "member" | "viewer";

export interface User {
  id: number;
  username: string;
  full_name: string;
  role: UserRole;
  is_active: boolean;
  created_at: string;
}

export interface Group {
  id: number;
  name: string;
  description: string | null;
}

export interface OrgItem {
  id: number;
  name: string;
  description: string;
  role?: string;
  joined_at?: string;
}
