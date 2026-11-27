import type { Page, TestInfo } from "@playwright/test";

export const OBJECTIVE_SCREENSHOT_ATTACHMENT_NAME = "objective-screenshot-error-v1";

export type ObjectiveScreenshotFailureCategory =
  | "capture_timeout"
  | "test_timeout"
  | "browser_closed_or_crashed"
  | "capture_unavailable"
  | "page_navigating"
  | "invalid_geometry"
  | "output_eacces"
  | "output_erofs"
  | "output_enospc"
  | "output_efbig"
  | "output_enoent"
  | "other";

export type ObjectiveScreenshotDiagnostic = {
  schema: 1;
  category: ObjectiveScreenshotFailureCategory;
  error_name: "TimeoutError" | "TargetClosedError" | "Error" | "unknown";
  phase: "resolve_options" | "capture";
  error_code:
    | "none"
    | "EACCES"
    | "EROFS"
    | "ENOSPC"
    | "EFBIG"
    | "ENOENT"
    | "EIO"
    | "ETIMEDOUT"
    | "ECONNRESET"
    | "EPIPE"
    | "other";
  viewport: { width: number | null; height: number | null };
  document_height_bucket: "unavailable" | "<10k" | "10k-30k" | "30k-60k" | ">=60k";
  elapsed_bucket: "<100ms" | "<1s" | "<5s" | "<30s" | ">=30s";
  page_closed: boolean;
  browser_connected: boolean;
  crash_observed: boolean;
};

function errorString(error: unknown, key: "name" | "message" | "code"): string {
  if (error instanceof Error) {
    if (key === "name") return error.name;
    if (key === "message") return error.message;
  }
  if (typeof error === "object" && error !== null && key in error) {
    const value = (error as Record<string, unknown>)[key];
    return typeof value === "string" ? value : "";
  }
  return "";
}

function closedErrorName(error: unknown): ObjectiveScreenshotDiagnostic["error_name"] {
  const name = errorString(error, "name");
  if (name === "TimeoutError") return "TimeoutError";
  if (name === "TargetClosedError") return "TargetClosedError";
  if (name === "Error") return "Error";
  return "unknown";
}

function closedErrorCode(error: unknown): ObjectiveScreenshotDiagnostic["error_code"] {
  const code = errorString(error, "code");
  if (!code) return "none";
  if (
    code === "EACCES"
    || code === "EROFS"
    || code === "ENOSPC"
    || code === "EFBIG"
    || code === "ENOENT"
    || code === "EIO"
    || code === "ETIMEDOUT"
    || code === "ECONNRESET"
    || code === "EPIPE"
  ) return code;
  return "other";
}

export function classifyObjectiveScreenshotError(
  error: unknown,
  browserConnected: boolean,
): ObjectiveScreenshotFailureCategory {
  const name = errorString(error, "name");
  const message = errorString(error, "message");
  const code = errorString(error, "code").toUpperCase();
  if (/test timeout(?: of .*?)? exceeded|test timed out/iu.test(message)) return "test_timeout";
  if (/navigat(?:ing|ion).*screenshot|screenshot.*navigat/iu.test(message)) return "page_navigating";
  if (/invalid.*(?:width|height|clip|dimension)|(?:width|height|clip).*out of (?:range|bounds)|cannot screenshot.*(?:empty|zero|dimension)/iu.test(message)) {
    return "invalid_geometry";
  }
  if (/Page\.captureScreenshot.*(?:not supported|unavailable|not implemented)|screenshot.*unavailable/iu.test(message)) {
    return "capture_unavailable";
  }
  if (code === "EACCES" || /eacces|permission denied/iu.test(message)) return "output_eacces";
  if (code === "EROFS" || /erofs|read-only file system/iu.test(message)) return "output_erofs";
  if (code === "ENOSPC" || /enospc|no space left/iu.test(message)) return "output_enospc";
  if (code === "EFBIG" || /efbig|file too large/iu.test(message)) return "output_efbig";
  if (code === "ENOENT" || /enoent|no such file or directory/iu.test(message)) return "output_enoent";
  if (
    name === "TargetClosedError"
    || !browserConnected
    || /target closed|browser has been closed|browser.*crash(?:ed)?/iu.test(message)
  ) return "browser_closed_or_crashed";
  if (name === "TimeoutError" || /timeout(?: of)?(?: [\d.]+(?:ms|s))? exceeded/iu.test(message)) {
    return "capture_timeout";
  }
  return "other";
}

function documentHeightBucket(height: unknown): ObjectiveScreenshotDiagnostic["document_height_bucket"] {
  if (typeof height !== "number" || !Number.isFinite(height) || height < 0) return "unavailable";
  if (height < 10_000) return "<10k";
  if (height < 30_000) return "10k-30k";
  if (height < 60_000) return "30k-60k";
  return ">=60k";
}

function elapsedBucket(milliseconds: number): ObjectiveScreenshotDiagnostic["elapsed_bucket"] {
  if (milliseconds < 100) return "<100ms";
  if (milliseconds < 1_000) return "<1s";
  if (milliseconds < 5_000) return "<5s";
  if (milliseconds < 30_000) return "<30s";
  return ">=30s";
}

function closedViewportDimension(value: number | undefined): number | null {
  return value !== undefined
    && Number.isSafeInteger(value)
    && value >= 1
    && value <= 10_000
    ? value
    : null;
}

async function boundedDocumentHeight(page: Page): Promise<number | null> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      page.evaluate(() => document.documentElement.scrollHeight),
      new Promise<null>((resolve) => {
        timer = setTimeout(() => resolve(null), 200);
      }),
    ]);
  } catch {
    return null;
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

export async function takeObjectiveScreenshotWithDiagnostics(
  page: Page,
  testInfo: TestInfo,
  resolveOptions: () => Parameters<Page["screenshot"]>[0],
): Promise<Buffer> {
  let viewport: ReturnType<Page["viewportSize"]> = null;
  try {
    viewport = page.viewportSize();
  } catch {
    viewport = null;
  }
  const height = await boundedDocumentHeight(page);
  const startedAt = performance.now();
  let phase: ObjectiveScreenshotDiagnostic["phase"] = "resolve_options";
  try {
    const options = resolveOptions();
    phase = "capture";
    return await page.screenshot(options);
  } catch (error) {
    let pageClosed = false;
    let browserConnected = false;
    try {
      pageClosed = page.isClosed();
      browserConnected = page.context().browser()?.isConnected() ?? false;
    } catch {
      pageClosed = true;
    }
    const message = errorString(error, "message");
    const diagnostic: ObjectiveScreenshotDiagnostic = {
      schema: 1,
      category: classifyObjectiveScreenshotError(error, browserConnected),
      error_name: closedErrorName(error),
      phase,
      error_code: closedErrorCode(error),
      viewport: {
        width: closedViewportDimension(viewport?.width),
        height: closedViewportDimension(viewport?.height),
      },
      document_height_bucket: documentHeightBucket(height),
      elapsed_bucket: elapsedBucket(performance.now() - startedAt),
      page_closed: pageClosed,
      browser_connected: browserConnected,
      crash_observed: !browserConnected && /crash(?:ed)?/iu.test(message),
    };
    try {
      await testInfo.attach(OBJECTIVE_SCREENSHOT_ATTACHMENT_NAME, {
        body: Buffer.from(JSON.stringify(diagnostic)),
        contentType: "application/json",
      });
    } catch {
      // Diagnostic failure must never replace the original capture failure.
    }
    throw error;
  }
}
