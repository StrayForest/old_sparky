"use client";

import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, ChevronRight, Search, Shield, Trash2, UserCog, UserRound } from "lucide-react";
import { useI18n } from "@/components/i18n-provider";
import { platformApiMessage, platformApiRequest } from "@/lib/platform-api";
import type { PlatformUser } from "@/lib/platform-types";

type AdminUsersPageProps = {
  users: PlatformUser[];
  currentUser: PlatformUser;
  isLoading: boolean;
  loadError: string;
  selectedUserId: string | null;
  formatDate: (value: string) => string;
  onSearch: (value: string) => void;
  onSelect: (id: string) => void;
  onUpdate: (user: PlatformUser) => void;
  onDelete: (id: string) => void;
};

export function AdminUsersPage({ users, currentUser, isLoading, loadError, selectedUserId, formatDate, onSearch, onSelect, onUpdate, onDelete }: AdminUsersPageProps) {
  const { enumLabel, t } = useI18n();
  const [search, setSearch] = useState("");
  const [stateFilter, setStateFilter] = useState("all");
  const [roleFilter, setRoleFilter] = useState("all");
  const selected = users.find((user) => user.id === selectedUserId) ?? null;
  const filtered = useMemo(() => users.filter((user) => {
    const stateMatch = stateFilter === "all" || user.status === stateFilter;
    const roleMatch = roleFilter === "all" || user.roles.includes(roleFilter);
    return stateMatch && roleMatch;
  }), [roleFilter, stateFilter, users]);

  useEffect(() => {
    const timeout = window.setTimeout(() => onSearch(search), 280);
    return () => window.clearTimeout(timeout);
  }, [onSearch, search]);

  return (
    <section className="ops-page" data-testid="admin-users-page">
      <div className="ops-page-title-row">
        <div><span className="ops-kicker">{t("admin.new.users")}</span><h1>{t("admin.new.usersTitle")}</h1><p>{t("admin.new.usersCopy")}</p></div>
        <div className="ops-page-count"><strong>{users.length.toLocaleString("ru-RU")}</strong><span>{t("admin.new.usersCount")}</span></div>
      </div>
      <div className="ops-toolbar ops-users-toolbar">
        <label className="ops-search"><Search size={17} /><input data-testid="admin-user-search" maxLength={120} value={search} placeholder={t("admin.new.userSearchPlaceholder")} onChange={(event) => setSearch(event.target.value)} /></label>
        <label className="ops-filter"><span>{t("admin.new.accountState")}</span><select value={stateFilter} onChange={(event) => setStateFilter(event.target.value)}><option value="all">{t("admin.new.allStates")}</option><option value="active">{enumLabel("active")}</option><option value="disabled">{enumLabel("disabled")}</option></select></label>
        <label className="ops-filter"><span>{t("admin.new.accessRole")}</span><select value={roleFilter} onChange={(event) => setRoleFilter(event.target.value)}><option value="all">{t("admin.new.allRoles")}</option><option value="admin">admin</option><option value="superadmin">superadmin</option></select></label>
        <span className="ops-toolbar-count">{isLoading ? t("common.loading") : t("admin.new.shownOf", { shown: filtered.length, total: users.length })}</span>
      </div>
      <div className="ops-list-detail">
        <section className="ops-panel ops-list-panel">
          <div className="ops-table-wrap"><table className="ops-table"><thead><tr><th>{t("admin.new.userColumn")}</th><th>{t("admin.new.readinessColumn")}</th><th>{t("admin.new.accessColumn")}</th><th>{t("admin.new.createdColumn")}</th><th><span className="sr-only">{t("common.view")}</span></th></tr></thead><tbody>
            {filtered.map((user) => <UserRow key={user.id} user={user} selected={user.id === selectedUserId} formatDate={formatDate} enumLabel={enumLabel} onSelect={onSelect} />)}
          </tbody></table></div>
          {loadError ? <div className="ops-feedback ops-feedback-error" role="alert">{loadError}</div> : null}
          {!isLoading && filtered.length === 0 ? <div className="ops-empty-inline"><UserRound size={18} /><span>{t("admin.new.noUsers")}</span></div> : null}
        </section>
        <UserDetail user={selected} currentUser={currentUser} formatDate={formatDate} onUpdate={onUpdate} onDelete={onDelete} />
      </div>
    </section>
  );
}

function UserRow({ user, selected, formatDate, enumLabel, onSelect }: { user: PlatformUser; selected: boolean; formatDate: (value: string) => string; enumLabel: (value: string | null | undefined) => string; onSelect: (id: string) => void }) {
  const { t } = useI18n();
  const readiness = Boolean(user.steam_linked);
  return <tr className={selected ? "is-selected" : ""} data-testid={`admin-user-${user.id}`} tabIndex={0} onClick={() => onSelect(user.id)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(user.id); } }}>
    <td><button className="ops-record" type="button" onClick={() => onSelect(user.id)}><span className="ops-avatar">{user.display_name.slice(0, 2).toUpperCase()}</span><span><strong>{user.display_name}</strong><small>{user.email ?? user.id}</small></span></button></td>
    <td><span className={readiness ? "ops-status ops-status-success" : "ops-status ops-status-muted"}>{readiness ? <CheckCircle2 size={13} /> : null}{readiness ? t("admin.new.identityLinked") : t("admin.new.identityMissing")}</span><small className="ops-table-subtext">{user.steam_linked ? t("admin.new.steamLinked") : t("admin.new.steamNotLinked")}</small></td>
    <td><div className="ops-chip-list">{user.roles.length ? user.roles.map((role) => <span className="ops-chip" key={role}>{role}</span>) : <span className="ops-table-subtext">{t("admin.new.noRoles")}</span>}</div><small className="ops-table-subtext">{enumLabel(user.status)}</small></td>
    <td><span className="ops-table-date">{formatDate(user.created_at)}</span></td>
    <td><ChevronRight size={16} /></td>
  </tr>;
}

function UserDetail({ user, currentUser, formatDate, onUpdate, onDelete }: { user: PlatformUser | null; currentUser: PlatformUser; formatDate: (value: string) => string; onUpdate: (user: PlatformUser) => void; onDelete: (id: string) => void }) {
  const { t } = useI18n();
  const [publicCredits, setPublicCredits] = useState(0);
  const [privateCredits, setPrivateCredits] = useState(0);
  const [note, setNote] = useState("");
  const [deleteConfirmation, setDeleteConfirmation] = useState("");
  const [deleteNote, setDeleteNote] = useState("");
  const [isSaving, setIsSaving] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    setPublicCredits(user?.public_tournament_credits ?? 0);
    setPrivateCredits(user?.private_tournament_credits ?? 0);
    setNote("");
    setDeleteConfirmation("");
    setDeleteNote("");
    setMessage("");
    setError("");
  }, [user?.id]);

  if (!user) return <aside className="ops-detail ops-detail-empty"><UserRound size={22} /><strong>{t("admin.new.selectUserTitle")}</strong><span>{t("admin.new.selectUserCopy")}</span></aside>;

  const inspectedUser = user;
  const isSuperadmin = currentUser.roles.includes("superadmin");
  const isAdmin = inspectedUser.roles.includes("admin");
  const creditsChanged = publicCredits !== (inspectedUser.public_tournament_credits ?? 0) || privateCredits !== (inspectedUser.private_tournament_credits ?? 0);
  const canDelete = isSuperadmin && inspectedUser.id !== currentUser.id && !inspectedUser.roles.includes("superadmin") && deleteConfirmation.trim() === (inspectedUser.email ?? inspectedUser.id) && deleteNote.trim().length >= 3 && !isDeleting;

  async function saveCredits() {
    if (!creditsChanged || note.trim().length < 3 || isSaving) return;
    setIsSaving(true); setMessage(""); setError("");
    try {
      const updated = await platformApiRequest<PlatformUser>(`/admin/users/${inspectedUser.id}/tournament-credits`, { method: "PATCH", body: JSON.stringify({ public_tournament_credits: publicCredits, private_tournament_credits: privateCredits, note: note.trim() }) });
      onUpdate(updated); setNote(""); setMessage(t("admin.new.creditsSaved", { name: updated.display_name }));
    } catch (requestError) { setError(platformApiMessage(requestError, t("admin.new.creditsFailed"))); }
    finally { setIsSaving(false); }
  }

  async function toggleRole() {
    if (!isSuperadmin || note.trim().length < 3 || isSaving) return;
    setIsSaving(true); setMessage(""); setError("");
    try {
      const updated = await platformApiRequest<PlatformUser>(`/admin/users/${inspectedUser.id}/admin-role`, { method: "PATCH", body: JSON.stringify({ is_admin: !isAdmin, note: note.trim() }) });
      onUpdate(updated); setNote(""); setMessage(t("admin.new.roleSaved", { name: updated.display_name }));
    } catch (requestError) { setError(platformApiMessage(requestError, t("admin.new.roleFailed"))); }
    finally { setIsSaving(false); }
  }

  async function deleteUser() {
    if (!canDelete) return;
    setIsDeleting(true); setMessage(""); setError("");
    try {
      await platformApiRequest<void>(`/admin/users/${inspectedUser.id}`, { method: "DELETE", body: JSON.stringify({ confirmation: deleteConfirmation.trim(), note: deleteNote.trim() }) });
      onDelete(inspectedUser.id);
    } catch (requestError) { setError(platformApiMessage(requestError, t("admin.new.deleteUserFailed"))); setIsDeleting(false); }
  }

  return <aside className="ops-detail" data-testid="admin-user-inspector">
    <div className="ops-detail-header"><div><span className="ops-kicker">{t("admin.new.userDetail")}</span><h2>{user.display_name}</h2><p>{user.email ?? user.id}</p></div><span className="ops-avatar ops-avatar-large">{user.display_name.slice(0, 2).toUpperCase()}</span></div>
    <div className="ops-detail-badges"><span className="ops-status ops-status-success">{user.status}</span>{user.roles.map((role) => <span className="ops-chip" key={role}>{role}</span>)}</div>
    <div className="ops-detail-facts"><Fact label={t("admin.new.userCreated")} value={formatDate(user.created_at)} /><Fact label={t("admin.new.userSteam")} value={user.steam_linked ? t("admin.new.connected") : t("admin.new.notConnected")} /><Fact label={t("admin.new.userPassword")} value={user.has_password ? t("admin.new.passwordSet") : t("admin.new.passwordNotSet")} /></div>
    <div className="ops-detail-section"><SectionTitle title={t("admin.new.creditTitle")} copy={t("admin.new.creditCopy")} /><div className="ops-form-grid-two"><NumberField label={t("admin.new.publicCredits")} value={publicCredits} onChange={setPublicCredits} testId="admin-public-credits" /><NumberField label={t("admin.new.privateCredits")} value={privateCredits} onChange={setPrivateCredits} testId="admin-private-credits" /></div><ReasonField value={note} onChange={setNote} testId="admin-user-action-note" /><button className="ops-button ops-button-primary" data-testid="admin-save-credits" type="button" disabled={!creditsChanged || note.trim().length < 3 || isSaving} onClick={() => void saveCredits()}><Shield size={16} />{isSaving ? t("common.saving") : t("admin.new.saveCredits")}</button></div>
    <div className="ops-detail-section"><SectionTitle title={t("admin.new.accessTitle")} copy={isSuperadmin ? t("admin.new.accessSuperadminCopy") : t("admin.new.accessReadonlyCopy")} /><button className={isAdmin ? "ops-button ops-button-danger" : "ops-button ops-button-secondary"} data-testid="admin-toggle-admin-role" type="button" disabled={!isSuperadmin || note.trim().length < 3 || isSaving} onClick={() => void toggleRole()}><UserCog size={16} />{isAdmin ? t("admin.new.revokeAdmin") : t("admin.new.grantAdmin")}</button></div>
    {message ? <div className="ops-feedback ops-feedback-success">{message}</div> : null}{error ? <div className="ops-feedback ops-feedback-error" role="alert">{error}</div> : null}
    <details className="ops-danger-zone"><summary><Trash2 size={15} />{t("admin.new.deleteUserTitle")}</summary><div className="ops-danger-content"><div className="ops-warning"><AlertTriangle size={16} /><span>{t("admin.new.deleteUserCopy")}</span></div>{user.id === currentUser.id ? <div className="ops-feedback ops-feedback-error">{t("admin.new.deleteSelf")}</div> : null}{user.roles.includes("superadmin") ? <div className="ops-feedback ops-feedback-error">{t("admin.new.deleteSuperadmin")}</div> : null}<label className="ops-field"><span>{t("admin.new.confirmExact")}</span><input data-testid="admin-delete-user-confirmation" maxLength={320} value={deleteConfirmation} onChange={(event) => setDeleteConfirmation(event.target.value)} /><small>{user.email ?? user.id}</small></label><ReasonField value={deleteNote} onChange={setDeleteNote} testId="admin-delete-user-note" /><button className="ops-button ops-button-danger" data-testid="admin-delete-user" type="button" disabled={!canDelete} onClick={() => void deleteUser()}><Trash2 size={16} />{isDeleting ? t("admin.new.deleting") : t("admin.new.deleteUserButton")}</button></div></details>
  </aside>;
}

function Fact({ label, value }: { label: string; value: string }) { return <div className="ops-fact"><span>{label}</span><strong>{value}</strong></div>; }
function SectionTitle({ title, copy }: { title: string; copy: string }) { return <div className="ops-section-title"><h3>{title}</h3><p>{copy}</p></div>; }
function ReasonField({ value, onChange, testId }: { value: string; onChange: (value: string) => void; testId?: string }) { const { t } = useI18n(); return <label className="ops-field"><span>{t("admin.new.reason")}</span><textarea data-testid={testId} maxLength={1000} value={value} placeholder={t("admin.new.reasonPlaceholder")} onChange={(event) => onChange(event.target.value)} /></label>; }
function NumberField({ label, value, onChange, testId }: { label: string; value: number; onChange: (value: number) => void; testId: string }) { return <label className="ops-field"><span>{label}</span><input data-testid={testId} min={0} type="number" value={value} onChange={(event) => onChange(Math.max(0, Number(event.target.value) || 0))} /></label>; }
