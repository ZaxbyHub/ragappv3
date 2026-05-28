// ADVERSARIAL TESTS for VaultMembersPanel — XSS, injection, edge cases, error handling
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import { VaultMembersPanel } from '@/components/VaultMembersPanel';
import { toast } from 'sonner';

// --- Mocks ---
vi.mock('@/lib/api', () => ({
  default: {
    get: vi.fn().mockResolvedValue({ data: { members: [
      { user_id: 1, username: 'alice', full_name: 'Alice Johnson', permission: 'admin', granted_at: '2024-01-01' },
      { user_id: 2, username: 'bob', full_name: 'Bob Smith', permission: 'read', granted_at: '2024-01-02' },
    ], total: 2 } }),
    post: vi.fn().mockResolvedValue({ data: {} }),
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

// Radix Select cannot be driven in jsdom; render the add-form permission
// picker as a native <select> so option labels and value wiring stay testable.
vi.mock('@/components/ui/select', async () => {
  const React = await import('react');
  const collectItems = (children: React.ReactNode): { value: string; label: React.ReactNode }[] => {
    const items: { value: string; label: React.ReactNode }[] = [];
    React.Children.forEach(children, (child: any) => {
      if (!child || typeof child !== 'object') return;
      if (child.props?.value !== undefined && child.props?.children !== undefined && !child.props?.children?.type) {
        items.push({ value: child.props.value, label: child.props.children });
      } else if (child.props?.children) {
        items.push(...collectItems(child.props.children));
      }
    });
    return items;
  };
  return {
    Select: ({ value, onValueChange, disabled, children }: any) =>
      React.createElement(
        'select',
        {
          value,
          disabled,
          'aria-label': 'Permission level for new member',
          onChange: (e: any) => onValueChange?.(e.target.value),
        },
        collectItems(children).map((item) =>
          React.createElement('option', { key: item.value, value: item.value }, item.label)
        )
      ),
    SelectTrigger: ({ children }: any) => React.createElement(React.Fragment, null, children),
    SelectValue: () => null,
    SelectContent: ({ children }: any) => React.createElement(React.Fragment, null, children),
    SelectItem: ({ children }: any) => React.createElement(React.Fragment, null, children),
  };
});

vi.mock('@/components/ui/dialog', () => ({
  Dialog: ({ children, open }: any) => open ? <div data-testid="dialog">{children}</div> : null,
  DialogContent: ({ children }: any) => <div data-testid="dialog-content">{children}</div>,
  DialogDescription: ({ children }: any) => <p>{children}</p>,
  DialogFooter: ({ children }: any) => <div>{children}</div>,
  DialogHeader: ({ children }: any) => <div>{children}</div>,
  DialogTitle: ({ children }: any) => <h2>{children}</h2>,
}));

describe('VaultMembersPanel ADVERSARIAL', () => {
  beforeEach(() => { vi.clearAllMocks(); });
  afterEach(() => { vi.restoreAllMocks(); });

  // 1. XSS in member data
  describe('XSS in member data', () => {
    const xssPayloads = [
      '<script>alert("xss")</script>',
      '<img onerror="alert(1)" src=x>',
      '<svg onload="alert(1)">',
    ];

    it.each(xssPayloads)('should NOT execute XSS in full_name: %s', async (payload) => {
      const api = await import('@/lib/api');
      vi.mocked(api.default.get).mockResolvedValueOnce({ data: { members: [
        { user_id: 99, username: 'safe', full_name: payload, permission: 'read', granted_at: '2024-01-01' },
      ], total: 1 } });

      await act(async () => { render(<VaultMembersPanel vaultId={1} />); });

      await waitFor(() => {
        expect(screen.getByText((content) => content.includes(payload) || content.includes('script'))).toBeInTheDocument();
      });
      expect(document.querySelectorAll('script')).toHaveLength(0);
    });

    it.each(xssPayloads)('should NOT execute XSS in username: %s', async (payload) => {
      const api = await import('@/lib/api');
      vi.mocked(api.default.get).mockResolvedValueOnce({ data: { members: [
        { user_id: 99, username: payload, full_name: 'Safe Name', permission: 'read', granted_at: '2024-01-01' },
      ], total: 1 } });

      await act(async () => { render(<VaultMembersPanel vaultId={1} />); });

      await waitFor(() => {
        expect(screen.getByText('Safe Name')).toBeInTheDocument();
      });
      expect(document.querySelectorAll('script')).toHaveLength(0);
    });
  });

  // 3. API error handling
  describe('API error handling', () => {
    it('should show error toast on fetch failure', async () => {
      const api = await import('@/lib/api');
      vi.mocked(api.default.get).mockRejectedValueOnce(new Error('500'));

      await act(async () => { render(<VaultMembersPanel vaultId={1} />); });

      await waitFor(() => {
        expect(toast.error).toHaveBeenCalledWith('Failed to load vault members');
      });
    });

    it('should show error toast on add member failure', async () => {
      const api = await import('@/lib/api');

      await act(async () => { render(<VaultMembersPanel vaultId={1} />); });

      await waitFor(() => {
        expect(screen.getByText('Alice Johnson')).toBeInTheDocument();
      });

      // Enter a user ID into the plain add-member input
      const input = screen.getByPlaceholderText('Enter user ID...');
      await act(async () => {
        fireEvent.change(input, { target: { value: '99' } });
      });

      // Mock the POST to fail
      vi.mocked(api.default.post).mockRejectedValueOnce(new Error('403'));

      const addButton = screen.getByRole('button', { name: /add/i });
      await act(async () => { fireEvent.click(addButton); });

      await waitFor(() => {
        expect(toast.error).toHaveBeenCalledWith('Failed to add member');
      });
    });

    it('should show error toast on permission change failure', async () => {
      const api = await import('@/lib/api');
      vi.mocked(api.default.patch).mockRejectedValueOnce(new Error('403'));

      await act(async () => { render(<VaultMembersPanel vaultId={1} />); });

      await waitFor(() => {
        expect(screen.getByText('Alice Johnson')).toBeInTheDocument();
      });

      const permSelects = document.querySelectorAll('select[aria-label*="alice"]');
      await act(async () => {
        fireEvent.change(permSelects[0], { target: { value: 'write' } });
      });

      await waitFor(() => {
        expect(toast.error).toHaveBeenCalledWith('Failed to update permission');
      });
    });

    it('should show error toast on remove failure', async () => {
      const api = await import('@/lib/api');
      vi.mocked(api.default.delete).mockRejectedValueOnce(new Error('404'));

      await act(async () => { render(<VaultMembersPanel vaultId={1} />); });

      await waitFor(() => {
        expect(screen.getByText('Alice Johnson')).toBeInTheDocument();
      });

      const removeBtn = document.querySelector('button[aria-label="Remove alice from vault"]') as HTMLElement;
      expect(removeBtn).not.toBeNull();
      await act(async () => { fireEvent.click(removeBtn); });

      await waitFor(() => {
        expect(screen.getByTestId('dialog')).toBeInTheDocument();
      });

      await act(async () => {
        fireEvent.click(screen.getByText('Remove'));
      });

      await waitFor(() => {
        expect(toast.error).toHaveBeenCalledWith('Failed to remove member');
      });
    });
  });

  // 5. Empty/null boundary
  describe('Empty/null boundary', () => {
    it('should handle empty members list', async () => {
      const api = await import('@/lib/api');
      vi.mocked(api.default.get).mockResolvedValueOnce({ data: { members: [], total: 0 } });

      await act(async () => { render(<VaultMembersPanel vaultId={1} />); });

      await waitFor(() => {
        expect(screen.getByText(/no members yet/i)).toBeInTheDocument();
      });
    });

    it('should handle null full_name without crash', async () => {
      const api = await import('@/lib/api');
      vi.mocked(api.default.get).mockResolvedValueOnce({ data: { members: [
        { user_id: 99, username: 'nullname', full_name: null, permission: 'read', granted_at: '2024-01-01' },
      ], total: 1 } });

      await act(async () => { render(<VaultMembersPanel vaultId={1} />); });

      await waitFor(() => {
        expect(screen.getByText('@nullname')).toBeInTheDocument();
      });
    });

    it('should handle empty string user ID on form submit', async () => {
      await act(async () => { render(<VaultMembersPanel vaultId={1} />); });

      await waitFor(() => {
        expect(screen.getByText('Alice Johnson')).toBeInTheDocument();
      });

      const addButton = screen.getByRole('button', { name: /add/i });
      // Button should be disabled when input is empty
      expect(addButton).toBeDisabled();
    });
  });

  // 6. Very long strings
  describe('Very long strings', () => {
    it('should handle 1000+ char full_name without crash', async () => {
      const api = await import('@/lib/api');
      vi.mocked(api.default.get).mockResolvedValueOnce({ data: { members: [
        { user_id: 99, username: 'longuser', full_name: 'X'.repeat(2000), permission: 'read', granted_at: '2024-01-01' },
      ], total: 1 } });

      await act(async () => { render(<VaultMembersPanel vaultId={1} />); });

      await waitFor(() => {
        expect(screen.getByText('@longuser')).toBeInTheDocument();
      });
    });
  });

  // 7. Rapid permission changes (concurrent)
  describe('Concurrent permission changes', () => {
    it('should handle rapid permission changes without corruption', async () => {
      const api = await import('@/lib/api');
      vi.mocked(api.default.patch).mockResolvedValue({ data: {} });

      await act(async () => { render(<VaultMembersPanel vaultId={1} />); });

      await waitFor(() => {
        expect(screen.getByText('Alice Johnson')).toBeInTheDocument();
      });

      const permSelect = document.querySelector('select[aria-label*="alice"]') as HTMLElement;
      // Rapidly change permissions
      for (let i = 0; i < 5; i++) {
        await act(async () => {
          fireEvent.change(permSelect, { target: { value: i % 2 === 0 ? 'write' : 'admin' } });
        });
      }

      // Should not crash
      expect(screen.getByText('Alice Johnson')).toBeInTheDocument();
    });
  });

  // 8. Unicode characters
  describe('Unicode handling', () => {
    it('should handle Unicode in member names', async () => {
      const api = await import('@/lib/api');
      vi.mocked(api.default.get).mockResolvedValueOnce({ data: { members: [
        { user_id: 10, username: '用户', full_name: '张三', permission: 'read', granted_at: '2024-01-01' },
        { user_id: 11, username: '😀user', full_name: '🎉 Name', permission: 'write', granted_at: '2024-01-01' },
      ], total: 2 } });

      await act(async () => { render(<VaultMembersPanel vaultId={1} />); });

      await waitFor(() => {
        expect(screen.getByText('张三')).toBeInTheDocument();
        expect(screen.getByText('🎉 Name')).toBeInTheDocument();
      });
    });
  });

  // 9. Invalid dates
  describe('Invalid date handling', () => {
    it('should handle invalid granted_at date', async () => {
      const api = await import('@/lib/api');
      vi.mocked(api.default.get).mockResolvedValueOnce({ data: { members: [
        { user_id: 99, username: 'baddate', full_name: 'Bad Date', permission: 'read', granted_at: 'not-a-date' },
      ], total: 1 } });

      await act(async () => { render(<VaultMembersPanel vaultId={1} />); });

      await waitFor(() => {
        expect(screen.getByText('Bad Date')).toBeInTheDocument();
      });
    });
  });

  // 10. Different vault IDs
  describe('Vault ID boundary', () => {
    it('should use correct vault ID in all API calls', async () => {
      const api = await import('@/lib/api');

      await act(async () => { render(<VaultMembersPanel vaultId={999} />); });

      await waitFor(() => {
        expect(api.default.get).toHaveBeenCalledWith('/vaults/999/members');
      });
    });

    it('should handle vaultId=0', async () => {
      const api = await import('@/lib/api');

      await act(async () => { render(<VaultMembersPanel vaultId={0} />); });

      await waitFor(() => {
        expect(api.default.get).toHaveBeenCalledWith('/vaults/0/members');
      });
    });

    it('should handle negative vaultId', async () => {
      const api = await import('@/lib/api');

      await act(async () => { render(<VaultMembersPanel vaultId={-1} />); });

      await waitFor(() => {
        expect(api.default.get).toHaveBeenCalledWith('/vaults/-1/members');
      });
    });
  });
});
