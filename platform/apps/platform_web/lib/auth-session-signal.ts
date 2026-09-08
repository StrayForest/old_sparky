import type { PlatformUser } from "@/lib/platform-types";

export type PlatformAuthStatus = "authenticated" | "anonymous" | "unavailable";

export type PlatformAuthState = {
  status: PlatformAuthStatus;
  user: PlatformUser | null;
};

let unauthorizedHandler: (() => void) | null = null;
const authStateListeners = new Set<(state: PlatformAuthState) => void>();
let authStateRequestHandler: ((state: PlatformAuthState) => void) | null = null;

export function registerPlatformUnauthorizedHandler(handler: () => void): () => void {
  unauthorizedHandler = handler;
  return () => {
    if (unauthorizedHandler === handler) {
      unauthorizedHandler = null;
    }
  };
}

export function registerPlatformAuthStateListener(
  listener: (state: PlatformAuthState) => void
): () => void {
  authStateListeners.add(listener);
  return () => authStateListeners.delete(listener);
}

export function registerPlatformAuthStateRequestHandler(
  handler: (state: PlatformAuthState) => void
): () => void {
  authStateRequestHandler = handler;
  return () => {
    if (authStateRequestHandler === handler) {
      authStateRequestHandler = null;
    }
  };
}

export function notifyPlatformAuthStateChanged(state: PlatformAuthState): void {
  for (const listener of authStateListeners) {
    listener(state);
  }
}

export function requestPlatformAuthStateChange(state: PlatformAuthState): void {
  authStateRequestHandler?.(state);
}

export function notifyPlatformUnauthorized(detail: string): void {
  if (
    detail === "Authentication required."
    || detail === "Session is invalid."
    || detail === "Session owner is missing."
    || detail.startsWith("Authentication required to ")
  ) {
    unauthorizedHandler?.();
  }
}
