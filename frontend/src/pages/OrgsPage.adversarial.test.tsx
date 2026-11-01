// ADVERSARIAL TESTS for OrgsPage — XSS, injection, self-action, edge cases, error handling
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, act, fireEvent, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import OrgsPage from '@/pages/OrgsPage';
import { toast } from 'sonner';
// Issue #771 (UI-ENH-05): the below-sm card list duplicates row
// text next to the desktop table; scope data queries to the table subtree
// (a card-only regression still fails these assertions).
const inTable = () => within(screen.getByRole('table'));

// --- Mocks ---
vi.mock('@/stores/useAuthStore', () => ({
  useAuthStore: vi.fn((selector) => {
    const state = {
      user: { id: 1, username: 'superadmin', full_name: 'Super Admin', role: 'superadmin' },
      isAuthenticated: true, isLoading: false,
    };
    return typeof selector === 'function' ? selector(state) : state;
  }),
}));

const mockOrgs = [
  { id: 1, name: 'Acme Corp', description: 'Acme organization', member_count: 5, vault_count: 3, created_at: '2024-01-01' },
  { id: 2, name: 'Globex Inc', description: null, member_count: 10, vault_count: 7, created_at: '2024-02-15' },
];

const mockMembers = [
  { user_id: 1, username: 'alice', full_name: 'Alice Johnson', role: 'admin' as const, joined_at: '2024-01-10' },
  { user_id: 2, username: 'bob', full_name: 'Bob Smith', role: 'member' as const, joined_at: '2024-01-15' },
];

vi.mock('@/lib/api', () => ({
  default: {
    get: vi.fn().mockImplementation((url: string) => {
      if (url.includes('/members')) return Promise.resolve({ data: { members: mockMembers } });
      return Promise.resolve({ data: mockOrgs });
    }),
    post: vi.fn().mockResolvedValue({ data: { id: 3, name: 'New Org', description: null, member_count: 0, vault_count: 0, created_at: '2024-03-01' } }),
    patch: vi.fn().mockResolvedValue({ data: {} }),
    delete: vi.fn().mockResolvedValue({ data: {} }),
  },
}));

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

vi.mock('@/components/ui/card', () => ({
  Card: ({ children }: any) => <div data-testid="card">{children}</div>,
  CardContent: ({ children }: any) => <div data-testid="card-content">{children}</div>,
  CardHeader: ({ children }: any) => <div data-testid="card-header">{children}</div>,
  CardTitle: ({ children }: any) => <h3>{children}</h3>,
  CardDescription: ({ children }: any) => <p>{children}</p>,
}));

vi.mock('@/components/ui/button', () => ({
  Button: ({ children, onClick, disabled, ...props }: any) => <button onClick={onClick} disabled={disabled} {...props}>{children}</button>,
}));

vi.mock('@/components/ui/input', () => ({
  Input: (props: any) => <input {...props} />,
}));

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

vi.mock('@/components/auth/RoleGuard', () => ({
  AdminGuard: ({ children }: any) => <div>{children}</div>,
  RoleGuard: ({ children }: any) => <div>{children}</div>,
}));

describe('OrgsPage ADVERSARIAL', () => {
  beforeEach(() => { vi.clearAllMocks(); });
  afterEach(() => { vi.restoreAllMocks(); });

  // 1. XSS in org names
  describe('XSS in organization data', () => {
    const xssPayloads = [
      '<script>alert("xss")</script>',
      '<img onerror="alert(1)" src=x>',
      '"><script>alert(document.cookie)</script>',
    ];

    it.each(xssPayloads)('should NOT execute XSS in org name: %s', async (payload) => {
      const api = await import('@/lib/api');
      vi.mocked(api.default.get).mockResolvedValueOnce({ data: [
        { id: 99, name: payload, description: 'Safe desc', member_count: 0, vault_count: 0, created_at: '2024-01-01' },
      ] });

      await act(async () => { render(<OrgsPage />); });

      await waitFor(() => {
        expect(screen.getByText((content) => content.includes(payload) || content.includes('script'))).toBeInTheDocument();
      });
      expect(document.querySelectorAll('script')).toHaveLength(0);
    });

    it.each(xssPayloads)('should NOT execute XSS in org description: %s', async (payload) => {
      const api = await import('@/lib/api');
      vi.mocked(api.default.get).mockResolvedValueOnce({ data: [
        { id: 99, name: 'Safe Org', description: payload, member_count: 0, vault_count: 0, created_at: '2024-01-01' },
      ] });

      await act(async () => { render(<OrgsPage />); });

      await waitFor(() => {
        expect(screen.getByText('Safe Org')).toBeInTheDocument();
      });
      expect(document.querySelectorAll('script')).toHaveLength(0);
    });
  });

  // 2. XSS in member data when expanded
  describe('XSS in member data', () => {
    it('should NOT execute XSS in member full_name', async () => {
      const api = await import('@/lib/api');
      const xssPayload = '<script>alert("xss")</script>';
      vi.mocked(api.default.get).mockImplementation((url: string) => {
        if (url.includes('/members')) {
          return Promise.resolve({ data: { members: [
            { user_id: 99, username: 'safe', full_name: xssPayload, role: 'member', joined_at: '2024-01-01' },
          ] } });
        }
        return Promise.resolve({ data: mockOrgs });
      });

      await act(async () => { render(<OrgsPage />); });

      await waitFor(() => {
        expect(screen.getByText('Acme Corp')).toBeInTheDocument();
      });

      const expandBtn = screen.getAllByRole('button', { name: /expand/i })[0];
      await act(async () => { fireEvent.click(expandBtn); });

      await waitFor(() => {
        // #771: table-scoped (below-sm card list duplicates the text)
        expect(inTable().getByText((content) => content.includes(xssPayload) || content.includes('script'))).toBeInTheDocument();
      });
      expect(document.querySelectorAll('script')).toHaveLength(0);
    });
  });

  // 3. API error handling
  describe('API error handling', () => {
    it('should show error toast on fetch failure', async () => {
      const api = await import('@/lib/api');
      vi.mocked(api.default.get).mockRejectedValueOnce(new Error('500'));

      await act(async () => { render(<OrgsPage />); });

      await waitFor(() => {
        expect(toast.error).toHaveBeenCalledWith('Failed to load organizations');
      });
    });

    it('should show error toast on create failure', async () => {
      const api = await import('@/lib/api');
      vi.mocked(api.default.post).mockRejectedValueOnce(new Error('400'));

      await act(async () => { render(<OrgsPage />); });

      await waitFor(() => {
        expect(screen.getByText('Acme Corp')).toBeInTheDocument();
      });

      const createBtn = screen.getByText('Create Organization');
      await act(async () => { fireEvent.click(createBtn); });

      await waitFor(() => {
        expect(screen.getByTestId('dialog')).toBeInTheDocument();
      });

      const nameInput = screen.getByPlaceholderText('Organization name...');
      await act(async () => {
        fireEvent.change(nameInput, { target: { value: 'Test Org' } });
      });

      const submitBtn = screen.getByRole('button', { name: /^create$/i });
      await act(async () => { fireEvent.click(submitBtn); });

      await waitFor(() => {
        expect(toast.error).toHaveBeenCalledWith('Failed to create organization');
      });
    });

    it('should show error toast on delete failure', async () => {
      const api = await import('@/lib/api');
      vi.mocked(api.default.delete).mockRejectedValueOnce(new Error('403'));

      await act(async () => { render(<OrgsPage />); });

      await waitFor(() => {
        expect(screen.getByText('Acme Corp')).toBeInTheDocument();
      });

      const deleteBtn = document.querySelector('button[aria-label="Delete organization Acme Corp"]') as HTMLElement;
      expect(deleteBtn).not.toBeNull();
      await act(async () => { fireEvent.click(deleteBtn); });

      await waitFor(() => {
        expect(screen.getByTestId('dialog')).toBeInTheDocument();
      });

      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Delete Organization' }));
      });

      await waitFor(() => {
        expect(toast.error).toHaveBeenCalledWith('Failed to delete organization');
      });
    });
  });

  // 4. Empty/null boundary
  describe('Empty/null boundary', () => {
    it('should handle empty org list', async () => {
      const api = await import('@/lib/api');
      vi.mocked(api.default.get).mockResolvedValueOnce({ data: [] });

      await act(async () => { render(<OrgsPage />); });

      await waitFor(() => {
        expect(screen.getByText(/no organizations found/i)).toBeInTheDocument();
      });
    });

    it('should handle null description without crash', async () => {
      await act(async () => { render(<OrgsPage />); });

      await waitFor(() => {
        expect(screen.getByText('Globex Inc')).toBeInTheDocument();
      });
      // Globex has null description - should not crash
    });

    it('should handle empty member list when expanded', async () => {
      const api = await import('@/lib/api');
      vi.mocked(api.default.get).mockImplementation((url: string) => {
        if (url.includes('/members')) return Promise.resolve({ data: { members: [] } });
        return Promise.resolve({ data: mockOrgs });
      });

      await act(async () => { render(<OrgsPage />); });

      await waitFor(() => {
        expect(screen.getByText('Acme Corp')).toBeInTheDocument();
      });

      const expandBtn = screen.getAllByRole('button', { name: /expand/i })[0];
      await act(async () => { fireEvent.click(expandBtn); });

      await waitFor(() => {
        // #771: table-scoped (below-sm card list duplicates the text)
        expect(inTable().getByText(/no members yet/i)).toBeInTheDocument();
      });
    });
  });

  // 5. Very long strings
  describe('Very long strings', () => {
    it('should handle 1000+ char org name', async () => {
      const api = await import('@/lib/api');
      const longName = 'A'.repeat(1500);
      vi.mocked(api.default.get).mockResolvedValueOnce({ data: [
        { id: 99, name: longName, description: null, member_count: 0, vault_count: 0, created_at: '2024-01-01' },
      ] });

      await act(async () => { render(<OrgsPage />); });

      await waitFor(() => {
        expect(screen.getByText(longName)).toBeInTheDocument();
      });
    });
  });

  // 6. Injection in create dialog
  describe('Injection in create dialog', () => {
    it('should reject empty org name on submit', async () => {
      await act(async () => { render(<OrgsPage />); });

      await waitFor(() => {
        expect(screen.getByText('Acme Corp')).toBeInTheDocument();
      });

      const createBtn = screen.getByText('Create Organization');
      await act(async () => { fireEvent.click(createBtn); });

      await waitFor(() => {
        expect(screen.getByTestId('dialog')).toBeInTheDocument();
      });

      const submitBtn = screen.getByRole('button', { name: /^create$/i });
      // Submit button should be disabled when name is empty
      expect(submitBtn).toBeDisabled();
    });

    it('should reject whitespace-only org name', async () => {
      await act(async () => { render(<OrgsPage />); });

      await waitFor(() => {
        expect(screen.getByText('Acme Corp')).toBeInTheDocument();
      });

      const createBtn = screen.getByText('Create Organization');
      await act(async () => { fireEvent.click(createBtn); });

      await waitFor(() => {
        expect(screen.getByTestId('dialog')).toBeInTheDocument();
      });

      const nameInput = screen.getByPlaceholderText('Organization name...');
      await act(async () => {
        fireEvent.change(nameInput, { target: { value: '   ' } });
      });

      const submitBtn = screen.getByRole('button', { name: /^create$/i });
      // Should be disabled because newOrgName.trim() is empty
      expect(submitBtn).toBeDisabled();
    });
  });

  // 7. Unicode handling
  describe('Unicode handling', () => {
    it('should handle Unicode org names and descriptions', async () => {
      const api = await import('@/lib/api');
      vi.mocked(api.default.get).mockResolvedValueOnce({ data: [
        { id: 99, name: '组织名称', description: '这是一个描述', member_count: 0, vault_count: 0, created_at: '2024-01-01' },
        { id: 100, name: '🏢 Organization', description: '🎉 Party', member_count: 0, vault_count: 0, created_at: '2024-01-01' },
      ] });

      await act(async () => { render(<OrgsPage />); });

      await waitFor(() => {
        expect(screen.getByText('组织名称')).toBeInTheDocument();
        expect(screen.getByText('🏢 Organization')).toBeInTheDocument();
      });
    });
  });

  // 8. Rapid expand/collapse
  describe('Rapid expand/collapse', () => {
    it('should handle rapid toggle without crash', async () => {
      await act(async () => { render(<OrgsPage />); });

      await waitFor(() => {
        expect(screen.getByText('Acme Corp')).toBeInTheDocument();
      });

      const expandBtns = screen.getAllByRole('button', { name: /expand/i });

      for (let i = 0; i < 10; i++) {
        await act(async () => {
          fireEvent.click(expandBtns[0]);
        });
      }

      // Should not crash
      expect(screen.getByText('Acme Corp')).toBeInTheDocument();
    });
  });

  // 9. Concurrent member add
  describe('Concurrent member operations', () => {
    it('should not crash on member role change', async () => {
      const api = await import('@/lib/api');
      vi.mocked(api.default.get).mockImplementation((url: string) => {
        if (url.includes('/members')) return Promise.resolve({ data: { members: mockMembers } });
        return Promise.resolve({ data: mockOrgs });
      });
      vi.mocked(api.default.patch).mockResolvedValue({ data: {} });

      await act(async () => { render(<OrgsPage />); });

      await waitFor(() => {
        expect(screen.getByText('Acme Corp')).toBeInTheDocument();
      });

      const expandBtn = screen.getAllByRole('button', { name: /expand/i })[0];
      await act(async () => { fireEvent.click(expandBtn); });

      await waitFor(() => {
        // #771: table-scoped (below-sm card list duplicates the text)
        expect(inTable().getByText('Alice Johnson')).toBeInTheDocument();
      });

      // Verify the role select exists and is changeable
      // #771: table-scoped (below-sm card list duplicates the text)
      const roleSelect = screen.getByRole('table').querySelector('select[aria-label*="alice"]') as HTMLElement;
      expect(roleSelect).not.toBeNull();

      // Change role and verify no crash
      await act(async () => {
        fireEvent.change(roleSelect, { target: { value: 'member' } });
      });

      await waitFor(() => {
        expect(api.default.patch).toHaveBeenCalled();
      });

      // Component should still render
      // #771: table-scoped (below-sm card list duplicates the text)
      expect(inTable().getByText('Alice Johnson')).toBeInTheDocument();
    });
  });

  // 10. Invalid dates
  describe('Invalid date handling', () => {
    it('should handle invalid created_at date', async () => {
      const api = await import('@/lib/api');
      vi.mocked(api.default.get).mockResolvedValueOnce({ data: [
        { id: 99, name: 'Bad Date Org', description: null, member_count: 0, vault_count: 0, created_at: 'invalid' },
      ] });

      await act(async () => { render(<OrgsPage />); });

      await waitFor(() => {
        expect(screen.getByText('Bad Date Org')).toBeInTheDocument();
      });
    });
  });

  // 11. Negative IDs
  describe('Negative IDs', () => {
    it('should handle org with negative ID', async () => {
      const api = await import('@/lib/api');
      vi.mocked(api.default.get).mockResolvedValueOnce({ data: [
        { id: -1, name: 'Negative Org', description: null, member_count: 0, vault_count: 0, created_at: '2024-01-01' },
      ] });

      await act(async () => { render(<OrgsPage />); });

      await waitFor(() => {
        expect(screen.getByText('Negative Org')).toBeInTheDocument();
      });
    });
  });
});
