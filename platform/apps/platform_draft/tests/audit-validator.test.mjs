import assert from "node:assert/strict";
import test from "node:test";

import { validateNpmAuditReport } from "../tools/validate_npm_audit_report.mjs";

function validReport() {
  return {
    auditReportVersion: 2,
    metadata: {
      vulnerabilities: {
        info: 0,
        low: 0,
        moderate: 0,
        high: 0,
        critical: 0,
        total: 0
      }
    }
  };
}

test("accepts a clean npm v2 audit report and preserves exit semantics", () => {
  const result = validateNpmAuditReport(validReport(), 0);
  assert.deepEqual(result, {
    ok: true,
    summary: "info=0 low=0 moderate=0 high=0 critical=0 total=0"
  });
});

test("rejects malformed vulnerability counters fail-closed", () => {
  const mutations = [
    ["negative", (report) => { report.metadata.vulnerabilities.high = -1; }],
    ["boolean", (report) => { report.metadata.vulnerabilities.high = true; }],
    ["string", (report) => { report.metadata.vulnerabilities.high = "0"; }],
    ["missing", (report) => { delete report.metadata.vulnerabilities.critical; }],
    ["inconsistent", (report) => { report.metadata.vulnerabilities.total = 1; }],
    ["wrong version", (report) => { report.auditReportVersion = 1; }],
  ];
  for (const [name, mutate] of mutations) {
    const report = validReport();
    mutate(report);
    assert.equal(validateNpmAuditReport(report, 0).ok, false, name);
  }
});

test("rejects nonzero audit and timeout statuses even with a clean report", () => {
  for (const status of [1, 124, 137]) {
    const result = validateNpmAuditReport(validReport(), status);
    assert.equal(result.ok, false, String(status));
  }
});

test("preserves high and critical threshold semantics", () => {
  for (const field of ["high", "critical"]) {
    const report = validReport();
    report.metadata.vulnerabilities[field] = 1;
    report.metadata.vulnerabilities.total = 1;
    assert.equal(validateNpmAuditReport(report, 0).ok, false, field);
  }
  const moderate = validReport();
  moderate.metadata.vulnerabilities.moderate = 1;
  moderate.metadata.vulnerabilities.total = 1;
  assert.equal(validateNpmAuditReport(moderate, 0).ok, true);
});
