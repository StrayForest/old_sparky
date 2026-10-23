#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const net = require("node:net");
const { spawnSync } = require("node:child_process");

const PROCESS_WAIT_MS = 1200;
const POLL_INTERVAL_MS = 40;

function readProcessEnvironment(pid) {
  try {
    return fs.readFileSync(`/proc/${pid}/environ`);
  } catch (error) {
    if (error.code === "ENOENT" || error.code === "ESRCH" || error.code === "EACCES") {
      return null;
    }
    throw error;
  }
}

function ownsRunId(pid, runId) {
  const environment = readProcessEnvironment(pid);
  if (!environment) return false;
  const expected = Buffer.from(`PLATFORM_WEB_HERMETIC_RUN_ID=${runId}`);
  return environment.toString("binary").split("\0").some((entry) =>
    Buffer.from(entry, "binary").equals(expected)
  );
}

function parentPid(pid) {
  try {
    const stat = fs.readFileSync(`/proc/${pid}/stat`, "utf8");
    const fields = stat.slice(stat.lastIndexOf(")") + 2).split(" ");
    return Number(fields[1]);
  } catch (error) {
    if (error.code === "ENOENT" || error.code === "ESRCH" || error.code === "EACCES") {
      return 0;
    }
    throw error;
  }
}

function ancestorPids() {
  const ancestors = new Set([process.pid]);
  let current = process.pid;
  while (current > 1) {
    current = parentPid(current);
    if (current > 0) ancestors.add(current);
  }
  return ancestors;
}

function ownedProcessIds(runId) {
  if (process.platform !== "linux") {
    throw new Error("Hermetic process cleanup requires Linux /proc process identity.");
  }
  if (!/^oldsparky-web-hermetic-[a-zA-Z0-9_-]+$/.test(runId)) {
    throw new Error("Invalid hermetic run identity.");
  }
  const excluded = ancestorPids();
  return fs.readdirSync("/proc")
    .filter((entry) => /^\d+$/.test(entry))
    .map(Number)
    .filter((pid) => pid > 1 && !excluded.has(pid) && ownsRunId(pid, runId));
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function stopOwnedProcesses(runId) {
  const initial = ownedProcessIds(runId);
  for (const pid of initial) {
    if (ownsRunId(pid, runId)) {
      try {
        process.kill(pid, "SIGTERM");
      } catch (error) {
        if (error.code !== "ESRCH") throw error;
      }
    }
  }

  const deadline = Date.now() + PROCESS_WAIT_MS;
  while (Date.now() < deadline) {
    const remaining = ownedProcessIds(runId);
    if (remaining.length === 0) return;
    await wait(POLL_INTERVAL_MS);
  }

  for (const pid of ownedProcessIds(runId)) {
    if (ownsRunId(pid, runId)) {
      try {
        process.kill(pid, "SIGKILL");
      } catch (error) {
        if (error.code !== "ESRCH") throw error;
      }
    }
  }
  await wait(POLL_INTERVAL_MS);
  const remaining = ownedProcessIds(runId);
  if (remaining.length > 0) {
    throw new Error(`Hermetic cleanup could not stop ${remaining.length} owned process(es).`);
  }
}

function preflightPorts(ports) {
  return ports.reduce((promise, port) => promise.then(() => new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", (error) => {
      if (error.code === "EADDRINUSE") {
        const owner = spawnSync("ss", ["-ltnp", `( sport = :${port} )`], { encoding: "utf8" });
        const details = owner.status === 0 ? owner.stdout.trim() : "owner details unavailable";
        reject(new Error(`Hermetic test port ${port} is already in use; stop its owner and retry.\n${details}`));
      } else {
        reject(error);
      }
    });
    server.listen(port, "127.0.0.1", () => server.close((error) => error ? reject(error) : resolve()));
  })), Promise.resolve());
}

async function main() {
  const [command, ...args] = process.argv.slice(2);
  if (command === "cleanup" && args.length === 1) {
    await stopOwnedProcesses(args[0]);
    return;
  }
  if (command === "preflight" && args.length > 0) {
    const ports = args.map(Number);
    if (ports.some((port) => !Number.isInteger(port) || port < 1 || port > 65535)) {
      throw new Error("Preflight ports must be valid TCP port numbers.");
    }
    await preflightPorts(ports);
    return;
  }
  throw new Error("Usage: platform_web_hermetic_cleanup.cjs cleanup <run-id> | preflight <port...>");
}

if (require.main === module) {
  main().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}

module.exports = { preflightPorts, stopOwnedProcesses };
