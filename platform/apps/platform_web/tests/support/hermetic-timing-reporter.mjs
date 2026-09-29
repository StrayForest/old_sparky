import fs from "node:fs";
import path from "node:path";

const STATUS_KEYS = ["passed", "failed", "timedOut", "skipped", "interrupted", "other"];

function emptyProject(name) {
  return {
    name,
    tests: 0,
    duration_ms: 0,
    status_counts: Object.fromEntries(STATUS_KEYS.map((key) => [key, 0])),
  };
}

function statusKey(status) {
  return STATUS_KEYS.includes(status) ? status : "other";
}

/**
 * Writes a bounded, title-free project timing summary when the hermetic
 * runner provides PLATFORM_WEB_TEST_TIMING_PATH. Test titles, source paths,
 * attachments and stdout are deliberately excluded from this report; those
 * remain in failure-only Playwright diagnostics.
 */
class HermeticTimingReporter {
  constructor() {
    this.outputPath = process.env.PLATFORM_WEB_TEST_TIMING_PATH;
    this.startedAt = Date.now();
    this.projects = new Map();
    this.totalTests = 0;
    this.statusCounts = Object.fromEntries(STATUS_KEYS.map((key) => [key, 0]));
  }

  onBegin(...args) {
    const suite = args[1];
    this.startedAt = Date.now();
    for (const test of suite.allTests()) {
      const projectName = test.parent?.project?.()?.name || "unknown";
      if (!this.projects.has(projectName)) {
        this.projects.set(projectName, emptyProject(projectName));
      }
    }
  }

  onTestEnd(test, result) {
    const projectName = test.parent?.project?.()?.name || "unknown";
    const project = this.projects.get(projectName) || emptyProject(projectName);
    this.projects.set(projectName, project);
    const key = statusKey(result.status);
    project.tests += 1;
    project.duration_ms += Number.isFinite(result.duration) ? result.duration : 0;
    project.status_counts[key] += 1;
    this.totalTests += 1;
    this.statusCounts[key] += 1;
  }

  onEnd(result) {
    if (!this.outputPath) {
      return;
    }
    const payload = {
      schema: 1,
      contour: "playwright",
      status: result.status,
      duration_ms: Math.max(0, Date.now() - this.startedAt),
      tests: this.totalTests,
      status_counts: this.statusCounts,
      projects: [...this.projects.values()]
        .sort((left, right) => left.name.localeCompare(right.name))
        .map((project) => ({
          ...project,
          duration_ms: Math.max(0, Math.round(project.duration_ms)),
        })),
    };
    fs.mkdirSync(path.dirname(this.outputPath), { recursive: true });
    fs.writeFileSync(this.outputPath, `${JSON.stringify(payload)}\n`, {
      encoding: "utf8",
      mode: 0o600,
    });
  }

  printsToStdio() {
    return false;
  }
}

export default HermeticTimingReporter;
