"use strict";

/* eslint @typescript-eslint/no-require-imports: "off" -- This process launcher is CommonJS. */

const { spawn } = require("node:child_process");
const path = require("node:path");
const { apiChildEnv } = require("./auth-e2e-child-env.cjs");
const {
  createPythonDiagnosticParser,
  shouldInitiateChildStop,
  shouldReportEarlyExit
} = require("./auth-e2e-python-diagnostics.cjs");

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
const diagnostics = createPythonDiagnosticParser(platformRoot);
let stderrBuffer = "";
child.stderr.on("data", (chunk) => {
  stderrBuffer += chunk.toString("utf8");
  if (stderrBuffer.length > 512) stderrBuffer = "";
  let newline = stderrBuffer.indexOf("\n");
  while (newline >= 0) {
    const line = stderrBuffer.slice(0, newline).replace(/\r$/u, "");
    stderrBuffer = stderrBuffer.slice(newline + 1);
    const safeOutput = diagnostics.consume(line + "\n");
    if (safeOutput) process.stderr.write(safeOutput);
    newline = stderrBuffer.indexOf("\n");
  }
});
let stopping = false;
let spawnFailed = false;
let childExited = false;
const stop = (signal) => {
  if (!shouldInitiateChildStop({
    stopping,
    childExited,
    exitCode: child.exitCode,
    signalCode: child.signalCode
  })) return;
  stopping = true;
  child.kill(signal);
  const force = setTimeout(() => {
    if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
  }, 5000);
  force.unref();
};
process.on("SIGTERM", () => stop("SIGTERM"));
process.on("SIGINT", () => stop("SIGINT"));
child.on("exit", () => {
  childExited = true;
});
child.on("error", () => {
  spawnFailed = true;
  process.stderr.write(
    "AUTH_E2E_API_EXIT status=spawn-error stage=launch exception=none "
    + "source=none line=0 code=none signal=none\n"
  );
  process.stderr.write("AUTH_E2E_API_LAUNCH status=failed\n");
  process.exitCode = 1;
});
child.on("close", (code, signal) => {
  if (!shouldReportEarlyExit({ stopping, spawnFailed })) return;
  process.stderr.write(diagnostics.exit({ code, signal }));
  process.stderr.write("AUTH_E2E_API_LAUNCH status=failed\n");
  process.exitCode = 1;
});
