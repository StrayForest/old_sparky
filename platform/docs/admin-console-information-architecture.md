# Admin console information architecture

- Status: Active design reference
- Owner: Platform web
- Last reviewed: 2026-09-01

## Product objective

`/platform-ops` is an internal operations product for answering three
questions quickly:

1. Is the platform healthy and growing?
2. Which user, tournament or match needs an operator decision now?
3. What exact, auditable action can the operator take on that resource?

The console is not a second public product and is not a generic database CRUD
surface. Authentication, application RBAC and the existing `/admin` API remain
the security boundary.

## Design audit

The previous UI had four structural problems:

- Monitoring, resource management, preprod cleanup and recovery controls were
  presented as one long surface.
- The dashboard promoted implementation state such as roster assignment into
  top-level metrics without explaining the business consequence.
- User deletion lived outside the user management context, so the operator
  could not discover it while inspecting a user.
- Important actions were hidden behind dense inspector panels with inconsistent
  hierarchy, terminology and visual weight.

The replacement uses a persistent scope/navigation shell, a business-first
overview, resource tables with search and filters, and a detail view that owns
actions for the selected resource.

This follows established dashboard patterns: Stripe puts business analytics
and shortcuts in Home, Vercel separates team/project scope and keeps common
operational areas in navigation, and Shopify treats analytics as a
time-comparable dashboard of configurable metrics. References:

- <https://docs.stripe.com/dashboard/basics>
- <https://vercel.com/academy/optimize-your-vercel-account/tour-the-dashboard>
- <https://help.shopify.com/en/manual/reports-and-analytics/shopify-reports/overview-dashboard>

## Information architecture

### Overview

The default screen is a decision dashboard, not a database summary.

- `North-star strip`: active users, player-profile activation, active
  tournaments and tournament completion.
- `Attention queue`: tournaments with unresolved matches, automation failures or
  a lifecycle inconsistency. Each row links to the tournament detail.
- `Player distribution`: all Deadlock profiles by rank and active tournament
  participants by rank. Counts and percentages answer who the product serves.
- `Funnel`: users → player profiles → Deadlock profiles → tournament
  participants. The funnel exposes activation gaps rather than roster internals.
- `Engagement`: new users, tournament registrations, tournaments and matches
  created over the available activity window.
- `Trust and access`: verified accounts, Steam-linked accounts, account states
  and a direct path to recent audit activity.

The dashboard does not show team assignment as a headline KPI. Assignment is an
implementation and tournament-operations detail, visible only in a tournament
detail when it affects a roster decision.

### Users

Users is the owner of account-level actions and support investigation.

- Search by display name, email or ID.
- Table columns: user, Steam identity state, account state, role and creation
  date.
- Detail view: identity, Steam/password state, roles, tournament credits and
  account actions. Rank distribution and tournament activity live in Analytics
  and the tournament workspace respectively.
- User deletion is in the selected user detail under a collapsed danger zone.
  It is superadmin-only, requires exact confirmation and a reason, and is never
  presented as a global toolbar action.

### Tournaments

Tournaments is the owner of lifecycle and incident operations.

- Search and filter by lifecycle, visibility and attention state.
- Table columns: tournament, lifecycle, participants, match progress and
  attention reason.
- Detail view tabs:
  - `Summary`: lifecycle, organizer, schedule, participant and match outcome.
  - `Roster`: current authoritative team state and safe domain commands.
  - `Bracket`: stable team identity, current match counts and a link to the
    public bracket.
  - `Audit`: evidence for the selected resource in the global audit surface.
  - `Recovery`: status/visibility override and deletion, only when the server
    capability model allows it.
- No automatic bracket regeneration or silent result reset is offered by the
  UI.

### Analytics

Analytics is a deeper read-only view for product decisions. It reuses the
overview snapshot and does not create a new analytics database in the frontend
phase.

- User activation and verification.
- Deadlock rank mix and profile coverage.
- Tournament demand and completion.
- Match throughput and completion.
- Workflow/automation reliability, separated from product KPIs.
- Audit and recent activity windows.

Historical retention beyond the source tables and audit/activity window is a
separate data-platform decision. It must not be faked by treating mutable
`updated_at` values as event history.

### Audit log

Audit is a searchable evidence surface, not an action surface.

- Filter by actor, action, resource type and time window.
- Show compact before/after context first.
- Keep raw structured payload available in the detail view.
- Link resource identifiers back to Users or Tournaments when the target is
  known.

### Preprod and recovery

Preprod cleanup remains isolated from production operations. The section shows
test-run status, fixture volume and report metrics; cleanup is a clearly
labelled superadmin action with exact confirmation and audit reason.

## Business metric definitions

The first release uses metrics that can be derived from authoritative current
tables without adding write load or a new event pipeline:

| Metric | Business question | Source |
| --- | --- | --- |
| Active users | How many accounts are usable now? | `users.status` |
| Verified users | How much account activation is complete? | `users.email_verified_at` |
| Steam-linked users | How much of the audience is connected to the game identity? | `external_identities` |
| Player-profile activation | How many users are ready to be represented in the product? | `player_profiles`, `deadlock_profiles` |
| Rank distribution | Which skill segments does the product serve? | `deadlock_profiles.rank` |
| Active participant rank mix | Which skill segments are currently entering tournaments? | active participants joined to `deadlock_profiles` |
| Active tournaments | How much current demand is in flight? | `tournaments.status` |
| Tournament completion | Does a created competition reach a result? | tournament and match status |
| Match completion | Is competition progressing or stuck? | `tournament_matches.status` |
| Automation failures | Is the platform creating operator/support cost? | tournament automation failure fields |
| Recent activity | Is usage and supply changing over time? | bounded created-at activity and audit events |

Counts such as `assigned_participants` and `rostered_members_total` remain
available to the tournament operations view because they help resolve a roster
incident. They are not presented as product-health KPIs.

## Interaction and permission rules

- Every page has a stable scope in the sidebar and a visible page title.
- Every table supports loading, empty, error, retry and no-result states.
- Selecting a row opens its detail context; actions are not detached from the
  selected resource.
- The UI renders server-provided capabilities and never infers authorization
  from a partial client object.
- Destructive actions are placed at the bottom of the resource detail, require
  explicit confirmation and preserve existing reason/audit requirements.
- Mutation failures show the server's domain error. The UI does not silently
  retry a mutation on a newer state.
- Desktop uses a two-column list/detail workspace; mobile switches to a single
  column with an explicit back-to-list control.

## Data and performance boundary

The analytics read model is a bounded, read-only aggregation over PostgreSQL.
It does not mutate workflow rows, assignment snapshots, Redis projections or
audit data. Existing list pagination, request cancellation and capability DTOs
remain the performance and correctness boundaries for operational screens.

If product decisions later require cohort retention, conversion by date or
historical rank movement, add a reviewed event/analytics owner rather than
expanding this page with expensive unbounded queries.
