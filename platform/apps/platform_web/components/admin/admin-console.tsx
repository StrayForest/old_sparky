"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Activity, BarChart3, ClipboardList, ExternalLink, LayoutDashboard, RefreshCcw, ShieldCheck, Trophy, UsersRound } from "lucide-react";
import { useAuth } from "@/components/auth/auth-provider";
import { useI18n } from "@/components/i18n-provider";
import { AdminAnalytics, AdminOverview, type AdminView } from "@/components/admin/admin-insights";
import { AdminAuditPage } from "@/components/admin/admin-audit-page";
import { AdminPreprodPage } from "@/components/admin/admin-preprod-page";
import { AdminTournamentsPage } from "@/components/admin/admin-tournaments-page";
import { AdminUsersPage } from "@/components/admin/admin-users-page";
import { PlatformApiError, platformApiMessage, platformApiPageRequest, platformApiRequest } from "@/lib/platform-api";
import type { PlatformAdminOverview, PlatformAdminPreprodTestRun, PlatformAdminTournament, PlatformAuditLog, PlatformUser } from "@/lib/platform-types";

type AccessState = "loading" | "ready" | "unauthenticated" | "forbidden" | "error";
type AdminTournamentFilters = { search: string; status: string; visibility: string; attentionOnly: boolean };
const adminTournamentPageSize = 25;
const defaultTournamentFilters: AdminTournamentFilters = { search: "", status: "all", visibility: "all", attentionOnly: false };

export function AdminConsole() {
  const { user: currentUser } = useAuth();
  const { enumLabel, formatDate, t } = useI18n();
  const [accessState, setAccessState] = useState<AccessState>("loading");
  const [activeView, setActiveView] = useState<AdminView>("overview");
  const [overview, setOverview] = useState<PlatformAdminOverview | null>(null);
  const [users, setUsers] = useState<PlatformUser[]>([]);
  const [tournaments, setTournaments] = useState<PlatformAdminTournament[]>([]);
  const [auditLogs, setAuditLogs] = useState<PlatformAuditLog[]>([]);
  const [preprodRuns, setPreprodRuns] = useState<PlatformAdminPreprodTestRun[]>([]);
  const [selectedUserId, setSelectedUserId] = useState<string | null>(null);
  const [selectedTournamentSlug, setSelectedTournamentSlug] = useState<string | null>(null);
  const [selectedAuditId, setSelectedAuditId] = useState<number | null>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [userLoadError, setUserLoadError] = useState("");
  const [isLoadingUsers, setIsLoadingUsers] = useState(false);
  const [tournamentFilters, setTournamentFilters] = useState(defaultTournamentFilters);
  const [debouncedTournamentSearch, setDebouncedTournamentSearch] = useState("");
  const [tournamentTotal, setTournamentTotal] = useState(0);
  const [nextTournamentOffset, setNextTournamentOffset] = useState(0);
  const [hasMoreTournaments, setHasMoreTournaments] = useState(false);
  const [isFilteringTournaments, setIsFilteringTournaments] = useState(false);
  const [isLoadingMoreTournaments, setIsLoadingMoreTournaments] = useState(false);
  const [tournamentPageError, setTournamentPageError] = useState("");
  const [failedTournamentOffset, setFailedTournamentOffset] = useState<number | null>(null);
  const consoleGeneration = useRef(0);
  const usersGeneration = useRef(0);
  const usersController = useRef<AbortController | null>(null);
  const tournamentController = useRef<AbortController | null>(null);
  const loadMoreInFlight = useRef(false);
  const activeTournamentFilters = useRef(defaultTournamentFilters);
  const requestedTournamentFilters = useMemo(() => ({ ...tournamentFilters, search: debouncedTournamentSearch }), [debouncedTournamentSearch, tournamentFilters]);
  const requestedTournamentQueryKey = tournamentQueryKey(requestedTournamentFilters);
  const [resolvedTournamentQueryKey, setResolvedTournamentQueryKey] = useState(tournamentQueryKey(defaultTournamentFilters));
  activeTournamentFilters.current = requestedTournamentFilters;

  const loadUsers = useCallback(async (search = "") => {
    const generation = ++usersGeneration.current;
    usersController.current?.abort();
    const controller = new AbortController();
    usersController.current = controller;
    setIsLoadingUsers(true); setUserLoadError("");
    try {
      const query = search.trim() ? `?search=${encodeURIComponent(search.trim())}` : "";
      const nextUsers = await platformApiRequest<PlatformUser[]>(`/admin/users${query}`, { signal: controller.signal });
      if (generation !== usersGeneration.current || controller.signal.aborted) return;
      setUsers(nextUsers); setSelectedUserId((current) => current && nextUsers.some((item) => item.id === current) ? current : nextUsers[0]?.id ?? null);
    } catch (error) {
      if (generation === usersGeneration.current && !controller.signal.aborted) setUserLoadError(platformApiMessage(error, t("admin.new.loadFailed")));
    } finally {
      if (generation === usersGeneration.current) { setIsLoadingUsers(false); if (usersController.current === controller) usersController.current = null; }
    }
  }, [t]);

  const loadPreprod = useCallback(async () => { const nextRuns = await platformApiRequest<PlatformAdminPreprodTestRun[]>("/admin/preprod-test-runs"); setPreprodRuns(nextRuns); }, []);

  const loadConsole = useCallback(async (refresh = false) => {
    const generation = ++consoleGeneration.current;
    const usersGenerationAtStart = ++usersGeneration.current;
    usersController.current?.abort(); usersController.current = null; setIsLoadingUsers(false);
    tournamentController.current?.abort();
    const controller = new AbortController(); tournamentController.current = controller;
    loadMoreInFlight.current = false; setIsLoadingMoreTournaments(false); setIsFilteringTournaments(false); setTournamentPageError(""); setFailedTournamentOffset(null);
    if (refresh) setIsRefreshing(true); else setAccessState("loading");
    setLoadError("");
    try {
      if (!currentUser) { setAccessState("unauthenticated"); return; }
      if (!currentUser.roles.some((role) => role === "admin" || role === "superadmin")) { setAccessState("forbidden"); return; }
      const [nextOverview, tournamentPage, nextUsers, nextAuditLogs, nextPreprodRuns] = await Promise.all([
        platformApiRequest<PlatformAdminOverview>("/admin/overview"),
        platformApiPageRequest<PlatformAdminTournament>(tournamentListPath(activeTournamentFilters.current, 0), { signal: controller.signal }),
        platformApiRequest<PlatformUser[]>("/admin/users"),
        platformApiRequest<PlatformAuditLog[]>("/admin/audit-logs?limit=200"),
        platformApiRequest<PlatformAdminPreprodTestRun[]>("/admin/preprod-test-runs")
      ]);
      if (generation !== consoleGeneration.current || controller.signal.aborted) return;
      setOverview(nextOverview); setTournaments(tournamentPage.items); setTournamentTotal(tournamentPage.total); setHasMoreTournaments(tournamentPage.hasMore); setNextTournamentOffset(tournamentPage.offset + tournamentPage.limit); setResolvedTournamentQueryKey(tournamentQueryKey(activeTournamentFilters.current));
      if (usersGenerationAtStart === usersGeneration.current) { setUsers(nextUsers); setSelectedUserId((current) => current && nextUsers.some((item) => item.id === current) ? current : nextUsers[0]?.id ?? null); }
      setAuditLogs(nextAuditLogs); setPreprodRuns(nextPreprodRuns); setSelectedTournamentSlug((current) => current && tournamentPage.items.some((item) => item.slug === current) ? current : tournamentPage.items[0]?.slug ?? null); setSelectedAuditId((current) => current && nextAuditLogs.some((item) => item.id === current) ? current : nextAuditLogs[0]?.id ?? null); setAccessState("ready");
    } catch (error) {
      if (generation !== consoleGeneration.current || controller.signal.aborted) return;
      if (error instanceof PlatformApiError && error.status === 401) setAccessState("unauthenticated"); else if (error instanceof PlatformApiError && error.status === 403) setAccessState("forbidden"); else { setLoadError(platformApiMessage(error, t("admin.new.loadFailed"))); setAccessState("error"); }
    } finally {
      if (generation === consoleGeneration.current) { setIsRefreshing(false); if (tournamentController.current === controller) tournamentController.current = null; }
    }
  }, [currentUser, t]);

  const loadFilteredTournaments = useCallback(async (filters: AdminTournamentFilters, queryKey: string) => {
    const generation = ++consoleGeneration.current; tournamentController.current?.abort(); const controller = new AbortController(); tournamentController.current = controller; loadMoreInFlight.current = false; setIsFilteringTournaments(true); setIsLoadingMoreTournaments(false); setTournamentPageError(""); setFailedTournamentOffset(null); setTournaments([]); setSelectedTournamentSlug(null);
    try { const page = await platformApiPageRequest<PlatformAdminTournament>(tournamentListPath(filters, 0), { signal: controller.signal }); if (generation !== consoleGeneration.current || controller.signal.aborted) return; setTournaments(page.items); setTournamentTotal(page.total); setHasMoreTournaments(page.hasMore); setNextTournamentOffset(page.offset + page.limit); setSelectedTournamentSlug(page.items[0]?.slug ?? null); setResolvedTournamentQueryKey(queryKey); }
    catch (error) { if (generation === consoleGeneration.current && !controller.signal.aborted) { setTournamentPageError(platformApiMessage(error, t("admin.new.loadFailed"))); setFailedTournamentOffset(0); setTournamentTotal(0); setHasMoreTournaments(false); } }
    finally { if (generation === consoleGeneration.current) { setIsFilteringTournaments(false); if (tournamentController.current === controller) tournamentController.current = null; } }
  }, [t]);

  const loadMoreTournaments = useCallback(async () => {
    if (!hasMoreTournaments || isFilteringTournaments || isRefreshing || loadMoreInFlight.current) return;
    const generation = consoleGeneration.current; const controller = new AbortController(); tournamentController.current = controller; loadMoreInFlight.current = true; setIsLoadingMoreTournaments(true); setTournamentPageError("");
    try { const page = await platformApiPageRequest<PlatformAdminTournament>(tournamentListPath(activeTournamentFilters.current, nextTournamentOffset), { signal: controller.signal }); if (generation !== consoleGeneration.current || controller.signal.aborted) return; setTournaments((current) => appendUnique(current, page.items)); setTournamentTotal(page.total); setHasMoreTournaments(page.hasMore); setNextTournamentOffset(page.offset + page.limit); setFailedTournamentOffset(null); }
    catch (error) { if (generation === consoleGeneration.current && !controller.signal.aborted) { setTournamentPageError(platformApiMessage(error, t("admin.new.loadFailed"))); setFailedTournamentOffset(nextTournamentOffset); } }
    finally { if (generation === consoleGeneration.current) { loadMoreInFlight.current = false; setIsLoadingMoreTournaments(false); if (tournamentController.current === controller) tournamentController.current = null; } }
  }, [hasMoreTournaments, isFilteringTournaments, isRefreshing, nextTournamentOffset, t]);

  useEffect(() => { const timeout = window.setTimeout(() => setDebouncedTournamentSearch(tournamentFilters.search), 280); return () => window.clearTimeout(timeout); }, [tournamentFilters.search]);
  useEffect(() => { if (accessState === "ready" && requestedTournamentQueryKey !== resolvedTournamentQueryKey) void loadFilteredTournaments(requestedTournamentFilters, requestedTournamentQueryKey); }, [accessState, loadFilteredTournaments, requestedTournamentFilters, requestedTournamentQueryKey, resolvedTournamentQueryKey]);
  useEffect(() => { void loadConsole(); }, [loadConsole]);

  function navigate(view: AdminView, context?: string) {
    setActiveView(view);
    if (view === "tournaments" && context === "attention") setTournamentFilters((current) => ({ ...current, attentionOnly: true }));
    if (view === "tournaments" && context && context !== "attention") setSelectedTournamentSlug(context);
  }

  if (accessState === "loading") return <AdminState title={t("admin.new.loading")} copy={t("admin.new.loadingCopy")} />;
  if (accessState === "unauthenticated") return <AdminState title={t("admin.new.signInTitle")} copy={t("admin.new.signInCopy")} action={<Link className="ops-button ops-button-primary" href="/auth/login" prefetch={false}>{t("auth.signIn")}</Link>} />;
  if (accessState === "forbidden") return <AdminState title={t("admin.new.forbiddenTitle")} copy={t("admin.new.forbiddenCopy", { name: currentUser?.display_name ?? t("common.unknown") })} />;
  if (accessState === "error") return <AdminState title={t("admin.new.loadFailed")} copy={loadError} action={<button className="ops-button ops-button-primary" type="button" onClick={() => void loadConsole()}><RefreshCcw size={16} />{t("common.retry")}</button>} />;
  if (!overview || !currentUser) return null;

  return <section className="ops-shell" data-testid="admin-console">
    <aside className="ops-sidebar">
      <div className="ops-brand"><span className="ops-brand-mark"><ShieldCheck size={18} /></span><span><strong>OLD SPARKY</strong><small>{t("admin.new.consoleSubtitle")}</small></span></div>
      <div className="ops-scope"><span>{t("admin.new.workspaceLabel")}</span><strong>{t("admin.new.workspaceName")}</strong></div>
      <nav className="ops-nav" aria-label={t("admin.new.navigation")}>
        <NavItem active={activeView === "overview"} icon={<LayoutDashboard size={17} />} label={t("admin.new.overview")} onClick={() => navigate("overview")} />
        <div className="ops-nav-group">{t("admin.new.manageGroup")}</div>
        <NavItem active={activeView === "users"} icon={<UsersRound size={17} />} label={t("admin.new.users")} count={overview.analytics?.users_total ?? overview.users_total} onClick={() => navigate("users")} />
        <NavItem active={activeView === "tournaments"} icon={<Trophy size={17} />} label={t("admin.new.tournaments")} count={overview.analytics?.tournaments_total ?? overview.tournaments_total} onClick={() => navigate("tournaments")} />
        <div className="ops-nav-group">{t("admin.new.observeGroup")}</div>
        <NavItem active={activeView === "analytics"} icon={<BarChart3 size={17} />} label={t("admin.new.analytics")} onClick={() => navigate("analytics")} />
        <NavItem active={activeView === "audit"} icon={<ClipboardList size={17} />} label={t("admin.new.audit")} count={overview.analytics?.audit_events_total ?? overview.audit_events_total} onClick={() => navigate("audit")} />
        <div className="ops-nav-group">{t("admin.new.environmentGroup")}</div>
        <NavItem active={activeView === "preprod"} icon={<Activity size={17} />} label={t("admin.new.preprod")} count={preprodRuns.length || null} onClick={() => navigate("preprod")} />
      </nav>
      <div className="ops-sidebar-bottom"><span className="ops-protected"><span className="ops-live-dot" />{t("admin.new.protectedContour")}</span><span className="ops-operator">{currentUser.display_name}<small>{currentUser.roles.join(" · ")}</small></span><Link href="/" className="ops-site-link"><ExternalLink size={14} />{t("admin.new.openSite")}</Link></div>
    </aside>
    <div className="ops-main">
      <header className="ops-topbar"><div><span className="ops-topbar-kicker">{t("admin.new.consoleName")}</span><strong>{viewLabel(activeView, t)}</strong></div><div className="ops-topbar-actions"><span className="ops-snapshot"><span className="ops-live-dot" />{t("admin.new.liveData")}</span><button className="ops-button ops-button-secondary" data-testid="admin-refresh" type="button" disabled={isRefreshing} onClick={() => void loadConsole(true)}><RefreshCcw className={isRefreshing ? "ops-spin" : ""} size={15} />{isRefreshing ? t("common.loading") : t("admin.new.refresh")}</button></div></header>
      <main className="ops-content">
        {activeView === "overview" ? <AdminOverview overview={overview} tournaments={tournaments} formatDate={formatDate} enumLabel={enumLabel} onNavigate={navigate} /> : null}
        {activeView === "analytics" && overview.analytics ? <AdminAnalytics analytics={overview.analytics} formatDate={formatDate} enumLabel={enumLabel} /> : null}
        {activeView === "users" ? <AdminUsersPage users={users} currentUser={currentUser} isLoading={isLoadingUsers} loadError={userLoadError} selectedUserId={selectedUserId} formatDate={formatDate} onSearch={loadUsers} onSelect={setSelectedUserId} onUpdate={(updated) => setUsers((current) => current.map((item) => item.id === updated.id ? updated : item))} onDelete={(id) => { setUsers((current) => current.filter((item) => item.id !== id)); setSelectedUserId(null); setOverview((current) => current?.analytics ? { ...current, users_total: Math.max(0, current.analytics.users_total - 1), analytics: { ...current.analytics, users_total: Math.max(0, current.analytics.users_total - 1), active_users: Math.max(0, current.analytics.active_users - 1) } } : current); }} /> : null}
        {activeView === "tournaments" ? <AdminTournamentsPage tournaments={tournaments} filteredTotal={tournamentTotal} selectedSlug={selectedTournamentSlug} filters={tournamentFilters} hasMore={hasMoreTournaments} isFiltering={isFilteringTournaments} isLoadingMore={isLoadingMoreTournaments} isReloading={isRefreshing} pageError={tournamentPageError} enumLabel={enumLabel} formatDate={formatDate} onFiltersChange={setTournamentFilters} onSelect={setSelectedTournamentSlug} onLoadMore={() => void loadMoreTournaments()} onRetry={() => failedTournamentOffset === 0 ? void loadFilteredTournaments(activeTournamentFilters.current, tournamentQueryKey(activeTournamentFilters.current)) : void loadMoreTournaments()} onUpdate={(updated) => setTournaments((current) => current.map((item) => item.id === updated.id ? updated : item))} onDelete={(slug) => { const deleted = tournaments.find((item) => item.slug === slug); setTournaments((current) => current.filter((item) => item.slug !== slug)); setSelectedTournamentSlug(null); setTournamentTotal((current) => Math.max(0, current - 1)); setOverview((current) => current?.analytics ? { ...current, tournaments_total: Math.max(0, current.analytics.tournaments_total - 1), analytics: { ...current.analytics, tournaments_total: Math.max(0, current.analytics.tournaments_total - 1), active_tournaments: deleted && ["registration_open", "registration_closed", "in_progress"].includes(deleted.status) ? Math.max(0, current.analytics.active_tournaments - 1) : current.analytics.active_tournaments } } : current); }} /> : null}
        {activeView === "audit" ? <AdminAuditPage auditLogs={auditLogs} selectedAuditId={selectedAuditId} formatDate={formatDate} onSelect={setSelectedAuditId} /> : null}
        {activeView === "preprod" ? <AdminPreprodPage runs={preprodRuns} currentUser={currentUser} formatDate={formatDate} onReload={loadPreprod} /> : null}
      </main>
    </div>
  </section>;
}

function NavItem({ active, icon, label, count, onClick }: { active: boolean; icon: ReactNode; label: string; count?: number | null; onClick: () => void }) { return <button className={active ? "ops-nav-item is-active" : "ops-nav-item"} type="button" aria-label={label} title={label} aria-current={active ? "page" : undefined} onClick={onClick}>{icon}<span>{label}</span>{count !== undefined && count !== null ? <small>{count.toLocaleString("ru-RU")}</small> : null}</button>; }
function AdminState({ title, copy, action }: { title: string; copy: string; action?: ReactNode }) { return <section className="ops-state-page"><div className="ops-state-mark"><ShieldCheck size={22} /></div><h1>{title}</h1><p>{copy}</p>{action ? <div className="ops-state-action">{action}</div> : null}</section>; }
function viewLabel(view: AdminView, t: (key: string) => string): string { return ({ overview: t("admin.new.overview"), users: t("admin.new.users"), tournaments: t("admin.new.tournaments"), analytics: t("admin.new.analytics"), audit: t("admin.new.audit"), preprod: t("admin.new.preprod") })[view]; }
function tournamentListPath(filters: AdminTournamentFilters, offset: number): string { const query = new URLSearchParams({ limit: String(adminTournamentPageSize), offset: String(offset) }); if (filters.search.trim()) query.set("search", filters.search.trim()); if (filters.status !== "all") query.set("status", filters.status); if (filters.visibility !== "all") query.set("visibility", filters.visibility); if (filters.attentionOnly) query.set("attention", "true"); return `/admin/tournaments?${query.toString()}`; }
function tournamentQueryKey(filters: AdminTournamentFilters): string { return JSON.stringify({ search: filters.search.trim(), status: filters.status, visibility: filters.visibility, attentionOnly: filters.attentionOnly }); }
function appendUnique(current: PlatformAdminTournament[], incoming: PlatformAdminTournament[]): PlatformAdminTournament[] { const ids = new Set(current.map((item) => item.id)); return [...current, ...incoming.filter((item) => { if (ids.has(item.id)) return false; ids.add(item.id); return true; })]; }
