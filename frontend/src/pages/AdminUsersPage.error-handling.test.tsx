// ERROR HANDLING TESTS for AdminUsersPage — verifies that catch blocks surface
// backend err?.response?.data?.detail strings when available, and fall back to
// generic messages otherwise.
//
// These tests mock the API at the module level and verify that the toast.error
// calls in catch blocks receive the correct messages. We test directly via the
// mock rather than through complex UI interactions.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import AdminUsersPage from '@/pages/AdminUsersPage';

// --- Mocks ---
vi.mock('@/stores/useAuthStore', () => ({
  useAuthStore: vi.fn((selector) => {
    if (typeof selector === 'function') {
      return selector({
        user: { id: 1, username: 'admin', full_name: 'Admin User', role: 'superadmin', is_active: true },
        isAuthenticated: true,
        isLoading: false,
      });
    }
    return { user: { id: 1, username: 'admin', full_name: 'Admin User', role: 'superadmin', is_active: true }, isAuthenticated: true, isLoading: false };
  }),
}));

vi.mock('@/lib/api', () => ({
  default: {
    get: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
  },
}));

vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

vi.mock('@/components/ui/card', () => ({
  Card: ({ children }: any) => <div data-testid="card">{children}</div>,
  CardContent: ({ children }: any) => <div data-testid="card-content">{children}</div>,
  CardHeader: ({ children }: any) => <div data-testid="card-header">{children}</div>,
}));

vi.mock('@/components/ui/button', () => ({
  Button: ({ children, onClick, disabled, ...props }: any) => <button onClick={onClick} disabled={disabled} {...props}>{children}</button>,
}));

vi.mock('@/components/ui/input', () => ({
  Input: (props: any) => <input {...props} />,
}));

vi.mock('@/components/ui/select', async () => {
  const React = await import('react');
  const SelectTrigger: any = ({ children }: any) => React.createElement(React.Fragment, null, children);
  SelectTrigger.__isSelectTrigger = true;
  const SelectItem: any = ({ children }: any) => React.createElement(React.Fragment, null, children);
  SelectItem.__isSelectItem = true;
  return {
    Select: ({ value, onValueChange, disabled, children }: any) => React.createElement('select', { value, disabled, onChange: (e: any) => onValueChange?.(e.target.value) }, React.Children.map(children, (c: any) => c)),
    SelectTrigger,
    SelectValue: () => null,
    SelectContent: ({ children }: any) => React.createElement(React.Fragment, null, children),
    SelectItem,
  };
});

vi.mock('@/components/ui/badge', () => ({
  Badge: ({ children }: any) => <span>{children}</span>,
}));

vi.mock('@/components/ui/dialog', () => ({
  Dialog: ({ children, open }: any) => open ? <div data-testid="dialog">{children}</div> : null,
  DialogContent: ({ children }: any) => <div data-testid="dialog-content">{children}</div>,
  DialogDescription: ({ children }: any) => <p>{children}</p>,
  DialogFooter: ({ children }: any) => <div>{children}</div>,
  DialogHeader: ({ children }: any) => <div>{children}</div>,
  DialogTitle: ({ children }: any) => <h2>{children}</h2>,
}));

vi.mock('@/components/ui/sheet', () => ({
  Sheet: ({ children, open }: any) => open ? <div data-testid="sheet">{children}</div> : null,
  SheetContent: ({ children }: any) => <div data-testid="sheet-content">{children}</div>,
  SheetDescription: ({ children }: any) => <p>{children}</p>,
  SheetFooter: ({ children }: any) => <div>{children}</div>,
  SheetHeader: ({ children }: any) => <div>{children}</div>,
  SheetTitle: ({ children }: any) => <h2>{children}</h2>,
}));

vi.mock('@/components/ui/checkbox', () => ({
  Checkbox: (props: any) => <input type="checkbox" {...props} />,
}));

vi.mock('@/components/ui/label', () => ({
  Label: ({ children, ...props }: any) => <label {...props}>{children}</label>,
}));

vi.mock('@/components/ui/scroll-area', () => ({
  ScrollArea: ({ children }: any) => <div data-testid="scroll-area">{children}</div>,
}));

vi.mock('@/components/ui/skeleton', () => ({
  Skeleton: () => <div data-testid="skeleton" />,
}));

vi.mock('@/components/ui/table', () => ({
  Table: ({ children }: any) => <table>{children}</table>,
  TableBody: ({ children }: any) => <tbody>{children}</tbody>,
  TableCaption: ({ children }: any) => <caption>{children}</caption>,
  TableCell: ({ children }: any) => <td>{children}</td>,
  TableHead: ({ children }: any) => <th>{children}</th>,
  TableHeader: ({ children }: any) => <thead>{children}</thead>,
  TableRow: ({ children }: any) => <tr>{children}</tr>,
}));

vi.mock('@/components/layout/PageTitleHeader', () => ({
  PageTitleHeader: ({ title }: any) => <div data-testid="page-title">{title}</div>,
}));

vi.mock('@/components/EmptyState', () => ({
  EmptyState: ({ title }: any) => <div data-testid="empty-state">{title}</div>,
}));

vi.mock('@/components/LoadingSpinner', () => ({
  LoadingSpinner: () => <div data-testid="loading-spinner" />,
}));

vi.mock('@/components/ui/pagination', () => ({
  Pagination: (props: any) => <div data-testid="pagination" />,
}));

vi.mock('@/components/auth/RoleGuard', () => ({
  AdminGuard: ({ children }: any) => <div>{children}</div>,
}));

vi.mock('lucide-react', () => ({
  Search: () => <span data-testid="search-icon" />,
  Trash2: () => <span data-testid="trash-icon" />,
  Loader2: () => <span data-testid="loader-icon" />,
  UserX: () => <span data-testid="user-x-icon" />,
  Users: () => <span data-testid="users-icon" />,
  Pencil: () => <span data-testid="pencil-icon" />,
  KeyRound: () => <span data-testid="key-icon" />,
  Plus: () => <span data-testid="plus-icon" />,
  Building2: () => <span data-testid="building-icon" />,
  ChevronUp: () => <span data-testid="chevron-up-icon" />,
  ChevronDown: () => <span data-testid="chevron-down-icon" />,
}));

// Helper to create an axios-like error with response.data.detail
function makeApiError(detail: string) {
  return {
    response: {
      data: {
        detail,
      },
    },
  };
}

// Helper to make a plain error without response (network error etc)
function makeNetworkError() {
  return new Error('Network error');
}

// Module-level URL response queue for per-endpoint test overrides.
// Tests register URL-prefix → queue-of-responses overrides here. The
// mockImplementation (set in beforeEach) checks this map first, consuming
// one response per matching URL call (FIFO). This avoids the
// mockResolvedValueOnce race condition where parallel /users/ + /groups +
// /organizations calls on mount would consume queue items unpredictably.
const urlResponseQueue = new Map<string, unknown[]>();

// Registers an override for a URL prefix. The mockImplementation checks
// urlResponseQueue first using url.startsWith(prefix), consuming queued
// responses FIFO. This allows tests to set up sequential responses for
// the same endpoint (multiple calls to /organizations/ etc.) without
// consuming calls to other endpoints.
// Stores plain values; mockImplementation throws if the value is an Error.
function registerUrlResponse(urlPrefix: string, response: unknown) {
  const queue = urlResponseQueue.get(urlPrefix) ?? [];
  queue.push(response);
  urlResponseQueue.set(urlPrefix, queue);
}

function clearUrlResponseMap() {
  urlResponseQueue.clear();
}

// Default GET responses for URL routing (used by beforeEach mockImplementation)
const DEFAULT_USERS_RESPONSE = {
  data: {
    users: [
      { id: 1, username: 'alice', full_name: 'Alice Admin', role: 'admin', is_active: true, created_at: '2024-01-01' },
      { id: 2, username: 'bob', full_name: 'Bob Member', role: 'member', is_active: true, created_at: '2024-01-02' },
    ],
    total: 2,
  },
};

const DEFAULT_GROUPS_RESPONSE = {
  data: { groups: [{ id: 1, name: 'Admins', description: null }] },
};

const DEFAULT_ORGS_RESPONSE = {
  data: { organizations: [{ id: 1, name: 'Org A', description: 'desc', role: 'member', joined_at: '2024-01-01' }] },
};

// --- Test Suite ---
describe('AdminUsersPage error handling', () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    clearUrlResponseMap();
    // Reset API mocks to resolve successfully by default for initial render.
    // mockImplementation checks urlResponseMap first (for test overrides), then
    // falls back to default URL routing. This ensures parallel /users/ + /groups
    // + /organizations calls on mount are each routed correctly regardless of
    // which call resolves first — avoiding the mockResolvedValueOnce race.
    const api = await import('@/lib/api');
    (api.default.get as ReturnType<typeof vi.fn>).mockImplementation((url: string) => {
      // Check test overrides first (url.startsWith prefix matching), consume FIFO
      for (const [prefix, queue] of urlResponseQueue) {
        if (url.startsWith(prefix) && queue.length > 0) {
          const val = queue.shift()!;
          // Reject if it's an Error instance OR an axios-style error object
          // (plain object with .response.data.detail — used by makeApiError)
          const isAxiosError =
            typeof val === 'object' && val !== null && (val as any).response?.data?.detail !== undefined;
          if (val instanceof Error || isAxiosError) {
            return Promise.reject(val);
          }
          return Promise.resolve(val);
        }
      }
      // Default URL routing
      const pathname = url.split('?')[0];
      if (pathname === '/users/') {
        return Promise.resolve(DEFAULT_USERS_RESPONSE);
      }
      if (pathname.endsWith('/groups')) {
        return Promise.resolve(DEFAULT_GROUPS_RESPONSE);
      }
      if (pathname.endsWith('/organizations')) {
        return Promise.resolve(DEFAULT_ORGS_RESPONSE);
      }
      return Promise.resolve({ data: {} });
    });
    (api.default.patch as ReturnType<typeof vi.fn>).mockImplementation(() => Promise.resolve({ data: {} }));
    (api.default.delete as ReturnType<typeof vi.fn>).mockImplementation(() => Promise.resolve({ data: {} }));
    (api.default.post as ReturnType<typeof vi.fn>).mockImplementation(() => Promise.resolve({ data: {} }));
    (api.default.put as ReturnType<typeof vi.fn>).mockImplementation(() => Promise.resolve({ data: {} }));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  // Helper to get the toast.error mock
  const getToastError = async () => {
    const sonner = await import('sonner');
    return vi.mocked(sonner.toast.error);
  };

  // ============================================================
  // fetchUsers — error in catch block at line 169
  // ============================================================
  it('fetchUsers: surfaces backend detail when API returns error with detail', async () => {
    const api = await import('@/lib/api');
    api.default.get.mockRejectedValueOnce(makeApiError('User not found'));

    await act(async () => { render(<AdminUsersPage />); });
    
    const toastError = await getToastError();
    await vi.waitFor(() => expect(toastError).toHaveBeenCalled());
    expect(toastError).toHaveBeenCalledWith('User not found');
  });

  it('fetchUsers: uses generic fallback when error has no response.detail', async () => {
    const api = await import('@/lib/api');
    api.default.get.mockRejectedValueOnce(makeNetworkError());

    await act(async () => { render(<AdminUsersPage />); });
    
    const toastError = await getToastError();
    await vi.waitFor(() => expect(toastError).toHaveBeenCalled());
    expect(toastError).toHaveBeenCalledWith('Failed to load users');
  });

  // ============================================================
  // handleRoleChange — error in catch block at line 186
  // ============================================================
  it('handleRoleChange: surfaces backend detail from API error', async () => {
    const api = await import('@/lib/api');
    api.default.patch.mockRejectedValueOnce(makeApiError('Cannot change role of superadmin'));

    await act(async () => { render(<AdminUsersPage />); });
    await vi.waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    // Trigger role change via the select
    const selects = document.querySelectorAll('select');
    await act(async () => {
      fireEvent.change(selects[1], { target: { value: 'admin' } });
    });

    const toastError = await getToastError();
    await vi.waitFor(() => expect(toastError).toHaveBeenCalledWith('Cannot change role of superadmin'));
  });

  it('handleRoleChange: uses generic fallback when error has no response.detail', async () => {
    const api = await import('@/lib/api');
    api.default.patch.mockRejectedValueOnce(makeNetworkError());

    await act(async () => { render(<AdminUsersPage />); });
    await vi.waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    const selects = document.querySelectorAll('select');
    await act(async () => {
      fireEvent.change(selects[1], { target: { value: 'admin' } });
    });

    const toastError = await getToastError();
    await vi.waitFor(() => expect(toastError).toHaveBeenCalledWith('Failed to update role'));
  });

  // ============================================================
  // handleActiveToggle — error in catch block at line 199
  // ============================================================
  it('handleActiveToggle: surfaces backend detail from API error', async () => {
    const api = await import('@/lib/api');
    api.default.patch.mockRejectedValueOnce(makeApiError('User is protected'));

    await act(async () => { render(<AdminUsersPage />); });
    await vi.waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    // Click toggle for bob (second user, index 1)
    const toggles = document.querySelectorAll('button[role="switch"]');
    await act(async () => {
      fireEvent.click(toggles[1]);
    });

    const toastError = await getToastError();
    await vi.waitFor(() => expect(toastError).toHaveBeenCalledWith('User is protected'));
  });

  it('handleActiveToggle: uses generic fallback when error has no response.detail', async () => {
    const api = await import('@/lib/api');
    api.default.patch.mockRejectedValueOnce(makeNetworkError());

    await act(async () => { render(<AdminUsersPage />); });
    await vi.waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    const toggles = document.querySelectorAll('button[role="switch"]');
    await act(async () => {
      fireEvent.click(toggles[1]);
    });

    const toastError = await getToastError();
    await vi.waitFor(() => expect(toastError).toHaveBeenCalledWith('Failed to update user status'));
  });

  // ============================================================
  // handleDelete — error in catch block at line 214
  // ============================================================
  it('handleDelete: surfaces backend detail from API error', async () => {
    const api = await import('@/lib/api');
    api.default.delete.mockRejectedValueOnce(makeApiError('Cannot delete last superadmin'));

    await act(async () => { render(<AdminUsersPage />); });
    await vi.waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    // Click delete button for bob (aria-label specific)
    const deleteButton = document.querySelector('button[aria-label="Delete user bob"]');
    await act(async () => {
      fireEvent.click(deleteButton!);
    });

    await vi.waitFor(() => expect(screen.getByTestId('dialog')).toBeInTheDocument());

    // Click the destructive variant button inside the dialog
    const dialogEl = screen.getByTestId('dialog');
    const confirmBtn = dialogEl.querySelector('button[variant="destructive"]');
    await act(async () => {
      fireEvent.click(confirmBtn!);
    });

    const toastError = await getToastError();
    await vi.waitFor(() => expect(toastError).toHaveBeenCalledWith('Cannot delete last superadmin'));
  });

  it('handleDelete: uses generic fallback when error has no response.detail', async () => {
    const api = await import('@/lib/api');
    api.default.delete.mockRejectedValueOnce(makeNetworkError());

    await act(async () => { render(<AdminUsersPage />); });
    await vi.waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    const deleteButton = document.querySelector('button[aria-label="Delete user bob"]');
    await act(async () => {
      fireEvent.click(deleteButton!);
    });

    await vi.waitFor(() => expect(screen.getByTestId('dialog')).toBeInTheDocument());

    const dialogEl = screen.getByTestId('dialog');
    const confirmBtn = dialogEl.querySelector('button[variant="destructive"]');
    await act(async () => {
      fireEvent.click(confirmBtn!);
    });

    const toastError = await getToastError();
    await vi.waitFor(() => expect(toastError).toHaveBeenCalledWith('Failed to delete user'));
  });

  // ============================================================
  // handleSaveEdit — error in catch block at line 249
  // ============================================================
  it('handleSaveEdit: surfaces backend detail from API error', async () => {
    const api = await import('@/lib/api');
    api.default.patch.mockRejectedValueOnce(makeApiError('Username already taken'));

    await act(async () => { render(<AdminUsersPage />); });
    await vi.waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    // Click edit button for bob
    const editButton = document.querySelector('button[aria-label="Edit user bob"]');
    await act(async () => {
      fireEvent.click(editButton!);
    });

    await vi.waitFor(() => expect(screen.getByTestId('dialog')).toBeInTheDocument());

    // Click save changes
    const dialogEl = screen.getByTestId('dialog');
    const saveBtn = dialogEl.querySelector('button:not([variant])');
    await act(async () => {
      fireEvent.click(saveBtn!);
    });

    const toastError = await getToastError();
    await vi.waitFor(() => expect(toastError).toHaveBeenCalledWith('Username already taken'));
  });

  it('handleSaveEdit: uses generic fallback when error has no response.detail', async () => {
    const api = await import('@/lib/api');
    api.default.patch.mockRejectedValueOnce(makeNetworkError());

    await act(async () => { render(<AdminUsersPage />); });
    await vi.waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    const editButton = document.querySelector('button[aria-label="Edit user bob"]');
    await act(async () => {
      fireEvent.click(editButton!);
    });

    await vi.waitFor(() => expect(screen.getByTestId('dialog')).toBeInTheDocument());

    const dialogEl = screen.getByTestId('dialog');
    const saveBtn = dialogEl.querySelector('button:not([variant])');
    await act(async () => {
      fireEvent.click(saveBtn!);
    });

    const toastError = await getToastError();
    await vi.waitFor(() => expect(toastError).toHaveBeenCalledWith('Failed to update user'));
  });

  // ============================================================
  // handleResetPassword — error in catch block at line 288
  // ============================================================
  it('handleResetPassword: surfaces backend detail from API error', async () => {
    const api = await import('@/lib/api');
    api.default.patch.mockRejectedValueOnce(makeApiError('Password does not meet complexity requirements'));

    await act(async () => { render(<AdminUsersPage />); });
    await vi.waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    // Click reset password button for bob
    const resetButton = document.querySelector('button[aria-label="Reset password for bob"]');
    await act(async () => {
      fireEvent.click(resetButton!);
    });

    await vi.waitFor(() => expect(screen.getByTestId('dialog')).toBeInTheDocument());

    // Fill passwords and submit - use id selectors to avoid matching other elements
    const newPassInput = document.getElementById('new-password')!;
    const confirmPassInput = document.getElementById('confirm-password')!;
    await act(async () => {
      fireEvent.change(newPassInput, { target: { value: 'Password123!' } });
      fireEvent.change(confirmPassInput, { target: { value: 'Password123!' } });
    });

    const dialogEl = screen.getByTestId('dialog');
    const submitBtn = dialogEl.querySelector('button:not([variant])');
    await act(async () => {
      fireEvent.click(submitBtn!);
    });

    const toastError = await getToastError();
    await vi.waitFor(() => expect(toastError).toHaveBeenCalledWith('Password does not meet complexity requirements'));
  });

  it('handleResetPassword: uses generic fallback when error has no response.detail', async () => {
    const api = await import('@/lib/api');
    api.default.patch.mockRejectedValueOnce(makeNetworkError());

    await act(async () => { render(<AdminUsersPage />); });
    await vi.waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    const resetButton = document.querySelector('button[aria-label="Reset password for bob"]');
    await act(async () => {
      fireEvent.click(resetButton!);
    });

    await vi.waitFor(() => expect(screen.getByTestId('dialog')).toBeInTheDocument());

    const newPassInput = document.getElementById('new-password')!;
    const confirmPassInput = document.getElementById('confirm-password')!;
    await act(async () => {
      fireEvent.change(newPassInput, { target: { value: 'Password123!' } });
      fireEvent.change(confirmPassInput, { target: { value: 'Password123!' } });
    });

    const dialogEl = screen.getByTestId('dialog');
    const submitBtn = dialogEl.querySelector('button:not([variant])');
    await act(async () => {
      fireEvent.click(submitBtn!);
    });

    const toastError = await getToastError();
    await vi.waitFor(() => expect(toastError).toHaveBeenCalledWith('Failed to reset password'));
  });

  // ============================================================
  // handleCreateUser — error in catch block at line 477
  // ============================================================
  it('handleCreateUser: surfaces backend detail from API error', async () => {
    const api = await import('@/lib/api');
    api.default.post.mockRejectedValueOnce(makeApiError('Username already exists'));

    await act(async () => { render(<AdminUsersPage />); });
    await vi.waitFor(() => expect(screen.getByText('User Management')).toBeInTheDocument());

    // Click Add User button
    const addBtn = screen.getByRole('button', { name: /add user/i });
    await act(async () => {
      fireEvent.click(addBtn);
    });

    await vi.waitFor(() => expect(screen.getByTestId('dialog')).toBeInTheDocument());

    // Fill form - use id selectors to avoid ambiguity
    const usernameInput = document.getElementById('create-username')!;
    const passwordInput = document.getElementById('create-password')!;
    await act(async () => {
      fireEvent.change(usernameInput, { target: { value: 'newuser' } });
      fireEvent.change(passwordInput, { target: { value: 'Password123!' } });
    });

    // Submit via form submit button inside dialog
    const dialogEl = screen.getByTestId('dialog');
    const createBtn = dialogEl.querySelector('button[type="submit"]');
    await act(async () => {
      fireEvent.click(createBtn!);
    });

    const toastError = await getToastError();
    await vi.waitFor(() => expect(toastError).toHaveBeenCalledWith('Username already exists'));
  });

  it('handleCreateUser: uses generic fallback when error has no response.detail', async () => {
    const api = await import('@/lib/api');
    api.default.post.mockRejectedValueOnce(makeNetworkError());

    await act(async () => { render(<AdminUsersPage />); });
    await vi.waitFor(() => expect(screen.getByText('User Management')).toBeInTheDocument());

    const addBtn = screen.getByRole('button', { name: /add user/i });
    await act(async () => {
      fireEvent.click(addBtn);
    });

    await vi.waitFor(() => expect(screen.getByTestId('dialog')).toBeInTheDocument());

    const usernameInput = document.getElementById('create-username')!;
    const passwordInput = document.getElementById('create-password')!;
    await act(async () => {
      fireEvent.change(usernameInput, { target: { value: 'newuser' } });
      fireEvent.change(passwordInput, { target: { value: 'Password123!' } });
    });

    const dialogEl = screen.getByTestId('dialog');
    const createBtn = dialogEl.querySelector('button[type="submit"]');
    await act(async () => {
      fireEvent.click(createBtn!);
    });

    const toastError = await getToastError();
    await vi.waitFor(() => expect(toastError).toHaveBeenCalledWith('Failed to create user'));
  });

  // ============================================================
  // handleSaveGroups — error in catch block at line 348
  // ============================================================
  it('handleSaveGroups: surfaces backend detail from API error', async () => {
    const api = await import('@/lib/api');
    // Only the put (save) fails; /groups and /users/ use default mockImplementation
    api.default.put.mockImplementation(() => Promise.reject(makeApiError('User not in required group')));

    await act(async () => { render(<AdminUsersPage />); });
    await vi.waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    // Open groups sheet for bob
    const groupButton = document.querySelector('button[aria-label="Manage groups for bob"]');
    await act(async () => {
      fireEvent.click(groupButton!);
    });

    await vi.waitFor(() => expect(screen.getByTestId('sheet')).toBeInTheDocument());

    // Click save
    const sheetEl = screen.getByTestId('sheet');
    const saveBtn = sheetEl.querySelector('button[aria-label="Save group changes"]');
    await act(async () => {
      fireEvent.click(saveBtn!);
    });

    const toastError = await getToastError();
    await vi.waitFor(() => expect(toastError).toHaveBeenCalledWith('User not in required group'));
  });

  it('handleSaveGroups: uses generic fallback when error has no response.detail', async () => {
    const api = await import('@/lib/api');
    // Only the put (save) fails; /groups and /users/ use default mockImplementation
    api.default.put.mockImplementation(() => Promise.reject(makeNetworkError()));

    await act(async () => { render(<AdminUsersPage />); });
    await vi.waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    const groupButton = document.querySelector('button[aria-label="Manage groups for bob"]');
    await act(async () => {
      fireEvent.click(groupButton!);
    });

    await vi.waitFor(() => expect(screen.getByTestId('sheet')).toBeInTheDocument());

    const sheetEl = screen.getByTestId('sheet');
    const saveBtn = sheetEl.querySelector('button[aria-label="Save group changes"]');
    await act(async () => {
      fireEvent.click(saveBtn!);
    });

    const toastError = await getToastError();
    await vi.waitFor(() => expect(toastError).toHaveBeenCalledWith('Failed to update groups'));
  });

  // ============================================================
  // handleSaveOrgs — error in catch block at line 425
  // ============================================================
  it('handleSaveOrgs: surfaces backend detail from API error', async () => {
    const api = await import('@/lib/api');
    // Only the put (save) fails; /organizations/ and /users/ use default mockImplementation
    api.default.put.mockImplementation(() => Promise.reject(makeApiError('User cannot manage this organization')));

    await act(async () => { render(<AdminUsersPage />); });
    await vi.waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    // Open orgs sheet for bob
    const orgButton = document.querySelector('button[aria-label="Manage organizations for bob"]');
    await act(async () => {
      fireEvent.click(orgButton!);
    });

    await vi.waitFor(() => expect(screen.getByTestId('sheet')).toBeInTheDocument());

    // Click save
    const sheetEl = screen.getByTestId('sheet');
    const saveBtn = sheetEl.querySelector('button[aria-label="Save organization changes"]');
    await act(async () => {
      fireEvent.click(saveBtn!);
    });

    const toastError = await getToastError();
    await vi.waitFor(() => expect(toastError).toHaveBeenCalledWith('User cannot manage this organization'));
  });

  it('handleSaveOrgs: uses generic fallback when error has no response.detail', async () => {
    const api = await import('@/lib/api');
    // Only the put (save) fails; /organizations/ and /users/ use default mockImplementation
    api.default.put.mockImplementation(() => Promise.reject(makeNetworkError()));

    await act(async () => { render(<AdminUsersPage />); });
    await vi.waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    const orgButton = document.querySelector('button[aria-label="Manage organizations for bob"]');
    await act(async () => {
      fireEvent.click(orgButton!);
    });

    await vi.waitFor(() => expect(screen.getByTestId('sheet')).toBeInTheDocument());

    const sheetEl = screen.getByTestId('sheet');
    const saveBtn = sheetEl.querySelector('button[aria-label="Save organization changes"]');
    await act(async () => {
      fireEvent.click(saveBtn!);
    });

    const toastError = await getToastError();
    await vi.waitFor(() => expect(toastError).toHaveBeenCalledWith('Failed to update organizations'));
  });

  // ============================================================
  // fetchAllGroups — error in catch block at line 301
  // ============================================================
  it('fetchAllGroups: surfaces backend detail from API error', async () => {
    // Register /groups to fail; /users/ uses default mockImplementation
    registerUrlResponse('/groups', makeApiError('Insufficient permissions to view groups'));

    await act(async () => { render(<AdminUsersPage />); });
    await vi.waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    const groupButton = document.querySelector('button[aria-label="Manage groups for bob"]');
    await act(async () => {
      fireEvent.click(groupButton!);
    });

    const toastError = await getToastError();
    await vi.waitFor(() => expect(toastError).toHaveBeenCalledWith('Insufficient permissions to view groups'));
  });

  it('fetchAllGroups: uses generic fallback when error has no response.detail', async () => {
    // Register /groups to fail with network error; /users/ uses default mockImplementation
    registerUrlResponse('/groups', makeNetworkError());

    await act(async () => { render(<AdminUsersPage />); });
    await vi.waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    const groupButton = document.querySelector('button[aria-label="Manage groups for bob"]');
    await act(async () => {
      fireEvent.click(groupButton!);
    });

    const toastError = await getToastError();
    await vi.waitFor(() => expect(toastError).toHaveBeenCalledWith('Failed to load groups'));
  });

  // ============================================================
  // fetchUserGroups — error in catch block at line 311
  // ============================================================
  it('fetchUserGroups: surfaces backend detail from API error', async () => {
    // Register: /groups succeeds, /users/2/groups fails (bob's user id is 2)
    registerUrlResponse('/groups', { data: { groups: [{ id: 1, name: 'Admins', description: null }] } });
    registerUrlResponse(`/users/2/groups`, makeApiError('User group fetch forbidden'));

    await act(async () => { render(<AdminUsersPage />); });
    await vi.waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    const groupButton = document.querySelector('button[aria-label="Manage groups for bob"]');
    await act(async () => {
      fireEvent.click(groupButton!);
    });

    const toastError = await getToastError();
    await vi.waitFor(() => expect(toastError).toHaveBeenCalledWith('User group fetch forbidden'));
  });

  it('fetchUserGroups: uses generic fallback when error has no response.detail', async () => {
    // Register: /groups succeeds, /users/2/groups fails with network error
    registerUrlResponse('/groups', { data: { groups: [{ id: 1, name: 'Admins', description: null }] } });
    registerUrlResponse(`/users/2/groups`, makeNetworkError());

    await act(async () => { render(<AdminUsersPage />); });
    await vi.waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    const groupButton = document.querySelector('button[aria-label="Manage groups for bob"]');
    await act(async () => {
      fireEvent.click(groupButton!);
    });

    const toastError = await getToastError();
    await vi.waitFor(() => expect(toastError).toHaveBeenCalledWith('Failed to load user groups'));
  });

  // ============================================================
  // fetchAllOrgs — error in catch block at line 361
  // ============================================================
  it('fetchAllOrgs: surfaces backend detail from API error', async () => {
    registerUrlResponse('/organizations/', makeApiError('Organization access denied'));

    await act(async () => { render(<AdminUsersPage />); });
    await vi.waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    const orgButton = document.querySelector('button[aria-label="Manage organizations for bob"]');
    await act(async () => {
      fireEvent.click(orgButton!);
    });

    const toastError = await getToastError();
    await vi.waitFor(() => expect(toastError).toHaveBeenCalledWith('Organization access denied'));
  });

  it('fetchAllOrgs: uses generic fallback when error has no response.detail', async () => {
    registerUrlResponse('/organizations/', makeNetworkError());

    await act(async () => { render(<AdminUsersPage />); });
    await vi.waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    const orgButton = document.querySelector('button[aria-label="Manage organizations for bob"]');
    await act(async () => {
      fireEvent.click(orgButton!);
    });

    const toastError = await getToastError();
    await vi.waitFor(() => expect(toastError).toHaveBeenCalledWith('Failed to load organizations'));
  });

  // ============================================================
  // fetchUserOrgs — error in catch block at line 375
  // ============================================================
  it('fetchUserOrgs: surfaces backend detail from API error', async () => {
    // Register: /organizations/ succeeds, /users/2/organizations fails
    registerUrlResponse('/organizations/', { data: { organizations: [{ id: 1, name: 'Org A', description: 'desc' }], total: 1 } });
    registerUrlResponse('/users/2/organizations', makeApiError('User org fetch forbidden'));

    await act(async () => { render(<AdminUsersPage />); });
    await vi.waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    const orgButton = document.querySelector('button[aria-label="Manage organizations for bob"]');
    await act(async () => {
      fireEvent.click(orgButton!);
    });

    const toastError = await getToastError();
    await vi.waitFor(() => expect(toastError).toHaveBeenCalledWith('User org fetch forbidden'));
  });

  it('fetchUserOrgs: uses generic fallback when error has no response.detail', async () => {
    // Register: /organizations/ succeeds, /users/2/organizations fails with network error
    registerUrlResponse('/organizations/', { data: { organizations: [{ id: 1, name: 'Org A', description: 'desc' }], total: 1 } });
    registerUrlResponse('/users/2/organizations', makeNetworkError());

    await act(async () => { render(<AdminUsersPage />); });
    await vi.waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    const orgButton = document.querySelector('button[aria-label="Manage organizations for bob"]');
    await act(async () => {
      fireEvent.click(orgButton!);
    });

    const toastError = await getToastError();
    await vi.waitFor(() => expect(toastError).toHaveBeenCalledWith('Failed to load user organizations'));
  });

  // ============================================================
  // Unicode and injection edge cases
  // ============================================================
  it('surfacing unicode detail string from backend', async () => {
    // Register error for /users/ endpoint
    registerUrlResponse('/users/', makeApiError('操作失败：用户不存在'));

    await act(async () => { render(<AdminUsersPage />); });

    const toastError = await getToastError();
    await vi.waitFor(() => expect(toastError).toHaveBeenCalled());
    expect(toastError).toHaveBeenCalledWith('操作失败：用户不存在');
  });

  it('detail string with HTML chars is passed as plain string, not executed', async () => {
    const xssAttempt = '<script>alert(1)</script>';
    // Register error for /users/ endpoint
    registerUrlResponse('/users/', makeApiError(xssAttempt));

    await act(async () => { render(<AdminUsersPage />); });

    const toastError = await getToastError();
    await vi.waitFor(() => expect(toastError).toHaveBeenCalled());
    expect(toastError).toHaveBeenCalledWith(xssAttempt);
  });
});
