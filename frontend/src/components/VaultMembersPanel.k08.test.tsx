// frontend/src/components/VaultMembersPanel.k08.test.tsx — issue #771 AC4
// acceptance check (FROZEN), authored from the AC letter only.
//
// AC4 — #771: WHEN VaultMembersPanel renders its members THE SYSTEM SHALL
// also render each member in a card subtree that is hidden from sm or md
// upward, alongside the table.
//
// RED at base (the panel renders only the scrolling Table — zero elements
// carry an sm:hidden/md:hidden class) and GREEN after the fix adds the
// small-screen card fallback. The panel is unrouted until [Workstream P]
// PR 4 wires it into /vaults, so this jsdom check stands in for a Playwright
// route test; it assumes the CSS-only switch used by
// DocumentTable/DocumentCardsList (hidden from sm, or from md if the fix
// follows the 640-767px band coverage). The check counts the OUTERMOST
// elements whose className matches (sm|md):hidden and whose text contains
// the member's username — md is included because a faithful copy of the
// Documents fix may move the switch to md.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import { VaultMembersPanel } from '@/components/VaultMembersPanel';

// Mock apiClient — mirrors VaultMembersPanel.test.tsx, scoped to the one
// member the AC names (alice).
vi.mock('@/lib/api', () => ({
  default: {
    get: vi.fn().mockResolvedValue({ data: { members: [
      { user_id: 1, username: 'alice', full_name: 'Alice Johnson', permission: 'admin', granted_at: '2024-01-01' },
    ], total: 1 } }),
    post: vi.fn().mockResolvedValue({ data: {} }),
    patch: vi.fn().mockResolvedValue({ data: {} }),
    delete: vi.fn().mockResolvedValue({ data: {} }),
  },
}));

// Mock sonner toast
vi.mock('sonner', () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

// Mock UI components — same set as VaultMembersPanel.test.tsx, with one
// disclosed deviation: Card/CardContent forward `className` to the rendered
// div. The legacy mock strips it, which would make the (sm|md):hidden class
// of a Card-based fallback unobservable and the AC4 metric permanently
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
  Button: ({ children, onClick, disabled, ...props }: { children: React.ReactNode; onClick?: () => void; disabled?: boolean }) => (
    <button onClick={onClick} disabled={disabled} {...props}>
      {children}
    </button>
  ),
}));

vi.mock('@/components/ui/input', () => ({
  Input: (props: React.InputHTMLAttributes<HTMLInputElement>) => <input {...props} />,
}));

vi.mock('@/components/ui/dialog', () => ({
  Dialog: ({ children, open, onOpenChange }: { children: React.ReactNode; open?: boolean; onOpenChange?: (open: boolean) => void }) =>
    open ? <div data-testid="dialog"><button aria-label="Simulate dialog dismissal" onClick={() => onOpenChange?.(false)} />{children}</div> : null,
  DialogContent: ({ children }: { children: React.ReactNode }) => <div data-testid="dialog-content">{children}</div>,
  DialogDescription: ({ children }: { children: React.ReactNode }) => <p>{children}</p>,
  DialogFooter: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogHeader: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogTitle: ({ children }: { children: React.ReactNode }) => <h2>{children}</h2>,
}));

describe('VaultMembersPanel k08 card fallback (issue #771 AC4)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('members render in a small screen card fallback', async () => {
    await act(async () => {
      render(<VaultMembersPanel vaultId={1} />);
    });

    // Wait until the member row has actually loaded (the fetch is async).
    await waitFor(() => {
      expect(screen.getAllByText('@alice').length).toBeGreaterThan(0);
    });

    // AC4 metric: count the OUTERMOST elements whose className matches
    // (sm|md):hidden and whose text contains "alice". The table subtree
    // carries no such class, so only the small-screen card fallback counts;
    // "outermost" keeps a wrapper-plus-card nesting from double counting.
    const matches = Array.from(document.querySelectorAll<HTMLElement>('*')).filter((el) => {
      const cls = el.getAttribute('class') || '';
      return /(sm|md):hidden/.test(cls) && (el.textContent || '').includes('alice');
    });
    const outermost = matches.filter(
      (el) => !matches.some((other) => other !== el && other.contains(el))
    );

    expect(outermost.length).toBe(1);

    // PRR-011: the metric above is also satisfied by wrapping the TABLE in a
    // sm:hidden div with no cards at all, so pin both halves of the AC4
    // contract: the desktop table still exists, AND a named compact card
    // container exists carrying the member's content. (Placed after the
    // metric so the base leg — no compact container — still fails on the
    // metric with the frozen signature.)
    expect(screen.getByRole('table')).toBeInTheDocument();
    const compact = screen.getByLabelText('Vault members (compact)');
    expect(compact.textContent).toContain('alice');
  });
});
