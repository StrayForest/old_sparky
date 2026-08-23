"use client";

import Link from "next/link";
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  ChevronRight,
  ClipboardList,
  Eye,
  LockKeyhole,
  RefreshCcw,
  Search,
  ShieldCheck,
  SlidersHorizontal,
  Trash2,
  Trophy,
  UserCog,
  Users
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useAuth } from "@/components/auth/auth-provider";
import { useI18n } from "@/components/i18n-provider";
import {
  PlatformApiError,
  platformApiMessage,
  platformApiPageRequest,
  platformApiRequest
} from "@/lib/platform-api";
import type {
  PlatformAdminOverview,
  PlatformAdminPreprodCleanupResult,
  PlatformAdminPreprodTestRun,
  PlatformAdminTournament,
  PlatformAuditLog,
  PlatformUser
} from "@/lib/platform-types";

type AdminTab = "tournaments" | "users" | "preprod" | "audit";
type AccessState = "loading" | "ready" | "unauthenticated" | "forbidden" | "error";
type AdminTournamentFilters = {
  search: string;
  status: string;
  visibility: string;
  attentionOnly: boolean;
};
const adminTournamentPageSize = 25;
const defaultAdminTournamentFilters: AdminTournamentFilters = {
  search: "",
  status: "all",
  visibility: "all",
  attentionOnly: false
};

const tournamentStatuses = [
  "registration_open",
  "registration_closed",
  "in_progress",
  "completed",
  "cancelled"
] as const;

export function AdminConsole() {
  const { user: currentUser } = useAuth();
  const { enumLabel, formatDate, t } = useI18n();
  const [accessState, setAccessState] = useState<AccessState>("loading");
  const [overview, setOverview] = useState<PlatformAdminOverview | null>(null);
  const [tournaments, setTournaments] = useState<PlatformAdminTournament[]>([]);
  const [users, setUsers] = useState<PlatformUser[]>([]);
  const [auditLogs, setAuditLogs] = useState<PlatformAuditLog[]>([]);
  const [preprodRuns, setPreprodRuns] = useState<PlatformAdminPreprodTestRun[]>([]);
  const [activeTab, setActiveTab] = useState<AdminTab>("tournaments");
  const [selectedTournamentSlug, setSelectedTournamentSlug] = useState<string | null>(null);
  const [selectedUserId, setSelectedUserId] = useState<string | null>(null);
  const [selectedAuditId, setSelectedAuditId] = useState<number | null>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [tournamentTotal, setTournamentTotal] = useState(0);
  const [hasMoreTournaments, setHasMoreTournaments] = useState(false);
  const [nextTournamentOffset, setNextTournamentOffset] = useState(0);
  const [isLoadingMoreTournaments, setIsLoadingMoreTournaments] = useState(false);
  const [isFilteringTournaments, setIsFilteringTournaments] = useState(false);
  const [tournamentPageError, setTournamentPageError] = useState("");
  const [failedTournamentOffset, setFailedTournamentOffset] = useState<number | null>(null);
  const [tournamentFilters, setTournamentFilters] = useState(defaultAdminTournamentFilters);
  const [isLoadingUsers, setIsLoadingUsers] = useState(false);
  const [userLoadError, setUserLoadError] = useState("");
  const [debouncedTournamentSearch, setDebouncedTournamentSearch] = useState("");
  const requestedTournamentFilters = useMemo(() => ({
    ...tournamentFilters,
    search: debouncedTournamentSearch
  }), [debouncedTournamentSearch, tournamentFilters]);
  const requestedTournamentQueryKey = adminTournamentQueryKey(requestedTournamentFilters);
  const [resolvedTournamentQueryKey, setResolvedTournamentQueryKey] = useState(
    adminTournamentQueryKey(defaultAdminTournamentFilters)
  );
  const activeTournamentFilters = useRef(requestedTournamentFilters);
  activeTournamentFilters.current = requestedTournamentFilters;
  const consoleRequestGeneration = useRef(0);
  const userSearchGeneration = useRef(0);
  const userSearchController = useRef<AbortController | null>(null);
  const tournamentPageController = useRef<AbortController | null>(null);
  const loadMoreInFlight = useRef(false);

  const loadConsole = useCallback(async (refresh = false) => {
    const requestGeneration = ++consoleRequestGeneration.current;
    const usersRequestGeneration = ++userSearchGeneration.current;
    userSearchController.current?.abort();
    userSearchController.current = null;
    setIsLoadingUsers(false);
    const filtersAtRequest = activeTournamentFilters.current;
    const queryKeyAtRequest = adminTournamentQueryKey(filtersAtRequest);
    tournamentPageController.current?.abort();
    const firstPageController = new AbortController();
    tournamentPageController.current = firstPageController;
    loadMoreInFlight.current = false;
    setIsLoadingMoreTournaments(false);
    setIsFilteringTournaments(false);
    setTournamentPageError("");
    setFailedTournamentOffset(null);

    if (refresh) {
      setIsRefreshing(true);
    } else {
      setAccessState("loading");
    }
    setLoadError("");

    try {
      if (!currentUser) {
        setAccessState("unauthenticated");
        return;
      }
      if (!currentUser.roles.some((role) => role === "admin" || role === "superadmin")) {
        setAccessState("forbidden");
        return;
      }

      const [nextOverview, nextTournamentPage, nextUsers, nextAuditLogs, nextPreprodRuns] = await Promise.all([
        platformApiRequest<PlatformAdminOverview>("/admin/overview"),
        platformApiPageRequest<PlatformAdminTournament>(
          adminTournamentListPath(filtersAtRequest, 0),
          { signal: firstPageController.signal }
        ),
        platformApiRequest<PlatformUser[]>("/admin/users"),
        platformApiRequest<PlatformAuditLog[]>("/admin/audit-logs?limit=200"),
        platformApiRequest<PlatformAdminPreprodTestRun[]>("/admin/preprod-test-runs")
      ]);
      if (requestGeneration !== consoleRequestGeneration.current) {
        return;
      }
      const nextTournaments = appendUniqueTournaments([], nextTournamentPage.items);
      setOverview(nextOverview);
      setTournaments(nextTournaments);
      setTournamentTotal(nextTournamentPage.total);
      setHasMoreTournaments(nextTournamentPage.hasMore);
      setNextTournamentOffset(nextTournamentPage.offset + nextTournamentPage.limit);
      setResolvedTournamentQueryKey(queryKeyAtRequest);
      if (usersRequestGeneration === userSearchGeneration.current) {
        setUsers(nextUsers);
      }
      setAuditLogs(nextAuditLogs);
      setPreprodRuns(nextPreprodRuns);
      setSelectedTournamentSlug((current) => (
        current && nextTournaments.some((item) => item.slug === current)
          ? current
          : nextTournaments[0]?.slug ?? null
      ));
      if (usersRequestGeneration === userSearchGeneration.current) {
        setSelectedUserId((current) => (
          current && nextUsers.some((item) => item.id === current)
            ? current
            : nextUsers[0]?.id ?? null
        ));
      }
      setSelectedAuditId((current) => (
        current && nextAuditLogs.some((item) => item.id === current)
          ? current
          : nextAuditLogs[0]?.id ?? null
      ));
      setAccessState("ready");
    } catch (error) {
      if (requestGeneration !== consoleRequestGeneration.current || firstPageController.signal.aborted) {
        return;
      }
      if (error instanceof PlatformApiError && error.status === 401) {
        setAccessState("unauthenticated");
      } else if (error instanceof PlatformApiError && error.status === 403) {
        setAccessState("forbidden");
      } else {
        setLoadError(platformApiMessage(error, t("admin.loadFailed")));
        setAccessState("error");
      }
    } finally {
      if (requestGeneration === consoleRequestGeneration.current) {
        setIsRefreshing(false);
        if (tournamentPageController.current === firstPageController) {
          tournamentPageController.current = null;
        }
      }
    }
  }, [currentUser, t]);

  const loadUsers = useCallback(async (search = "") => {
    const requestGeneration = ++userSearchGeneration.current;
    userSearchController.current?.abort();
    const controller = new AbortController();
    userSearchController.current = controller;
    setIsLoadingUsers(true);
    setUserLoadError("");
    try {
      const query = search.trim() ? `?search=${encodeURIComponent(search.trim())}` : "";
      const nextUsers = await platformApiRequest<PlatformUser[]>(`/admin/users${query}`, {
        signal: controller.signal
      });
      if (requestGeneration !== userSearchGeneration.current || controller.signal.aborted) {
        return;
      }
      setUsers(nextUsers);
      setSelectedUserId((current) => (
        current && nextUsers.some((item) => item.id === current)
          ? current
          : nextUsers[0]?.id ?? null
      ));
    } catch (error) {
      if (requestGeneration === userSearchGeneration.current && !controller.signal.aborted) {
        setUserLoadError(platformApiMessage(error, t("admin.loadFailed")));
      }
    } finally {
      if (requestGeneration === userSearchGeneration.current) {
        setIsLoadingUsers(false);
        if (userSearchController.current === controller) {
          userSearchController.current = null;
        }
      }
    }
  }, [t]);

  const loadPreprodRuns = useCallback(async () => {
    const nextRuns = await platformApiRequest<PlatformAdminPreprodTestRun[]>("/admin/preprod-test-runs");
    setPreprodRuns(nextRuns);
  }, []);

  const loadMoreTournaments = useCallback(async () => {
    if (!hasMoreTournaments || isFilteringTournaments || isRefreshing || loadMoreInFlight.current) {
      return;
    }

    const requestGeneration = consoleRequestGeneration.current;
    const pageController = new AbortController();
    tournamentPageController.current = pageController;
    loadMoreInFlight.current = true;
    setIsLoadingMoreTournaments(true);
    setTournamentPageError("");

    try {
      const nextPage = await platformApiPageRequest<PlatformAdminTournament>(
        adminTournamentListPath(activeTournamentFilters.current, nextTournamentOffset),
        { signal: pageController.signal }
      );
      if (requestGeneration !== consoleRequestGeneration.current || pageController.signal.aborted) {
        return;
      }
      setTournaments((current) => appendUniqueTournaments(current, nextPage.items));
      setTournamentTotal(nextPage.total);
      setHasMoreTournaments(nextPage.hasMore);
      setNextTournamentOffset(nextPage.offset + nextPage.limit);
      setFailedTournamentOffset(null);
    } catch (error) {
      if (requestGeneration === consoleRequestGeneration.current && !pageController.signal.aborted) {
        setTournamentPageError(platformApiMessage(error, t("admin.loadFailed")));
        setFailedTournamentOffset(nextTournamentOffset);
      }
    } finally {
      if (requestGeneration === consoleRequestGeneration.current) {
        loadMoreInFlight.current = false;
        setIsLoadingMoreTournaments(false);
        if (tournamentPageController.current === pageController) {
          tournamentPageController.current = null;
        }
      }
    }
  }, [hasMoreTournaments, isFilteringTournaments, isRefreshing, nextTournamentOffset, t]);

  const loadFilteredTournamentPage = useCallback(async (
    filters: AdminTournamentFilters,
    queryKey: string
  ) => {
    const requestGeneration = ++consoleRequestGeneration.current;
    tournamentPageController.current?.abort();
    const controller = new AbortController();
    tournamentPageController.current = controller;
    loadMoreInFlight.current = false;
    setIsFilteringTournaments(true);
    setIsLoadingMoreTournaments(false);
    setTournamentPageError("");
    setFailedTournamentOffset(null);
    setTournaments([]);
    setSelectedTournamentSlug(null);

    try {
      const nextPage = await platformApiPageRequest<PlatformAdminTournament>(
        adminTournamentListPath(filters, 0),
        { signal: controller.signal }
      );
      if (requestGeneration !== consoleRequestGeneration.current || controller.signal.aborted) {
        return;
      }
      const nextTournaments = appendUniqueTournaments([], nextPage.items);
      setTournaments(nextTournaments);
      setTournamentTotal(nextPage.total);
      setHasMoreTournaments(nextPage.hasMore);
      setNextTournamentOffset(nextPage.offset + nextPage.limit);
      setSelectedTournamentSlug(nextTournaments[0]?.slug ?? null);
      setResolvedTournamentQueryKey(queryKey);
    } catch (error) {
      if (requestGeneration === consoleRequestGeneration.current && !controller.signal.aborted) {
        setTournamentPageError(platformApiMessage(error, t("admin.loadFailed")));
        setFailedTournamentOffset(0);
        setTournamentTotal(0);
        setHasMoreTournaments(false);
      }
    } finally {
      if (requestGeneration === consoleRequestGeneration.current) {
        setIsFilteringTournaments(false);
        if (tournamentPageController.current === controller) {
          tournamentPageController.current = null;
        }
      }
    }
  }, [t]);

  useEffect(() => {
    const timeout = window.setTimeout(
      () => setDebouncedTournamentSearch(tournamentFilters.search),
      300
    );
    return () => window.clearTimeout(timeout);
  }, [tournamentFilters.search]);

  useEffect(() => {
    if (
      accessState !== "ready"
      || requestedTournamentQueryKey === resolvedTournamentQueryKey
    ) {
      return;
    }
    void loadFilteredTournamentPage(
      requestedTournamentFilters,
      requestedTournamentQueryKey
    );
  }, [
    accessState,
    loadFilteredTournamentPage,
    requestedTournamentFilters,
    requestedTournamentQueryKey,
    resolvedTournamentQueryKey
  ]);

  useEffect(() => {
    void loadConsole();
  }, [loadConsole]);

  if (accessState === "loading") {
    return <AdminStatePanel title={t("admin.loading")} copy={t("admin.loadingCopy")} />;
  }
  if (accessState === "unauthenticated") {
    return (
      <AdminStatePanel title={t("admin.signInTitle")} copy={t("admin.signInCopy")}>
        <Link className="primary-button" href="/auth/login" prefetch={false}>{t("auth.signIn")}</Link>
      </AdminStatePanel>
    );
  }
  if (accessState === "forbidden") {
    return (
      <AdminStatePanel
        title={t("admin.forbiddenTitle")}
        copy={t("admin.forbiddenCopy", { name: currentUser?.display_name ?? t("common.unknown") })}
      />
    );
  }
  if (accessState === "error") {
    return (
      <AdminStatePanel title={t("admin.loadFailed")} copy={loadError}>
        <button className="primary-button" type="button" onClick={() => void loadConsole()}>
          <RefreshCcw size={18} />{t("common.retry")}
        </button>
      </AdminStatePanel>
    );
  }

  const loadedAttentionCount = tournaments.filter((tournament) => (
    tournament.unfinished_match_count > 0 || Boolean(tournament.admin_override_warning)
  )).length;

  return (
    <section className="admin-console" data-testid="admin-console">
      <header className="admin-console-header">
        <div>
          <div className="admin-eyebrow"><ShieldCheck size={16} />{t("admin.operations")}</div>
          <h1>{t("admin.consoleTitle")}</h1>
          <p>{t("admin.consoleCopy")}</p>
        </div>
        <div className="admin-header-actions">
          <span className="admin-live-status"><span />{t("admin.liveData")}</span>
          <button
            className="secondary-button admin-refresh-button"
            data-testid="admin-refresh"
            disabled={isRefreshing}
            type="button"
            onClick={() => void loadConsole(true)}
          >
            <RefreshCcw className={isRefreshing ? "spin" : ""} size={17} />
            {isRefreshing ? t("common.loading") : t("admin.refresh")}
          </button>
        </div>
      </header>

      <div className="admin-metrics">
        <AdminMetric icon={<Users size={21} />} label={t("common.users")} value={overview?.users_total ?? 0} />
        <AdminMetric icon={<Trophy size={21} />} label={t("common.tournaments")} value={overview?.tournaments_total ?? 0} />
        <AdminMetric icon={<Activity size={21} />} label={t("admin.preprodUsers")} value={overview?.preprod_test_users_total ?? 0} />
        <AdminMetric icon={<Activity size={21} />} label={t("admin.auditEvents")} value={overview?.audit_events_total ?? 0} />
        <AdminMetric
          danger={(overview?.tournaments_attention_total ?? loadedAttentionCount) > 0}
          icon={<AlertTriangle size={21} />}
          label={t("admin.needAttention")}
          value={overview?.tournaments_attention_total ?? loadedAttentionCount}
        />
      </div>

      <div className="admin-tabs" role="tablist" aria-label={t("admin.sections")}>
        <AdminTabButton
          active={activeTab === "tournaments"}
          count={`${tournaments.length}/${tournamentTotal}`}
          icon={<Trophy size={17} />}
          label={t("admin.tournamentOperations")}
          onClick={() => setActiveTab("tournaments")}
        />
        <AdminTabButton
          active={activeTab === "users"}
          count={users.length}
          icon={<Users size={17} />}
          label={t("admin.userPermissions")}
          onClick={() => setActiveTab("users")}
        />
        <AdminTabButton
          active={activeTab === "preprod"}
          count={preprodRuns.length}
          icon={<Activity size={17} />}
          label={t("admin.preprodQa")}
          onClick={() => setActiveTab("preprod")}
        />
        <AdminTabButton
          active={activeTab === "audit"}
          count={auditLogs.length}
          icon={<ClipboardList size={17} />}
          label={t("admin.auditLog")}
          onClick={() => setActiveTab("audit")}
        />
      </div>

      {activeTab === "tournaments" ? (
        <TournamentOperations
          enumLabel={enumLabel}
          formatDate={formatDate}
          hasMore={hasMoreTournaments}
          filters={tournamentFilters}
          filteredTotal={tournamentTotal}
          isFiltering={isFilteringTournaments}
          isLoadingMore={isLoadingMoreTournaments}
          isReloading={isRefreshing}
          pageError={tournamentPageError}
          selectedSlug={selectedTournamentSlug}
          tournaments={tournaments}
          onLoadMore={loadMoreTournaments}
          onFiltersChange={setTournamentFilters}
          onRetry={() => {
            if (failedTournamentOffset === 0) {
              void loadFilteredTournamentPage(
                activeTournamentFilters.current,
                adminTournamentQueryKey(activeTournamentFilters.current)
              );
            } else {
              void loadMoreTournaments();
            }
          }}
          onSelect={setSelectedTournamentSlug}
          onDelete={(deletedSlug) => {
            const deletedTournament = tournaments.find((item) => item.slug === deletedSlug);
            const nextTournament = tournaments.find((item) => item.slug !== deletedSlug);
            const deletedNeededAttention = Boolean(
              deletedTournament
              && (deletedTournament.unfinished_match_count > 0 || deletedTournament.admin_override_warning)
            );
            setTournaments((current) => current.filter((item) => item.slug !== deletedSlug));
            setSelectedTournamentSlug(nextTournament?.slug ?? null);
            setTournamentTotal((current) => Math.max(0, current - 1));
            setOverview((current) => current ? {
              ...current,
              tournaments_total: Math.max(0, current.tournaments_total - 1),
              tournaments_attention_total: Math.max(
                0,
                (current.tournaments_attention_total ?? 0) - (deletedNeededAttention ? 1 : 0)
              )
            } : current);
          }}
          onUpdate={(updated) => {
            setTournaments((current) => current.map((item) => item.id === updated.id ? updated : item));
          }}
        />
      ) : null}
      {activeTab === "users" && currentUser ? (
        <UserPermissions
          currentUser={currentUser}
          formatDate={formatDate}
          isLoading={isLoadingUsers}
          loadError={userLoadError}
          selectedUserId={selectedUserId}
          users={users}
          onSelect={setSelectedUserId}
          onSearch={loadUsers}
          onUpdate={(updated) => {
            setUsers((current) => current.map((item) => item.id === updated.id ? updated : item));
          }}
        />
      ) : null}
      {activeTab === "preprod" && currentUser ? (
        <PreprodQaOperations
          currentUser={currentUser}
          formatDate={formatDate}
          runs={preprodRuns}
          onReload={loadPreprodRuns}
        />
      ) : null}
      {activeTab === "audit" ? (
        <AuditOperations
          auditLogs={auditLogs}
          formatDate={formatDate}
          selectedAuditId={selectedAuditId}
          onSelect={setSelectedAuditId}
        />
      ) : null}
    </section>
  );
}

function AdminTabButton({
  active,
  count,
  icon,
  label,
  onClick
}: {
  active: boolean;
  count: React.ReactNode;
  icon: React.ReactNode;
  label: string;
  onClick: () => void;
}) {
  return (
    <button className={active ? "active" : ""} role="tab" aria-selected={active} type="button" onClick={onClick}>
      {icon}{label}<span>{count}</span>
    </button>
  );
}

function TournamentOperations({
  tournaments,
  filters,
  filteredTotal,
  selectedSlug,
  enumLabel,
  formatDate,
  hasMore,
  isFiltering,
  isLoadingMore,
  isReloading,
  pageError,
  onLoadMore,
  onFiltersChange,
  onDelete,
  onRetry,
  onSelect,
  onUpdate
}: {
  tournaments: PlatformAdminTournament[];
  filters: AdminTournamentFilters;
  filteredTotal: number;
  selectedSlug: string | null;
  enumLabel: (value: string | null | undefined) => string;
  formatDate: (value: string) => string;
  hasMore: boolean;
  isFiltering: boolean;
  isLoadingMore: boolean;
  isReloading: boolean;
  pageError: string;
  onLoadMore: () => void;
  onFiltersChange: (filters: AdminTournamentFilters) => void;
  onDelete: (slug: string) => void;
  onRetry: () => void;
  onSelect: (slug: string) => void;
  onUpdate: (tournament: PlatformAdminTournament) => void;
}) {
  const { t } = useI18n();
  const selected = tournaments.find((tournament) => tournament.slug === selectedSlug) ?? null;

  return (
    <div className="admin-section">
      <div className="admin-toolbar">
        <label className="admin-search">
          <Search size={18} />
          <input
            data-testid="admin-tournament-search"
            maxLength={120}
            value={filters.search}
            placeholder={t("admin.searchTournaments")}
            onChange={(event) => onFiltersChange({ ...filters, search: event.target.value })}
          />
        </label>
        <label className="admin-select-wrap">
          <span>{t("admin.status")}</span>
          <select
            data-testid="admin-tournament-status-filter"
            value={filters.status}
            onChange={(event) => onFiltersChange({ ...filters, status: event.target.value })}
          >
            <option value="all">{t("admin.allStatuses")}</option>
            {tournamentStatuses.map((value) => <option key={value} value={value}>{enumLabel(value)}</option>)}
          </select>
        </label>
        <label className="admin-select-wrap">
          <span>{t("admin.visibility")}</span>
          <select
            data-testid="admin-tournament-visibility-filter"
            value={filters.visibility}
            onChange={(event) => onFiltersChange({ ...filters, visibility: event.target.value })}
          >
            <option value="all">{t("admin.allVisibility")}</option>
            <option value="public">{enumLabel("public")}</option>
            <option value="invite_only">{enumLabel("invite_only")}</option>
          </select>
        </label>
        <button
          className={filters.attentionOnly ? "admin-filter-toggle active" : "admin-filter-toggle"}
          data-testid="admin-tournament-attention-filter"
          type="button"
          aria-pressed={filters.attentionOnly}
          onClick={() => onFiltersChange({
            ...filters,
            attentionOnly: !filters.attentionOnly
          })}
        >
          <AlertTriangle size={17} />{t("admin.onlyAttention")}
        </button>
        <div className="admin-result-count">{t("admin.recordsFound", { count: filteredTotal })}</div>
      </div>

      <div className="admin-workspace">
        <div className="admin-table-panel" aria-busy={isFiltering || isLoadingMore || isReloading}>
          <div className="admin-table-scroll">
            <table className="admin-table">
              <thead>
                <tr>
                  <th>{t("admin.tournament")}</th>
                  <th>{t("admin.state")}</th>
                  <th>{t("admin.workflow")}</th>
                  <th>{t("common.matches")}</th>
                  <th>{t("admin.risk")}</th>
                  <th><span className="sr-only">{t("common.edit")}</span></th>
                </tr>
              </thead>
              <tbody>
                {tournaments.map((tournament) => {
                  const needsAttention = tournament.unfinished_match_count > 0 || Boolean(tournament.admin_override_warning);
                  return (
                    <tr
                      className={selectedSlug === tournament.slug ? "selected admin-clickable-row" : "admin-clickable-row"}
                      data-testid={`admin-tournament-${tournament.slug}`}
                      key={tournament.id}
                      tabIndex={0}
                      onClick={() => onSelect(tournament.slug)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          onSelect(tournament.slug);
                        }
                      }}
                    >
                      <td>
                        <div className="admin-record-button">
                          <strong>{tournament.name}</strong>
                          <span>{tournament.organizer_display_name ?? t("common.unknown")} · {tournament.slug}</span>
                        </div>
                      </td>
                      <td>
                        <div className="admin-badge-stack">
                          <StatusBadge value={tournament.status} label={enumLabel(tournament.status)} />
                          <StatusBadge value={tournament.visibility} label={enumLabel(tournament.visibility)} />
                        </div>
                      </td>
                      <td>
                        <span className="admin-table-primary">{t("admin.participantCount", { count: tournament.participant_count })}</span>
                        <span className="admin-table-secondary">
                          {tournament.has_locked_deadlock_roster ? t("admin.rosterLocked") : t("admin.rosterOpen")}
                        </span>
                      </td>
                      <td>
                        <span className="admin-table-primary">{tournament.match_count}</span>
                        <span className="admin-table-secondary">
                          {t("admin.matchBreakdown", {
                            completed: tournament.completed_match_count,
                            unfinished: tournament.unfinished_match_count
                          })}
                        </span>
                      </td>
                      <td>
                        <span className={needsAttention ? "admin-risk danger" : "admin-risk ok"}>
                          {needsAttention ? <AlertTriangle size={15} /> : <CheckCircle2 size={15} />}
                          {needsAttention ? t("admin.review") : t("admin.stable")}
                        </span>
                      </td>
                      <td><ChevronRight size={18} /></td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {isFiltering ? <div className="admin-empty">{t("common.loading")}</div> : null}
          {!isFiltering && tournaments.length === 0 ? (
            <div className="admin-empty">{t("admin.noTournaments")}</div>
          ) : null}
          {pageError ? (
            <div className="admin-feedback error" data-testid="admin-tournaments-page-error" role="alert">
              <span>{pageError}</span>
              <button
                className="secondary-button"
                data-testid="admin-tournaments-page-retry"
                disabled={isFiltering || isLoadingMore || isReloading}
                type="button"
                onClick={onRetry}
              >
                <RefreshCcw className={isLoadingMore ? "spin" : ""} size={17} />
                {isLoadingMore ? t("tournaments.loadingMore") : t("common.retry")}
              </button>
            </div>
          ) : null}
          {hasMore && !pageError ? (
            <button
              className="secondary-button"
              data-testid="admin-tournaments-load-more"
              disabled={isFiltering || isLoadingMore || isReloading}
              type="button"
              onClick={onLoadMore}
            >
              {isLoadingMore ? t("tournaments.loadingMore") : t("tournaments.loadMore")}
            </button>
          ) : null}
        </div>

        <TournamentInspector
          key={selected?.id ?? "empty"}
          enumLabel={enumLabel}
          formatDate={formatDate}
          tournament={selected}
          onDelete={onDelete}
          onUpdate={onUpdate}
        />
      </div>
    </div>
  );
}

function adminTournamentListPath(
  filters: AdminTournamentFilters,
  offset: number
): string {
  const query = new URLSearchParams({
    limit: String(adminTournamentPageSize),
    offset: String(offset)
  });
  const search = filters.search.trim();
  if (search) {
    query.set("search", search);
  }
  if (filters.status !== "all") {
    query.set("status", filters.status);
  }
  if (filters.visibility !== "all") {
    query.set("visibility", filters.visibility);
  }
  if (filters.attentionOnly) {
    query.set("attention", "true");
  }
  return `/admin/tournaments?${query.toString()}`;
}

function adminTournamentQueryKey(filters: AdminTournamentFilters): string {
  return JSON.stringify({
    search: filters.search.trim(),
    status: filters.status,
    visibility: filters.visibility,
    attentionOnly: filters.attentionOnly
  });
}

function appendUniqueTournaments(
  current: PlatformAdminTournament[],
  incoming: PlatformAdminTournament[]
): PlatformAdminTournament[] {
  const knownIds = new Set(current.map((tournament) => tournament.id));
  return [
    ...current,
    ...incoming.filter((tournament) => {
      if (knownIds.has(tournament.id)) {
        return false;
      }
      knownIds.add(tournament.id);
      return true;
    })
  ];
}

function TournamentInspector({
  tournament,
  enumLabel,
  formatDate,
  onDelete,
  onUpdate
}: {
  tournament: PlatformAdminTournament | null;
  enumLabel: (value: string | null | undefined) => string;
  formatDate: (value: string) => string;
  onDelete: (slug: string) => void;
  onUpdate: (tournament: PlatformAdminTournament) => void;
}) {
  const { t } = useI18n();
  const [status, setStatus] = useState(tournament?.status ?? "");
  const [visibility, setVisibility] = useState(tournament?.visibility ?? "");
  const [schedule, setSchedule] = useState(() => scheduleFromTournament(tournament));
  const [note, setNote] = useState("");
  const [isSaving, setIsSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [deleteConfirmation, setDeleteConfirmation] = useState("");
  const [deleteNote, setDeleteNote] = useState("");
  const [isDeleting, setIsDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState("");

  if (!tournament) {
    return <aside className="admin-inspector admin-empty">{t("admin.selectTournament")}</aside>;
  }

  const currentTournament = tournament;
  const scheduleChanged = status === "registration_open"
    && Object.entries(schedule).some(([key, value]) => (
      value !== dateTimeLocal(currentTournament[key as keyof PlatformAdminTournament] as string | null | undefined)
    ));
  const hasChange = status !== currentTournament.status
    || visibility !== currentTournament.visibility
    || scheduleChanged;
  const scheduleComplete = status !== "registration_open"
    || Object.values(schedule).every(Boolean);
  const canSave = hasChange && scheduleComplete && Boolean(note.trim()) && !isSaving;

  function changeStatus(nextStatus: string) {
    setStatus(nextStatus);
    if (nextStatus === "registration_open") {
      setSchedule((current) => fillFutureSchedule(current));
    }
  }

  async function applyOverride() {
    if (!canSave) {
      return;
    }
    setIsSaving(true);
    setMessage("");
    setError("");
    try {
      const includeSchedule = status === "registration_open";
      const updated = await platformApiRequest<PlatformAdminTournament>(`/admin/tournaments/${currentTournament.slug}`, {
        method: "PATCH",
        body: JSON.stringify({
          status: status !== currentTournament.status || scheduleChanged ? status : null,
          visibility: visibility === currentTournament.visibility ? null : visibility,
          registration_closes_at: includeSchedule ? toIso(schedule.registration_closes_at) : null,
          ready_check_starts_at: includeSchedule ? toIso(schedule.ready_check_starts_at) : null,
          ready_check_ends_at: includeSchedule ? toIso(schedule.ready_check_ends_at) : null,
          captain_selection_starts_at: includeSchedule ? toIso(schedule.captain_selection_starts_at) : null,
          starts_at: includeSchedule ? toIso(schedule.starts_at) : null,
          note: note.trim()
        })
      });
      onUpdate(updated);
      setStatus(updated.status);
      setVisibility(updated.visibility);
      setSchedule(scheduleFromTournament(updated));
      setNote("");
      setMessage(t("admin.overrideSaved", { name: updated.name }));
    } catch (requestError) {
      setError(platformApiMessage(requestError, t("admin.overrideFailed")));
    } finally {
      setIsSaving(false);
    }
  }

  async function deleteTournament() {
    if (
      deleteConfirmation.trim() !== currentTournament.name
      || deleteNote.trim().length < 3
      || isDeleting
    ) {
      return;
    }
    setIsDeleting(true);
    setDeleteError("");
    try {
      await platformApiRequest<void>(`/admin/tournaments/${currentTournament.slug}`, {
        method: "DELETE",
        body: JSON.stringify({
          confirmation_name: deleteConfirmation.trim(),
          note: deleteNote.trim()
        })
      });
      onDelete(currentTournament.slug);
    } catch (requestError) {
      setDeleteError(platformApiMessage(requestError, t("admin.deleteTournamentFailed")));
      setIsDeleting(false);
    }
  }

  return (
    <aside className="admin-inspector" data-testid="admin-tournament-inspector">
      <div className="admin-inspector-head">
        <div>
          <div className="admin-inspector-kicker">{t("admin.tournamentInspector")}</div>
          <h2>{tournament.name}</h2>
          <p>{tournament.organizer_display_name ?? t("common.unknown")} · {formatDate(tournament.created_at)}</p>
        </div>
        <Link className="admin-icon-link" href={`/tournaments/${tournament.slug}`} aria-label={t("admin.openTournament")}>
          <Eye size={18} />
        </Link>
      </div>

      <div className="admin-inspector-badges">
        <StatusBadge value={tournament.status} label={enumLabel(tournament.status)} />
        <StatusBadge value={tournament.visibility} label={enumLabel(tournament.visibility)} />
        <span className={tournament.has_locked_deadlock_roster ? "admin-lock-badge locked" : "admin-lock-badge"}>
          <LockKeyhole size={14} />
          {tournament.has_locked_deadlock_roster ? t("admin.rosterLocked") : t("admin.rosterOpen")}
        </span>
      </div>

      <div className="admin-diagnostics">
        <Diagnostic label={t("common.participants")} value={tournament.participant_count} />
        <Diagnostic label={t("common.matches")} value={tournament.match_count} />
        <Diagnostic label={t("common.latestRound")} value={tournament.latest_round_number ?? "—"} />
        <Diagnostic label={t("admin.unfinished")} value={tournament.unfinished_match_count} danger={tournament.unfinished_match_count > 0} />
      </div>

      {tournament.admin_override_warning ? (
        <div className="admin-callout danger"><AlertTriangle size={17} /><span>{tournament.admin_override_warning}</span></div>
      ) : null}
      {tournament.admin_recovery_hint ? (
        <div className="admin-callout info"><SlidersHorizontal size={17} /><span>{tournament.admin_recovery_hint}</span></div>
      ) : null}

      <div className="admin-form-section">
        <div className="admin-section-title">{t("admin.overrideControls")}</div>
        <div className="admin-form-grid">
          <label>
            <span>{t("admin.statusOverride")}</span>
            <select data-testid="admin-status-override" value={status} onChange={(event) => changeStatus(event.target.value)}>
              {tournamentStatuses.map((value) => <option key={value} value={value}>{enumLabel(value)}</option>)}
            </select>
          </label>
          <label>
            <span>{t("admin.visibilityOverride")}</span>
            <select data-testid="admin-visibility-override" value={visibility} onChange={(event) => setVisibility(event.target.value)}>
              <option value="public">{enumLabel("public")}</option>
              <option value="invite_only">{enumLabel("invite_only")}</option>
            </select>
          </label>
        </div>

        {status === "registration_open" ? (
          <div className="admin-schedule-editor" data-testid="admin-schedule-editor">
            <div className="admin-callout info">
              <SlidersHorizontal size={17} />
              <span>{t("admin.reopenScheduleNotice")}</span>
            </div>
            <ScheduleField
              label={t("admin.registrationClosesAt")}
              testId="admin-registration-closes-at"
              value={schedule.registration_closes_at}
              onChange={(value) => setSchedule((current) => ({ ...current, registration_closes_at: value }))}
            />
            <ScheduleField
              label={t("admin.readyCheckStartsAt")}
              value={schedule.ready_check_starts_at}
              onChange={(value) => setSchedule((current) => ({ ...current, ready_check_starts_at: value }))}
            />
            <ScheduleField
              label={t("admin.readyCheckEndsAt")}
              value={schedule.ready_check_ends_at}
              onChange={(value) => setSchedule((current) => ({ ...current, ready_check_ends_at: value }))}
            />
            <ScheduleField
              label={t("admin.captainSelectionStartsAt")}
              value={schedule.captain_selection_starts_at}
              onChange={(value) => setSchedule((current) => ({ ...current, captain_selection_starts_at: value }))}
            />
            <ScheduleField
              label={t("admin.tournamentStartsAt")}
              value={schedule.starts_at}
              onChange={(value) => setSchedule((current) => ({ ...current, starts_at: value }))}
            />
          </div>
        ) : null}

        <AuditNote value={note} onChange={setNote} />
        {!hasChange ? <div className="admin-inline-hint">{t("admin.chooseDifferentOverride")}</div> : null}
        {message ? <div className="admin-feedback success">{message}</div> : null}
        {error ? <div className="admin-feedback error">{error}</div> : null}
        <button
          className="primary-button admin-apply-button"
          data-testid="admin-apply-override"
          disabled={!canSave}
          type="button"
          onClick={() => void applyOverride()}
        >
          <ShieldCheck size={18} />{isSaving ? t("common.saving") : t("admin.applyOverride")}
        </button>
      </div>

      <div className="admin-form-section admin-danger-zone">
        <div className="admin-section-title">{t("admin.deleteTournament")}</div>
        <div className="admin-callout danger">
          <AlertTriangle size={17} />
          <span>{t("admin.deleteTournamentCopy")}</span>
        </div>
        <label className="admin-note-field">
          <span>{t("admin.deleteTournamentConfirm")}</span>
          <input
            data-testid="admin-delete-tournament-confirmation"
            maxLength={25}
            value={deleteConfirmation}
            onChange={(event) => setDeleteConfirmation(event.target.value)}
          />
          <small>{currentTournament.name}</small>
        </label>
        <label className="admin-note-field">
          <span>{t("admin.deleteTournamentNote")}</span>
          <textarea
            data-testid="admin-delete-tournament-note"
            maxLength={1000}
            value={deleteNote}
            onChange={(event) => setDeleteNote(event.target.value)}
          />
        </label>
        {deleteError ? <div className="admin-feedback error">{deleteError}</div> : null}
        <button
          className="secondary-button admin-apply-button danger"
          data-testid="admin-delete-tournament"
          disabled={deleteConfirmation.trim() !== currentTournament.name || deleteNote.trim().length < 3 || isDeleting}
          type="button"
          onClick={() => void deleteTournament()}
        >
          <Trash2 size={18} />
          {isDeleting ? t("common.loading") : t("admin.deleteTournament")}
        </button>
      </div>
    </aside>
  );
}

function UserPermissions({
  users,
  currentUser,
  selectedUserId,
  isLoading,
  loadError,
  formatDate,
  onSelect,
  onSearch,
  onUpdate
}: {
  users: PlatformUser[];
  currentUser: PlatformUser;
  selectedUserId: string | null;
  isLoading: boolean;
  loadError: string;
  formatDate: (value: string) => string;
  onSelect: (userId: string) => void;
  onSearch: (search: string) => void;
  onUpdate: (user: PlatformUser) => void;
}) {
  const { t } = useI18n();
  const [search, setSearch] = useState("");
  const [permissionFilter, setPermissionFilter] = useState("all");
  const filtered = users.filter((user) => {
    const matchesPermission = permissionFilter === "all"
      || (permissionFilter === "allowed"
        ? user.can_create_public_tournaments
        : !user.can_create_public_tournaments);
    return matchesPermission;
  });
  const selected = users.find((user) => user.id === selectedUserId) ?? null;

  useEffect(() => {
    const timeout = window.setTimeout(() => onSearch(search), 300);
    return () => window.clearTimeout(timeout);
  }, [onSearch, search]);

  return (
    <div className="admin-section">
      <div className="admin-toolbar admin-users-toolbar">
        <label className="admin-search">
          <Search size={18} />
          <input
            data-testid="admin-user-search"
            maxLength={120}
            value={search}
            placeholder={t("admin.searchUsers")}
            onChange={(event) => setSearch(event.target.value)}
          />
        </label>
        <label className="admin-select-wrap">
          <span>{t("admin.publicCredits")}</span>
          <select value={permissionFilter} onChange={(event) => setPermissionFilter(event.target.value)}>
            <option value="all">{t("admin.allPermissions")}</option>
            <option value="allowed">{t("admin.publicCreateAllowed")}</option>
            <option value="blocked">{t("admin.publicCreateBlocked")}</option>
          </select>
        </label>
        <div className="admin-result-count">
          {isLoading ? t("common.loading") : t("admin.recordsFound", { count: filtered.length })}
        </div>
      </div>

      <div className="admin-workspace">
        <div className="admin-table-panel">
          <div className="admin-table-scroll">
            <table className="admin-table">
              <thead>
                <tr>
                  <th>{t("admin.user")}</th>
                  <th>{t("common.roles")}</th>
                  <th>{t("admin.accountState")}</th>
                  <th>{t("admin.publicCredits")}</th>
                  <th>{t("admin.privateCredits")}</th>
                  <th>{t("common.created")}</th>
                  <th><span className="sr-only">{t("common.edit")}</span></th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((user) => (
                  <tr
                    className={selectedUserId === user.id ? "selected admin-clickable-row" : "admin-clickable-row"}
                    data-testid={`admin-user-${user.id}`}
                    key={user.id}
                    tabIndex={0}
                    onClick={() => onSelect(user.id)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        onSelect(user.id);
                      }
                    }}
                  >
                    <td>
                      <div className="admin-record-button">
                        <strong>{user.display_name}</strong>
                        <span>{user.email ?? "Без почты"}</span>
                      </div>
                    </td>
                    <td><div className="admin-role-list">{user.roles.map((role) => <span key={role}>{role}</span>)}</div></td>
                    <td><StatusBadge value={user.status} label={user.status} /></td>
                    <td>{creditDisplay(user, user.public_tournament_credits ?? 0, t("admin.unlimited"))}</td>
                    <td>{creditDisplay(user, user.private_tournament_credits ?? 0, t("admin.unlimited"))}</td>
                    <td>{formatDate(user.created_at)}</td>
                    <td><ChevronRight size={18} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {loadError ? <div className="admin-feedback error">{loadError}</div> : null}
          {filtered.length === 0 ? <div className="admin-empty">{t("admin.noUsers")}</div> : null}
        </div>

        <UserInspector
          currentUser={currentUser}
          key={selected?.id ?? "empty"}
          user={selected}
          onUpdate={onUpdate}
        />
      </div>
    </div>
  );
}

function UserInspector({
  user,
  currentUser,
  onUpdate
}: {
  user: PlatformUser | null;
  currentUser: PlatformUser;
  onUpdate: (user: PlatformUser) => void;
}) {
  const { t } = useI18n();
  const [publicCredits, setPublicCredits] = useState(user?.public_tournament_credits ?? 0);
  const [privateCredits, setPrivateCredits] = useState(user?.private_tournament_credits ?? 0);
  const [note, setNote] = useState("");
  const [isSavingCredits, setIsSavingCredits] = useState(false);
  const [isSavingRole, setIsSavingRole] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  if (!user) {
    return <aside className="admin-inspector admin-empty">{t("admin.selectUser")}</aside>;
  }

  const currentInspectedUser = user;
  const isAdmin = user.roles.includes("admin");
  const isSuperadmin = currentUser.roles.includes("superadmin");
  const creditsChanged = publicCredits !== (user.public_tournament_credits ?? 0)
    || privateCredits !== (user.private_tournament_credits ?? 0);
  const canSaveCredits = creditsChanged && Boolean(note.trim()) && !isSavingCredits;

  async function saveCredits() {
    if (!canSaveCredits) {
      return;
    }
    setIsSavingCredits(true);
    setMessage("");
    setError("");
    try {
      const updated = await platformApiRequest<PlatformUser>(`/admin/users/${currentInspectedUser.id}/tournament-credits`, {
        method: "PATCH",
        body: JSON.stringify({
          public_tournament_credits: publicCredits,
          private_tournament_credits: privateCredits,
          note: note.trim()
        })
      });
      onUpdate(updated);
      setNote("");
      setMessage(t("admin.creditsSaved", { name: updated.display_name }));
    } catch (requestError) {
      setError(platformApiMessage(requestError, t("admin.creditsFailed")));
    } finally {
      setIsSavingCredits(false);
    }
  }

  async function toggleAdminRole() {
    if (!isSuperadmin || !note.trim()) {
      return;
    }
    setIsSavingRole(true);
    setMessage("");
    setError("");
    try {
      const updated = await platformApiRequest<PlatformUser>(`/admin/users/${currentInspectedUser.id}/admin-role`, {
        method: "PATCH",
        body: JSON.stringify({
          is_admin: !isAdmin,
          note: note.trim()
        })
      });
      onUpdate(updated);
      setNote("");
      setMessage(t("admin.adminRoleSaved", { name: updated.display_name }));
    } catch (requestError) {
      setError(platformApiMessage(requestError, t("admin.adminRoleFailed")));
    } finally {
      setIsSavingRole(false);
    }
  }

  return (
    <aside className="admin-inspector" data-testid="admin-user-inspector">
      <div className="admin-inspector-head">
        <div>
          <div className="admin-inspector-kicker">{t("admin.userInspector")}</div>
          <h2>{user.display_name}</h2>
          <p>{user.email ?? "Без почты · Steam"}</p>
        </div>
        <div className="admin-user-avatar">{user.display_name.slice(0, 2).toUpperCase()}</div>
      </div>

      <div className="admin-role-list admin-role-list-large">
        {user.roles.length ? user.roles.map((role) => <span key={role}>{role}</span>) : <span>{t("admin.noRoles")}</span>}
      </div>

      <div className="admin-form-section">
        <div className="admin-section-title">{t("admin.tournamentCredits")}</div>
        <p className="admin-section-copy">{t("admin.creditsExplanation")}</p>
        <div className="admin-form-grid">
          <NumberField
            label={t("admin.publicCredits")}
            testId="admin-public-credits"
            value={publicCredits}
            onChange={setPublicCredits}
          />
          <NumberField
            label={t("admin.privateCredits")}
            testId="admin-private-credits"
            value={privateCredits}
            onChange={setPrivateCredits}
          />
        </div>

        <AuditNote value={note} onChange={setNote} />
        {message ? <div className="admin-feedback success">{message}</div> : null}
        {error ? <div className="admin-feedback error">{error}</div> : null}
        <button
          className="primary-button admin-apply-button"
          data-testid="admin-save-credits"
          disabled={!canSaveCredits}
          type="button"
          onClick={() => void saveCredits()}
        >
          <ShieldCheck size={18} />{isSavingCredits ? t("common.saving") : t("admin.saveCredits")}
        </button>
      </div>

      <div className="admin-form-section">
        <div className="admin-section-title">{t("admin.adminAccess")}</div>
        <p className="admin-section-copy">
          {isSuperadmin ? t("admin.adminAccessExplanation") : t("admin.superadminRequired")}
        </p>
        <button
          className={isAdmin ? "secondary-button admin-apply-button danger" : "primary-button admin-apply-button"}
          data-testid="admin-toggle-admin-role"
          disabled={!isSuperadmin || !note.trim() || isSavingRole}
          type="button"
          onClick={() => void toggleAdminRole()}
        >
          <UserCog size={18} />
          {isSavingRole
            ? t("common.updating")
            : isAdmin
              ? t("admin.revokeAdmin")
              : t("admin.grantAdmin")}
        </button>
      </div>
    </aside>
  );
}

function PreprodQaOperations({
  runs,
  currentUser,
  formatDate,
  onReload
}: {
  runs: PlatformAdminPreprodTestRun[];
  currentUser: PlatformUser;
  formatDate: (value: string) => string;
  onReload: () => Promise<void>;
}) {
  const { t } = useI18n();
  const [selectedMarker, setSelectedMarker] = useState<string | null>(runs[0]?.marker ?? null);
  const [note, setNote] = useState("");
  const [confirm, setConfirm] = useState("");
  const [isCleaning, setIsCleaning] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const selected = runs.find((run) => run.marker === selectedMarker) ?? runs[0] ?? null;
  const isSuperadmin = currentUser.roles.includes("superadmin");
  const canClean = isSuperadmin && confirm === "DELETE_TEST_DATA" && note.trim().length >= 3 && !isCleaning;

  useEffect(() => {
    if (selectedMarker && runs.some((run) => run.marker === selectedMarker)) {
      return;
    }
    setSelectedMarker(runs[0]?.marker ?? null);
  }, [runs, selectedMarker]);

  async function cleanup(all: boolean) {
    if (!canClean || (!all && !selected)) {
      return;
    }
    setIsCleaning(true);
    setMessage("");
    setError("");

    try {
      const path = all
        ? "/admin/preprod-test-runs/cleanup"
        : `/admin/preprod-test-runs/${encodeURIComponent(selected!.marker)}/cleanup`;
      const result = await platformApiRequest<PlatformAdminPreprodCleanupResult>(path, {
        method: "POST",
        body: JSON.stringify({
          confirm,
          note: note.trim()
        })
      });
      setMessage(t("admin.preprodCleanupDone", {
        users: result.users_deleted,
        tournaments: result.tournaments_deleted
      }));
      setConfirm("");
      setNote("");
    } catch (requestError) {
      setError(platformApiMessage(requestError, t("admin.preprodCleanupFailed")));
      setIsCleaning(false);
      return;
    }

    try {
      await onReload();
    } catch {
      // Cleanup is already committed. A refresh failure must not turn the
      // successful destructive action into a false cleanup error.
    } finally {
      setIsCleaning(false);
    }
  }

  return (
    <div className="admin-section">
      <div className="admin-audit-explanation">
        <Activity size={22} />
        <div>
          <strong>{t("admin.preprodQaTitle")}</strong>
          <span>{t("admin.preprodQaCopy")}</span>
        </div>
      </div>
      <div className="admin-workspace">
        <div className="admin-table-panel">
          <div className="admin-table-scroll">
            <table className="admin-table">
              <thead>
                <tr>
                  <th>{t("admin.preprodMarker")}</th>
                  <th>{t("admin.state")}</th>
                  <th>{t("common.users")}</th>
                  <th>{t("common.tournaments")}</th>
                  <th>{t("common.teams")}</th>
                  <th>{t("common.matches")}</th>
                  <th>{t("admin.auditWhen")}</th>
                  <th><span className="sr-only">{t("common.edit")}</span></th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <tr
                    className={selected?.marker === run.marker ? "selected admin-clickable-row" : "admin-clickable-row"}
                    data-testid={`admin-preprod-run-${run.marker}`}
                    key={run.marker}
                    tabIndex={0}
                    onClick={() => setSelectedMarker(run.marker)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        setSelectedMarker(run.marker);
                      }
                    }}
                  >
                    <td>
                      <div className="admin-record-button">
                        <strong>{run.marker}</strong>
                        <span>{run.origin ?? t("common.unknown")}</span>
                      </div>
                    </td>
                    <td><StatusBadge value={run.status} label={enumPreprodStatus(run.status, t)} /></td>
                    <td>{run.created_users.toLocaleString("ru-RU")} / {run.requested_users.toLocaleString("ru-RU")}</td>
                    <td>{run.tournaments_created}</td>
                    <td>{run.teams_count}</td>
                    <td>{run.matches_count}</td>
                    <td>{formatDate(run.started_at ?? run.created_at)}</td>
                    <td><ChevronRight size={18} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {runs.length === 0 ? <div className="admin-empty">{t("admin.preprodNoRuns")}</div> : null}
        </div>

        <aside className="admin-inspector" data-testid="admin-preprod-inspector">
          {selected ? (
            <>
              <div className="admin-inspector-head">
                <div>
                  <div className="admin-inspector-kicker">{t("admin.preprodRun")}</div>
                  <h2>{selected.marker}</h2>
                  <p>{selected.finished_at ? formatDate(selected.finished_at) : t("admin.preprodStillRunning")}</p>
                </div>
                <StatusBadge value={selected.status} label={enumPreprodStatus(selected.status, t)} />
              </div>

              <div className="admin-diagnostics">
                <Diagnostic label={t("admin.preprodRegistered")} value={selected.created_users.toLocaleString("ru-RU")} />
                <Diagnostic label={t("admin.preprodParticipants")} value={selected.active_participants.toLocaleString("ru-RU")} />
                <Diagnostic label={t("common.teams")} value={selected.teams_count} />
                <Diagnostic label={t("common.matches")} value={selected.matches_count} />
                <Diagnostic label={t("admin.preprodPreferenceRate")} value={`${reportMetric(selected, "starter_preference_slots_fully_honored_rate_percent").toFixed(2)}%`} />
                <Diagnostic label={t("admin.preprodSpread")} value={`${reportMetric(selected, "spread_percent").toFixed(2)}%`} />
              </div>

              {selected.report_path ? (
                <div className="admin-callout info"><ClipboardList size={17} /><span>{selected.report_path}</span></div>
              ) : null}
              {selected.cleanup_state?.ok ? (
                <div className="admin-callout info"><CheckCircle2 size={17} /><span>{t("admin.preprodCleaned")}</span></div>
              ) : null}
            </>
          ) : (
            <div className="admin-empty">{t("admin.preprodSelectRun")}</div>
          )}

          <div className="admin-form-section">
            <div className="admin-section-title">{t("admin.preprodCleanup")}</div>
            <p className="admin-section-copy">
              {isSuperadmin ? t("admin.preprodCleanupCopy") : t("admin.superadminRequired")}
            </p>
            <label>
              <span>{t("admin.preprodCleanupConfirm")}</span>
              <input
                data-testid="admin-preprod-cleanup-confirm"
                disabled={!isSuperadmin}
                maxLength={16}
                value={confirm}
                onChange={(event) => setConfirm(event.target.value)}
              />
            </label>
            <AuditNote value={note} onChange={setNote} />
            {message ? <div className="admin-feedback success">{message}</div> : null}
            {error ? <div className="admin-feedback error">{error}</div> : null}
            <div className="admin-preprod-cleanup-actions">
              <button
                className="secondary-button admin-apply-button danger"
                data-testid="admin-preprod-cleanup-selected"
                disabled={!canClean || !selected}
                type="button"
                onClick={() => void cleanup(false)}
              >
                <Trash2 size={18} />{isCleaning ? t("common.saving") : t("admin.preprodCleanupSelected")}
              </button>
              <button
                className="primary-button admin-apply-button danger"
                data-testid="admin-preprod-cleanup-all"
                disabled={!canClean || runs.length === 0}
                type="button"
                onClick={() => void cleanup(true)}
              >
                <Trash2 size={18} />{isCleaning ? t("common.saving") : t("admin.preprodCleanupAll")}
              </button>
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}

function AuditOperations({
  auditLogs,
  selectedAuditId,
  formatDate,
  onSelect
}: {
  auditLogs: PlatformAuditLog[];
  selectedAuditId: number | null;
  formatDate: (value: string) => string;
  onSelect: (id: number) => void;
}) {
  const { t } = useI18n();
  const [search, setSearch] = useState("");
  const normalizedSearch = search.trim().toLowerCase();
  const filtered = auditLogs.filter((audit) => (
    !normalizedSearch
    || [
      audit.action,
      audit.subject_type,
      audit.subject_id ?? "",
      audit.actor_display_name ?? "",
      audit.actor_email ?? "",
      String(audit.payload.note ?? "")
    ].join(" ").toLowerCase().includes(normalizedSearch)
  ));
  const selected = auditLogs.find((audit) => audit.id === selectedAuditId) ?? null;

  return (
    <div className="admin-section">
      <div className="admin-audit-explanation">
        <ClipboardList size={22} />
        <div>
          <strong>{t("admin.auditMeaningTitle")}</strong>
          <span>{t("admin.auditMeaningCopy")}</span>
        </div>
      </div>
      <div className="admin-toolbar">
        <label className="admin-search">
          <Search size={18} />
          <input maxLength={120} value={search} placeholder={t("admin.searchAudit")} onChange={(event) => setSearch(event.target.value)} />
        </label>
        <div className="admin-result-count">{t("admin.recordsFound", { count: filtered.length })}</div>
      </div>
      <div className="admin-workspace">
        <div className="admin-table-panel">
          <div className="admin-table-scroll">
            <table className="admin-table admin-audit-table">
              <thead>
                <tr>
                  <th>{t("admin.auditWhen")}</th>
                  <th>{t("admin.auditActor")}</th>
                  <th>{t("admin.auditAction")}</th>
                  <th>{t("admin.auditSubject")}</th>
                  <th>{t("admin.auditReason")}</th>
                  <th><span className="sr-only">{t("common.view")}</span></th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((audit) => (
                  <tr
                    className={selectedAuditId === audit.id ? "selected admin-clickable-row" : "admin-clickable-row"}
                    key={audit.id}
                    tabIndex={0}
                    onClick={() => onSelect(audit.id)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        onSelect(audit.id);
                      }
                    }}
                  >
                    <td>{formatDate(audit.created_at)}</td>
                    <td>
                      <span className="admin-table-primary">{audit.actor_display_name ?? t("admin.systemActor")}</span>
                      <span className="admin-table-secondary">{audit.actor_email ?? "—"}</span>
                    </td>
                    <td><code>{audit.action}</code></td>
                    <td>
                      <span className="admin-table-primary">{audit.subject_type}</span>
                      <span className="admin-table-secondary">{audit.subject_id ?? "—"}</span>
                    </td>
                    <td>{String(audit.payload.note ?? "—")}</td>
                    <td><ChevronRight size={18} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {filtered.length === 0 ? <div className="admin-empty">{t("admin.noAuditEvents")}</div> : null}
        </div>
        <AuditInspector audit={selected} formatDate={formatDate} />
      </div>
    </div>
  );
}

function AuditInspector({
  audit,
  formatDate
}: {
  audit: PlatformAuditLog | null;
  formatDate: (value: string) => string;
}) {
  const { t } = useI18n();
  if (!audit) {
    return <aside className="admin-inspector admin-empty">{t("admin.selectAudit")}</aside>;
  }
  return (
    <aside className="admin-inspector" data-testid="admin-audit-inspector">
      <div className="admin-inspector-head">
        <div>
          <div className="admin-inspector-kicker">{t("admin.auditInspector")}</div>
          <h2>{audit.action}</h2>
          <p>{formatDate(audit.created_at)}</p>
        </div>
        <ClipboardList size={24} />
      </div>
      <div className="admin-diagnostics">
        <Diagnostic label={t("admin.auditActor")} value={audit.actor_display_name ?? t("admin.systemActor")} />
        <Diagnostic label={t("admin.auditSubject")} value={audit.subject_type} />
      </div>
      <div className="admin-audit-detail">
        <strong>{t("admin.auditSubjectId")}</strong>
        <code>{audit.subject_id ?? "—"}</code>
      </div>
      <div className="admin-audit-detail">
        <strong>{t("admin.auditReason")}</strong>
        <span>{String(audit.payload.note ?? t("admin.noAuditReason"))}</span>
      </div>
      <div className="admin-audit-detail">
        <strong>{t("admin.auditPayload")}</strong>
        <pre>{JSON.stringify(audit.payload, null, 2)}</pre>
      </div>
    </aside>
  );
}

function AuditNote({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const { t } = useI18n();
  return (
    <label className="admin-note-field">
      <span>{t("admin.auditNote")}</span>
      <textarea
        data-testid="admin-override-note"
        maxLength={1000}
        value={value}
        placeholder={t("admin.auditNotePlaceholder")}
        onChange={(event) => onChange(event.target.value)}
      />
      <small>{t("admin.auditNoteExplanation")}</small>
    </label>
  );
}

function ScheduleField({
  label,
  value,
  testId,
  onChange
}: {
  label: string;
  value: string;
  testId?: string;
  onChange: (value: string) => void;
}) {
  return (
    <label>
      <span>{label}</span>
      <input
        data-testid={testId}
        type="datetime-local"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}

function NumberField({
  label,
  value,
  testId,
  onChange
}: {
  label: string;
  value: number;
  testId: string;
  onChange: (value: number) => void;
}) {
  return (
    <label>
      <span>{label}</span>
      <input
        data-testid={testId}
        min={0}
        max={1000}
        type="number"
        value={value}
        onChange={(event) => onChange(Math.max(0, Math.min(1000, Number(event.target.value) || 0)))}
      />
    </label>
  );
}

function AdminMetric({
  icon,
  label,
  value,
  danger = false
}: {
  icon: React.ReactNode;
  label: string;
  value: number;
  danger?: boolean;
}) {
  return (
    <article className={danger ? "admin-metric danger" : "admin-metric"}>
      <div className="admin-metric-icon">{icon}</div>
      <div>
        <span>{label}</span>
        <strong>{value.toLocaleString("ru-RU")}</strong>
      </div>
    </article>
  );
}

function Diagnostic({ label, value, danger = false }: { label: string; value: string | number; danger?: boolean }) {
  return (
    <div className={danger ? "admin-diagnostic danger" : "admin-diagnostic"}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function StatusBadge({ value, label }: { value: string; label: string }) {
  return <span className={`admin-status-badge status-${value}`}>{label}</span>;
}

function enumPreprodStatus(
  value: string,
  t: (key: string, params?: Record<string, string | number | null | undefined>) => string
): string {
  const labels: Record<string, string> = {
    running: t("admin.preprodStatusRunning"),
    passed: t("admin.preprodStatusPassed"),
    failed: t("admin.preprodStatusFailed"),
    cleaned: t("admin.preprodStatusCleaned")
  };
  return labels[value] ?? value;
}

function reportMetric(run: PlatformAdminPreprodTestRun, key: string): number {
  const report = run.report ?? {};
  const direct = report[key];
  if (typeof direct === "number" && Number.isFinite(direct)) {
    return direct;
  }
  const preferenceMetrics = report.preference_metrics;
  if (preferenceMetrics && typeof preferenceMetrics === "object" && key in preferenceMetrics) {
    const value = (preferenceMetrics as Record<string, unknown>)[key];
    return typeof value === "number" && Number.isFinite(value) ? value : 0;
  }
  const optimizationSummary = report.optimization_summary;
  if (optimizationSummary && typeof optimizationSummary === "object" && key in optimizationSummary) {
    const value = (optimizationSummary as Record<string, unknown>)[key];
    return typeof value === "number" && Number.isFinite(value) ? value : 0;
  }
  return 0;
}

function AdminStatePanel({
  title,
  copy,
  children
}: {
  title: string;
  copy: string;
  children?: React.ReactNode;
}) {
  return (
    <section className="panel admin-state-panel">
      <ShieldCheck size={34} />
      <h1>{title}</h1>
      <p>{copy}</p>
      {children ? <div className="admin-state-actions">{children}</div> : null}
    </section>
  );
}

type ScheduleDraft = {
  registration_closes_at: string;
  ready_check_starts_at: string;
  ready_check_ends_at: string;
  captain_selection_starts_at: string;
  starts_at: string;
};

function scheduleFromTournament(tournament: PlatformAdminTournament | null): ScheduleDraft {
  return {
    registration_closes_at: dateTimeLocal(tournament?.registration_closes_at),
    ready_check_starts_at: dateTimeLocal(tournament?.ready_check_starts_at),
    ready_check_ends_at: dateTimeLocal(tournament?.ready_check_ends_at),
    captain_selection_starts_at: dateTimeLocal(tournament?.captain_selection_starts_at),
    starts_at: dateTimeLocal(tournament?.starts_at)
  };
}

function fillFutureSchedule(schedule: ScheduleDraft): ScheduleDraft {
  const now = Date.now();
  const offsets = {
    registration_closes_at: 24 * 60,
    ready_check_starts_at: 25 * 60,
    ready_check_ends_at: 25 * 60 + 30,
    captain_selection_starts_at: 25 * 60 + 35,
    starts_at: 27 * 60
  };
  return Object.fromEntries(
    Object.entries(offsets).map(([key, minutes]) => {
      const current = schedule[key as keyof ScheduleDraft];
      const currentTimestamp = current ? new Date(current).getTime() : 0;
      return [
        key,
        currentTimestamp > now ? current : dateTimeLocal(new Date(now + minutes * 60_000).toISOString())
      ];
    })
  ) as ScheduleDraft;
}

function dateTimeLocal(value: string | null | undefined): string {
  if (!value) {
    return "";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function toIso(value: string): string | null {
  if (!value) {
    return null;
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

function creditDisplay(user: PlatformUser, credits: number, unlimitedLabel: string): string | number {
  return user.roles.some((role) => role === "admin" || role === "superadmin")
    ? unlimitedLabel
    : credits;
}
