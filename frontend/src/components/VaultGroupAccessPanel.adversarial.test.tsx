// ADVERSARIAL TESTS for VaultGroupAccessPanel — boundary, edge cases, XSS
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import { VaultGroupAccessPanel } from '@/components/VaultGroupAccessPanel';

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

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

vi.mock('@/components/ui/card', () => ({
  Card: ({ children }: any) => <div data-testid="card">{children}</div>,
  CardContent: ({ children }: any) => <div data-testid="card-content">{children}</div>,
  CardHeader: ({ children }: any) => <div data-testid="card-header">{children}</div>,
  CardTitle: ({ children }: any) => <h3>{children}</h3>,
  CardDescription: ({ children }: any) => <p>{children}</p>,
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
  Dialog: ({ children, open }: { children: React.ReactNode; open: boolean }) =>
    open ? <div data-testid="dialog">{children}</div> : null,
  DialogContent: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogDescription: ({ children }: { children: React.ReactNode }) => <p>{children}</p>,
  DialogFooter: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogHeader: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  DialogTitle: ({ children }: { children: React.ReactNode }) => <h2>{children}</h2>,
}));

describe('VaultGroupAccessPanel ADVERSARIAL', () => {
  beforeEach(() => { vi.clearAllMocks(); });
  afterEach(() => { vi.restoreAllMocks(); });

  // Helper: resolve the get with empty data
  const mockEmptyResponse = async () => {
    const { apiClient } = await import('@/lib/api');
    (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ data: { group_access: [], total: 0 } });
  };

  // 1. Vault ID boundary values
  describe('Vault ID boundary', () => {
    it('should render with vaultId=0', async () => {
      await mockEmptyResponse();
      await act(async () => { render(<VaultGroupAccessPanel vaultId={0} />); });
      await new Promise(r => setTimeout(r, 0)); // allow useEffect to run
      expect(screen.getByText('Group Access')).toBeInTheDocument();
    });

    it('should render with negative vaultId', async () => {
      await mockEmptyResponse();
      await act(async () => { render(<VaultGroupAccessPanel vaultId={-1} />); });
      await new Promise(r => setTimeout(r, 0));
      expect(screen.getByText('Group Access')).toBeInTheDocument();
    });

    it('should render with MAX_SAFE_INTEGER vaultId', async () => {
      await mockEmptyResponse();
      await act(async () => { render(<VaultGroupAccessPanel vaultId={Number.MAX_SAFE_INTEGER} />); });
      await new Promise(r => setTimeout(r, 0));
      expect(screen.getByText('Group Access')).toBeInTheDocument();
    });

    it('should render with NaN vaultId', async () => {
      await mockEmptyResponse();
      await act(async () => { render(<VaultGroupAccessPanel vaultId={NaN} />); });
      await new Promise(r => setTimeout(r, 0));
      expect(screen.getByText('Group Access')).toBeInTheDocument();
    });
  });

  // 2. Accessibility
  describe('Accessibility', () => {
    it('should have role=status for screen readers (loading)', async () => {
      const { apiClient } = await import('@/lib/api');
      (apiClient.get as ReturnType<typeof vi.fn>).mockImplementationOnce(
        () => new Promise(() => {}) // Never resolves
      );
      await act(async () => { render(<VaultGroupAccessPanel vaultId={1} />); });
      expect(document.querySelector('[role="status"]')).toBeInTheDocument();
    });

    it('should have aria-live=polite', async () => {
      const { apiClient } = await import('@/lib/api');
      (apiClient.get as ReturnType<typeof vi.fn>).mockImplementationOnce(
        () => new Promise(() => {})
      );
      await act(async () => { render(<VaultGroupAccessPanel vaultId={1} />); });
      const statusEl = document.querySelector('[role="status"]');
      expect(statusEl).toHaveAttribute('aria-live', 'polite');
    });
  });

  // 3. Content integrity
  describe('Content integrity', () => {
    it('should not contain any script elements', async () => {
      await mockEmptyResponse();
      await act(async () => { render(<VaultGroupAccessPanel vaultId={1} />); });
      expect(document.querySelectorAll('script')).toHaveLength(0);
    });

    it('should render Group Access title regardless of vaultId', async () => {
      const { apiClient } = await import('@/lib/api');
      (apiClient.get as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ data: { group_access: [], total: 0 } });
      const { rerender } = render(<VaultGroupAccessPanel vaultId={1} />);

      await act(async () => {
        rerender(<VaultGroupAccessPanel vaultId={99999} />);
      });

      expect(screen.getByText('Group Access')).toBeInTheDocument();
    });
  });
});
