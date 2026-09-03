export function safeAuthReturnPath(value: string | undefined | null): string {
  if (
    !value
    || !value.startsWith("/")
    || value.startsWith("//")
    || value.startsWith("/auth/")
    || /[\\\r\n]/u.test(value)
  ) {
    return "/";
  }
  return value;
}

export function steamCompletionPath(
  returnTo: string,
  flow: "login" | "link" = "login"
): string {
  const params = new URLSearchParams({ returnTo: safeAuthReturnPath(returnTo) });
  if (flow === "link") {
    params.set("flow", flow);
  }
  return `/auth/steam-complete?${params.toString()}`;
}

export function googleCompletionPath(returnTo: string): string {
  const params = new URLSearchParams({ returnTo: safeAuthReturnPath(returnTo) });
  return `/auth/google-complete?${params.toString()}`;
}

export function withSteamAuthStatus(
  destination: string,
  status: "error" | "success"
): string {
  const url = new URL(safeAuthReturnPath(destination), "https://platform.invalid");
  url.searchParams.set("steam_auth", status);
  return `${url.pathname}${url.search}${url.hash}`;
}
