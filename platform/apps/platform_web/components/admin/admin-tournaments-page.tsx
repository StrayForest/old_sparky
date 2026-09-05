"use client";

import Link from "next/link";
import { useState } from "react";
import { AlertTriangle, ArrowUpRight, CalendarClock, CheckCircle2, ChevronRight, Eye, GitBranch, LockKeyhole, Search, Settings2, Trash2, Trophy, UsersRound } from "lucide-react";
import { useI18n } from "@/components/i18n-provider";
import { AdminTournamentRoster } from "@/components/admin/admin-tournament-roster";
import { platformApiMessage, platformApiRequest } from "@/lib/platform-api";
import type { PlatformAdminTournament } from "@/lib/platform-types";

type AdminTournamentFilters = { search: string; status: string; visibility: string; attentionOnly: boolean };
type AdminTournamentsPageProps = {
  tournaments: PlatformAdminTournament[];
  filteredTotal: number;
  selectedSlug: string | null;
  filters: AdminTournamentFilters;
  hasMore: boolean;
  isFiltering: boolean;
  isLoadingMore: boolean;
  isReloading: boolean;
  pageError: string;
  enumLabel: (value: string | null | undefined) => string;
  formatDate: (value: string) => string;
  onFiltersChange: (filters: AdminTournamentFilters) => void;
  onSelect: (slug: string) => void;
  onLoadMore: () => void;
  onRetry: () => void;
  onUpdate: (tournament: PlatformAdminTournament) => void;
  onDelete: (slug: string) => void;
};

export function AdminTournamentsPage(props: AdminTournamentsPageProps) {
  const { t } = useI18n();
  const selected = props.tournaments.find((item) => item.slug === props.selectedSlug) ?? null;
  return <section className="ops-page" data-testid="admin-tournaments-page">
    <div className="ops-page-title-row"><div><span className="ops-kicker">{t("admin.new.tournaments")}</span><h1>{t("admin.new.tournamentsTitle")}</h1><p>{t("admin.new.tournamentsCopy")}</p></div><div className="ops-page-count"><strong>{props.filteredTotal.toLocaleString("ru-RU")}</strong><span>{t("admin.new.tournamentsCount")}</span></div></div>
    <div className="ops-toolbar ops-tournaments-toolbar">
      <label className="ops-search"><Search size={17} /><input data-testid="admin-tournament-search" value={props.filters.search} maxLength={140} placeholder={t("admin.new.tournamentSearchPlaceholder")} onChange={(event) => props.onFiltersChange({ ...props.filters, search: event.target.value })} /></label>
      <label className="ops-filter"><span>{t("admin.new.lifecycle")}</span><select data-testid="admin-tournament-status-filter" value={props.filters.status} onChange={(event) => props.onFiltersChange({ ...props.filters, status: event.target.value })}><option value="all">{t("admin.new.allStatuses")}</option>{["registration_open", "registration_closed", "in_progress", "completed", "cancelled"].map((value) => <option key={value} value={value}>{props.enumLabel(value)}</option>)}</select></label>
      <label className="ops-filter"><span>{t("admin.new.visibility")}</span><select data-testid="admin-tournament-visibility-filter" value={props.filters.visibility} onChange={(event) => props.onFiltersChange({ ...props.filters, visibility: event.target.value })}><option value="all">{t("admin.new.allVisibility")}</option><option value="public">{props.enumLabel("public")}</option><option value="invite_only">{props.enumLabel("invite_only")}</option></select></label>
      <button className={props.filters.attentionOnly ? "ops-filter-button is-active" : "ops-filter-button"} data-testid="admin-tournament-attention-filter" type="button" aria-pressed={props.filters.attentionOnly} onClick={() => props.onFiltersChange({ ...props.filters, attentionOnly: !props.filters.attentionOnly })}><AlertTriangle size={15} />{t("admin.new.attentionOnly")}</button>
      <span className="ops-toolbar-count">{props.isFiltering ? t("common.loading") : t("admin.new.shownOf", { shown: props.tournaments.length, total: props.filteredTotal })}</span>
    </div>
    <div className="ops-list-detail ops-tournament-workspace">
      <section className="ops-panel ops-list-panel" aria-busy={props.isFiltering || props.isLoadingMore || props.isReloading}>
        <div className="ops-table-wrap"><table className="ops-table ops-tournament-table"><thead><tr><th>{t("admin.new.tournamentColumn")}</th><th>{t("admin.new.lifecycleColumn")}</th><th>{t("admin.new.demandColumn")}</th><th>{t("admin.new.progressColumn")}</th><th>{t("admin.new.attentionColumn")}</th><th><span className="sr-only">{t("common.view")}</span></th></tr></thead><tbody>{props.tournaments.map((tournament) => <TournamentRow key={tournament.id} tournament={tournament} selected={selected?.id === tournament.id} enumLabel={props.enumLabel} onSelect={props.onSelect} />)}</tbody></table></div>
        {props.isFiltering ? <div className="ops-loading-line">{t("common.loading")}</div> : null}
        {!props.isFiltering && props.tournaments.length === 0 ? <div className="ops-empty-inline"><Trophy size={18} /><span>{t("admin.new.noTournaments")}</span></div> : null}
        {props.pageError ? <div className="ops-feedback ops-feedback-error" data-testid="admin-tournaments-page-error" role="alert"><span>{props.pageError}</span><button className="ops-button ops-button-secondary" data-testid="admin-tournaments-page-retry" type="button" disabled={props.isFiltering || props.isLoadingMore || props.isReloading} onClick={props.onRetry}>{t("common.retry")}</button></div> : null}
        {props.hasMore && !props.pageError ? <button className="ops-load-more" data-testid="admin-tournaments-load-more" type="button" disabled={props.isFiltering || props.isLoadingMore || props.isReloading} onClick={props.onLoadMore}>{props.isLoadingMore ? t("common.loading") : t("admin.new.loadMore")}</button> : null}
      </section>
      <TournamentDetail tournament={selected} enumLabel={props.enumLabel} formatDate={props.formatDate} onUpdate={props.onUpdate} onDelete={props.onDelete} />
    </div>
  </section>;
}

function TournamentRow({ tournament, selected, enumLabel, onSelect }: { tournament: PlatformAdminTournament; selected: boolean; enumLabel: (value: string | null | undefined) => string; onSelect: (slug: string) => void }) {
  const { t } = useI18n();
  const attention = tournament.unfinished_match_count > 0 || Boolean(tournament.admin_override_warning) || Boolean(tournament.automation_last_error);
  return <tr className={selected ? "is-selected" : ""} data-testid={`admin-tournament-${tournament.slug}`} tabIndex={0} onClick={() => onSelect(tournament.slug)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(tournament.slug); } }}>
    <td><button className="ops-record" type="button" onClick={() => onSelect(tournament.slug)}><span className="ops-record-icon"><Trophy size={16} /></span><span><strong>{tournament.name}</strong><small>{tournament.organizer_display_name ?? t("common.unknown")} · {tournament.slug}</small></span></button></td>
    <td><span className="ops-status ops-status-info">{enumLabel(tournament.status)}</span><small className="ops-table-subtext">{enumLabel(tournament.visibility)}</small></td>
    <td><strong className="ops-table-number">{tournament.participant_count}</strong><small className="ops-table-subtext">{tournament.max_participants ? t("admin.new.capacityOf", { value: tournament.max_participants }) : t("admin.new.noCapacity")}</small></td>
    <td><strong className="ops-table-number">{tournament.completed_match_count}/{tournament.match_count}</strong><small className="ops-table-subtext">{t("admin.new.matchesCompletedShort")}</small></td>
    <td><span className={attention ? "ops-status ops-status-danger" : "ops-status ops-status-success"}>{attention ? <AlertTriangle size={13} /> : <CheckCircle2 size={13} />}{attention ? t("admin.new.review") : t("admin.new.stable")}</span></td>
    <td><ChevronRight size={16} /></td>
  </tr>;
}

type DetailTab = "summary" | "roster" | "bracket" | "recovery";

function TournamentDetail({ tournament, enumLabel, formatDate, onUpdate, onDelete }: { tournament: PlatformAdminTournament | null; enumLabel: (value: string | null | undefined) => string; formatDate: (value: string) => string; onUpdate: (tournament: PlatformAdminTournament) => void; onDelete: (slug: string) => void }) {
  const { t } = useI18n();
  const [tab, setTab] = useState<DetailTab>("summary");
  if (!tournament) return <aside className="ops-detail ops-detail-empty"><Trophy size={22} /><strong>{t("admin.new.selectTournamentTitle")}</strong><span>{t("admin.new.selectTournamentCopy")}</span></aside>;
  return <aside className="ops-detail ops-tournament-detail" data-testid="admin-tournament-inspector">
    <div className="ops-detail-header"><div><span className="ops-kicker">{t("admin.new.tournamentDetail")}</span><h2>{tournament.name}</h2><p>{tournament.organizer_display_name ?? t("common.unknown")} · {tournament.slug}</p></div><Link className="ops-icon-button" href={`/tournaments/${tournament.slug}`} aria-label={t("admin.new.openTournament")}><Eye size={17} /></Link></div>
    <div className="ops-detail-badges"><span className="ops-status ops-status-info">{enumLabel(tournament.status)}</span><span className="ops-status ops-status-muted">{enumLabel(tournament.visibility)}</span><span className={tournament.has_locked_deadlock_roster ? "ops-status ops-status-warning" : "ops-status ops-status-muted"}><LockKeyhole size={13} />{tournament.has_locked_deadlock_roster ? t("admin.new.rosterLocked") : t("admin.new.rosterOpen")}</span></div>
    <nav className="ops-detail-tabs" aria-label={t("admin.new.tournamentSections")}>{(["summary", "roster", "bracket", "recovery"] as DetailTab[]).map((value) => <button className={tab === value ? "is-active" : ""} key={value} type="button" onClick={() => setTab(value)}>{detailTabLabel(value, t)}</button>)}</nav>
    {tournament.admin_override_warning ? <div className="ops-warning"><AlertTriangle size={16} /><span>{tournament.admin_override_warning}</span></div> : null}
    {tab === "summary" ? <TournamentSummary tournament={tournament} formatDate={formatDate} onOpenRoster={() => setTab("roster")} /> : null}
    {tab === "roster" ? <AdminTournamentRoster slug={tournament.slug} formatDate={formatDate} /> : null}
    {tab === "bracket" ? <BracketImpact tournament={tournament} /> : null}
    {tab === "recovery" ? <TournamentRecovery tournament={tournament} enumLabel={enumLabel} onUpdate={onUpdate} onDelete={onDelete} /> : null}
  </aside>;
}

function TournamentSummary({ tournament, formatDate, onOpenRoster }: { tournament: PlatformAdminTournament; formatDate: (value: string) => string; onOpenRoster: () => void }) {
  const { t } = useI18n();
  const progress = tournament.match_count > 0 ? Math.round((tournament.completed_match_count / tournament.match_count) * 100) : 0;
  return <div className="ops-detail-content"><div className="ops-fact-grid"><Fact label={t("admin.new.participants")} value={String(tournament.participant_count)} /><Fact label={t("admin.new.matches")} value={String(tournament.match_count)} /><Fact label={t("admin.new.completed")} value={String(tournament.completed_match_count)} /><Fact label={t("admin.new.created")} value={formatDate(tournament.created_at)} /></div><section className="ops-detail-section"><SectionTitle title={t("admin.new.progressTitle")} copy={t("admin.new.progressCopy")} /><div className="ops-big-progress"><progress className="ops-progress ops-progress-teal" max={100} value={progress} /><strong>{progress}%</strong></div><div className="ops-progress-caption"><span>{tournament.completed_match_count} {t("admin.new.completedShort")}</span><span>{tournament.unfinished_match_count} {t("admin.new.unfinishedShort")}</span><span>{tournament.cancelled_match_count} {t("admin.new.cancelledShort")}</span></div></section><section className="ops-detail-section"><SectionTitle title={t("admin.new.scheduleTitle")} copy={t("admin.new.scheduleCopy")} /><Timeline items={[[t("admin.new.registrationStart"), tournament.registration_starts_at], [t("admin.new.registrationEnd"), tournament.registration_closes_at], [t("admin.new.tournamentStart"), tournament.starts_at]]} formatDate={formatDate} /></section><button className="ops-button ops-button-secondary ops-full-button" type="button" onClick={onOpenRoster}><UsersRound size={16} />{t("admin.new.openRoster")}</button></div>;
}

function BracketImpact({ tournament }: { tournament: PlatformAdminTournament }) {
  const { t } = useI18n();
  return <div className="ops-detail-content"><section className="ops-detail-section"><SectionTitle title={t("admin.new.bracketTitle")} copy={t("admin.new.bracketCopy")} /><div className="ops-bracket-facts"><Fact label={t("admin.new.matches")} value={String(tournament.match_count)} /><Fact label={t("admin.new.latestRound")} value={String(tournament.latest_round_number ?? "—")} /><Fact label={t("admin.new.completed")} value={String(tournament.completed_match_count)} /></div><div className="ops-info"><GitBranch size={16} /><span>{t("admin.new.bracketIdentityCopy")}</span></div><Link className="ops-button ops-button-secondary ops-full-button" href={`/tournaments/${tournament.slug}/bracket`}><ArrowUpRight size={16} />{t("admin.new.openBracket")}</Link></section></div>;
}

function TournamentRecovery({ tournament, enumLabel, onUpdate, onDelete }: { tournament: PlatformAdminTournament; enumLabel: (value: string | null | undefined) => string; onUpdate: (tournament: PlatformAdminTournament) => void; onDelete: (slug: string) => void }) {
  const { t } = useI18n();
  const [status, setStatus] = useState(tournament.status);
  const [visibility, setVisibility] = useState(tournament.visibility);
  const [schedule, setSchedule] = useState(() => scheduleFromTournament(tournament));
  const [note, setNote] = useState("");
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [deleteName, setDeleteName] = useState("");
  const [deleteNote, setDeleteNote] = useState("");
  const [isDeleting, setIsDeleting] = useState(false);
  const scheduleChanged = Object.entries(schedule).some(([key, value]) => value !== dateTimeLocal(tournament[key as keyof PlatformAdminTournament] as string | null | undefined));
  const changed = status !== tournament.status || visibility !== tournament.visibility || (status === "registration_open" && scheduleChanged);
  const scheduleComplete = status !== "registration_open" || Object.values(schedule).every(Boolean);

  function changeStatus(value: string) {
    setStatus(value);
    if (value === "registration_open") setSchedule((current) => fillFutureSchedule(current));
  }

  async function save() {
    if (!changed || !scheduleComplete || note.trim().length < 3 || isSaving) return;
    setIsSaving(true); setError(""); setMessage("");
    try {
      const includeSchedule = status === "registration_open";
      const updated = await platformApiRequest<PlatformAdminTournament>(`/admin/tournaments/${tournament.slug}`, {
        method: "PATCH",
        body: JSON.stringify({
          status: status !== tournament.status || scheduleChanged ? status : null,
          visibility: visibility === tournament.visibility ? null : visibility,
          registration_closes_at: includeSchedule ? toIso(schedule.registration_closes_at) : null,
          ready_check_starts_at: includeSchedule ? toIso(schedule.ready_check_starts_at) : null,
          ready_check_ends_at: includeSchedule ? toIso(schedule.ready_check_ends_at) : null,
          captain_selection_starts_at: includeSchedule ? toIso(schedule.captain_selection_starts_at) : null,
          starts_at: includeSchedule ? toIso(schedule.starts_at) : null,
          note: note.trim()
        })
      });
      onUpdate(updated);
      setSchedule(scheduleFromTournament(updated));
      setStatus(updated.status);
      setVisibility(updated.visibility);
      setNote("");
      setMessage(t("admin.new.overrideSaved"));
    } catch (requestError) {
      setError(platformApiMessage(requestError, t("admin.new.overrideFailed")));
    } finally {
      setIsSaving(false);
    }
  }

  async function remove() {
    if (deleteName.trim() !== tournament.name || deleteNote.trim().length < 3 || isDeleting) return;
    setIsDeleting(true); setError("");
    try {
      await platformApiRequest<void>(`/admin/tournaments/${tournament.slug}`, {
        method: "DELETE",
        body: JSON.stringify({ confirmation_name: deleteName.trim(), note: deleteNote.trim() })
      });
      onDelete(tournament.slug);
    } catch (requestError) {
      setError(platformApiMessage(requestError, t("admin.new.deleteTournamentFailed")));
      setIsDeleting(false);
    }
  }

  return <div className="ops-detail-content">
    <section className="ops-detail-section">
      <SectionTitle title={t("admin.new.recoveryTitle")} copy={t("admin.new.recoveryCopy")} />
      <div className="ops-form-grid-two">
        <label className="ops-field"><span>{t("admin.new.lifecycle")}</span><select data-testid="admin-status-override" value={status} onChange={(event) => changeStatus(event.target.value)}>{["registration_open", "registration_closed", "in_progress", "completed", "cancelled"].map((value) => <option key={value} value={value}>{enumLabel(value)}</option>)}</select></label>
        <label className="ops-field"><span>{t("admin.new.visibility")}</span><select data-testid="admin-visibility-override" value={visibility} onChange={(event) => setVisibility(event.target.value)}><option value="public">{enumLabel("public")}</option><option value="invite_only">{enumLabel("invite_only")}</option></select></label>
      </div>
      {status === "registration_open" ? <div className="ops-schedule" data-testid="admin-schedule-editor">
        <div className="ops-info"><CalendarClock size={15} /><span>{t("admin.new.reopenScheduleNotice")}</span></div>
        <ScheduleField label={t("admin.new.registrationClosesAt")} testId="admin-registration-closes-at" value={schedule.registration_closes_at} onChange={(value) => setSchedule((current) => ({ ...current, registration_closes_at: value }))} />
        <ScheduleField label={t("admin.new.readyCheckStartsAt")} value={schedule.ready_check_starts_at} onChange={(value) => setSchedule((current) => ({ ...current, ready_check_starts_at: value }))} />
        <ScheduleField label={t("admin.new.readyCheckEndsAt")} value={schedule.ready_check_ends_at} onChange={(value) => setSchedule((current) => ({ ...current, ready_check_ends_at: value }))} />
        <ScheduleField label={t("admin.new.captainSelectionStartsAt")} value={schedule.captain_selection_starts_at} onChange={(value) => setSchedule((current) => ({ ...current, captain_selection_starts_at: value }))} />
        <ScheduleField label={t("admin.new.tournamentStartsAt")} value={schedule.starts_at} onChange={(value) => setSchedule((current) => ({ ...current, starts_at: value }))} />
      </div> : null}
      <ReasonField value={note} onChange={setNote} testId="admin-override-note" />
      <button className="ops-button ops-button-primary ops-full-button" data-testid="admin-apply-override" type="button" disabled={!changed || !scheduleComplete || note.trim().length < 3 || isSaving} onClick={() => void save()}><Settings2 size={16} />{isSaving ? t("common.saving") : t("admin.new.applyOverride")}</button>
      {message ? <div className="ops-feedback ops-feedback-success">{message}</div> : null}
      {error ? <div className="ops-feedback ops-feedback-error">{error}</div> : null}
    </section>
    <details className="ops-danger-zone">
      <summary><Trash2 size={15} />{t("admin.new.deleteTournamentTitle")}</summary>
      <div className="ops-danger-content">
        <div className="ops-warning"><AlertTriangle size={16} /><span>{t("admin.new.deleteTournamentCopy")}</span></div>
        <label className="ops-field"><span>{t("admin.new.confirmTournamentName")}</span><input data-testid="admin-delete-tournament-confirmation" value={deleteName} maxLength={140} onChange={(event) => setDeleteName(event.target.value)} /><small>{tournament.name}</small></label>
        <ReasonField value={deleteNote} onChange={setDeleteNote} testId="admin-delete-tournament-note" />
        <button className="ops-button ops-button-danger" data-testid="admin-delete-tournament" type="button" disabled={deleteName.trim() !== tournament.name || deleteNote.trim().length < 3 || isDeleting} onClick={() => void remove()}><Trash2 size={16} />{isDeleting ? t("admin.new.deleting") : t("admin.new.deleteTournamentButton")}</button>
      </div>
    </details>
  </div>;
}

function Timeline({ items, formatDate }: { items: [string, string | null | undefined][]; formatDate: (value: string) => string }) { return <div className="ops-timeline">{items.map(([label, value]) => <div key={label}><span className={value ? "ops-timeline-dot is-set" : "ops-timeline-dot"} /><div><strong>{label}</strong><small>{value ? formatDate(value) : "—"}</small></div></div>)}</div>; }
function Fact({ label, value }: { label: string; value: string }) { return <div className="ops-fact"><span>{label}</span><strong>{value}</strong></div>; }
function SectionTitle({ title, copy }: { title: string; copy: string }) { return <div className="ops-section-title"><h3>{title}</h3><p>{copy}</p></div>; }
function ReasonField({ value, onChange, testId }: { value: string; onChange: (value: string) => void; testId?: string }) { const { t } = useI18n(); return <label className="ops-field"><span>{t("admin.new.reason")}</span><textarea data-testid={testId} maxLength={1000} value={value} placeholder={t("admin.new.reasonPlaceholder")} onChange={(event) => onChange(event.target.value)} /></label>; }
type ScheduleDraft = { registration_closes_at: string; ready_check_starts_at: string; ready_check_ends_at: string; captain_selection_starts_at: string; starts_at: string };
function ScheduleField({ label, value, onChange, testId }: { label: string; value: string; onChange: (value: string) => void; testId?: string }) { return <label className="ops-field"><span>{label}</span><input data-testid={testId} type="datetime-local" value={value} onChange={(event) => onChange(event.target.value)} /></label>; }
function scheduleFromTournament(tournament: PlatformAdminTournament): ScheduleDraft { return { registration_closes_at: dateTimeLocal(tournament.registration_closes_at), ready_check_starts_at: dateTimeLocal(tournament.ready_check_starts_at), ready_check_ends_at: dateTimeLocal(tournament.ready_check_ends_at), captain_selection_starts_at: dateTimeLocal(tournament.captain_selection_starts_at), starts_at: dateTimeLocal(tournament.starts_at) }; }
function fillFutureSchedule(schedule: ScheduleDraft): ScheduleDraft { const base = Date.now() + 3600000; return { registration_closes_at: schedule.registration_closes_at || dateTimeLocal(new Date(base).toISOString()), ready_check_starts_at: schedule.ready_check_starts_at || dateTimeLocal(new Date(base + 3600000).toISOString()), ready_check_ends_at: schedule.ready_check_ends_at || dateTimeLocal(new Date(base + 7200000).toISOString()), captain_selection_starts_at: schedule.captain_selection_starts_at || dateTimeLocal(new Date(base + 10800000).toISOString()), starts_at: schedule.starts_at || dateTimeLocal(new Date(base + 14400000).toISOString()) }; }
function dateTimeLocal(value: string | null | undefined): string { if (!value) return ""; const date = new Date(value); const offset = date.getTimezoneOffset(); return new Date(date.getTime() - offset * 60000).toISOString().slice(0, 16); }
function toIso(value: string): string | null { return value ? new Date(value).toISOString() : null; }
function detailTabLabel(tab: DetailTab, t: (key: string) => string): string { return { summary: t("admin.new.summaryTab"), roster: t("admin.new.rosterTab"), bracket: t("admin.new.bracketTab"), recovery: t("admin.new.recoveryTab") }[tab]; }
