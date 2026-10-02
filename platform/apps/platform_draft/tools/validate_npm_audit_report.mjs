import { readFileSync, statSync } from "node:fs";
import { pathToFileURL } from "node:url";

const VULNERABILITY_FIELDS = Object.freeze([
  "info",
  "low",
  "moderate",
  "high",
  "critical",
  "total"
]);
const SEVERITY_FIELDS = VULNERABILITY_FIELDS.slice(0, -1);

function fail(message) {
  console.error(`Draft npm audit failed: ${message}`);
  return 1;
}

export function validateNpmAuditReport(report, auditExit) {
  if (!Number.isSafeInteger(auditExit) || auditExit < 0) {
    return { ok: false, message: "audit exit code is invalid" };
  }
  if (
    !report ||
    typeof report !== "object" ||
    Array.isArray(report) ||
    report.auditReportVersion !== 2
  ) {
    return { ok: false, message: "missing or unsupported npm v2 JSON report" };
  }
  const vulnerabilities = report.metadata?.vulnerabilities;
  if (!vulnerabilities || typeof vulnerabilities !== "object" || Array.isArray(vulnerabilities)) {
    return { ok: false, message: "report has no valid vulnerability summary" };
  }
  const actualFields = Object.keys(vulnerabilities).sort();
  const expectedFields = [...VULNERABILITY_FIELDS].sort();
  if (actualFields.join("\0") !== expectedFields.join("\0")) {
    return { ok: false, message: "report vulnerability fields are not closed" };
  }
  for (const field of VULNERABILITY_FIELDS) {
    const value = vulnerabilities[field];
    if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
      return { ok: false, message: "report vulnerability counts are invalid" };
    }
  }
  const severityTotal = SEVERITY_FIELDS.reduce(
    (total, field) => total + vulnerabilities[field],
    0,
  );
  if (!Number.isSafeInteger(severityTotal) || vulnerabilities.total !== severityTotal) {
    return { ok: false, message: "report vulnerability counts are inconsistent" };
  }
  const summary = VULNERABILITY_FIELDS
    .map((field) => `${field}=${vulnerabilities[field]}`)
    .join(" ");
  if (auditExit !== 0) {
    return {
      ok: false,
      message: auditExit === 124 || auditExit === 137
        ? "30-second audit deadline expired"
        : "npm returned a non-zero status",
      summary,
    };
  }
  if (vulnerabilities.high > 0 || vulnerabilities.critical > 0) {
    return { ok: false, message: "high or critical vulnerabilities remain", summary };
  }
  return { ok: true, summary };
}

export function main(argumentsList = process.argv.slice(2)) {
  const [reportPath, rawExit, stderrPath] = argumentsList;
  if (!reportPath || !/^(0|[1-9][0-9]*)$/u.test(rawExit ?? "")) {
    return fail("audit invocation arguments are invalid");
  }
  const auditExit = Number(rawExit);
  let report;
  try {
    report = JSON.parse(readFileSync(reportPath, "utf8"));
  } catch {
    return fail("missing or malformed JSON report");
  }
  const validation = validateNpmAuditReport(report, auditExit);
  let stderrBytes = 0;
  try {
    stderrBytes = statSync(stderrPath).size;
  } catch {
    // The report and exit status are authoritative; diagnostics stay bounded and private.
  }
  if (validation.summary) {
    console.log(`Draft npm audit: ${validation.summary} exit=${auditExit} stderr_bytes=${stderrBytes}`);
  }
  if (!validation.ok) {
    return fail(validation.message);
  }
  return 0;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  process.exitCode = main();
}
