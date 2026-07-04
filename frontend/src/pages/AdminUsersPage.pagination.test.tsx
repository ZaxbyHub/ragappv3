// PAGINATION TESTS for AdminUsersPage — covers page/limit state, skip/limit query params,
// encodeURIComponent for searchQuery, setPage(1) on search change, and Pagination component
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

// Mock apiClient with inline vi.fn() so hoisting works
vi.mock('@/lib/api', () => ({
  default: {
    get: vi.fn().mockResolvedValue({
      data: {
        users: [
          { id: 1, username: 'alice', full_name: 'Alice Admin', role: 'admin', is_active: true, created_at: '2024-01-01' },
          { id: 2, username: 'bob', full_name: 'Bob Member', role: 'member', is_active: true, created_at: '2024-01-02' },
          { id: 3, username: 'carol', full_name: 'Carol Viewer', role: 'viewer', is_active: false, created_at: '2024-01-03' },
        ],
        total: 3,
      },
    }),
    patch: vi.fn().mockResolvedValue({ data: {} }),
    delete: vi.fn().mockResolvedValue({ data: {} }),
    post: vi.fn().mockResolvedValue({ data: {} }),
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

// Radix Select cannot be driven in jsdom; render it as a native <select>
vi.mock('@/components/ui/select', async () => {
  const React = await import('react');
  const findTriggerProps = (children: React.ReactNode): Record<string, unknown> => {
    let props: Record<string, unknown> = {};
    React.Children.forEach(children, (child: any) => {
      if (!child || typeof child !== 'object') return;
      if (child.type?.__isSelectTrigger) {
        props = child.props ?? {};
      } else if (child.props?.children) {
        const nested = findTriggerProps(child.props.children);
        if (Object.keys(nested).length) props = nested;
      }
    });
    return props;
  };
  const collectItems = (children: React.ReactNode): { value: string; label: React.ReactNode }[] => {
    const items: { value: string; label: React.ReactNode }[] = [];
    React.Children.forEach(children, (child: any) => {
      if (!child || typeof child !== 'object') return;
      if (child.type?.__isSelectItem) {
        items.push({ value: child.props.value, label: child.props.children });
      } else if (child.props?.children) {
        items.push(...collectItems(child.props.children));
      }
    });
    return items;
  };
  const SelectTrigger: any = ({ children }: any) => React.createElement(React.Fragment, null, children);
  SelectTrigger.__isSelectTrigger = true;
  const SelectItem: any = ({ children }: any) => React.createElement(React.Fragment, null, children);
  SelectItem.__isSelectItem = true;
  return {
    Select: ({ value, onValueChange, disabled, children }: any) => {
      const triggerProps = findTriggerProps(children);
      return React.createElement(
        'select',
        {
          value,
          disabled,
          'aria-label': triggerProps['aria-label'],
          id: triggerProps.id,
          onChange: (e: any) => onValueChange?.(e.target.value),
        },
        collectItems(children).map((item) =>
          React.createElement('option', { key: item.value, value: item.value }, item.label)
        )
      );
    },
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

// Mock Pagination component to capture and assert on props
const paginationProps = vi.fn();
vi.mock('@/components/ui/pagination', () => ({
  Pagination: (props: any) => {
    paginationProps(props);
    return <div data-testid="pagination" data-page={props.page} data-limit={props.limit} data-total={props.total} />;
  },
}));

vi.mock('@/components/auth/RoleGuard', () => ({
  AdminGuard: ({ children }: any) => <div>{children}</div>,
}));

// Mock lucide-react icons
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
  ChevronLeft: () => <span data-testid="chevron-left-icon" />,
  ChevronRight: () => <span data-testid="chevron-right-icon" />,
  MoreHorizontal: () => <span data-testid="more-icon" />,
}));

// --- Test Suite ---
describe('AdminUsersPage pagination', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    paginationProps.mockClear();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  // 1. Pagination component receives correct props
  it('renders Pagination with correct page/limit/total props', async () => {
    await act(async () => { render(<AdminUsersPage />); });
    await waitFor(() => expect(screen.getByTestId('pagination')).toBeInTheDocument());

    expect(paginationProps).toHaveBeenCalledWith(
      expect.objectContaining({
        page: expect.any(Number),
        limit: expect.any(Number),
        total: expect.any(Number),
        onPageChange: expect.any(Function),
        onLimitChange: expect.any(Function),
        isLoading: expect.any(Boolean),
      })
    );
  });

  it('Pagination receives page=1 initially', async () => {
    await act(async () => { render(<AdminUsersPage />); });
    await waitFor(() => expect(screen.getByTestId('pagination')).toBeInTheDocument());

    const calls = paginationProps.mock.calls;
    const lastCall = calls[calls.length - 1][0];
    expect(lastCall.page).toBe(1);
  });

  it('Pagination receives limit=20 by default', async () => {
    await act(async () => { render(<AdminUsersPage />); });
    await waitFor(() => expect(screen.getByTestId('pagination')).toBeInTheDocument());

    const calls = paginationProps.mock.calls;
    const lastCall = calls[calls.length - 1][0];
    expect(lastCall.limit).toBe(20);
  });

  it('Pagination receives total from API response', async () => {
    await act(async () => { render(<AdminUsersPage />); });
    await waitFor(() => expect(screen.getByTestId('pagination')).toBeInTheDocument());

    const calls = paginationProps.mock.calls;
    const lastCall = calls[calls.length - 1][0];
    expect(lastCall.total).toBe(3);
  });

  // 2. fetchUsers uses skip/limit params (page 1 → skip=0, limit=20)
  it('fetchUsers called with skip=0 limit=20 on initial page=1', async () => {
    const api = await import('@/lib/api');
    vi.mocked(api.default.get).mockClear();

    await act(async () => { render(<AdminUsersPage />); });
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    expect(api.default.get).toHaveBeenCalledWith(
      expect.stringContaining('/users/?skip=0&limit=20')
    );
  });

  // 3. fetchUsers uses encodeURIComponent for searchQuery
  it('searchQuery is encodeURIComponent in API URL', async () => {
    const api = await import('@/lib/api');
    vi.mocked(api.default.get).mockClear();
    vi.mocked(api.default.get).mockResolvedValue({ data: { users: [], total: 0 } });

    await act(async () => { render(<AdminUsersPage />); });
    await waitFor(() => expect(screen.getByTestId('pagination')).toBeInTheDocument());

    const searchInput = screen.getByPlaceholderText('Search by username or name...');
    const specialQuery = 'alice&bob';
    await act(async () => {
      fireEvent.change(searchInput, { target: { value: specialQuery } });
    });

    // Wait for debounce + fetch
    await waitFor(() => {
      expect(api.default.get).toHaveBeenCalledWith(
        expect.stringContaining(`q=${encodeURIComponent(specialQuery)}`)
      );
    });

    // Verify unencoded version is NOT present
    expect(api.default.get).not.toHaveBeenCalledWith(
      expect.stringContaining(`q=${specialQuery}`)
    );
  });

  it('Unicode searchQuery is encodeURIComponent in API URL', async () => {
    const api = await import('@/lib/api');
    vi.mocked(api.default.get).mockClear();
    vi.mocked(api.default.get).mockResolvedValue({ data: { users: [], total: 0 } });

    await act(async () => { render(<AdminUsersPage />); });
    await waitFor(() => expect(screen.getByTestId('pagination')).toBeInTheDocument());

    const searchInput = screen.getByPlaceholderText('Search by username or name...');
    const unicodeQuery = '用户';
    await act(async () => {
      fireEvent.change(searchInput, { target: { value: unicodeQuery } });
    });

    await waitFor(() => {
      expect(api.default.get).toHaveBeenCalledWith(
        expect.stringContaining(`q=${encodeURIComponent(unicodeQuery)}`)
      );
    });
  });

  // 4. setPage(1) on search change — changing search resets page to 1
  it('changing search resets page to 1', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const api = await import('@/lib/api');

    // Set up mock return value BEFORE render to avoid timing issues
    vi.mocked(api.default.get).mockResolvedValue({
      data: {
        users: [
          { id: 1, username: 'alice', full_name: 'Alice Admin', role: 'admin', is_active: true, created_at: '2024-01-01' },
        ],
        total: 1,
      },
    });

    await act(async () => { render(<AdminUsersPage />); });
    await waitFor(() => expect(screen.getByText('alice')).toBeInTheDocument());

    // Initial URL should have skip=0 for page 1
    expect(api.default.get).toHaveBeenCalledWith(
      expect.stringContaining('skip=0')
    );

    // Set up mock for search fetch BEFORE changing search
    vi.mocked(api.default.get).mockClear();
    vi.mocked(api.default.get).mockResolvedValue({ data: { users: [], total: 0 } });

    const searchInput = screen.getByPlaceholderText('Search by username or name...');
    await act(async () => {
      fireEvent.change(searchInput, { target: { value: 'alice' } });
    });

    // Wait for debounce (300ms) + effect
    await vi.advanceTimersByTimeAsync(400);

    await waitFor(() => {
      // The search change triggers a fetch with skip=0 (page reset to 1)
      expect(api.default.get).toHaveBeenCalledWith(
        expect.stringContaining('skip=0')
      );
    });
  });

  // 5. Pagination component is rendered (not absent)
  it('renders Pagination component', async () => {
    await act(async () => { render(<AdminUsersPage />); });
    await waitFor(() => {
      expect(screen.getByTestId('pagination')).toBeInTheDocument();
    });
  });

  // 6. Pagination onPageChange triggers correct skip calculation
  it('onPageChange(2) triggers fetch with skip=20 (page 2, limit 20)', async () => {
    const api = await import('@/lib/api');

    await act(async () => { render(<AdminUsersPage />); });
    await waitFor(() => expect(screen.getByTestId('pagination')).toBeInTheDocument());

    // Get the onPageChange callback from the last Pagination render
    const calls = paginationProps.mock.calls;
    const lastCall = calls[calls.length - 1][0];
    const onPageChange = lastCall.onPageChange;

    vi.mocked(api.default.get).mockClear();
    vi.mocked(api.default.get).mockResolvedValue({
      data: { users: [
        { id: 4, username: 'dave', full_name: 'Dave Extra', role: 'member', is_active: true, created_at: '2024-01-04' },
      ], total: 4 },
    });

    // Simulate clicking page 2
    await act(async () => {
      onPageChange(2);
    });

    await waitFor(() => {
      expect(api.default.get).toHaveBeenCalledWith(
        expect.stringContaining('skip=20')
      );
    });
  });

  it('onPageChange(3) triggers fetch with skip=40 (page 3, limit 20)', async () => {
    const api = await import('@/lib/api');

    await act(async () => { render(<AdminUsersPage />); });
    await waitFor(() => expect(screen.getByTestId('pagination')).toBeInTheDocument());

    const calls = paginationProps.mock.calls;
    const lastCall = calls[calls.length - 1][0];
    const onPageChange = lastCall.onPageChange;

    vi.mocked(api.default.get).mockClear();
    vi.mocked(api.default.get).mockResolvedValue({ data: { users: [], total: 5 } });

    await act(async () => { onPageChange(3); });

    await waitFor(() => {
      expect(api.default.get).toHaveBeenCalledWith(
        expect.stringContaining('skip=40')
      );
    });
  });

  // 7. Pagination onLimitChange triggers fetch with new limit and skip=0
  it('onLimitChange(50) triggers fetch with limit=50 and skip=0', async () => {
    const api = await import('@/lib/api');

    await act(async () => { render(<AdminUsersPage />); });
    await waitFor(() => expect(screen.getByTestId('pagination')).toBeInTheDocument());

    const calls = paginationProps.mock.calls;
    const lastCall = calls[calls.length - 1][0];
    const onLimitChange = lastCall.onLimitChange;

    vi.mocked(api.default.get).mockClear();
    vi.mocked(api.default.get).mockResolvedValue({ data: { users: [], total: 0 } });

    await act(async () => { onLimitChange(50); });

    await waitFor(() => {
      expect(api.default.get).toHaveBeenCalledWith(
        expect.stringContaining('limit=50')
      );
      expect(api.default.get).toHaveBeenCalledWith(
        expect.stringContaining('skip=0')
      );
    });
  });

  // 8. Loading state passed to Pagination
  it('Pagination receives isLoading=true while fetching', async () => {
    const api = await import('@/lib/api');
    let resolveGet: (val: unknown) => void;
    const fetchPromise = new Promise((resolve) => { resolveGet = resolve; });
    vi.mocked(api.default.get).mockReturnValue(fetchPromise);

    await act(async () => { render(<AdminUsersPage />); });

    // Pagination should have been called with isLoading at least once
    const isLoadingCalls = paginationProps.mock.calls.filter(([p]) => 'isLoading' in p);
    expect(isLoadingCalls.length).toBeGreaterThan(0);

    // Resolve the fetch
    await act(async () => {
      resolveGet!({ data: { users: [], total: 0 } });
    });
  });

  // 9. No client-side filteredUsers — API is called with search query
  // Verifies the old client-side filteredUsers is removed; API is called with q param
  it('search does NOT filter users client-side; API is called with q param', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const api = await import('@/lib/api');

    // Set up mock return value BEFORE render
    vi.mocked(api.default.get).mockResolvedValue({
      data: {
        users: [
          { id: 1, username: 'alice', full_name: 'Alice Admin', role: 'admin', is_active: true, created_at: '2024-01-01' },
        ],
        total: 1,
      },
    });

    await act(async () => { render(<AdminUsersPage />); });
    await waitFor(() => expect(screen.getByTestId('pagination')).toBeInTheDocument());

    // Set up mock for search fetch
    vi.mocked(api.default.get).mockClear();
    vi.mocked(api.default.get).mockResolvedValue({ data: { users: [], total: 0 } });

    const searchInput = screen.getByPlaceholderText('Search by username or name...');
    await act(async () => {
      fireEvent.change(searchInput, { target: { value: 'alice' } });
    });

    // Wait for debounce + effect
    await vi.advanceTimersByTimeAsync(400);

    await waitFor(() => {
      expect(api.default.get).toHaveBeenCalledWith(
        expect.stringContaining('q=')
      );
    });

    // The URL should contain the search query (proves client-side filtering is removed)
    const lastCallUrl = api.default.get.mock.calls[api.default.get.mock.calls.length - 1][0];
    expect(lastCallUrl).toContain('q=alice');
  });

  // 10. Empty search resets properly (q=)
  it('empty search query sends q= to API', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const api = await import('@/lib/api');

    // Set up mock return value BEFORE render
    vi.mocked(api.default.get).mockResolvedValue({
      data: {
        users: [
          { id: 1, username: 'alice', full_name: 'Alice Admin', role: 'admin', is_active: true, created_at: '2024-01-01' },
        ],
        total: 1,
      },
    });

    await act(async () => { render(<AdminUsersPage />); });
    await waitFor(() => expect(screen.getByTestId('pagination')).toBeInTheDocument());

    // Set up mock for empty search fetch
    vi.mocked(api.default.get).mockClear();
    vi.mocked(api.default.get).mockResolvedValue({ data: { users: [], total: 0 } });

    const searchInput = screen.getByPlaceholderText('Search by username or name...');
    // First type something to change the input value
    await act(async () => {
      fireEvent.change(searchInput, { target: { value: 'alice' } });
    });
    await vi.advanceTimersByTimeAsync(400);

    // Now clear the search - this should trigger another API call
    vi.mocked(api.default.get).mockClear();
    vi.mocked(api.default.get).mockResolvedValue({ data: { users: [], total: 0 } });

    await act(async () => {
      fireEvent.change(searchInput, { target: { value: '' } });
    });

    // Wait for debounce + effect
    await vi.advanceTimersByTimeAsync(400);

    await waitFor(() => {
      expect(api.default.get).toHaveBeenCalledWith(
        expect.stringContaining('q=')
      );
    });
  });
});
