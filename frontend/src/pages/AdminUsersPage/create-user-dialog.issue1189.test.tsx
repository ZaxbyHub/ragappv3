// Issue #1189 (Workstream O, PR 3 of 7) — admin-user creation contract.
// AC1: WHEN the add-user dialog is shown, THE SYSTEM SHALL require the
// password to be entered twice and state that it must be changed at first
// sign-in. Provenance: #1167 AC4 — the dialog had one field and no statement.
// The statement mirrors the backend contract: POST /users/ inserts
// must_change_password=1 (backend/app/api/routes/users.py), which the auth
// middleware enforces until the user changes it (backend/app/api/deps.py).
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, act, fireEvent, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import { toast } from 'sonner';
import { CreateUserDialog } from './CreateUserDialog';
import { EditUserDialog } from './EditUserDialog';
import type { User } from './types';

// --- Mocks (same shape as AdminUsersPage.error-handling.test.tsx) ---
vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

vi.mock('@/components/ui/dialog', () => ({
  Dialog: ({ children, open }: any) => (open ? <div data-testid="dialog">{children}</div> : null),
  DialogContent: ({ children }: any) => <div data-testid="dialog-content">{children}</div>,
  DialogDescription: ({ children }: any) => <p>{children}</p>,
  DialogFooter: ({ children }: any) => <div>{children}</div>,
  DialogHeader: ({ children }: any) => <div>{children}</div>,
  DialogTitle: ({ children }: any) => <h2>{children}</h2>,
}));

vi.mock('@/components/ui/button', () => ({
  Button: ({ children, onClick, disabled, ...props }: any) => (
    <button onClick={onClick} disabled={disabled} {...props}>
      {children}
    </button>
  ),
}));

vi.mock('@/components/ui/input', () => ({
  Input: (props: any) => <input {...props} />,
}));

vi.mock('@/components/ui/label', () => ({
  Label: ({ children, ...props }: any) => <label {...props}>{children}</label>,
}));

vi.mock('@/components/ui/select', async () => {
  const React = await import('react');
  const SelectTrigger: any = ({ children }: any) => React.createElement(React.Fragment, null, children);
  SelectTrigger.__isSelectTrigger = true;
  const SelectItem: any = ({ children }: any) => React.createElement(React.Fragment, null, children);
  SelectItem.__isSelectItem = true;
  return {
    Select: ({ value, onValueChange, disabled, children }: any) =>
      React.createElement(
        'select',
        { value, disabled, onChange: (e: any) => onValueChange?.(e.target.value) },
        React.Children.map(children, (c: any) => c),
      ),
    SelectTrigger,
    SelectValue: () => null,
    SelectContent: ({ children }: any) => React.createElement(React.Fragment, null, children),
    SelectItem,
  };
});

const onCreate = vi.fn().mockResolvedValue(undefined);

function renderDialog() {
  return render(
    <CreateUserDialog open onCreate={onCreate} onOpenChange={vi.fn()} isSuperAdmin={false} />,
  );
}

function fill(id: string, value: string) {
  const input = document.getElementById(id)!;
  act(() => {
    fireEvent.change(input, { target: { value } });
  });
}

async function submit() {
  const dialogEl = screen.getByTestId('dialog');
  const createBtn = dialogEl.querySelector('button[type="submit"]');
  await act(async () => {
    fireEvent.click(createBtn!);
  });
}

describe('CreateUserDialog — issue #1189 admin-user creation contract', () => {
  beforeEach(() => {
    onCreate.mockClear();
    onCreate.mockResolvedValue(undefined);
  });

  it('AC1: shows two password fields and states the password must change at first sign-in', () => {
    renderDialog();

    const password = document.getElementById('create-password') as HTMLInputElement;
    const confirm = document.getElementById('create-confirm-password') as HTMLInputElement;
    expect(password).toBeTruthy();
    expect(confirm).toBeTruthy();
    expect(password.type).toBe('password');
    expect(confirm.type).toBe('password');
    // Confirm field is a required entry, not an optional nicety.
    expect(confirm.required).toBe(true);

    expect(
      screen.getByText(/must change this password at their first sign-in/i),
    ).toBeInTheDocument();
    // The statement is reachable from both password inputs for assistive tech.
    expect(password.getAttribute('aria-describedby')).toContain('first-signin-note');
    expect(confirm.getAttribute('aria-describedby')).toBe('first-signin-note');
  });

  it('AC1: blocks creation when the two password entries differ', async () => {
    renderDialog();

    fill('create-username', 'newuser');
    fill('create-password', 'Password123');
    fill('create-confirm-password', 'Password456');
    await submit();

    expect(toast.error).toHaveBeenCalledWith('Passwords do not match');
    expect(onCreate).not.toHaveBeenCalled();
  });

  it('AC1: the confirm field is required — empty confirm blocks submission natively', async () => {
    renderDialog();

    fill('create-username', 'newuser');
    fill('create-password', 'Password123');

    const confirm = document.getElementById('create-confirm-password') as HTMLInputElement;
    expect(confirm.validity.valueMissing).toBe(true);
    const form = screen.getByTestId('dialog').querySelector('form') as HTMLFormElement;
    expect(form.checkValidity()).toBe(false);

    // Clicking the submit button runs constraint validation: the invalid
    // form never reaches handleSubmit, so the JS mismatch gate is not hit.
    await submit();
    expect(toast.error).not.toHaveBeenCalled();
    expect(onCreate).not.toHaveBeenCalled();
  });

  it('AC1: the JS gate still blocks an empty confirm when native validation is bypassed', async () => {
    renderDialog();

    fill('create-username', 'newuser');
    fill('create-password', 'Password123');

    // fireEvent.submit dispatches submit directly (no constraint validation),
    // so this pins handleSubmit's own equality gate as defense in depth.
    const form = screen.getByTestId('dialog').querySelector('form') as HTMLFormElement;
    await act(async () => {
      fireEvent.submit(form);
    });

    expect(toast.error).toHaveBeenCalledWith('Passwords do not match');
    expect(onCreate).not.toHaveBeenCalled();
  });

  it('AC1: creates the user when both entries match', async () => {
    renderDialog();

    fill('create-username', 'newuser');
    fill('create-password', 'Password123');
    fill('create-confirm-password', 'Password123');
    await submit();

    await waitFor(() => expect(onCreate).toHaveBeenCalledTimes(1));
    expect(onCreate).toHaveBeenCalledWith('newuser', '', 'Password123', 'member');
  });
});

// Issue #1189's in-scope list also names the edit dialog. Its error paths are
// covered by AdminUsersPage.error-handling.test.tsx; this pins the success
// path: prefilled from the row, username read-only, and Save passes the
// edited values through to onSave.
describe('EditUserDialog — issue #1189 edit-dialog contract', () => {
  const bob: User = {
    id: 2,
    username: 'bob',
    full_name: 'Bob Builder',
    role: 'member',
    is_active: true,
    created_at: '2026-01-01T00:00:00Z',
  };

  it('prefills from the user, keeps username read-only, and saves the edited full name', async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(<EditUserDialog open user={bob} onSave={onSave} onClose={vi.fn()} isSuperAdmin={false} />);

    const username = document.getElementById('edit-username') as HTMLInputElement;
    const fullName = document.getElementById('edit-fullname') as HTMLInputElement;
    expect(username.value).toBe('bob');
    expect(username.disabled).toBe(true);
    expect(fullName.value).toBe('Bob Builder');

    fill('edit-fullname', 'Bob Edited');
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }));
    });

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave).toHaveBeenCalledWith(bob, 'Bob Edited', 'member');
  });
});
