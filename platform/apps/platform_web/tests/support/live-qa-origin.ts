export function validateLiveQaOrigin({
  allowLoopback,
  configured,
  expected,
}: {
  allowLoopback: boolean;
  configured: string | undefined;
  expected: string | undefined;
}): string {
  const configuredOrigin = parseCredentialOrigin(
    configured,
    "PLAYWRIGHT_LIVE_BASE_URL",
    allowLoopback,
  );
  const expectedOrigin = parseCredentialOrigin(
    expected,
    "PLATFORM_LIVE_EXPECTED_ORIGIN",
    allowLoopback,
  );
  if (configuredOrigin !== expectedOrigin) {
    throw new Error("The live QA origin does not match PLATFORM_WEB_ORIGIN.");
  }
  const productionOrigin = "https://old-sparky.com";
  if (allowLoopback) {
    const hostname = new URL(configuredOrigin).hostname;
    if (!["127.0.0.1", "[::1]", "localhost"].includes(hostname)) {
      throw new Error("Explicit local live QA is restricted to a loopback origin.");
    }
  } else if (
    configuredOrigin !== productionOrigin
    || expectedOrigin !== productionOrigin
  ) {
    throw new Error("Credential-bearing production QA requires https://old-sparky.com.");
  }
  return configuredOrigin;
}

function parseCredentialOrigin(
  value: string | undefined,
  label: string,
  allowLoopback: boolean,
): string {
  if (!value) {
    throw new Error(`${label} is required for credential-bearing live QA.`);
  }
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error(`${label} must be an absolute URL.`);
  }
  if (
    parsed.username
    || parsed.password
    || parsed.pathname !== "/"
    || parsed.search
    || parsed.hash
  ) {
    throw new Error(`${label} must be an origin without credentials, path, query or fragment.`);
  }
  const loopback = ["127.0.0.1", "[::1]", "localhost"].includes(parsed.hostname);
  if (loopback && !allowLoopback) {
    throw new Error(`${label} loopback use requires PLATFORM_LIVE_CSP_ALLOW_LOOPBACK=1.`);
  }
  if (
    parsed.protocol !== "https:"
    && !(loopback && allowLoopback && parsed.protocol === "http:")
  ) {
    throw new Error(`${label} must use HTTPS outside an explicit loopback QA run.`);
  }
  return parsed.origin;
}
