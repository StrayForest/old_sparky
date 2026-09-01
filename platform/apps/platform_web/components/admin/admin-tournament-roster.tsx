"use client";

import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, ArrowRight, Crown, LockKeyhole, RefreshCcw, UsersRound } from "lucide-react";
import { useI18n } from "@/components/i18n-provider";
import { getAdminTournamentRoster, mutateAdminTournamentRoster, PlatformApiError, platformApiMessage, type AdminRosterMutationOperation } from "@/lib/platform-api";
import type { PlatformAdminRoster, PlatformAdminRosterMember } from "@/lib/platform-types";

const operationCapability: Record<AdminRosterMutationOperation, keyof PlatformAdminRoster["capabilities"]> = {
  "add-player": "can_add_player",
  "remove-player": "can_remove_player",
  "move-player": "can_move_player",
  "replace-player": "can_replace_player",
  "change-captain": "can_change_captain"
};

export function AdminTournamentRoster({ slug, formatDate }: { slug: string; formatDate: (value: string) => string }) {
  const { t } = useI18n();
  const [roster, setRoster] = useState<PlatformAdminRoster | null>(null); const [isLoading, setIsLoading] = useState(true); const [loadError, setLoadError] = useState(""); const [actionError, setActionError] = useState(""); const [success, setSuccess] = useState(""); const [busy, setBusy] = useState<AdminRosterMutationOperation | null>(null); const [reason, setReason] = useState(""); const [selectedMemberId, setSelectedMemberId] = useState(""); const [addUserId, setAddUserId] = useState(""); const [addTeamKey, setAddTeamKey] = useState(""); const [addSlot, setAddSlot] = useState("1"); const [destinationTeamKey, setDestinationTeamKey] = useState(""); const [destinationSlot, setDestinationSlot] = useState("1"); const [replacementUserId, setReplacementUserId] = useState(""); const [override, setOverride] = useState(false);

  useEffect(() => {
    const controller = new AbortController(); let active = true; setIsLoading(true); setLoadError("");
    void getAdminTournamentRoster(slug, controller.signal).then((value) => { if (!active) return; if (!value || typeof value.state_version !== "number" || !Array.isArray(value.teams)) { setLoadError(t("admin.new.rosterLoadFailed")); return; } setRoster(value); }).catch((error: unknown) => { if (active && !controller.signal.aborted) setLoadError(platformApiMessage(error, t("admin.new.rosterLoadFailed"))); }).finally(() => { if (active) setIsLoading(false); });
    return () => { active = false; controller.abort(); };
  }, [slug, t]);

  useEffect(() => {
    if (!roster) return;
    setAddTeamKey((current) => current && roster.teams.some((team) => team.team_key === current) ? current : roster.teams[0]?.team_key ?? "");
    setDestinationTeamKey((current) => current && roster.teams.some((team) => team.team_key === current) ? current : roster.teams[0]?.team_key ?? "");
    setAddUserId((current) => current && roster.unassigned_participants.some((player) => player.user_id === current) ? current : roster.unassigned_participants[0]?.user_id ?? "");
    setReplacementUserId((current) => current && roster.unassigned_participants.some((player) => player.user_id === current) ? current : roster.unassigned_participants[0]?.user_id ?? "");
  }, [roster]);

  const selected = useMemo(() => { if (!roster || !selectedMemberId) return null; for (const team of roster.teams) { const member = team.members.find((item) => item.id === selectedMemberId); if (member) return { member, team }; } return null; }, [roster, selectedMemberId]);
  const canRun = (operation: AdminRosterMutationOperation) => Boolean(roster && !busy && roster.capabilities[operationCapability[operation]] && (!roster.capabilities.requires_override || (roster.capabilities.can_override && override)));

  async function runMutation(operation: AdminRosterMutationOperation, fields: Record<string, unknown>) {
    if (!roster || !canRun(operation)) return;
    if (reason.trim().length < 3) { setActionError(t("admin.new.reasonRequired")); return; }
    setBusy(operation); setActionError(""); setSuccess("");
    try { const updated = await mutateAdminTournamentRoster(slug, operation, { expected_state_version: roster.state_version, reason: reason.trim(), override: roster.capabilities.requires_override && override, ...fields }); setRoster(updated); setReason(""); setSelectedMemberId(""); setSuccess(t("admin.new.rosterSaved")); }
    catch (error: unknown) { setActionError(error instanceof PlatformApiError && error.status === 409 ? t("admin.new.rosterStale") : platformApiMessage(error, t("admin.new.rosterLoadFailed"))); }
    finally { setBusy(null); }
  }

  if (isLoading) return <div className="ops-detail-content ops-roster-state"><RefreshCcw className="ops-spin" size={17} />{t("common.loading")}</div>;
  if (!roster) return <div className="ops-detail-content ops-roster-state"><UsersRound size={19} /><span>{loadError || t("admin.new.rosterNoMaterialized")}</span></div>;
  const blocked = Boolean(roster.capabilities.blocked_reason);
  return <div className="ops-detail-content ops-roster" data-testid="admin-roster-panel">
    <div className="ops-roster-meta"><span>{roster.locked ? <LockKeyhole size={13} /> : null}{roster.locked ? t("admin.new.rosterLocked") : t("admin.new.rosterOpen")}</span><span>{t("admin.new.stateVersion", { value: roster.state_version })}</span><span>{roster.last_modified_at ? t("admin.new.changedAt", { value: formatDate(roster.last_modified_at) }) : t("admin.new.noManualChanges")}</span></div>
    {roster.bracket.exists ? <div className="ops-info"><AlertTriangle size={15} /><span>{t("admin.new.rosterBracketImpact", { matches: roster.bracket.match_count, started: roster.bracket.started_count })}</span></div> : null}
    {roster.manually_modified ? <div className="ops-info"><span>{t("admin.new.rosterManualModified")}</span></div> : null}
    {blocked ? <div className="ops-warning"><AlertTriangle size={15} /><span>{t("admin.new.rosterBlocked")}: {roster.capabilities.blocked_reason}</span></div> : null}
    <div className="ops-roster-teams"><div className="ops-subsection-label">{t("admin.new.currentTeams")}</div>{roster.teams.length ? roster.teams.map((team) => <div className="ops-team-row" key={team.id}><div className="ops-team-header"><div><strong>{team.name}</strong><small>{team.team_key}</small></div><span>{team.starter_average_strength.toFixed(1)}</span></div><div className="ops-team-members">{team.members.map((member) => <button className={member.id === selectedMemberId ? "ops-member-row is-selected" : "ops-member-row"} key={member.id} type="button" onClick={() => { setSelectedMemberId(member.id); setDestinationTeamKey(team.team_key); setDestinationSlot(String(member.slot_number === 6 ? 1 : member.slot_number)); }}><span className="ops-member-slot">{slotLabel(member)}</span><span><strong>{member.display_name}</strong><small>{member.rank ?? "—"} · {member.assigned_role ?? "—"}</small></span>{member.roster_role === "captain" ? <Crown size={14} /> : null}</button>)}</div></div>) : <div className="ops-empty-inline">{t("admin.new.rosterNoTeams")}</div>}</div>
    <div className="ops-roster-form"><div className="ops-subsection-label">{t("admin.new.addPlayer")}</div><div className="ops-form-grid-roster"><label className="ops-field"><span>{t("admin.new.player")}</span><select value={addUserId} onChange={(event) => setAddUserId(event.target.value)}><option value="">{t("admin.new.selectPlayer")}</option>{roster.unassigned_participants.map((player) => <option key={player.user_id} value={player.user_id}>{player.display_name} · {player.rank ?? "—"}</option>)}</select></label><label className="ops-field"><span>{t("admin.new.team")}</span><select value={addTeamKey} onChange={(event) => setAddTeamKey(event.target.value)}>{roster.teams.map((team) => <option key={team.team_key} value={team.team_key}>{team.name}</option>)}</select></label><label className="ops-field"><span>{t("admin.new.slot")}</span><select value={addSlot} onChange={(event) => setAddSlot(event.target.value)}>{[1, 2, 3, 4, 5, 6].map((slot) => <option key={slot} value={slot}>{slot === 6 ? "S" : slot}</option>)}</select></label><button className="ops-button ops-button-secondary" type="button" disabled={!addUserId || !canRun("add-player")} onClick={() => void runMutation("add-player", { user_id: addUserId, team_key: addTeamKey, slot_number: Number(addSlot) })}>{busy === "add-player" ? <RefreshCcw className="ops-spin" size={15} /> : <ArrowRight size={15} />}{t("admin.new.add")}</button></div></div>
    {selected ? <div className="ops-roster-form"><div className="ops-subsection-label">{t("admin.new.selectedPlayer", { name: selected.member.display_name })}</div><div className="ops-form-grid-roster"><label className="ops-field"><span>{t("admin.new.team")}</span><select value={destinationTeamKey} onChange={(event) => setDestinationTeamKey(event.target.value)}>{roster.teams.map((team) => <option key={team.team_key} value={team.team_key}>{team.name}</option>)}</select></label><label className="ops-field"><span>{t("admin.new.slot")}</span><select value={destinationSlot} onChange={(event) => setDestinationSlot(event.target.value)}>{[1, 2, 3, 4, 5, 6].map((slot) => <option key={slot} value={slot}>{slot === 6 ? "S" : slot}</option>)}</select></label><label className="ops-field"><span>{t("admin.new.replacement")}</span><select value={replacementUserId} onChange={(event) => setReplacementUserId(event.target.value)}><option value="">{t("admin.new.selectPlayer")}</option>{roster.unassigned_participants.map((player) => <option key={player.user_id} value={player.user_id}>{player.display_name}</option>)}</select></label></div><div className="ops-action-row"><button className="ops-button ops-button-secondary" type="button" disabled={!canRun("move-player") || selected.member.roster_role === "captain"} onClick={() => void runMutation("move-player", { team_key: selected.team.team_key, user_id: selected.member.user_id, destination_team_key: destinationTeamKey, destination_slot: Number(destinationSlot) })}><ArrowRight size={15} />{t("admin.new.move")}</button><button className="ops-button ops-button-secondary" type="button" disabled={!replacementUserId || !canRun("replace-player")} onClick={() => void runMutation("replace-player", { team_key: selected.team.team_key, slot_number: selected.member.slot_number, replacement_user_id: replacementUserId })}>{t("admin.new.replace")}</button><button className="ops-button ops-button-secondary" type="button" disabled={!canRun("change-captain") || selected.member.roster_role === "captain"} onClick={() => void runMutation("change-captain", { team_key: selected.team.team_key, user_id: selected.member.user_id })}><Crown size={15} />{t("admin.new.captain")}</button><button className="ops-button ops-button-danger" type="button" disabled={!canRun("remove-player") || selected.member.roster_role === "captain"} onClick={() => void runMutation("remove-player", { team_key: selected.team.team_key, user_id: selected.member.user_id })}>{t("admin.new.remove")}</button></div></div> : <div className="ops-muted">{t("admin.new.selectPlayerHint")}</div>}
    <label className="ops-field"><span>{t("admin.new.reason")}</span><textarea maxLength={1000} value={reason} placeholder={t("admin.new.rosterReasonPlaceholder")} onChange={(event) => setReason(event.target.value)} /></label>{roster.capabilities.requires_override && roster.capabilities.can_override ? <label className="ops-checkbox"><input type="checkbox" checked={override} onChange={(event) => setOverride(event.target.checked)} />{t("admin.new.overrideRoster")}</label> : null}{actionError ? <div className="ops-feedback ops-feedback-error" role="alert">{actionError}</div> : null}{success ? <div className="ops-feedback ops-feedback-success">{success}</div> : null}
  </div>;
}

function slotLabel(member: PlatformAdminRosterMember): string { return member.roster_role === "captain" ? "C" : member.roster_role === "substitute" ? "S" : String(member.slot_number); }
