import { useSyncExternalStore } from "react";
import {
  captureAuthOwner,
  onAuthOwnerReplacement,
  type AuthOwner,
} from "@/lib/api/auth-lifecycle";

function subscribeAuthOwner(listener: () => void): () => void {
  return onAuthOwnerReplacement(() => listener());
}

/** Reactive lease snapshot; subscribing never aborts or disposes the shared owner. */
export function useAuthOwner(): AuthOwner {
  return useSyncExternalStore(subscribeAuthOwner, captureAuthOwner, captureAuthOwner);
}
