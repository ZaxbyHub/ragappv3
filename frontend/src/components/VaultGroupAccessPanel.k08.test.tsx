// frontend/src/components/VaultGroupAccessPanel.k08.test.tsx — issue #771
// AC5 acceptance check (FROZEN), authored from the AC letter only.
//
// AC5 — #771: WHEN VaultGroupAccessPanel renders its group grants THE
// SYSTEM SHALL also render each grant in a card subtree that is hidden from
// sm or md upward, alongside the table.
//
// RED at base (the panel renders only the scrolling Table — zero elements
// carry an sm:hidden/md:hidden class) and GREEN after the fix adds the
// small-screen card fallback. The panel is unrouted until [Workstream P]
// PR 4 wires it into /vaults, so this jsdom check stands in for a
// Playwright route test; it uses the same assumption and metric as AC4,
// over one mocked group grant named "Group Alpha".

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import { VaultGroupAccessPanel } from '@/components/VaultGroupAccessPanel';

// Mock API — mirrors VaultGroupAccessPanel.test.tsx's hoisted pattern.
const mockApiClient = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}));

vi.mock('@/lib/api', () => ({
  default: mockApiClient,
  apiClient: mockApiClient,
}));

// Mock sonner
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

// Mock UI components — same set as VaultGroupAccessPanel.test.tsx, with one
// disclosed deviation: Card/CardContent forward `className` to the rendered
// div. The legacy mock strips it, which would make the (sm|md):hidden class
// of a Card-based fallback unobservable and the AC5 metric permanently
// unsatisfiable; forwarding keeps the metric measurable for any wrapper
// element the fix chooses.
vi.mock('@/components/ui/card', () => ({
  Card: ({ children, className }: { children: React.ReactNode; className?: string }) => (
    <div data-testid="card" className={className}>{children}</div>
  ),
  CardContent: ({ children, className }: { children: React.ReactNode; className?: string }) => (
    <div data-testid="card-content" className={className}>{children}</div>
  ),
  CardHeader: ({ children }: { children: React.ReactNode }) => <div data-testid="card-header">{children}</div>,
  CardTitle: ({ children }: { children: React.ReactNode }) => <h3>{children}</h3>,
  CardDescription: ({ children }: { children: React.ReactNode }) => <p>{children}</p>,
}));

vi.mock('@/components/ui/button', () => ({
  Button: ({ children, onClick, disabled, ...props }: any) => (
    <button onClick={onClick} disabled={disabled} {...props}>{children}</button>
  ),
}));

vi.mock('@/components/ui/input', () => ({
  Input: (props: any) => <input {...props} />,
}));

vi.mock('@/components/ui/dialog', () => ({
  Dialog: ({ children, open, onOpenChange }: { children: React.ReactNode; open?: boolean; onOpenChange?: (open: boolean) => void }) =>
    open ? <div data-testid="dialog"><button aria-label="Simulate dialog dismissal" onClick={() => onOpenChange?.(false)} />{children}</div> : null,
  DialogContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogDescription: ({ children }: { children: React.ReactNode }) => <p>{children}</p>,
  DialogFooter: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogHeader: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogTitle: ({ children }: { children: React.ReactNode }) => <h2>{children}</h2>,
}));

describe('VaultGroupAccessPanel k08 card fallback (issue #771 AC5)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApiClient.get.mockResolvedValue({ data: {
      group_access: [
        { group_id: 7, group_name: 'Group Alpha', org_name: 'Example Org', permission: 'read', granted_at: '2024-01-01', granted_by: 'e2e-admin' },
      ],
      total: 1,
    } });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('group grants render in a small screen card fallback', async () => {
    await act(async () => {
      render(<VaultGroupAccessPanel vaultId={1} />);
    });

    // Wait until the grant row has actually loaded (the fetch is async).
    await waitFor(() => {
      expect(screen.getAllByText('Group Alpha').length).toBeGreaterThan(0);
    });

    // AC5 metric (same as AC4's): count the OUTERMOST elements whose
    // className matches (sm|md):hidden and whose text contains the grant's
    // name. The table subtree carries no such class, so only the
    // small-screen card fallback counts.
    const matches = Array.from(document.querySelectorAll<HTMLElement>('*')).filter((el) => {
      const cls = el.getAttribute('class') || '';
      return /(sm|md):hidden/.test(cls) && (el.textContent || '').includes('Group Alpha');
    });
    const outermost = matches.filter(
      (el) => !matches.some((other) => other !== el && other.contains(el))
    );

    expect(outermost.length).toBe(1);

    // PRR-011: pin both halves of the AC5 contract (see the VaultMembersPanel
    // k08 test for the full rationale) — table presence AND a named compact
    // card container carrying the grant's content, after the metric so the
    // base leg keeps the frozen failure signature.
    expect(screen.getByRole('table')).toBeInTheDocument();
    const compact = screen.getByLabelText('Vault group access (compact)');
    expect(compact.textContent).toContain('Group Alpha');
  });
});
