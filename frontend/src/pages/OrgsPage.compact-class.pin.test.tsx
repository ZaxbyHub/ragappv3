// frontend/src/pages/OrgsPage.compact-class.pin.test.tsx
// Issue #771 (UI-ENH-05): the below-sm member card list and the desktop
// table form a CSS-only switch. The frozen C25 check proves the PHONE side;
// this pin proves the CLASS side (compact list hidden from sm upward),
// closing the C25 class-removal probe survivor (final-critic R1).
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, act, fireEvent, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import OrgsPage from '@/pages/OrgsPage';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    default: {
      ...actual.default,
      get: vi.fn().mockResolvedValue({
        data: [
          { id: 1, name: 'Acme Corp', description: 'fixture', member_count: 1, vault_count: 0, created_at: '2024-01-01',
            members: [{ user_id: 2, username: 'bob', full_name: 'Bob Smith', role: 'admin', joined_at: '2024-01-01' }] },
        ],
      }),
      post: vi.fn().mockResolvedValue({ data: {} }),
      patch: vi.fn().mockResolvedValue({ data: {} }),
      delete: vi.fn().mockResolvedValue({ data: {} }),
    },
  };
});
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock('@/components/auth/RoleGuard', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/components/auth/RoleGuard')>();
  return {
    ...actual,
    RoleGuard: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
    AdminGuard: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  };
});
vi.mock('@/stores/useAuthStore', () => ({
  useAuthStore: vi.fn((selector: (s: unknown) => unknown) => {
    if (typeof selector === 'function') {
      return selector({ user: { id: 1, username: 'admin', full_name: 'Admin User', role: 'superadmin', is_active: true } });
    }
    return { user: { id: 1, username: 'admin', role: 'superadmin' } };
  }),
}));

describe('OrgsPage member compact/desktop CSS switch (issue #771)', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('the compact member card list is hidden from sm upward once expanded', async () => {
    await act(async () => { render(<OrgsPage />); });
    await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
    const expand = screen.getAllByRole('button', { name: /expand/i })[0];
    await act(async () => { fireEvent.click(expand); });
    await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
    const compact = screen.getByLabelText('Organization members (compact)');
    expect(compact.className).toMatch(/(^|\s)sm:hidden(\s|$)/);
    expect(screen.getByRole('table')).toBeInTheDocument();

    // PRR-008: pin the pair's OTHER half — the desktop table wrapper must
    // keep hidden+sm:block, mirroring the AdminUsersPage pin's walk-up.
    let wrapper: HTMLElement | null = screen.getByRole('table');
    while (wrapper && !(/(^|\s)hidden(\s|$)/.test(wrapper.className) && /(^|\s)sm:block(\s|$)/.test(wrapper.className))) {
      wrapper = wrapper.parentElement;
    }
    expect(wrapper).not.toBeNull();
  });
});
