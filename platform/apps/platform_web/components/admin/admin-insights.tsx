"use client";

import {
  Activity,
  AlertTriangle,
  ArrowUpRight,
  BarChart3,
  CheckCircle2,
  GitBranch,
  ShieldAlert,
  Sparkles,
  Trophy,
  UserRound,
  UsersRound
} from "lucide-react";
import type {
  PlatformAdminActivityPoint,
  PlatformAdminAnalytics,
  PlatformAdminAnalyticsBucket,
  PlatformAdminOverview,
  PlatformAdminTournament
} from "@/lib/platform-types";
import { useI18n } from "@/components/i18n-provider";

export type AdminView = "overview" | "users" | "tournaments" | "analytics" | "audit" | "preprod";

type InsightProps = {
  formatDate: (value: string) => string;
  enumLabel: (value: string | null | undefined) => string;
};

type AnalyticsProps = InsightProps & { analytics: PlatformAdminAnalytics };

type Navigate = (view: AdminView, context?: string) => void;

export function AdminOverview({
  overview,
  tournaments,
  formatDate,
  enumLabel,
  onNavigate
}: InsightProps & { overview: PlatformAdminOverview; tournaments: PlatformAdminTournament[]; onNavigate: Navigate }) {
  const { t } = useI18n();
  const analytics = overview.analytics;
  if (!analytics) {
    return (
      <section className="ops-empty-page" data-testid="admin-dashboard">
        <div className="ops-empty-icon"><BarChart3 size={22} /></div>
        <div>
          <span className="ops-kicker">{t("admin.new.overview")}</span>
          <h2>{t("admin.new.analyticsUnavailableTitle")}</h2>
          <p>{t("admin.new.analyticsUnavailableCopy")}</p>
        </div>
      </section>
    );
  }

  const attentionTournaments = tournaments.filter((item) => (
    item.unfinished_match_count > 0 || Boolean(item.admin_override_warning) || Boolean(item.automation_last_error)
  )).slice(0, 5);
  const profileActivation = ratio(analytics.deadlock_profiles_total, analytics.users_total);
  const tournamentCompletion = ratio(analytics.completed_tournaments, analytics.tournaments_total);
  const matchCompletion = ratio(analytics.completed_matches, analytics.matches_total);

  return (
    <section className="ops-page ops-overview-page" data-testid="admin-dashboard">
      <PageIntro
        eyebrow={t("admin.new.overview")}
        title={t("admin.new.overviewTitle")}
        copy={t("admin.new.overviewCopy")}
        meta={<><span className="ops-live-dot" />{t("admin.new.liveSnapshot")} · {formatDate(analytics.generated_at)}</>}
      />

      <div className="ops-kpi-grid ops-kpi-grid-four">
        <BusinessKpi icon={<UsersRound size={19} />} label={t("admin.new.kpiActiveUsers")} value={analytics.active_users} detail={t("admin.new.kpiOfTotal", { value: analytics.users_total })} tone="violet" />
        <BusinessKpi icon={<Sparkles size={19} />} label={t("admin.new.kpiActivation")} value={`${profileActivation}%`} detail={t("admin.new.kpiProfilesReady", { value: analytics.deadlock_profiles_total })} tone="teal" />
        <BusinessKpi icon={<Trophy size={19} />} label={t("admin.new.kpiActiveTournaments")} value={analytics.active_tournaments} detail={t("admin.new.kpiOfTotal", { value: analytics.tournaments_total })} tone="amber" />
        <BusinessKpi icon={<GitBranch size={19} />} label={t("admin.new.kpiCompletion")} value={`${tournamentCompletion}%`} detail={t("admin.new.kpiMatchesDone", { value: analytics.completed_matches })} tone="green" />
      </div>

      <div className="ops-section-heading">
        <div><span className="ops-kicker">{t("admin.new.decisionLayer")}</span><h2>{t("admin.new.whatNeedsAttention")}</h2></div>
        <button className="ops-text-button" type="button" onClick={() => onNavigate("tournaments", "attention")}>
          {t("admin.new.openAllTournaments")} <ArrowUpRight size={15} />
        </button>
      </div>

      <div className="ops-overview-grid ops-overview-grid-main">
        <section className="ops-panel ops-attention-panel">
          <PanelHeader icon={<ShieldAlert size={17} />} title={t("admin.new.attentionTitle")} copy={t("admin.new.attentionCopy")} />
          {attentionTournaments.length > 0 ? (
            <div className="ops-attention-list">
              {attentionTournaments.map((tournament) => (
                <button className="ops-attention-row" key={tournament.id} type="button" onClick={() => onNavigate("tournaments", tournament.slug)}>
                  <span className="ops-attention-status"><AlertTriangle size={15} /></span>
                  <span className="ops-attention-main"><strong>{tournament.name}</strong><small>{attentionReason(tournament, t)}</small></span>
                  <span className="ops-attention-value">{tournament.unfinished_match_count || tournament.automation_failure_count || 0}<ArrowUpRight size={15} /></span>
                </button>
              ))}
            </div>
          ) : (
            <EmptyInline icon={<CheckCircle2 size={19} />} copy={t("admin.new.noAttention")} />
          )}
          <div className="ops-panel-footer"><span>{t("admin.new.attentionCount")}</span><strong>{analytics.tournaments_attention_total}</strong></div>
        </section>

        <section className="ops-panel">
          <PanelHeader icon={<Sparkles size={17} />} title={t("admin.new.activationTitle")} copy={t("admin.new.activationCopy")} />
          <FunnelRow label={t("admin.new.funnelAccounts")} value={analytics.users_total} percent={100} tone="violet" />
          <FunnelRow label={t("admin.new.funnelVerified")} value={analytics.verified_users} percent={ratio(analytics.verified_users, analytics.users_total)} tone="teal" />
          <FunnelRow label={t("admin.new.funnelProfiles")} value={analytics.deadlock_profiles_total} percent={profileActivation} tone="blue" />
          <FunnelRow label={t("admin.new.funnelSteam")} value={analytics.steam_linked_users} percent={ratio(analytics.steam_linked_users, analytics.users_total)} tone="amber" />
          <div className="ops-panel-footer"><span>{t("admin.new.profileCoverage")}</span><strong>{analytics.participant_profile_coverage_percent}%</strong></div>
        </section>
      </div>

      <div className="ops-overview-grid ops-overview-grid-secondary">
        <section className="ops-panel ops-rank-panel">
          <PanelHeader icon={<BarChart3 size={17} />} title={t("admin.new.rankMixTitle")} copy={t("admin.new.rankMixCopy")} action={<button className="ops-text-button" type="button" onClick={() => onNavigate("analytics")}>{t("admin.new.details")} <ArrowUpRight size={15} /></button>} />
          <RankBars buckets={analytics.rank_distribution} />
          <div className="ops-subsection-label">{t("admin.new.activeRankMix")}</div>
          <RankBars buckets={analytics.active_participant_rank_distribution} compact />
        </section>

        <section className="ops-panel">
          <PanelHeader icon={<Trophy size={17} />} title={t("admin.new.tournamentHealthTitle")} copy={t("admin.new.tournamentHealthCopy")} />
          <MetricLine label={t("admin.new.tournamentsCompleted")} value={analytics.completed_tournaments} suffix={`${tournamentCompletion}%`} progress={tournamentCompletion} tone="green" />
          <MetricLine label={t("admin.new.matchesCompleted")} value={analytics.completed_matches} suffix={`${matchCompletion}%`} progress={matchCompletion} tone="teal" />
          <MetricLine label={t("admin.new.matchesLive")} value={analytics.live_matches} suffix={t("admin.new.now")} progress={ratio(analytics.live_matches, analytics.matches_total)} tone="violet" />
          <MetricLine label={t("admin.new.automationFailures")} value={analytics.automation_failures_total} suffix={t("admin.new.failures")} progress={ratio(analytics.tournaments_with_automation_failures, analytics.tournaments_total)} tone={analytics.automation_failures_total > 0 ? "danger" : "green"} />
        </section>

        <section className="ops-panel">
          <PanelHeader icon={<Activity size={17} />} title={t("admin.new.activityTitle")} copy={t("admin.new.activityCopy")} />
          <ActivitySummary activity={analytics.activity} />
          <button className="ops-link-row" type="button" onClick={() => onNavigate("analytics")}><span>{t("admin.new.openAnalytics")}</span><ArrowUpRight size={15} /></button>
        </section>
      </div>

      <div className="ops-quick-links">
        <QuickLink icon={<UserRound size={17} />} label={t("admin.new.quickUsers")} value={analytics.users_total} onClick={() => onNavigate("users")} />
        <QuickLink icon={<Trophy size={17} />} label={t("admin.new.quickTournaments")} value={analytics.tournaments_total} onClick={() => onNavigate("tournaments")} />
        <QuickLink icon={<ShieldAlert size={17} />} label={t("admin.new.quickAudit")} value={analytics.audit_events_24h} suffix={t("admin.new.last24hShort")} onClick={() => onNavigate("audit")} />
      </div>
    </section>
  );
}

export function AdminAnalytics({ analytics, formatDate, enumLabel }: AnalyticsProps) {
  const { t } = useI18n();
  return (
    <section className="ops-page" data-testid="admin-analytics">
      <PageIntro
        eyebrow={t("admin.new.analytics")}
        title={t("admin.new.analyticsTitle")}
        copy={t("admin.new.analyticsCopy")}
        meta={<><span className="ops-live-dot" />{t("admin.new.generatedAt")} · {formatDate(analytics.generated_at)}</>}
      />
      <div className="ops-kpi-grid ops-kpi-grid-six">
        <BusinessKpi icon={<UsersRound size={18} />} label={t("admin.new.kpiUsers")} value={analytics.users_total} detail={t("admin.new.kpiActiveValue", { value: analytics.active_users })} tone="violet" />
        <BusinessKpi icon={<Sparkles size={18} />} label={t("admin.new.kpiVerified")} value={`${ratio(analytics.verified_users, analytics.users_total)}%`} detail={t("admin.new.kpiVerifiedValue", { value: analytics.verified_users })} tone="teal" />
        <BusinessKpi icon={<Trophy size={18} />} label={t("admin.new.kpiTournaments")} value={analytics.tournaments_total} detail={t("admin.new.kpiCompletedValue", { value: analytics.completed_tournaments })} tone="amber" />
        <BusinessKpi icon={<GitBranch size={18} />} label={t("admin.new.kpiMatches")} value={analytics.matches_total} detail={t("admin.new.kpiCompletedValue", { value: analytics.completed_matches })} tone="blue" />
        <BusinessKpi icon={<AlertTriangle size={18} />} label={t("admin.new.kpiAttention")} value={analytics.tournaments_attention_total} detail={t("admin.new.kpiAutomationValue", { value: analytics.automation_failures_total })} tone={analytics.tournaments_attention_total ? "danger" : "green"} />
        <BusinessKpi icon={<ShieldAlert size={18} />} label={t("admin.new.kpiAudit")} value={analytics.audit_events_7d} detail={t("admin.new.kpiAuditWindow")} tone="slate" />
      </div>
      <div className="ops-analytics-grid">
        <section className="ops-panel">
          <PanelHeader icon={<BarChart3 size={17} />} title={t("admin.new.rankMixTitle")} copy={t("admin.new.rankMixCopy")} />
          <RankBars buckets={analytics.rank_distribution} />
        </section>
        <section className="ops-panel">
          <PanelHeader icon={<UsersRound size={17} />} title={t("admin.new.activeRankTitle")} copy={t("admin.new.activeRankCopy")} />
          <RankBars buckets={analytics.active_participant_rank_distribution} />
        </section>
        <section className="ops-panel">
          <PanelHeader icon={<Trophy size={17} />} title={t("admin.new.statusTitle")} copy={t("admin.new.statusCopy")} />
          <DistributionBars title={t("admin.new.tournamentStatuses")} buckets={analytics.tournament_status_distribution} label={enumLabel} />
          <DistributionBars title={t("admin.new.visibilityStatuses")} buckets={analytics.tournament_visibility_distribution} label={enumLabel} compact />
        </section>
        <section className="ops-panel">
          <PanelHeader icon={<GitBranch size={17} />} title={t("admin.new.matchStatusTitle")} copy={t("admin.new.matchStatusCopy")} />
          <DistributionBars title={t("admin.new.matchStatuses")} buckets={analytics.match_status_distribution} label={enumLabel} />
          <div className="ops-inline-metrics"><InlineMetric label={t("admin.new.scheduled")} value={analytics.scheduled_matches} /><InlineMetric label={t("admin.new.live")} value={analytics.live_matches} /><InlineMetric label={t("admin.new.cancelled")} value={analytics.cancelled_matches} /></div>
        </section>
        <section className="ops-panel">
          <PanelHeader icon={<UsersRound size={17} />} title={t("admin.new.participantScaleTitle")} copy={t("admin.new.participantScaleCopy")} />
          <div className="ops-inline-metrics"><InlineMetric label={t("admin.new.participantsTotal")} value={analytics.participants_total} /><InlineMetric label={t("admin.new.activeParticipantsMetric")} value={analytics.active_participants} /><InlineMetric label={t("admin.new.teamsTotal")} value={analytics.teams_total} /></div>
          <MetricLine label={t("admin.new.profileCoverageShort")} value={analytics.participant_profile_coverage_percent} suffix="%" progress={analytics.participant_profile_coverage_percent} tone="teal" />
        </section>
        <section className="ops-panel">
          <PanelHeader icon={<Activity size={17} />} title={t("admin.new.workflowHealthTitle")} copy={t("admin.new.workflowHealthCopy")} />
          <DistributionBars title={t("admin.new.assignmentStatuses")} buckets={analytics.assignment_status_distribution} label={enumLabel} compact />
          <DistributionBars title={t("admin.new.readyRoundStatuses")} buckets={analytics.ready_round_status_distribution} label={enumLabel} compact />
          <DistributionBars title={t("admin.new.captainRoundStatuses")} buckets={analytics.captain_round_status_distribution} label={enumLabel} compact />
          <div className="ops-inline-metrics"><InlineMetric label={t("admin.new.assignmentRuns")} value={analytics.assignment_runs_total} /><InlineMetric label={t("admin.new.readyRounds")} value={analytics.ready_rounds_total} /><InlineMetric label={t("admin.new.captainRounds")} value={analytics.captain_rounds_total} /></div>
        </section>
        <section className="ops-panel ops-panel-wide">
          <PanelHeader icon={<Activity size={17} />} title={t("admin.new.activityTitle")} copy={t("admin.new.activityCopy")} />
          <ActivityTable activity={analytics.activity} />
        </section>
        <section className="ops-panel">
          <PanelHeader icon={<ShieldAlert size={17} />} title={t("admin.new.auditTitle")} copy={t("admin.new.auditCopy")} />
          <div className="ops-audit-window-grid"><InlineMetric label={t("admin.new.last24h")} value={analytics.audit_events_24h} /><InlineMetric label={t("admin.new.last7d")} value={analytics.audit_events_7d} /><InlineMetric label={t("admin.new.allTime")} value={analytics.audit_events_total} /></div>
          <DistributionBars title={t("admin.new.accountStatuses")} buckets={analytics.user_status_distribution} label={enumLabel} compact />
          <DistributionBars title={t("admin.new.participantStatuses")} buckets={analytics.participant_status_distribution} label={enumLabel} compact />
        </section>
      </div>
    </section>
  );
}

export function PageIntro({ eyebrow, title, copy, meta }: { eyebrow: string; title: string; copy: string; meta?: React.ReactNode }) {
  return (
    <header className="ops-page-intro">
      <div><span className="ops-kicker">{eyebrow}</span><h1>{title}</h1><p>{copy}</p></div>
      {meta ? <div className="ops-page-meta">{meta}</div> : null}
    </header>
  );
}

function PanelHeader({ icon, title, copy, action }: { icon: React.ReactNode; title: string; copy: string; action?: React.ReactNode }) {
  return <div className="ops-panel-header"><div className="ops-panel-heading"><span className="ops-panel-icon">{icon}</span><div><h3>{title}</h3><p>{copy}</p></div></div>{action}</div>;
}

function BusinessKpi({ icon, label, value, detail, tone }: { icon: React.ReactNode; label: string; value: string | number; detail: string; tone: string }) {
  return <div className={`ops-kpi ops-kpi-${tone}`}><span className="ops-kpi-icon">{icon}</span><div><span>{label}</span><strong>{formatNumber(value)}</strong><small>{detail}</small></div></div>;
}

function FunnelRow({ label, value, percent, tone }: { label: string; value: number; percent: number; tone: string }) {
  return <div className="ops-funnel-row"><div><span>{label}</span><strong>{formatNumber(value)}</strong></div><progress className={`ops-progress ops-progress-${tone}`} max={100} value={Math.min(100, percent)} /></div>;
}

function MetricLine({ label, value, suffix, progress, tone }: { label: string; value: number; suffix: string; progress: number; tone: string }) {
  return <div className="ops-metric-line"><div><span>{label}</span><strong>{formatNumber(value)} <small>{suffix}</small></strong></div><progress className={`ops-progress ops-progress-${tone}`} max={100} value={Math.min(100, progress)} /></div>;
}

export function RankBars({ buckets, compact = false }: { buckets: PlatformAdminAnalyticsBucket[]; compact?: boolean }) {
  const { enumLabel } = useI18n();
  const { t } = useI18n();
  if (!buckets.length) return <EmptyInline copy={t("admin.new.noData")} />;
  return <div className={compact ? "ops-rank-list ops-rank-list-compact" : "ops-rank-list"}>{buckets.map((bucket) => <div className="ops-rank-row" key={bucket.key}><div className="ops-rank-label"><span>{enumLabel(bucket.key)}</span><strong>{formatNumber(bucket.count)}</strong><small>{bucket.percentage}%</small></div><progress className="ops-progress ops-progress-rank" max={100} value={bucket.percentage} /></div>)}</div>;
}

function DistributionBars({ title, buckets, label, compact = false }: { title: string; buckets: PlatformAdminAnalyticsBucket[]; label: (value: string) => string; compact?: boolean }) {
  const { t } = useI18n();
  return <div className={compact ? "ops-distribution ops-distribution-compact" : "ops-distribution"}><span className="ops-subsection-label">{title}</span>{buckets.length ? buckets.map((bucket) => <div className="ops-distribution-row" key={bucket.key}><div><span>{label(bucket.key)}</span><strong>{formatNumber(bucket.count)}</strong></div><progress className="ops-progress ops-progress-distribution" max={100} value={bucket.percentage} /></div>) : <span className="ops-muted">{t("admin.new.noData")}</span>}</div>;
}

function ActivitySummary({ activity }: { activity: PlatformAdminActivityPoint[] }) {
  const { t } = useI18n();
  const recent = activity.slice(-7);
  const total = recent.reduce((sum, point) => sum + point.users + point.tournaments + point.participants + point.matches, 0);
  const peak = Math.max(...recent.map((point) => point.users + point.tournaments + point.participants + point.matches), 1);
  return <div className="ops-activity-summary"><div className="ops-activity-total"><strong>{formatNumber(total)}</strong><span>{t("admin.new.eventsLast7d")}</span></div><div className="ops-mini-chart">{recent.map((point) => { const value = point.users + point.tournaments + point.participants + point.matches; return <div className="ops-mini-column" key={point.date}><progress className="ops-progress ops-progress-violet" max={peak} value={value} /><small>{new Intl.DateTimeFormat("ru-RU", { day: "numeric" }).format(new Date(point.date))}</small></div>; })}</div></div>;
}

function ActivityTable({ activity }: { activity: PlatformAdminActivityPoint[] }) {
  const { t } = useI18n();
  const max = Math.max(...activity.map((point) => point.users + point.tournaments + point.participants + point.matches), 1);
  return <div className="ops-activity-table">{activity.map((point) => { const total = point.users + point.tournaments + point.participants + point.matches; return <div className="ops-activity-row" key={point.date}><span>{new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short" }).format(new Date(point.date))}</span><progress className="ops-progress ops-progress-violet" max={max} value={total} /><strong>{formatNumber(total)}</strong><small>{point.users} {t("admin.new.usersShort")} · {point.participants} {t("admin.new.participantsShort")} · {point.tournaments} {t("admin.new.tournamentsShort")} · {point.matches} {t("admin.new.matchesShort")}</small></div>; })}</div>;
}

function QuickLink({ icon, label, value, suffix, onClick }: { icon: React.ReactNode; label: string; value: number; suffix?: string; onClick: () => void }) {
  return <button className="ops-quick-link" type="button" onClick={onClick}><span>{icon}</span><div><strong>{label}</strong><small>{formatNumber(value)}{suffix ? ` · ${suffix}` : ""}</small></div><ArrowUpRight size={15} /></button>;
}

function InlineMetric({ label, value }: { label: string; value: number }) { return <div className="ops-inline-metric"><span>{label}</span><strong>{formatNumber(value)}</strong></div>; }

function EmptyInline({ icon, copy }: { icon?: React.ReactNode; copy: string }) { return <div className="ops-empty-inline">{icon ? <span>{icon}</span> : null}<span>{copy}</span></div>; }

function attentionReason(tournament: PlatformAdminTournament, t: (key: string, params?: Record<string, string | number | null | undefined>) => string) {
  if (tournament.automation_last_error) return t("admin.new.attentionAutomation");
  if (tournament.unfinished_match_count > 0) return t("admin.new.attentionMatches", { value: tournament.unfinished_match_count });
  return tournament.admin_override_warning ?? t("admin.new.attentionReview");
}

function ratio(value: number, total: number): number { return total > 0 ? Number(((value / total) * 100).toFixed(1)) : 0; }
function formatNumber(value: string | number): string { return typeof value === "number" ? value.toLocaleString("ru-RU") : value; }
