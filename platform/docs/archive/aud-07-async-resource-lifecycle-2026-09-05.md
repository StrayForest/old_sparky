# AUD-07 async resource lifecycle closure — 2026-09-05

- Status: Resolved
- Owner: Backend/platform infrastructure maintainers
- Source finding: [`pre-release-audit-report-2026-09-05.md`](pre-release-audit-report-2026-09-05.md#aud-07--backend-test-process-emits-unclosed-redisasyncpg-resources-p2-test-hygiene)
- Primary implementation commit: `ce9b2017ff7353c5028dcb657c92fe54d3e6ba72`

## Resolution

The warnings came from process-global asynchronous resources surviving beyond
the `IsolatedAsyncioTestCase` event loop that created them. Shared Redis clients
were silently replaced when another loop appeared, and cross-loop SQLAlchemy
engine disposal detached the asyncpg pool with `close=False` instead of closing
its connections.

The infrastructure now rejects cross-loop reuse or disposal. The common async
test base registers a finalizer before test execution and closes shared Redis
clients followed by the SQLAlchemy engine on the owning loop. All backend async
test classes use that base, and a source-contract regression rejects future
direct inheritance from the stdlib isolated async test case. Focused Redis and
database lifecycle tests cover same-loop closure and cross-loop rejection.
The shared test base retains asyncio debug mode with a five-second callback
threshold. That removes routine 100 ms PostgreSQL integration-step diagnostics
while preserving a signal for an actual test-loop stall; `ResourceWarning`
visibility is unchanged.

No schema, persistent data, permission or public API contract changed. API
lifespan shutdown and the worker's persistent event-loop shutdown retain the
same resource order and now always perform full pool closure.

## Evidence

- Local canonical backend discovery ran 1,001 tests with
  `ResourceWarning` converted to an error and completed with `OK`; the filtered
  output contained no resource, unclosed-connection or unclosed-transport
  marker and no slow-callback diagnostic above the five-second stall threshold.
- Exact-SHA
  [`Platform security and build` run 33968720445](https://github.com/StrayForest/old_sparky/actions/runs/33968720445)
  passed all required jobs for the implementation commit. Its backend job ran
  1,000 tests and the complete job log contained zero `ResourceWarning`,
  unclosed Redis connection, unclosed asyncio transport or ignored-finalizer
  markers.
- Python quality, migration scenarios, security, documentation,
  verification-contract, web-quality and Web hermetic jobs passed in the same
  exact-SHA workflow.

The active security tracker contains only unresolved findings, so AUD-07 is not
listed there. This dated record preserves the original observation and its
closure evidence without restoring it to the active queue.
