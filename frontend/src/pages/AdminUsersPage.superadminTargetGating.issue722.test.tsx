// Issue #722 (R4/R11): the AdminUsersPage UI mirrors the backend
// actor/target policy — a plain admin is offered no modifying actions on a
// superadmin row (Edit / Reset password / Groups / Orgs hidden, active toggle
// disabled), and the row-level role Select is inert (only the target's
// current role is offered).
import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import AdminUsersPage from '@/pages/AdminUsersPage';
import { roleOptionsFor } from '@/pages/AdminUsersPage/roleOptions';

vi.mock('@/stores/useAuthStore', () => ({
  useAuthStore: vi.fn((selector: any) => {
    const state = {
      user: { id: 2, username: 'admin1', full_name: 'Admin One', role: 'admin', is_active: true },
      isAuthenticated: true,
      isLoading: false,
    };
    return typeof selector === 'function' ? selector(state) : state;
  }),
}));

vi.mock('@/lib/api', () => ({
  default: {
    get: vi.fn().mockResolvedValue({ data: { users: [
      { id: 1, username: 'root', full_name: 'Root', role: 'superadmin', is_active: true, created_at: '2024-01-01' },
      { id: 3, username: 'bob', full_name: 'Bob', role: 'member', is_active: true, created_at: '2024-01-02' },
    ], total: 2 } }),
    patch: vi.fn().mockResolvedValue({ data: {} }),
    delete: vi.fn().mockResolvedValue({ data: {} }),
  },
}));

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

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
  const collectItems = (children: React.ReactNode): { value: string }[] => {
    const items: { value: string }[] = [];
    React.Children.forEach(children, (child: any) => {
      if (!child || typeof child !== 'object') return;
      if (child.type?.__isSelectItem) items.push({ value: child.props.value });
      else if (child.props?.children) items.push(...collectItems(child.props.children));
    });
    return items;
  };
  const SelectRoot: any = ({ children }: any) => <div>{children}</div>;
  const SelectTrigger: any = ({ children }: any) => <>{children}</>;
  SelectTrigger.__isSelectTrigger = true;
  const SelectValue: any = () => null;
  const SelectContent: any = ({ children }: any) => {
    const items = collectItems(children);
    return <select data-testid="role-options" multiple>{items.map((i) => <option key={i.value} value={i.value}>{i.value}</option>)}</select>;
  };
  const SelectItem: any = ({ value }: any) => <option value={value} />;
  SelectItem.__isSelectItem = true;
  return { Select: SelectRoot, SelectTrigger, SelectValue, SelectContent, SelectItem };
});

vi.mock('@/components/ui/table', () => ({
  Table: ({ children }: any) => <table>{children}</table>,
  TableHeader: ({ children }: any) => <thead>{children}</thead>,
  TableBody: ({ children }: any) => <tbody>{children}</tbody>,
  TableRow: ({ children }: any) => <tr>{children}</tr>,
  TableHead: ({ children }: any) => <th>{children}</th>,
  TableCell: ({ children }: any) => <td>{children}</td>,
  TableCaption: ({ children }: any) => <caption>{children}</caption>,
}));

vi.mock('@/components/PageTitleHeader', () => ({
  PageTitleHeader: () => <div />,
}));

describe('AdminUsersPage superadmin-target gating (issue #722)', () => {
  it('offers a plain admin no modifying actions on a superadmin row', async () => {
    render(<AdminUsersPage />);
    await waitFor(() => expect(screen.getByText('root')).toBeInTheDocument());

    expect(screen.queryByLabelText('Edit user root')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Reset password for root')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Manage groups for root')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Manage organizations for root')).not.toBeInTheDocument();

    // The same admin still gets the actions on an ordinary member row.
    expect(screen.getByLabelText('Edit user bob')).toBeInTheDocument();
    expect(screen.getByLabelText('Reset password for bob')).toBeInTheDocument();

    // The active toggle on the superadmin row is disabled for the admin.
    const toggle = screen.getByRole('switch', { name: /deactivate user root/i });
    expect(toggle).toBeDisabled();

    // The rendered role Select options (not just the helper): rows render in
    // list order — root (superadmin target) first — and the admin is offered
    // only the target's current role there (issue #722 review F-014: pins the
    // wiring through roleOptionsFor into the rendered options).
    const renderedRoleSelects = screen.getAllByTestId('role-options');
    const rootRowOptions = Array.from(
      renderedRoleSelects[0].querySelectorAll('option'),
    ).map((o) => o.value);
    expect(rootRowOptions).toEqual(['superadmin']);
  });

  it('roleOptionsFor keeps the role Select inert for an admin on a superadmin target', () => {
    const options = roleOptionsFor('admin', { mode: 'edit', targetRole: 'superadmin' });
    expect(options.map((o) => o.value)).toEqual(['superadmin']);
  });
});
