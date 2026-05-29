import type { User, UserListItem } from "@/lib/api";

export const mockUsers: User[] = import.meta.env.DEV ? [
  {
    id: 1,
    email: "alex.doe@example.com",
    full_name: "Alex Doe",
    is_active: true,
    is_superuser: true,
    created_at: "2023-09-01T08:00:00Z",
    updated_at: "2024-05-01T12:00:00Z",
  },
  {
    id: 2,
    email: "jordan.admin@example.com",
    full_name: "Jordan Smith",
    is_active: true,
    is_superuser: false,
    created_at: "2023-09-05T09:00:00Z",
    updated_at: "2024-04-20T10:00:00Z",
  },
  {
    id: 3,
    email: "casey.member@example.com",
    full_name: "Casey Johnson",
    is_active: true,
    is_superuser: false,
    created_at: "2023-10-01T11:00:00Z",
    updated_at: "2024-04-15T14:00:00Z",
  },
  {
    id: 4,
    email: "taylor.viewer@example.com",
    full_name: "Taylor Brown",
    is_active: true,
    is_superuser: false,
    created_at: "2023-10-10T08:30:00Z",
    updated_at: "2024-03-28T09:00:00Z",
  },
  {
    id: 5,
    email: "morgan.inactive@example.com",
    full_name: "Morgan Wilson",
    is_active: false,
    is_superuser: false,
    created_at: "2023-11-01T10:00:00Z",
    updated_at: "2024-02-15T16:00:00Z",
  },
  {
    id: 6,
    email: "riley.eng@example.com",
    full_name: "Riley Garcia",
    is_active: true,
    is_superuser: false,
    created_at: "2024-01-15T09:00:00Z",
    updated_at: "2024-05-06T11:00:00Z",
  },
  {
    id: 7,
    email: "quinn.lead@example.com",
    full_name: "Quinn Martinez",
    is_active: true,
    is_superuser: false,
    created_at: "2024-02-01T08:00:00Z",
    updated_at: "2024-05-05T10:00:00Z",
  },
  {
    id: 8,
    email: "skyler.new@example.com",
    full_name: "Skyler Lee",
    is_active: true,
    is_superuser: false,
    created_at: "2024-04-20T13:00:00Z",
    updated_at: "2024-04-20T13:00:00Z",
  },
] : [];

export const mockUserListItems: UserListItem[] = import.meta.env.DEV ? [
  { id: 1, username: "alex.doe", full_name: "Alex Doe", role: "superadmin", is_active: true },
  { id: 2, username: "jordan.admin", full_name: "Jordan Smith", role: "admin", is_active: true },
  { id: 3, username: "casey.member", full_name: "Casey Johnson", role: "member", is_active: true },
  { id: 4, username: "taylor.viewer", full_name: "Taylor Brown", role: "viewer", is_active: true },
  { id: 5, username: "morgan.inactive", full_name: "Morgan Wilson", role: "member", is_active: false },
  { id: 6, username: "riley.eng", full_name: "Riley Garcia", role: "member", is_active: true },
  { id: 7, username: "quinn.lead", full_name: "Quinn Martinez", role: "admin", is_active: true },
  { id: 8, username: "skyler.new", full_name: "Skyler Lee", role: "viewer", is_active: true },
] : [];

export interface MockAdminUser {
  id: number;
  username: string;
  full_name: string;
  role: "superadmin" | "admin" | "member" | "viewer";
  is_active: boolean;
  created_at: string;
}

export const mockAdminUsers: MockAdminUser[] = import.meta.env.DEV ? [
  { id: 1, username: "alex.doe", full_name: "Alex Doe", role: "superadmin", is_active: true, created_at: "2023-09-01T08:00:00Z" },
  { id: 2, username: "jordan.admin", full_name: "Jordan Smith", role: "admin", is_active: true, created_at: "2023-09-05T09:00:00Z" },
  { id: 3, username: "casey.member", full_name: "Casey Johnson", role: "member", is_active: true, created_at: "2023-10-01T11:00:00Z" },
  { id: 4, username: "taylor.viewer", full_name: "Taylor Brown", role: "viewer", is_active: true, created_at: "2023-10-10T08:30:00Z" },
  { id: 5, username: "morgan.inactive", full_name: "Morgan Wilson", role: "member", is_active: false, created_at: "2023-11-01T10:00:00Z" },
  { id: 6, username: "riley.eng", full_name: "Riley Garcia", role: "member", is_active: true, created_at: "2024-01-15T09:00:00Z" },
  { id: 7, username: "quinn.lead", full_name: "Quinn Martinez", role: "admin", is_active: true, created_at: "2024-02-01T08:00:00Z" },
  { id: 8, username: "skyler.new", full_name: "Skyler Lee", role: "viewer", is_active: true, created_at: "2024-04-20T13:00:00Z" },
] : [];
