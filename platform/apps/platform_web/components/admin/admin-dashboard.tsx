"use client";

import {
  Activity,
  AlertTriangle,
  BarChart3,
  Bot,
  CheckCircle2,
  Clock3,
  Gauge,
  GitBranch,
  Layers3,
  ListChecks,
  ShieldCheck,
  TrendingUp,
  Trophy,
  UserCheck,
  Users
} from "lucide-react";
import type {
  PlatformAdminActivityPoint,
  PlatformAdminAnalyticsBucket,
  PlatformAdminOverview
} from "@/lib/platform-types";
import { useI18n } from "@/components/i18n-provider";

type AdminDashboardProps = {
  overview: PlatformAdminOverview | null;
  formatDate: (value: string) => string;
  onNavigate: (tab: "tournaments" | "users" | "preprod" | "audit") => void;
};

const rankColors = ["#f5c04f", "#df9b42", "#bf7341", "#9b5745", "#75515a", "#53637b", "#3f7184"];

export function AdminDashboard({ overview, formatDate, onNavigate }: AdminDashboardProps) {
  const { enumLabel, t } = useI18n();
  const analytics = overview?.analytics ?? null;

  if (!analytics) {
    return (
      <section className="admin-dashboard admin-dashboard-empty-state" data-testid="admin-dashboard">
        <div className="admin-dashboard-empty-icon"><BarChart3 size={22} /></div>
        <div>
          <div className="admin-section-kicker">{t("admin.dashboardTitle")}</div>
          <h2>{t("admin.dashboardUnavailableTitle")}</h2>
          <p>{t("admin.dashboardUnavailableCopy")}</p>
        </div>
      </section>
    );
  }

  return (
    <section className="admin-dashboard" data-testid="admin-dashboard">
      <div className="admin-dashboard-intro">
        <div>
          <div className="admin-section-kicker"><BarChart3 size={15} />{t("admin.dashboardTitle")}</div>
          <h2>{t("admin.dashboardHeading")}</h2>
          <p>{t("admin.dashboardCopy")}</p>
        </div>
        <div className="admin-dashboard-updated">
          <span>{t("admin.dashboardUpdated")}</span>
          <strong>{formatDate(analytics.generated_at)}</strong>
          <small>{t("admin.dashboardLiveQuery")}</small>
        </div>
      </div>

      <div className="admin-dashboard-kpis">
        <DashboardKpi icon={<Users size={19} />} label={t("admin.dashboardActiveUsers")} value={analytics.active_users} detail={`${analytics.users_total.toLocaleString("ru-RU")} ${t("admin.dashboardTotalSuffix")}`} />
        <DashboardKpi icon={<UserCheck size={19} />} label={t("admin.dashboardParticipants")} value={analytics.active_participants} detail={`${analytics.assigned_participants} ${t("admin.dashboardAssigned")}`} />
        <DashboardKpi icon={<Trophy size={19} />} label={t("admin.dashboardActiveTournaments")} value={analytics.active_tournaments} detail={`${analytics.tournaments_total} ${t("admin.dashboardTotalSuffix")}`} />
        <DashboardKpi icon={<GitBranch size={19} />} label={t("admin.dashboardMatches")} value={analytics.matches_total} detail={`${analytics.live_matches} ${t("admin.dashboardLive")}`} />
        <DashboardKpi danger={analytics.tournaments_attention_total > 0} icon={<AlertTriangle size={19} />} label={t("admin.dashboardAttention")} value={analytics.tournaments_attention_total} detail={t("admin.dashboardNeedsReview")} />
        <DashboardKpi danger={analytics.tournaments_with_automation_failures > 0} icon={<Bot size={19} />} label={t("admin.dashboardAutomation")} value={analytics.automation_failures_total} detail={`${analytics.tournaments_with_automation_failures} ${t("admin.dashboardAffectedTournaments")}`} />
      </div>

      <div className="admin-dashboard-grid admin-dashboard-grid-primary">
        <AnalyticsPanel className="admin-dashboard-ranks" icon={<TrendingUp size={17} />} title={t("admin.dashboardRanksTitle")} copy={t("admin.dashboardRanksCopy")}>
          <div className="admin-rank-summary">
            <strong>{analytics.deadlock_profiles_total.toLocaleString("ru-RU")}</strong>
            <span>{t("admin.dashboardProfilesTracked")}</span>
            <small>{analytics.participant_profile_coverage_percent.toLocaleString("ru-RU")}% {t("admin.dashboardParticipantCoverage")}</small>
          </div>
          <RankDistribution buckets={analytics.rank_distribution} />
          <div className="admin-panel-divider" />
          <div className="admin-subpanel-label">{t("admin.dashboardActiveParticipantRanks")}</div>
          <RankDistribution buckets={analytics.active_participant_rank_distribution} compact />
        </AnalyticsPanel>

        <AnalyticsPanel icon={<Gauge size={17} />} title={t("admin.dashboardPlatformHealth")} copy={t("admin.dashboardPlatformHealthCopy")}>
          <div className="admin-health-grid">
            <HealthStat label={t("admin.dashboardUsersVerified")} value={analytics.verified_users} total={analytics.users_total} suffix={t("admin.dashboardPeople")} />
            <HealthStat label={t("admin.dashboardSteamLinked")} value={analytics.steam_linked_users} total={analytics.users_total} suffix={t("admin.dashboardPeople")} />
            <HealthStat label={t("admin.dashboardPlayerProfiles")} value={analytics.player_profiles_total} total={analytics.users_total} suffix={t("admin.dashboardPeople")} />
            <HealthStat label={t("admin.dashboardRostered")} value={analytics.assigned_participants} total={analytics.active_participants} suffix={t("admin.dashboardParticipantsShort")} />
            <HealthStat label={t("admin.dashboardUnassigned")} value={analytics.unassigned_participants} total={analytics.active_participants} suffix={t("admin.dashboardParticipantsShort")} danger={analytics.unassigned_participants > 0} />
          </div>
          <div className="admin-dashboard-inline-stats">
            <InlineStat label={t("admin.dashboardPublic")} value={analytics.public_tournaments} />
            <InlineStat label={t("admin.dashboardInviteOnly")} value={analytics.invite_only_tournaments} />
            <InlineStat label={t("admin.dashboardAvgParticipants")} value={analytics.average_active_participants_per_tournament.toLocaleString("ru-RU")} />
            <InlineStat label={t("admin.dashboardTeams")} value={analytics.teams_total} />
            <InlineStat label={t("admin.dashboardRosteredMembers")} value={analytics.rostered_members_total} />
          </div>
          <div className="admin-panel-divider" />
          <div className="admin-dashboard-mini-grid">
            <div><div className="admin-subpanel-label">{t("admin.dashboardUserStates")}</div><DistributionList buckets={analytics.user_status_distribution} label={enumLabel} compact /></div>
            <div><div className="admin-subpanel-label">{t("admin.dashboardParticipantStates")}</div><DistributionList buckets={analytics.participant_status_distribution} label={enumLabel} compact /></div>
          </div>
        </AnalyticsPanel>
      </div>

      <div className="admin-dashboard-grid admin-dashboard-grid-secondary">
        <AnalyticsPanel icon={<Trophy size={17} />} title={t("admin.dashboardTournamentFlow")} copy={t("admin.dashboardTournamentFlowCopy")}>
          <DistributionList buckets={analytics.tournament_status_distribution} label={enumLabel} />
          <div className="admin-panel-divider" />
          <div className="admin-dashboard-section-row">
            <div><span>{t("admin.dashboardVisibility")}</span><strong>{analytics.public_tournaments + analytics.invite_only_tournaments}</strong></div>
            <DistributionList buckets={analytics.tournament_visibility_distribution} label={enumLabel} compact />
          </div>
        </AnalyticsPanel>

        <AnalyticsPanel icon={<GitBranch size={17} />} title={t("admin.dashboardMatchFlow")} copy={t("admin.dashboardMatchFlowCopy")}>
          <DistributionList buckets={analytics.match_status_distribution} label={enumLabel} />
          <div className="admin-panel-footer-stat">
            <span>{t("admin.dashboardCompletionRate")}</span>
            <strong>{percentage(analytics.completed_matches, analytics.matches_total)}%</strong>
          </div>
        </AnalyticsPanel>

        <AnalyticsPanel icon={<Layers3 size={17} />} title={t("admin.dashboardWorkflow")} copy={t("admin.dashboardWorkflowCopy")}>
          <WorkflowRow activeLabel={t("admin.dashboardActive")} label={t("admin.dashboardAssignments")} active={analytics.current_assignment_runs} total={analytics.assignment_runs_total} buckets={analytics.assignment_status_distribution} labelBucket={enumLabel} />
          <WorkflowRow activeLabel={t("admin.dashboardActive")} label={t("admin.dashboardReadyChecks")} active={analytics.active_ready_rounds} total={analytics.ready_rounds_total} buckets={analytics.ready_round_status_distribution} labelBucket={enumLabel} />
          <WorkflowRow activeLabel={t("admin.dashboardActive")} label={t("admin.dashboardCaptainRounds")} active={analytics.active_captain_rounds} total={analytics.captain_rounds_total} buckets={analytics.captain_round_status_distribution} labelBucket={enumLabel} />
          <div className="admin-panel-footer-stat">
            <span>{t("admin.dashboardLockedRosters")}</span>
            <strong>{analytics.locked_rosters.toLocaleString("ru-RU")}</strong>
          </div>
        </AnalyticsPanel>
      </div>

      <AnalyticsPanel className="admin-dashboard-activity-panel" icon={<Activity size={17} />} title={t("admin.dashboardActivityTitle")} copy={t("admin.dashboardActivityCopy")}>
        <ActivityChart activity={analytics.activity} />
      </AnalyticsPanel>

      <div className="admin-dashboard-bottom-grid">
        <AnalyticsPanel icon={<ListChecks size={17} />} title={t("admin.dashboardAuditTitle")} copy={t("admin.dashboardAuditCopy")}>
          <div className="admin-audit-window-grid">
            <div><strong>{analytics.audit_events_24h.toLocaleString("ru-RU")}</strong><span>{t("admin.dashboardLast24h")}</span></div>
            <div><strong>{analytics.audit_events_7d.toLocaleString("ru-RU")}</strong><span>{t("admin.dashboardLast7d")}</span></div>
            <div><strong>{analytics.audit_events_total.toLocaleString("ru-RU")}</strong><span>{t("admin.dashboardAllTime")}</span></div>
          </div>
          <button className="admin-dashboard-link" type="button" onClick={() => onNavigate("audit")}>{t("admin.dashboardOpenAudit")} <span>→</span></button>
        </AnalyticsPanel>
        <AnalyticsPanel icon={<ShieldCheck size={17} />} title={t("admin.dashboardOperationsTitle")} copy={t("admin.dashboardOperationsCopy")}>
          <div className="admin-quick-actions">
            <button type="button" onClick={() => onNavigate("tournaments")}><Trophy size={16} /><span>{t("admin.dashboardOpenTournaments")}</span><span>→</span></button>
            <button type="button" onClick={() => onNavigate("users")}><Users size={16} /><span>{t("admin.dashboardOpenUsers")}</span><span>→</span></button>
            <button type="button" onClick={() => onNavigate("preprod")}><Clock3 size={16} /><span>{t("admin.dashboardOpenPreprod")}</span><span>→</span></button>
          </div>
        </AnalyticsPanel>
      </div>
    </section>
  );
}

function DashboardKpi({
  danger = false,
  detail,
  icon,
  label,
  value
}: {
  danger?: boolean;
  detail: string;
  icon: React.ReactNode;
  label: string;
  value: number;
}) {
  return (
    <article className={danger ? "admin-dashboard-kpi danger" : "admin-dashboard-kpi"}>
      <div className="admin-dashboard-kpi-icon">{icon}</div>
      <div>
        <span>{label}</span>
        <strong>{value.toLocaleString("ru-RU")}</strong>
        <small>{detail}</small>
      </div>
    </article>
  );
}

function AnalyticsPanel({
  children,
  className = "",
  copy,
  icon,
  title
}: {
  children: React.ReactNode;
  className?: string;
  copy: string;
  icon: React.ReactNode;
  title: string;
}) {
  return (
    <section className={`admin-analytics-panel ${className}`.trim()}>
      <header className="admin-analytics-panel-head">
        <div><span className="admin-analytics-panel-icon">{icon}</span><h3>{title}</h3></div>
        <p>{copy}</p>
      </header>
      <div className="admin-analytics-panel-body">{children}</div>
    </section>
  );
}

function RankDistribution({ buckets, compact = false }: { buckets: PlatformAdminAnalyticsBucket[]; compact?: boolean }) {
  const { t } = useI18n();
  if (buckets.length === 0) {
    return <div className={compact ? "admin-chart-empty compact" : "admin-chart-empty"}>{t("admin.dashboardNoData")}</div>;
  }
  const max = Math.max(...buckets.map((bucket) => bucket.count), 1);
  return (
    <div className={compact ? "admin-rank-list compact" : "admin-rank-list"}>
      {buckets.map((bucket, index) => (
        <div className="admin-rank-row" key={bucket.key}>
          <span className="admin-rank-label">{bucket.key}</span>
          <span className="admin-rank-track"><span style={{ width: `${Math.max(4, (bucket.count / max) * 100)}%`, background: rankColors[index % rankColors.length] }} /></span>
          <strong>{bucket.count.toLocaleString("ru-RU")}</strong>
          <small>{bucket.percentage.toLocaleString("ru-RU")} %</small>
        </div>
      ))}
    </div>
  );
}

function DistributionList({
  buckets,
  compact = false,
  label
}: {
  buckets: PlatformAdminAnalyticsBucket[];
  compact?: boolean;
  label: (value: string) => string;
}) {
  const { t } = useI18n();
  if (buckets.length === 0) {
    return <div className="admin-chart-empty">{t("admin.dashboardNoData")}</div>;
  }
  return (
    <div className={compact ? "admin-distribution-list compact" : "admin-distribution-list"}>
      {buckets.map((bucket) => (
        <div className="admin-distribution-row" key={bucket.key}>
          <span>{label(bucket.key)}</span>
          <span className="admin-distribution-track"><span style={{ width: `${Math.max(bucket.count > 0 ? 4 : 0, bucket.percentage)}%` }} /></span>
          <strong>{bucket.count.toLocaleString("ru-RU")}</strong>
          <small>{bucket.percentage.toLocaleString("ru-RU")} %</small>
        </div>
      ))}
    </div>
  );
}

function HealthStat({ danger = false, label, suffix, total, value }: { danger?: boolean; label: string; suffix: string; total: number; value: number }) {
  const ratio = percentage(value, total);
  return (
    <div className={danger ? "admin-health-stat danger" : "admin-health-stat"}>
      <div><span>{label}</span><strong>{value.toLocaleString("ru-RU")}</strong></div>
      <small>{suffix} · {ratio}%</small>
      <span className="admin-health-track"><span style={{ width: `${Math.max(value > 0 ? 4 : 0, ratio)}%` }} /></span>
    </div>
  );
}

function InlineStat({ label, value }: { label: string; value: number | string }) {
  return <div><span>{label}</span><strong>{value}</strong></div>;
}

function WorkflowRow({ active, activeLabel, buckets, label, labelBucket, total }: { active: number; activeLabel: string; buckets: PlatformAdminAnalyticsBucket[]; label: string; labelBucket: (value: string) => string; total: number }) {
  return (
    <div className="admin-workflow-row">
      <div className="admin-workflow-row-head"><span>{label}</span><strong>{active} <small>{activeLabel}</small> / {total}</strong></div>
      <DistributionList buckets={buckets} compact label={labelBucket} />
    </div>
  );
}

function ActivityChart({ activity }: { activity: PlatformAdminActivityPoint[] }) {
  const { t } = useI18n();
  const max = Math.max(...activity.map((point) => point.users + point.participants + point.tournaments + point.matches), 1);
  return (
    <div className="admin-activity-chart">
      <div className="admin-activity-legend">
        <span className="users">{t("admin.dashboardActivityUsers")}</span>
        <span className="participants">{t("admin.dashboardActivityParticipants")}</span>
        <span className="tournaments">{t("admin.dashboardActivityTournaments")}</span>
        <span className="matches">{t("admin.dashboardActivityMatches")}</span>
      </div>
      <div className="admin-activity-bars">
        {activity.map((point) => {
          const total = point.users + point.participants + point.tournaments + point.matches;
          return (
            <div className="admin-activity-day" key={point.date} title={`${point.date}: ${total}`}>
              <div className="admin-activity-stack" style={{ height: `${Math.max(total > 0 ? 8 : 2, (total / max) * 100)}%` }}>
                <span className="users" style={{ flex: point.users }} />
                <span className="participants" style={{ flex: point.participants }} />
                <span className="tournaments" style={{ flex: point.tournaments }} />
                <span className="matches" style={{ flex: point.matches }} />
              </div>
              <small>{point.date.slice(5)}</small>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function percentage(value: number, total: number): number {
  return total > 0 ? Math.round((value / total) * 100) : 0;
}
