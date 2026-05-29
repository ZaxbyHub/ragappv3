import type { Group, GroupMember } from "@/lib/api";

export const mockGroups: Group[] = import.meta.env.DEV ? [
  {
    id: 1,
    name: "Engineering Team",
    description: "All engineers with access to technical vaults",
    created_at: "2023-09-01T08:00:00Z",
    org_id: 1,
    organization_name: "Acme Corporation",
  },
  {
    id: 2,
    name: "Product Managers",
    description: "Product team with read access to most resources",
    created_at: "2023-09-05T10:00:00Z",
    org_id: 1,
    organization_name: "Acme Corporation",
  },
  {
    id: 3,
    name: "Legal Department",
    description: "Restricted access to compliance and legal documents",
    created_at: "2023-10-01T09:00:00Z",
    org_id: 1,
    organization_name: "Acme Corporation",
  },
  {
    id: 4,
    name: "Research Unit A",
    description: "Core research team for experimental projects",
    created_at: "2023-10-15T11:00:00Z",
    org_id: 2,
    organization_name: "Beta Labs",
  },
  {
    id: 5,
    name: "Support Tier 1",
    description: "Front-line customer support agents",
    created_at: "2023-11-20T08:00:00Z",
    org_id: 3,
    organization_name: "Gamma Solutions",
  },
  {
    id: 6,
    name: "DevOps Squad",
    description: "Infrastructure and deployment specialists",
    created_at: "2024-01-05T09:00:00Z",
    org_id: 4,
    organization_name: "Delta Systems",
  },
] : [];

export const mockGroupMembers: Record<number, GroupMember[]> = import.meta.env.DEV ? ({
  1: [
    { id: 2, username: "jordan.admin", full_name: "Jordan Smith" },
    { id: 3, username: "casey.member", full_name: "Casey Johnson" },
    { id: 6, username: "riley.eng", full_name: "Riley Garcia" },
    { id: 7, username: "quinn.lead", full_name: "Quinn Martinez" },
  ],
  2: [
    { id: 2, username: "jordan.admin", full_name: "Jordan Smith" },
    { id: 4, username: "taylor.viewer", full_name: "Taylor Brown" },
  ],
  3: [
    { id: 1, username: "alex.doe", full_name: "Alex Doe" },
    { id: 5, username: "morgan.inactive", full_name: "Morgan Wilson" },
  ],
  4: [
    { id: 6, username: "riley.eng", full_name: "Riley Garcia" },
  ],
  5: [
    { id: 3, username: "casey.member", full_name: "Casey Johnson" },
    { id: 8, username: "skyler.new", full_name: "Skyler Lee" },
  ],
  6: [
    { id: 7, username: "quinn.lead", full_name: "Quinn Martinez" },
  ],
}) : ({} as Record<number, GroupMember[]>);
