interface FixtureUser {
  id: number;
  username: string;
  full_name: string;
  role: "superadmin" | "admin" | "member" | "viewer";
  is_active: boolean;
}

export const mockCurrentUser: FixtureUser = import.meta.env.DEV ? ({
  id: 1,
  username: "alex.doe",
  full_name: "Alex Doe",
  role: "superadmin",
  is_active: true,
}) : ({} as FixtureUser);

export const mockAdminUser: FixtureUser = import.meta.env.DEV ? ({
  id: 2,
  username: "jordan.admin",
  full_name: "Jordan Admin",
  role: "admin",
  is_active: true,
}) : ({} as FixtureUser);

export const mockMemberUser: FixtureUser = import.meta.env.DEV ? ({
  id: 3,
  username: "casey.member",
  full_name: "Casey Member",
  role: "member",
  is_active: true,
}) : ({} as FixtureUser);

export const mockViewerUser: FixtureUser = import.meta.env.DEV ? ({
  id: 4,
  username: "taylor.viewer",
  full_name: "Taylor Viewer",
  role: "viewer",
  is_active: true,
}) : ({} as FixtureUser);

export const mockInactiveUser: FixtureUser = import.meta.env.DEV ? ({
  id: 5,
  username: "morgan.inactive",
  full_name: "Morgan Inactive",
  role: "member",
  is_active: false,
}) : ({} as FixtureUser);
