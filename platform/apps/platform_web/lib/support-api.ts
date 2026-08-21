import { PlatformApiError, platformApiUrl } from "@/lib/platform-api";

export type SupportCategory = "account" | "tournament" | "technical" | "rules" | "other";

export type SupportMessagePayload = {
  name: string;
  email: string;
  category: SupportCategory;
  message: string;
  website: string;
};

export async function submitSupportMessage(payload: SupportMessagePayload): Promise<{ accepted: boolean }> {
  const response = await fetch(platformApiUrl("/content/support/messages"), {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json"
    },
    credentials: "omit",
    cache: "no-store",
    body: JSON.stringify(payload)
  });

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json() as { detail?: unknown };
      if (typeof body.detail === "string" && body.detail) {
        detail = body.detail;
      }
    } catch {
      detail = detail || "Platform request failed.";
    }
    const retryAfterHeader = response.headers.get("Retry-After");
    const retryAfterSeconds = retryAfterHeader && /^\d+$/u.test(retryAfterHeader)
      ? Number(retryAfterHeader)
      : null;
    throw new PlatformApiError(detail || "Platform request failed.", response.status, retryAfterSeconds);
  }

  return await response.json() as { accepted: boolean };
}
