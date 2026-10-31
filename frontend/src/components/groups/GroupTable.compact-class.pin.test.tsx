// frontend/src/components/groups/GroupTable.compact-class.pin.test.tsx
// Issue #771 (UI-ENH-05): the below-sm card list and the desktop table form
// a CSS-only switch. The frozen C24 check proves the PHONE side (controls
// visible at 390px); this pin proves the CLASS side — the compact list must
// stay hidden from sm upward — because removing sm:hidden renders the card
// list on desktop where no frozen check looks (the C24 class-removal probe
// survived exactly this removal; final-critic R1).
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import '@testing-library/jest-dom';
import { GroupTable } from '@/components/groups/GroupTable';

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    listGroups: vi.fn().mockResolvedValue({
      groups: [
        { id: 1, name: 'Alpha Team', description: 'fixture', created_at: '2024-01-01', org_id: null, organization_name: null },
      ],
      total: 1, page: 1, per_page: 20,
    }),
  };
});
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));
vi.mock('@/components/ui/card', () => ({
  Card: ({ children, className }: { children: React.ReactNode; className?: string }) => <div data-testid="card" className={className}>{children}</div>,
  CardContent: ({ children, className }: { children: React.ReactNode; className?: string }) => <div data-testid="card-content" className={className}>{children}</div>,
}));

describe('GroupTable compact/desktop CSS switch (issue #771)', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('the compact card list is hidden from sm upward', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    await act(async () => {
      render(
        <QueryClientProvider client={queryClient}>
          <GroupTable onEdit={vi.fn()} onDelete={vi.fn()} onManageMembers={vi.fn()} onManageVaults={vi.fn()} />
        </QueryClientProvider>
      );
    });
    await act(async () => { await new Promise((r) => setTimeout(r, 50)); });
    const compact = screen.getByLabelText('Groups (compact)');
    expect(compact.className).toMatch(/(^|\s)sm:hidden(\s|$)/);
    expect(screen.getByRole('table')).toBeInTheDocument();
  });
});
