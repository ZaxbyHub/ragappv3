// frontend/src/pages/AdminUsersPage.compact-class.pin.test.tsx
// Issue #771 (UI-ENH-05): the below-sm card list and the desktop table form
// a CSS-only switch. The frozen C23 check proves the PHONE side (controls
// visible at 390px); this pin proves the CLASS side — the compact list must
// stay hidden from sm upward and the table hidden below sm — because a
// removed sm:hidden renders the card list on desktop where no frozen check
// looks (the C23 mutation probe survived exactly this removal).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import AdminUsersPage from '@/pages/AdminUsersPage';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    default: {
      ...actual.default,
      get: vi.fn().mockResolvedValue({
        data: {
          users: [
            { id: 2, username: 'alice', full_name: 'Alice Johnson', role: 'admin', is_active: true, created_at: '2024-01-01' },
          ],
          total: 1,
        },
      }),
      post: vi.fn().mockResolvedValue({ data: {} }),
      patch: vi.fn().mockResolvedValue({ data: {} }),
      delete: vi.fn().mockResolvedValue({ data: {} }),
    },
  };
});
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

// Mock the guard (same shape as AdminUsersPage.test.tsx) — its Navigate
// requires a Router the pin does not need.
vi.mock('@/components/auth/RoleGuard', () => ({
  AdminGuard: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));

// Mock useAuthStore for the page's currentUser reads (superadmin).
vi.mock('@/stores/useAuthStore', () => ({
  useAuthStore: vi.fn((selector: (s: unknown) => unknown) => {
    if (typeof selector === 'function') {
      return selector({
        user: { id: 1, username: 'admin', full_name: 'Admin User', role: 'superadmin', is_active: true },
      });
    }
    return { user: { id: 1, username: 'admin', role: 'superadmin' } };
  }),
}));

describe('AdminUsersPage compact/desktop CSS switch (issue #771)', () => {
  beforeEach(() => { vi.clearAllMocks(); });
  afterEach(() => { vi.restoreAllMocks(); });

  it('the compact card list is hidden from sm upward and the table hidden below sm', async () => {
    await act(async () => {
      render(<AdminUsersPage />);
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });

    const compact = screen.getByLabelText('Users (compact)');
    expect(compact.className).toMatch(/(^|\s)sm:hidden(\s|$)/);

    // The desktop table wrapper hides below sm (the pair's other half). The
    // shared Table renders its own overflow wrapper inside ours, so walk up
    // to the div carrying both classes.
    const table = screen.getByRole('table');
    let wrapper: HTMLElement | null = table;
    while (wrapper && !(/(^|\s)hidden(\s|$)/.test(wrapper.className) && /(^|\s)sm:block(\s|$)/.test(wrapper.className))) {
      wrapper = wrapper.parentElement;
    }
    expect(wrapper).not.toBeNull();
  });
});
