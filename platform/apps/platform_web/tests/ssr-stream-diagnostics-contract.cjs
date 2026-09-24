"use strict";

const childProcess = process.getBuiltinModule("node:child_process");
const path = process.getBuiltinModule("node:path");

const guardPath = path.resolve(__dirname, "..", "server-shutdown-guard.cjs");
const fixtureWatchdogMs = 3_000;
const behaviorTimeoutMs = 5_000;
const cleanupTimeoutMs = 1_000;

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

function fixtureSource({ disabled }) {
  return `
const http = process.getBuiltinModule("node:http");
const watchdog = setTimeout(() => {
  console.error("SSR diagnostics fixture watchdog elapsed");
  process.exit(124);
}, ${fixtureWatchdogMs});
const server = http.createServer((request, response) => {
  if (request.url !== "/tournaments/private-slug") {
    response.writeHead(404);
    response.end();
    return;
  }
  response.writeHead(200, {"content-type": "text/html"});
  ${disabled ? 'response.end("<html></html>");' : 'response.write("<html>"); response.end("</html>");'}
});
server.on("error", (error) => {
  console.error(error);
  clearTimeout(watchdog);
  process.exitCode = 1;
});
server.listen(0, "127.0.0.1", () => {
  const port = server.address().port;
  fetch("http://127.0.0.1:" + port + "/tournaments/private-slug", {
    headers: {
      accept: "text/html",
      "x-request-id": "${disabled ? "request-disabled" : "request-1"}",
      "cf-ray": "ray-1",
    },
  }).then((response) => response.text()).then(() => {
    server.close((error) => {
      clearTimeout(watchdog);
      if (error) {
        console.error(error);
        process.exitCode = 1;
      }
    });
  }).catch((error) => {
    console.error(error);
    clearTimeout(watchdog);
    process.exitCode = 1;
    server.close();
  });
});
`;
}

async function runFixture(name, disabled) {
  const environment = { ...process.env };
  if (disabled) {
    delete environment.PLATFORM_SSR_PERF_LOG_ENABLED;
  } else {
    environment.PLATFORM_SSR_PERF_LOG_ENABLED = "true";
    environment.PLATFORM_SSR_PERF_SAMPLE_RATE = "1";
  }
  const child = childProcess.spawn(
    process.execPath,
    ["--require", guardPath, "-e", fixtureSource({ disabled })],
    {
      detached: true,
      env: environment,
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
      failure = `SSR diagnostics ${name} fixture exceeded behavior deadline (${behaviorTimeoutMs} ms)`;
    } else if (result.kind === "error") {
      failure = `SSR diagnostics ${name} fixture failed to start: ${result.error}`;
    } else if (result.code !== 0) {
      failure = `SSR diagnostics ${name} fixture exited with code ${result.code ?? "null"}`;
    }
  } finally {
    if (child.exitCode === null && child.signalCode === null) {
      signalProcessGroup(child, "SIGKILL");
      const cleanup = await withTimeout(exit, cleanupTimeoutMs);
      if (cleanup.kind === "timeout" && !failure) {
        failure = `SSR diagnostics ${name} fixture cleanup exceeded deadline (${cleanupTimeoutMs} ms)`;
      }
    }
  }

  if (failure) {
    throw new Error(
      `${failure}; stdout=${JSON.stringify(bounded(stdout))} stderr=${JSON.stringify(bounded(stderr))}`,
    );
  }
  return stdout;
}

function assertCount(output, fragment, expected, name) {
  const actual = output.split(fragment).length - 1;
  if (actual !== expected) {
    throw new Error(`${name}: expected ${fragment} ${expected} time(s), got ${actual}`);
  }
}

async function main() {
  const enabledOutput = await runFixture("enabled", false);
  for (const stage of [
    "response_stream_start",
    "first_body_write_attempt",
    "response_finish",
    "response_close",
  ]) {
    assertCount(enabledOutput, `stage=${stage}`, 1, "enabled SSR diagnostics");
  }
  if (enabledOutput.includes("stage=response_error")) {
    throw new Error("enabled SSR diagnostics emitted response_error");
  }
  for (const fragment of [
    "writable_finished=0",
    "writable_finished=1",
    "write_count=2",
    "body_bytes=13",
    "request_id=request-1",
    "cf_ray=ray-1",
  ]) {
    if (!enabledOutput.includes(fragment)) {
      throw new Error(`enabled SSR diagnostics omitted ${fragment}`);
    }
  }

  const disabledOutput = await runFixture("disabled", true);
  if (disabledOutput.includes("ssr_stream")) {
    throw new Error("disabled SSR diagnostics emitted a stream log");
  }
}

void main().catch((error) => {
  console.error(`SSR diagnostics contract failed: ${error}`);
  process.exitCode = 1;
});
