/**
 * The app-wide React Query client (issue #1032).
 *
 * main.tsx renders the provider with this singleton, and the auth store's
 * identity-boundary reset (`resetChatStateForIdentitySwitch`) clears it at
 * logout / different-principal login / register so no previous-principal
 * cached entry (Draft Room, Canvas, Groups — none of the query keys carry a
 * principal segment) can render for the next user in the same tab.
 *
 * `QueryClient.clear()` removes every cached Query object; in-flight fetches
 * settle into their detached instances and do not re-enter the cache (pinned
 * by useAuthStore.identityScope.queryCache.test.ts's in-flight leg).
 */
import { QueryClient } from "@tanstack/react-query";

export const queryClient = new QueryClient();
