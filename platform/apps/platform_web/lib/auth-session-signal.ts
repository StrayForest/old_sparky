let unauthorizedHandler: (() => void) | null = null;

export function registerPlatformUnauthorizedHandler(handler: () => void): () => void {
  unauthorizedHandler = handler;
  return () => {
    if (unauthorizedHandler === handler) {
      unauthorizedHandler = null;
    }
  };
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
