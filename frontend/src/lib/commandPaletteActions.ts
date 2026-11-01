import { useCallback, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { useLocation } from "react-router-dom";
import { useAuthOwner } from "@/hooks/useAuthOwner";
import { captureAuthPrincipalGeneration, subscribeAuthPrincipal } from "@/lib/api/auth-lifecycle";
import { useAuthStore } from "@/stores/useAuthStore";
import { useVaultStore } from "@/stores/useVaultStore";

export const commandPaletteActionIds = [
  "new-chat", "switch-vault", "open-keyboard-shortcuts", "cycle-theme", "attach-file",
] as const;
export type CommandPaletteActionId = (typeof commandPaletteActionIds)[number];
export interface CommandPaletteActionGuard { isCurrent(): boolean; }
export interface CommandPaletteActionRegistration {
  id: CommandPaletteActionId;
  label: string;
  enabled: boolean;
  execute(guard: CommandPaletteActionGuard): void | Promise<void>;
}
export interface CommandPaletteActionSnapshot {
  id: CommandPaletteActionId;
  label: string;
  readonly registration: symbol;
}
interface RegisteredAction extends CommandPaletteActionSnapshot {
  isCurrent(): boolean;
  execute(guard: CommandPaletteActionGuard): void | Promise<void>;
  running: boolean;
}

const registrations = new Map<CommandPaletteActionId, RegisteredAction[]>();
const listeners = new Set<() => void>();
let snapshot: readonly CommandPaletteActionSnapshot[] = [];

function currentReceiver(id: CommandPaletteActionId): RegisteredAction | undefined {
  const entries = registrations.get(id);
  if (!entries) return;
  for (let i = entries.length - 1; i >= 0; i -= 1) {
    const entry = entries[i];
    if (entry?.isCurrent()) return entry;
  }
}
function publish(): void {
  const next = commandPaletteActionIds.flatMap((id) => {
    const entry = currentReceiver(id);
    return entry ? [{ id, label: entry.label, registration: entry.registration }] : [];
  });
  if (next.length === snapshot.length && next.every((entry, i) =>
    entry.registration === snapshot[i]?.registration && entry.label === snapshot[i]?.label)) return;
  snapshot = next;
  for (const listener of [...listeners]) listener();
}
function register(entry: RegisteredAction): () => void {
  const entries = registrations.get(entry.id) ?? [];
  entries.push(entry);
  registrations.set(entry.id, entries);
  publish();
  return () => {
    const current = registrations.get(entry.id);
    if (!current) return;
    const remaining = current.filter((candidate) => candidate !== entry);
    if (remaining.length === current.length) return;
    if (remaining.length) registrations.set(entry.id, remaining);
    else registrations.delete(entry.id);
    publish();
  };
}

/** The returned guard owns later menu/picker callbacks independently of the palette. */
export function useCommandPaletteAction(
  registration: CommandPaletteActionRegistration | null,
): CommandPaletteActionGuard {
  const owner = useAuthOwner();
  const principalGeneration = useSyncExternalStore(
    subscribeAuthPrincipal, captureAuthPrincipalGeneration, captureAuthPrincipalGeneration,
  );
  const user = useAuthStore((state) => state.user);
  const principalId = user?.id ?? null;
  const role = user?.role ?? null;
  const vaultId = useVaultStore((state) => state.activeVaultId);
  const { pathname } = useLocation();
  const vaultRevisionRef = useRef(0);
  const [vaultRevision, setVaultRevision] = useState(0);
  useLayoutEffect(() => {
    let currentVaultId = useVaultStore.getState().activeVaultId;
    return useVaultStore.subscribe((state) => {
      if (state.activeVaultId === currentVaultId) return;
      currentVaultId = state.activeVaultId;
      vaultRevisionRef.current += 1;
      setVaultRevision(vaultRevisionRef.current);
    });
  }, []);
  const context = useMemo(() => ({ owner, principalId, role, principalGeneration, vaultId, vaultRevision, pathname }),
    [owner, principalId, role, principalGeneration, vaultId, vaultRevision, pathname]);
  const latest = useRef({ context, registration });
  latest.current = { context, registration };
  const mounted = useRef(false);
  const isCurrent = useCallback(() => mounted.current &&
    latest.current.context === context && !context.owner.signal.aborted &&
    captureAuthPrincipalGeneration() === context.principalGeneration &&
    vaultRevisionRef.current === context.vaultRevision &&
    useVaultStore.getState().activeVaultId === context.vaultId, [context]);
  const id = registration?.id;
  const label = registration?.label;
  const enabled = registration?.enabled ?? false;

  useLayoutEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  useLayoutEffect(() => {
    if (!id || !label || !enabled) return;
    let active = true;
    const entry: RegisteredAction = {
      id, label, registration: Symbol(id), running: false,
      isCurrent: () => {
        const current = latest.current.registration;
        return active && isCurrent() && current?.id === id && current.enabled;
      },
      execute: (guard) => {
        if (!entry.isCurrent() || !guard.isCurrent()) return;
        return latest.current.registration?.execute(guard);
      },
    };
    const dispose = register(entry);
    return () => { active = false; dispose(); };
  }, [id, label, enabled, isCurrent]);
  return useMemo(() => ({ isCurrent }), [isCurrent]);
}

export function subscribeCommandPaletteActions(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
export function getCommandPaletteActionSnapshot(): readonly CommandPaletteActionSnapshot[] {
  return snapshot;
}
export function invokeCommandPaletteAction(
  offered: CommandPaletteActionSnapshot,
  outerGuard: CommandPaletteActionGuard,
): boolean {
  const entry = currentReceiver(offered.id);
  if (!entry || entry.registration !== offered.registration || entry.running ||
      !outerGuard.isCurrent()) return false;
  const guard = { isCurrent: () => outerGuard.isCurrent() && entry.isCurrent() &&
    currentReceiver(entry.id) === entry };
  entry.running = true;
  const finish = () => { entry.running = false; };
  try {
    // Consumers own their error UI. Observe both outcomes without a dropped rejecting finally.
    void Promise.resolve(entry.execute(guard)).then(finish, finish);
    return true;
  } catch (error) {
    finish();
    throw error;
  }
}
