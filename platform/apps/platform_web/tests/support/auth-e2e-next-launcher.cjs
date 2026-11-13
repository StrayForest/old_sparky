"use strict";

/* eslint @typescript-eslint/no-require-imports: "off" -- This process launcher is CommonJS. */

const { spawn } = require("node:child_process");
const path = require("node:path");
const { nextChildEnv } = require("./auth-e2e-child-env.cjs");

const standaloneServer = path.resolve(process.cwd(), ".next/standalone/server.js");
const env = nextChildEnv();

const child = spawn(process.execPath, [standaloneServer], {
  cwd: process.cwd(),
  env,
  stdio: "ignore"
});
let stopping = false;
const stop = (signal) => {
  if (stopping) return;
  stopping = true;
  if (child.exitCode !== null || child.signalCode !== null) return;
  child.kill(signal);
  const force = setTimeout(() => {
    if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
  }, 5000);
  force.unref();
};
process.on("SIGTERM", () => stop("SIGTERM"));
process.on("SIGINT", () => stop("SIGINT"));
child.on("error", () => {
  process.stderr.write("AUTH_E2E_NEXT_LAUNCH status=failed\n");
  process.exitCode = 1;
});
child.on("exit", (code) => {
  if (!stopping || (code !== 0 && code !== null)) {
    process.stderr.write("AUTH_E2E_NEXT_LAUNCH status=failed\n");
    process.exitCode = 1;
  }
});
