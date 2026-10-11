import { constants, closeSync, fstatSync, openSync, writeSync } from "node:fs";
import { isAbsolute, dirname } from "node:path";
import { constants as osConstants } from "node:os";
import { spawn } from "node:child_process";

const capturePath = process.env.PLATFORM_WEB_404_DIAGNOSTIC_FILE;
const childArgs = process.argv.slice(2);
const maxCaptureBytes = 64 * 1024;
const truncationMarker = Buffer.from("\n[WEB_SERVER_CAPTURE_TRUNCATED]\n", "utf8");

if (!capturePath || !isAbsolute(capturePath) || childArgs.length !== 1) {
  process.exit(2);
}

let outputFd;
try {
  const parentFd = openSync(dirname(capturePath), constants.O_RDONLY | constants.O_DIRECTORY);
  const parent = fstatSync(parentFd);
  closeSync(parentFd);
  if (
    !parent.isDirectory()
    || parent.uid !== process.getuid()
    || (parent.mode & 0o077) !== 0
  ) {
    process.exit(2);
  }
  outputFd = openSync(
    capturePath,
    constants.O_WRONLY | constants.O_APPEND | constants.O_NOFOLLOW,
  );
  const capture = fstatSync(outputFd);
  if (
    !capture.isFile()
    || capture.nlink !== 1
    || capture.uid !== process.getuid()
    || (capture.mode & 0o077) !== 0
  ) {
    closeSync(outputFd);
    process.exit(2);
  }
} catch {
  process.exit(2);
}

let capturedBytes = 0;
let truncated = false;
function captureOutput(channel, chunk) {
  if (truncated) return;
  const record = Buffer.concat([Buffer.from(`[${channel}] `, "utf8"), Buffer.from(chunk)]);
  const remaining = maxCaptureBytes - capturedBytes;
  const payloadBudget = Math.max(0, remaining - truncationMarker.length);
  if (record.length <= payloadBudget) {
    writeSync(outputFd, record);
    capturedBytes += record.length;
    return;
  }
  const payloadLimit = payloadBudget;
  if (payloadLimit > 0) {
    writeSync(outputFd, record.subarray(0, payloadLimit));
    capturedBytes += payloadLimit;
  }
  if (remaining >= truncationMarker.length) {
    writeSync(outputFd, truncationMarker);
    capturedBytes += truncationMarker.length;
  }
  truncated = true;
}

let child;
try {
  child = spawn(process.execPath, childArgs, {
    cwd: process.cwd(),
    env: process.env,
    stdio: ["ignore", "pipe", "pipe"],
  });
} catch {
  writeSync(outputFd, Buffer.from("[capture] server_spawn_failed\n", "utf8"));
  closeSync(outputFd);
  process.exit(1);
}

child.stdout.on("data", (chunk) => captureOutput("stdout", chunk));
child.stderr.on("data", (chunk) => captureOutput("stderr", chunk));
child.on("error", () => captureOutput("capture", Buffer.from("server_spawn_failed\n", "utf8")));

for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) {
  process.on(signal, () => {
    if (child && child.exitCode === null && child.signalCode === null) {
      child.kill(signal);
    }
  });
}

child.once("close", (code) => {
  closeSync(outputFd);
  if (Number.isInteger(code)) {
    process.exitCode = code;
    return;
  }
  const signalNumber = child.signalCode ? osConstants.signals[child.signalCode] : undefined;
  process.exitCode = Number.isInteger(signalNumber) ? 128 + signalNumber : 1;
});
