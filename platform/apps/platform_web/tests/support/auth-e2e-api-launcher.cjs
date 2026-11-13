"use strict";

/* eslint @typescript-eslint/no-require-imports: "off" -- This process launcher is CommonJS. */

const { spawn } = require("node:child_process");
const path = require("node:path");
const { apiChildEnv } = require("./auth-e2e-child-env.cjs");

const webRoot = path.resolve(__dirname, "../..");
const platformRoot = path.resolve(webRoot, "../..");
const python = path.join(platformRoot, ".venv_platform", "bin", "python");
const server = path.join(platformRoot, "tests", "support", "auth_lifecycle_e2e_server.py");

let env;
try {
  env = apiChildEnv();
} catch {
  process.stderr.write("AUTH_E2E_API_LAUNCH status=missing-config\n");
  process.exit(2);
}

const child = spawn(python, ["-I", "-B", server], {
  cwd: platformRoot,
  env,
  stdio: ["ignore", "ignore", "pipe"]
});
const safeStartupStages = new Set([
  "environment",
  "smtp-capture",
  "application-import",
  "settings-validation",
  "service-isolation",
  "app-creation",
  "route-registration",
  "control-file",
  "uvicorn-startup",
  "preflight",
  "startup"
]);
const safeStartupCategories = new Set([
  "module-missing",
  "module-import",
  "permission",
  "path-missing",
  "os-error",
  "value-error",
  "type-error",
  "name-error",
  "guard-or-runtime",
  "unexpected"
]);
let stderrBuffer = "";
child.stderr.on("data", (chunk) => {
  stderrBuffer += chunk.toString("utf8");
  if (stderrBuffer.length > 512) stderrBuffer = "";
  let newline = stderrBuffer.indexOf("\n");
  while (newline >= 0) {
    const line = stderrBuffer.slice(0, newline).replace(/\r$/u, "");
    stderrBuffer = stderrBuffer.slice(newline + 1);
    const match = /^AUTH_E2E_API_STARTUP status=failed stage=([a-z-]+) category=([a-z-]+)$/u
      .exec(line);
    if (match && safeStartupStages.has(match[1]) && safeStartupCategories.has(match[2])) {
      process.stderr.write(`${line}\n`);
    }
    newline = stderrBuffer.indexOf("\n");
  }
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
  process.stderr.write("AUTH_E2E_API_LAUNCH status=failed\n");
  process.exitCode = 1;
});
child.on("exit", (code) => {
  if (!stopping || (code !== 0 && code !== null)) {
    process.stderr.write("AUTH_E2E_API_LAUNCH status=failed\n");
    process.exitCode = 1;
  }
});
