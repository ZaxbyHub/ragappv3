/** A session owner grants authority to publish authentication state. */
export interface AuthOwner {
  readonly id: number;
  readonly signal: AbortSignal;
}

export class StaleAuthOwnerError extends Error {
  constructor() {
    super("Authentication owner was replaced");
    this.name = "StaleAuthOwnerError";
  }
}

export class AuthTransportTimeoutError extends Error {
  constructor() {
    super("authentication transport timed out");
    this.name = "AuthTransportTimeoutError";
  }
}

export const AUTH_TRANSPORT_TIMEOUT_MS = 10_000;

interface OwnedAuthOwner extends AuthOwner { readonly controller: AbortController; }
export interface AuthTransportContext {
  readonly signal: AbortSignal;
  readonly timedOut: boolean;
  assertCurrent(): void;
}

interface TransportEntry<T> {
  readonly owner: AuthOwner;
  readonly isCurrentScope: () => boolean;
  readonly controller: AbortController;
  readonly work: (context: AuthTransportContext) => Promise<T>;
  readonly resolve: (value: T) => void;
  readonly reject: (reason: unknown) => void;
  active: boolean;
  timedOut: boolean;
  started: boolean;
  timer: ReturnType<typeof setTimeout> | null;
  unsubscribe: (() => void) | null;
}

// Principal authority can change without replacing a token/transport owner.
let principalId: number | null = null;
let principalRole: string | null = null;
let principalGeneration = 0;
const principalListeners = new Set<() => void>();

export function captureAuthPrincipalGeneration(): number { return principalGeneration; }
export function subscribeAuthPrincipal(listener: () => void): () => void {
  principalListeners.add(listener);
  return () => { principalListeners.delete(listener); };
}
export function publishAuthPrincipal(user: { id: number; role: string } | null): void {
  const nextId = user?.id ?? null;
  const nextRole = user?.role ?? null;
  if (nextId === principalId && nextRole === principalRole) return;
  principalId = nextId;
  principalRole = nextRole;
  principalGeneration += 1;
  const publishedGeneration = principalGeneration;
  for (const listener of [...principalListeners]) {
    listener();
    if (principalGeneration !== publishedGeneration) break;
  }
}

let nextOwnerId = 1;
let currentOwner = createOwner();
const replacementListeners = new Set<(previous: AuthOwner, replacement: AuthOwner) => void>();
const credentialQueue: TransportEntry<unknown>[] = [];
let activeCredentialTransport: TransportEntry<unknown> | null = null;
const transportSignalOwners = new WeakMap<AbortSignal, AuthOwner>();
const transportSignalContexts = new WeakMap<AbortSignal, AuthTransportContext>();

/** Only the active FIFO entry can provide its physical transport context. */
export function getAuthTransportContext(signal: unknown): AuthTransportContext | null {
  return signal instanceof AbortSignal ? transportSignalContexts.get(signal) ?? null : null;
}

/** Returns the owner only for a signal created by the credential FIFO. */
export function getAuthTransportOwner(signal: unknown): AuthOwner | null {
  return signal instanceof AbortSignal ? transportSignalOwners.get(signal) ?? null : null;
}

function createOwner(): OwnedAuthOwner {
  const controller = new AbortController();
  return { id: nextOwnerId++, controller, signal: controller.signal };
}

export function captureAuthOwner(): AuthOwner { return currentOwner; }
export function isCurrentAuthOwner(owner: AuthOwner | null | undefined): owner is AuthOwner {
  return owner === currentOwner && !owner.signal.aborted;
}
export function reserveReplacementAuthOwner(): AuthOwner {
  const previous = currentOwner;
  const replacement = createOwner();
  currentOwner = replacement;
  previous.controller.abort();
  for (const listener of [...replacementListeners]) {
    listener(previous, replacement);
    if (currentOwner !== replacement) break;
  }
  return replacement;
}

export function onAuthOwnerReplacement(listener: (previous: AuthOwner, replacement: AuthOwner) => void): () => void {
  replacementListeners.add(listener);
  return () => replacementListeners.delete(listener);
}

/**
 * Serializes cookie-mutating credential transports.  The returned promise is
 * logical: replacement/deadline rejects it promptly, while the physical slot
 * remains occupied until fetch/axios and its response body have really settled.
 */
export function enqueueAuthTransport<T>(
  owner: AuthOwner,
  work: (context: AuthTransportContext) => Promise<T>,
  deadlineMs: number = AUTH_TRANSPORT_TIMEOUT_MS,
  isCurrentScope: () => boolean = () => true,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const entry: TransportEntry<T> = {
      owner, isCurrentScope, controller: new AbortController(), work, resolve, reject,
      active: true, timedOut: false, started: false, timer: null, unsubscribe: null,
    };
    const cancel = (reason: unknown, timedOut = false) => {
      if (!entry.active) return;
      entry.active = false;
      entry.timedOut = timedOut;
      entry.controller.abort();
      if (entry.timer) clearTimeout(entry.timer);
      entry.timer = null;
      entry.unsubscribe?.();
      entry.unsubscribe = null;
      reject(reason);
    };
    if (!isCurrentAuthOwner(owner) || !isCurrentScope()) {
      cancel(new StaleAuthOwnerError());
      return;
    }
    // Listen directly to this lease's abort signal. Replacement-listener
    // delivery intentionally stops on reentry, so it cannot be the sole
    // cancellation path for a queued transport.
    const onOwnerAbort = () => cancel(new StaleAuthOwnerError());
    owner.signal.addEventListener("abort", onOwnerAbort, { once: true });
    entry.unsubscribe = () => owner.signal.removeEventListener("abort", onOwnerAbort);
    entry.timer = setTimeout(() => cancel(new AuthTransportTimeoutError(), true), deadlineMs);
    credentialQueue.push(entry as TransportEntry<unknown>);
    drainCredentialQueue();
  });
}

function drainCredentialQueue(): void {
  if (activeCredentialTransport) return;
  const entry = credentialQueue.shift();
  if (!entry) return;
  if (!entry.active) {
    drainCredentialQueue();
    return;
  }
  activeCredentialTransport = entry;
  entry.started = true;
  transportSignalOwners.set(entry.controller.signal, entry.owner);
  const context: AuthTransportContext = {
    signal: entry.controller.signal,
    get timedOut() { return entry.timedOut; },
    assertCurrent: () => {
      if (!entry.active || !isCurrentAuthOwner(entry.owner) || !entry.isCurrentScope()) throw new StaleAuthOwnerError();
    },
  };
  transportSignalContexts.set(entry.controller.signal, context);
  let physical: Promise<unknown>;
  try {
    context.assertCurrent();
    // Call synchronously so an idle transport begins in the initiating event turn.
    physical = Promise.resolve(entry.work(context));
  } catch (error) {
    physical = Promise.reject(error);
  }
  physical.then(
    (value) => { if (entry.active) { entry.active = false; entry.resolve(value); } },
    (error) => { if (entry.active) { entry.active = false; entry.reject(error); } },
  ).then(
    () => finishCredentialEntry(entry),
    () => finishCredentialEntry(entry),
  );
}

function finishCredentialEntry(entry: TransportEntry<unknown>): void {
  transportSignalOwners.delete(entry.controller.signal);
  transportSignalContexts.delete(entry.controller.signal);
  if (entry.timer) clearTimeout(entry.timer);
  entry.timer = null;
  entry.unsubscribe?.();
  entry.unsubscribe = null;
  if (activeCredentialTransport === entry) activeCredentialTransport = null;
  drainCredentialQueue();
}
