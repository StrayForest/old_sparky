"use strict";

const cluster = process.getBuiltinModule("node:cluster");
const path = process.getBuiltinModule("node:path");

const serverEntry = path.join(process.cwd(), ".next", "standalone", "server.js");
const workerCount = Number.parseInt(process.env.PLATFORM_WEB_WORKERS ?? "2", 10);
const maxWorkerRestartsPerMinute = 4;
const restartWindowMs = 60_000;

if (!Number.isInteger(workerCount) || workerCount < 2 || workerCount > 2) {
  console.error("PLATFORM_WEB_WORKERS must be exactly 2 for the clustered web runner.");
  process.exit(1);
}

if (cluster.isPrimary) {
  let shuttingDown = false;
  const restartTimes = [];

  const spawnWorker = () => {
    if (shuttingDown) {
      return;
    }
    const worker = cluster.fork();
    worker.once("exit", (code, signal) => {
      if (shuttingDown) {
        return;
      }
      const now = Date.now();
      while (restartTimes[0] && now - restartTimes[0] > restartWindowMs) {
        restartTimes.shift();
      }
      restartTimes.push(now);
      if (restartTimes.length > maxWorkerRestartsPerMinute) {
        console.error(
          `Web worker restart budget exceeded (code=${code ?? "none"}, signal=${signal ?? "none"}).`,
        );
        process.exitCode = 1;
        process.kill(process.pid, "SIGTERM");
        return;
      }
      console.error(
        `Web worker exited; replacing it (code=${code ?? "none"}, signal=${signal ?? "none"}).`,
      );
      spawnWorker();
    });
  };

  const shutdown = (signal) => {
    if (shuttingDown) {
      return;
    }
    shuttingDown = true;
    for (const worker of Object.values(cluster.workers)) {
      worker?.process.kill(signal);
    }
  };

  process.once("SIGINT", () => shutdown("SIGINT"));
  process.once("SIGTERM", () => shutdown("SIGTERM"));
  for (let index = 0; index < workerCount; index += 1) {
    spawnWorker();
  }
} else {
  import(serverEntry).catch((error) => {
    console.error("Web worker failed to load the Next.js server.", error);
    process.exitCode = 1;
  });
}
