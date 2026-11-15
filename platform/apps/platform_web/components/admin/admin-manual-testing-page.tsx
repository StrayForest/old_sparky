"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { AlertTriangle, Clipboard, GitBranch, Plus, RefreshCcw, Trash2, Trophy, UsersRound } from "lucide-react";
import { useI18n } from "@/components/i18n-provider";
import { PlatformApiError, platformApiMessage } from "@/lib/platform-api";
import { createAdminTournamentTest, deleteAdminTournamentTest, getAdminTournamentTests, mutateAdminTournamentTest, type AdminTournamentTestMutation } from "@/lib/platform-api";
import type { PlatformAdminTournamentTestBlocker, PlatformAdminTournamentTestSession } from "@/lib/platform-types";

const fakeLimit = 50;

export function AdminManualTestingPage() {
  const { t } = useI18n();
  const [sessions, setSessions] = useState<PlatformAdminTournamentTestSession[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [isMutating, setIsMutating] = useState(false);
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const [name, setName] = useState("");
  const [visibility, setVisibility] = useState<"public" | "invite_only">("invite_only");
  const [fakeCount, setFakeCount] = useState(1);
  const [deleteName, setDeleteName] = useState("");
  const [copied, setCopied] = useState(false);
  const keyByAction = useRef(new Map<string, string>());
  const requestGeneration = useRef(0);
  const loadController = useRef<AbortController | null>(null);
  const selected = sessions.find((item) => item.id === selectedId) ?? null;

  const load = useCallback(async () => {
    const generation = ++requestGeneration.current;
    loadController.current?.abort();
    const controller = new AbortController();
    loadController.current = controller;
    setIsLoading(true);
    setLoadError("");
    try {
      const next = await getAdminTournamentTests(controller.signal);
      if (generation !== requestGeneration.current || controller.signal.aborted) return;
      setSessions(next);
      setSelectedId((current) => current && next.some((item) => item.id === current) ? current : next[0]?.id ?? null);
    } catch (error) {
      if (generation === requestGeneration.current && !controller.signal.aborted) setLoadError(platformApiMessage(error, t("admin.test.loadFailed")));
    } finally {
      if (generation === requestGeneration.current) {
        setIsLoading(false);
        if (loadController.current === controller) loadController.current = null;
      }
    }
  }, [t]);

  useEffect(() => { void load(); return () => { requestGeneration.current += 1; loadController.current?.abort(); loadController.current = null; }; }, [load]);

  function idempotencyKey(action: string, payload: unknown): string {
    const key = `${action}:${JSON.stringify(payload)}`;
    const existing = keyByAction.current.get(key);
    if (existing) return existing;
    const storageKey = `oldsparky-admin-test-idempotency:${key}`;
    let next: string | null = null;
    try { next = window.sessionStorage.getItem(storageKey); } catch { /* The in-memory key still protects retries in this view. */ }
    next ??= globalThis.crypto.randomUUID();
    keyByAction.current.set(key, next);
    try { window.sessionStorage.setItem(storageKey, next); } catch { /* Keep the key in memory when storage is unavailable. */ }
    return next;
  }

  function finishAction(action: string, payload: unknown) {
    const key = `${action}:${JSON.stringify(payload)}`;
    keyByAction.current.delete(key);
    try { window.sessionStorage.removeItem(`oldsparky-admin-test-idempotency:${key}`); } catch { /* The server remains authoritative for deduplication. */ }
  }

  async function createSession(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const payload = { name: name.trim(), visibility, format_slug: "solo" as const };
    if (!payload.name || isMutating) return;
    setIsMutating(true); setActionError(""); setNotice("");
    try {
      const created = await createAdminTournamentTest(payload, idempotencyKey("create", payload));
      finishAction("create", payload);
      setSessions((current) => [created, ...current.filter((item) => item.id !== created.id)]);
      setSelectedId(created.id); setName(""); setDeleteName("");
      setNotice(t("admin.test.created"));
    } catch (error) {
      if (error instanceof PlatformApiError && error.status === 409) {
        finishAction("create", payload);
        await load();
      }
      setActionError(platformApiMessage(error, t("admin.test.actionFailed")));
    } finally { setIsMutating(false); }
  }

  async function mutate(operation: AdminTournamentTestMutation, payload: Record<string, unknown> = {}) {
    if (!selected || isMutating) return;
    const action = `${selected.id}:${operation}`;
    setIsMutating(true); setActionError(""); setNotice("");
    try {
      const updated = await mutateAdminTournamentTest(selected.id, operation, payload, idempotencyKey(action, payload));
      finishAction(action, payload);
      setSessions((current) => current.map((item) => item.id === updated.id ? updated : item));
      setNotice(t("admin.test.actionDone"));
    } catch (error) {
      if (error instanceof PlatformApiError && error.status === 409) {
        finishAction(action, payload);
        await load();
      }
      setActionError(error instanceof PlatformApiError && error.status === 409
        ? platformApiMessage(error, t("admin.test.actionBlocked"))
        : platformApiMessage(error, t("admin.test.actionFailed")));
    } finally { setIsMutating(false); }
  }

  async function removeSession() {
    if (!selected || !selected.capabilities.can_delete || deleteName !== selected.name || isMutating) return;
    const payload = { confirmation_name: deleteName };
    const action = `${selected.id}:delete`;
    setIsMutating(true); setActionError(""); setNotice("");
    try {
      await deleteAdminTournamentTest(selected.id, deleteName, idempotencyKey(action, payload));
      finishAction(action, payload);
      setSessions((current) => current.filter((item) => item.id !== selected.id));
      setSelectedId((current) => current === selected.id ? sessions.find((item) => item.id !== current)?.id ?? null : current);
      setDeleteName(""); setNotice(t("admin.test.deleted"));
    } catch (error) {
      if (error instanceof PlatformApiError && error.status === 409) {
        finishAction(action, payload);
        await load();
      }
      setActionError(platformApiMessage(error, t("admin.test.cleanupFailed")));
    } finally { setIsMutating(false); }
  }

  async function copyCode() {
    if (!selected) return;
    try {
      await navigator.clipboard.writeText(selected.join_code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch { setActionError(t("admin.test.copyFailed")); }
  }

  const remainingFakeCapacity = selected ? Math.max(0, fakeLimit - selected.counts.fake_total) : fakeLimit;
  const tournamentHref = selected ? `/tournaments/${encodeURIComponent(selected.slug)}?invite_code=${encodeURIComponent(selected.join_code)}` : "#";
  const privateLabel = t("admin.test.private");

  return <section className="ops-page ops-manual-test" data-testid="admin-manual-testing">
    <div className="ops-page-title-row"><div><span className="ops-kicker">{t("admin.test.kicker")}</span><h1>{t("admin.test.title")}</h1><p>{t("admin.test.copy")}</p></div><button className="ops-button ops-button-secondary" type="button" disabled={isLoading} onClick={() => void load()}><RefreshCcw className={isLoading ? "ops-spin" : ""} size={15} />{t("admin.test.refresh")}</button></div>
    {actionError ? <div className="ops-feedback ops-feedback-error" role="alert">{actionError}</div> : null}
    {notice ? <div className="ops-feedback ops-feedback-success" aria-live="polite">{notice}</div> : null}

    <div className="ops-manual-test-grid">
      <section className="ops-panel ops-test-create-panel">
        <div className="ops-panel-heading"><span className="ops-panel-icon"><Plus size={17} /></span><div><h2>{t("admin.test.createTitle")}</h2><p>{t("admin.test.createCopy")}</p></div></div>
        <form className="ops-test-create-form" onSubmit={(event) => void createSession(event)}>
          <label className="ops-field"><span>{t("admin.test.name")}</span><input required minLength={3} maxLength={110} value={name} onChange={(event) => setName(event.target.value)} placeholder={t("admin.test.namePlaceholder")} /></label>
          <label className="ops-field"><span>{t("admin.test.visibility")}</span><select value={visibility} onChange={(event) => setVisibility(event.target.value as "public" | "invite_only")}><option value="public">{t("admin.test.public")}</option><option value="invite_only">{privateLabel}</option></select></label>
          <p className="ops-info ops-info-wide">{t("admin.test.openRegistrationNote")}</p>
          <button className="ops-button ops-button-primary" type="submit" disabled={!name.trim() || isMutating}>{isMutating ? <RefreshCcw className="ops-spin" size={15} /> : <Plus size={15} />}{t("admin.test.create")}</button>
        </form>
      </section>

      <section className="ops-panel ops-test-sessions-panel">
        <div className="ops-panel-heading"><span className="ops-panel-icon"><Clipboard size={17} /></span><div><h2>{t("admin.test.sessionsTitle")}</h2><p>{t("admin.test.sessionsCopy")}</p></div></div>
        {loadError ? <div className="ops-feedback ops-feedback-error" role="alert">{loadError}<button className="ops-button ops-button-secondary" type="button" onClick={() => void load()}>{t("common.retry")}</button></div> : null}
        {isLoading ? <div className="ops-test-empty"><RefreshCcw className="ops-spin" size={18} />{t("admin.test.loading")}</div> : null}
        {!isLoading && !loadError && sessions.length === 0 ? <div className="ops-test-empty"><Trophy size={18} />{t("admin.test.noSessions")}</div> : null}
        <div className="ops-test-session-list">{sessions.map((session) => <button key={session.id} type="button" className={session.id === selectedId ? "ops-test-session is-selected" : "ops-test-session"} onClick={() => { setSelectedId(session.id); setDeleteName(""); setActionError(""); }}><span className="ops-test-session-status">{stageLabel(session.stage, t)}</span><strong>{session.name}</strong><small>{session.slug} · {session.counts.human_active} {t("admin.test.humansShort")} · {session.counts.fake_total} {t("admin.test.fakesShort")}</small></button>)}</div>
      </section>

      {selected ? <section className="ops-panel ops-test-detail-panel">
        <div className="ops-test-detail-header"><div><span className="ops-kicker">{t("admin.test.fixtureLabel")}</span><h2>{selected.name}</h2><p>{selected.slug} · {stageLabel(selected.stage, t)}</p></div><Link className="ops-button ops-button-secondary" href={tournamentHref} prefetch={false}>{t("admin.test.openTournament")}</Link></div>
        {selected.is_test_fixture ? <div className="ops-warning"><AlertTriangle size={16} /><span>{t("admin.test.publicFixtureWarning")}</span></div> : null}
        <div className="ops-test-access"><div><span>{t("admin.test.joinCode")}</span><code>{selected.join_code}</code></div><button className="ops-button ops-button-secondary" type="button" onClick={() => void copyCode()}><Clipboard size={15} />{copied ? t("admin.test.copied") : t("admin.test.copyCode")}</button></div>
        <div className="ops-test-count-grid">
          <TestCount label={t("admin.test.humans")} value={selected.counts.human_active} detail={t("admin.test.humansReady", { ready: selected.counts.human_ready_yes })} />
          <TestCount label={t("admin.test.fakes")} value={selected.counts.fake_total} detail={t("admin.test.fakesReady", { ready: selected.counts.fake_ready_yes, limit: fakeLimit })} />
          <TestCount label={t("admin.test.teams")} value={selected.counts.teams_count} detail={t("admin.test.teamSlots", { value: selected.counts.required_slots })} />
          <TestCount label={t("admin.test.assignedHumans")} value={selected.counts.assigned_humans} detail={t("admin.test.teamsLocked", { value: selected.counts.teams_locked })} />
        </div>
        <div className="ops-info ops-info-wide"><UsersRound size={16} /><span>{t("admin.test.humansAlwaysIncluded")}</span></div>
        {selected.blockers.map((blocker) => <Blocker key={`${blocker.code}:${blocker.required}:${blocker.available}`} blocker={blocker} t={t} />)}
        <section className="ops-test-stage"><h3>{t("admin.test.workflowTitle")}</h3>
          {selected.capabilities.can_add_fake_players ? <div className="ops-test-inline-action"><label className="ops-field"><span>{t("admin.test.addFakes")}</span><input type="number" min={1} max={remainingFakeCapacity} value={fakeCount} onChange={(event) => setFakeCount(Math.min(remainingFakeCapacity, Math.max(1, Number(event.target.value))))} /></label><small>{t("admin.test.fillerNeeded", { value: selected.counts.filler_needed })}</small><button className="ops-button ops-button-secondary" type="button" disabled={isMutating || remainingFakeCapacity < 1} onClick={() => void mutate("players", { count: Math.min(fakeCount, remainingFakeCapacity) })}>{isMutating ? t("common.loading") : t("admin.test.addFakesButton")}</button></div> : null}
          <div className="ops-action-row">
            {selected.capabilities.can_start_ready_check ? <button className="ops-button ops-button-primary" type="button" disabled={isMutating} onClick={() => void mutate("ready-check/start")}>{t("admin.test.startReady")}</button> : null}
            {selected.capabilities.can_close_ready_check ? <button className="ops-button ops-button-secondary" type="button" disabled={isMutating} onClick={() => void mutate("ready-check/close")}>{t("admin.test.closeReady")}</button> : null}
            {selected.capabilities.can_generate_teams ? <button className="ops-button ops-button-primary" type="button" disabled={isMutating} onClick={() => void mutate("teams/generate")}><GitBranch size={15} />{t("admin.test.formTeams")}</button> : null}
          </div>
          {!selected.capabilities.can_start_ready_check && !selected.capabilities.can_close_ready_check && !selected.capabilities.can_generate_teams && selected.blockers.length === 0 ? <p className="ops-muted">{t("admin.test.noStageActions")}</p> : null}
        </section>
        <details className="ops-danger-zone ops-test-cleanup"><summary><Trash2 size={15} />{t("admin.test.cleanupTitle")}</summary><div className="ops-danger-content"><p>{t("admin.test.cleanupCopy", { name: selected.name, count: selected.counts.fake_total })}</p><label className="ops-field"><span>{t("admin.test.confirmName")}</span><input value={deleteName} maxLength={120} onChange={(event) => setDeleteName(event.target.value)} /></label>{selected.capabilities.can_delete ? <button className="ops-button ops-button-danger" type="button" disabled={isMutating || deleteName !== selected.name} onClick={() => void removeSession()}><Trash2 size={15} />{t("admin.test.deleteTestTournament")}</button> : <p className="ops-muted">{cleanupBlocker(selected.cleanup_blocked_reason, t)}</p>}</div></details>
      </section> : null}
    </div>
  </section>;
}

function TestCount({ label, value, detail }: { label: string; value: number; detail: string }) { return <div className="ops-test-count"><span>{label}</span><strong>{value.toLocaleString("ru-RU")}</strong><small>{detail}</small></div>; }

function Blocker({ blocker, t }: { blocker: PlatformAdminTournamentTestBlocker; t: (key: string, params?: Record<string, string | number | null | undefined>) => string }) {
  const label = blocker.code === "insufficient_fillers"
    ? t("admin.test.blockerFillers", { value: blocker.filler_needed ?? blocker.required ?? 0 })
    : blocker.code === "too_many_humans"
      ? t("admin.test.blockerHumans", { required: blocker.required ?? 0, available: blocker.available ?? 0 })
      : blocker.code === "human_profiles_missing"
        ? t("admin.test.blockerHumanProfiles")
      : blocker.code === "external_fake_player_reference" || blocker.code === "fake_users_have_external_references"
        ? t("admin.test.cleanupExternalReferences")
        : t("admin.test.blockerGeneric");
  return <div className="ops-warning" role="status"><AlertTriangle size={16} /><span>{label}</span></div>;
}

function stageLabel(stage: PlatformAdminTournamentTestSession["stage"], t: (key: string) => string): string {
  return ({ registration_open: t("admin.test.stageRegistration"), ready_check_active: t("admin.test.stageReadyActive"), ready_check_closed: t("admin.test.stageReadyClosed"), teams_locked: t("admin.test.stageTeamsLocked") })[stage];
}

function cleanupBlocker(reason: string | null, t: (key: string) => string): string {
  if (reason === "external_fake_player_reference" || reason === "fake_users_have_external_references") return t("admin.test.cleanupExternalReferences");
  return t("admin.test.cleanupBlocked");
}
