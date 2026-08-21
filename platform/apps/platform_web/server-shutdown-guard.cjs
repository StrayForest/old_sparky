"use strict";

const configuredGraceMs = Number.parseInt(
  process.env.PLATFORM_WEB_SHUTDOWN_GRACE_MS ?? "10000",
  10,
);
const graceMs = Number.isFinite(configuredGraceMs)
  ? Math.min(60_000, Math.max(1_000, configuredGraceMs))
  : 10_000;

let shutdownScheduled = false;

for (const [signal, exitCode] of [["SIGINT", 130], ["SIGTERM", 143]]) {
  process.once(signal, () => {
    if (shutdownScheduled) {
      return;
    }
    shutdownScheduled = true;
    const forceExit = setTimeout(() => {
      console.log(`Web shutdown grace period (${graceMs} ms) elapsed; exiting.`);
      process.exit(exitCode);
    }, graceMs);
    forceExit.unref();
  });
}
