"use strict";
/* eslint-disable @typescript-eslint/no-require-imports */

const { spawn } = require("node:child_process");
const path = require("node:path");

const guardPath = path.resolve(__dirname, "..", "server-shutdown-guard.cjs");
const graceMs = 1_000;
const fixtureWatchdogMs = 4_000;
const behaviorTimeoutMs = 6_000;
const cleanupTimeoutMs = 1_000;
const fixtureSource = [
  "process.on('SIGTERM', () => {});",
  "setTimeout(() => process.kill(process.pid, 'SIGTERM'), 20);",
  `setTimeout(() => { console.log('shutdown fixture watchdog elapsed'); process.exit(124); }, ${fixtureWatchdogMs});`,
  "setInterval(() => {}, 1000);",
].join(" ");

function bounded(value) {
  return String(value || "").slice(0, 4_000);
}

function withTimeout(promise, timeoutMs) {
  let timer;
  const timeout = new Promise((resolve) => {
    timer = setTimeout(() => resolve({ kind: "timeout" }), timeoutMs);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

function signalProcessGroup(child, signal) {
  if (child.exitCode !== null || child.signalCode !== null || !child.pid) {
    return;
  }
  try {
    process.kill(-child.pid, signal);
  } catch (error) {
    if (error && error.code !== "ESRCH") {
      throw error;
    }
  }
}

async function main() {
  const child = spawn(
    process.execPath,
    ["--require", guardPath, "-e", fixtureSource],
    {
      detached: true,
      env: {
        ...process.env,
        PLATFORM_WEB_SHUTDOWN_GRACE_MS: String(graceMs),
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );

  let stdout = "";
  let stderr = "";
  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk) => {
    stdout += chunk;
  });
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => {
    stderr += chunk;
  });

  const exit = new Promise((resolve) => {
    child.once("error", (error) => resolve({ kind: "error", error }));
    child.once("close", (code, signal) => resolve({ kind: "close", code, signal }));
  });

  let failure = null;
  try {
    const result = await withTimeout(exit, behaviorTimeoutMs);
    if (result.kind === "timeout") {
      failure = `shutdown fixture exceeded behavior deadline (${behaviorTimeoutMs} ms)`;
    } else if (result.kind === "error") {
      failure = `shutdown fixture failed to start: ${result.error}`;
    } else if (result.code !== 143) {
      failure = `shutdown fixture exited with code ${result.code ?? "null"}`;
    } else if (!stdout.includes(`Web shutdown grace period (${graceMs} ms) elapsed; exiting.`)) {
      failure = "shutdown fixture did not emit the guard deadline marker";
    } else if (stdout.includes("shutdown fixture watchdog elapsed")) {
      failure = "shutdown fixture watchdog fired before the guard deadline";
    }
  } finally {
    if (child.exitCode === null && child.signalCode === null) {
      signalProcessGroup(child, "SIGKILL");
      const cleanup = await withTimeout(exit, cleanupTimeoutMs);
      if (cleanup.kind === "timeout" && !failure) {
        failure = `shutdown fixture cleanup exceeded deadline (${cleanupTimeoutMs} ms)`;
      }
    }
  }

  if (failure) {
    console.error(
      `${failure}; stdout=${JSON.stringify(bounded(stdout))} stderr=${JSON.stringify(bounded(stderr))}`,
    );
    process.exitCode = 1;
  }
}

void main().catch((error) => {
  console.error(`shutdown fixture contract failed: ${error}`);
  process.exitCode = 1;
});
