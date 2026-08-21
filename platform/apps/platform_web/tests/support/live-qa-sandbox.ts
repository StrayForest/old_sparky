import { expect } from "@playwright/test";
import fs from "node:fs";

type SandboxSnapshot = {
  browserMainCount: number;
  browserMainUidMatches: boolean;
  browserMainDisablesSandbox: boolean;
  sandboxedChromiumProcessObserved: boolean;
};

const LIVE_QA_CGROUP = "/system.slice/oldsparky-liveqa-browser.service";

export async function assertLiveQaChromiumSandbox(
  context: import("@playwright/test").BrowserContext,
  browserName: string,
) {
  if (browserName !== "chromium") {
    return;
  }
  const page = await context.newPage();
  try {
    await page.goto(process.env.PLAYWRIGHT_LIVE_BASE_URL ?? "about:blank");
    await expect.poll(
      inspectChromiumCgroup,
      {
        message: "Chromium must run as the QA uid with an active process sandbox",
        timeout: 10_000,
      },
    ).toEqual({
      browserMainCount: 1,
      browserMainUidMatches: true,
      browserMainDisablesSandbox: false,
      sandboxedChromiumProcessObserved: true,
    });
  } finally {
    await page.close();
  }
}

function inspectChromiumCgroup(): SandboxSnapshot {
  const currentUid = process.geteuid?.() ?? 0;
  const browserMainUids: number[] = [];
  let browserMainDisablesSandbox = false;
  let sandboxedChromiumProcessObserved = false;
  for (const pid of liveQaCgroupProcessIds()) {
    try {
      const argumentsList = fs.readFileSync(`/proc/${pid}/cmdline`)
        .toString("utf8")
        .split("\0")
        .filter(Boolean);
      const status = fs.readFileSync(`/proc/${pid}/status`, "utf8");
      const processUid = statusNumber(status, "Uid");
      const processType = argumentsList.find((argument) => argument.startsWith("--type="));
      if (
        argumentsList.includes("--remote-debugging-pipe")
        && !processType
      ) {
        browserMainUids.push(processUid);
        browserMainDisablesSandbox ||= argumentsList.some((argument) => (
          argument === "--no-sandbox" || argument === "--disable-setuid-sandbox"
        ));
      }
      // Once the SUID sandbox has made a Chromium child non-dumpable, Linux
      // can expose only argv[0] to this same-UID observer. Kernel status stays
      // readable, so prove the sandbox from the exact cgroup, Chromium name,
      // nested PID namespace, no-new-privileges, and seccomp filter instead of
      // trusting a hidden --type=renderer argument.
      const namespacePids = statusNumbers(status, "NSpid");
      if (
        statusName(status).startsWith("chrome")
        && processUid === currentUid
        && namespacePids.length >= 2
        && namespacePids.at(-1) === 1
        && statusNumber(status, "NoNewPrivs") === 1
        && statusNumber(status, "Seccomp") === 2
      ) {
        sandboxedChromiumProcessObserved = true;
      }
    } catch {
      // Chromium processes can exit between /proc enumeration and inspection.
    }
  }
  return {
    browserMainCount: browserMainUids.length,
    browserMainUidMatches: (
      currentUid !== 0
      && browserMainUids.length > 0
      && browserMainUids.every((uid) => uid === currentUid)
    ),
    browserMainDisablesSandbox,
    sandboxedChromiumProcessObserved,
  };
}

function liveQaCgroupProcessIds(): number[] {
  const memberships = fs.readFileSync("/proc/self/cgroup", "utf8").split("\n");
  const unified = memberships.find((membership) => membership.startsWith("0::"));
  const cgroup = unified?.slice(3);
  if (cgroup !== LIVE_QA_CGROUP) {
    throw new Error("Production Chromium is outside the dedicated live QA cgroup.");
  }
  return fs.readFileSync(`/sys/fs/cgroup${cgroup}/cgroup.procs`, "utf8")
    .split("\n")
    .filter((pid) => /^[1-9][0-9]*$/u.test(pid))
    .map(Number);
}

function statusNumber(status: string, field: "Uid" | "NoNewPrivs" | "Seccomp"): number {
  const match = status.match(new RegExp(`^${field}:\\s+([0-9]+)`, "mu"));
  if (!match) {
    throw new Error(`Missing ${field} in process status.`);
  }
  return Number(match[1]);
}

function statusName(status: string): string {
  const match = status.match(/^Name:\s+(\S+)/mu);
  if (!match) {
    throw new Error("Missing Name in process status.");
  }
  return match[1];
}

function statusNumbers(status: string, field: "NSpid"): number[] {
  const match = status.match(new RegExp(`^${field}:\\s+([0-9\\s]+)$`, "mu"));
  if (!match) {
    throw new Error(`Missing ${field} in process status.`);
  }
  return match[1].trim().split(/\s+/u).map(Number);
}
