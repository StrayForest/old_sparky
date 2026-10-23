import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import net from "node:net";
import cleanupTools from "../../../../tools/platform_web_hermetic_cleanup.cjs";

const { preflightPorts, stopOwnedProcesses } = cleanupTools;

function waitForExit(child, timeoutMs) {
  return new Promise((resolve, reject) => {
    if (child.exitCode !== null || child.signalCode !== null) return resolve();
    const timeout = setTimeout(() => reject(new Error("Owned hermetic child did not exit.")), timeoutMs);
    child.once("exit", () => {
      clearTimeout(timeout);
      resolve();
    });
  });
}

function childWithRunId(runId, ignoreTerm = false) {
  return spawn(process.execPath, ["-e", ignoreTerm ? "process.on('SIGTERM', () => {}); setInterval(() => {}, 1000)" : "setInterval(() => {}, 1000)"], {
    env: { ...process.env, PLATFORM_WEB_HERMETIC_RUN_ID: runId },
    stdio: "ignore"
  });
}

async function main() {
  const ownerRunId = `oldsparky-web-hermetic-${process.pid}-contract-owner`;
  const otherRunId = `oldsparky-web-hermetic-${process.pid}-contract-other`;
  const owned = childWithRunId(ownerRunId, true);
  const unrelated = childWithRunId(otherRunId);
  try {
    await stopOwnedProcesses(ownerRunId);
    await waitForExit(owned, 3000);
    assert.equal(unrelated.exitCode, null, "cleanup must leave another run's process alive");

    const occupied = net.createServer();
    await new Promise((resolve, reject) => {
      occupied.once("error", reject);
      occupied.listen(0, "127.0.0.1", resolve);
    });
    const { port } = occupied.address();
    await assert.rejects(preflightPorts([port]), /already in use/);
    await new Promise((resolve, reject) => occupied.close((error) => error ? reject(error) : resolve()));
    await preflightPorts([port]);
  } finally {
    try {
      if (owned.exitCode === null && owned.signalCode === null) owned.kill("SIGKILL");
      if (unrelated.exitCode === null && unrelated.signalCode === null) unrelated.kill("SIGTERM");
      await waitForExit(unrelated, 1500).catch(() => unrelated.kill("SIGKILL"));
    } catch {}
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
