interface FixtureUser {
  id: number;
  username: string;
  full_name: string;
  role: "superadmin" | "admin" | "member" | "viewer";
  is_active: boolean;
}

export const mockCurrentUser: FixtureUser = {
  id: 1,
  username: "alex.doe",
  full_name: "Alex Doe",
  role: "superadmin",
  is_active: true,
};

export const mockAdminUser: FixtureUser = {
  id: 2,
  username: "jordan.admin",
  full_name: "Jordan Admin",
  role: "admin",
  is_active: true,
};

export const mockMemberUser: FixtureUser = {
  id: 3,
  username: "casey.member",
  full_name: "Casey Member",
  role: "member",
  is_active: true,
};

export const mockViewerUser: FixtureUser = {
  id: 4,
  username: "taylor.viewer",
  full_name: "Taylor Viewer",
  role: "viewer",
  is_active: true,
};

export const mockInactiveUser: FixtureUser = {
  id: 5,
  username: "morgan.inactive",
  full_name: "Morgan Inactive",
  role: "member",
  is_active: false,
};
