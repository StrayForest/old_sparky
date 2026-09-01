"use client";

import { AlertTriangle, ArrowRight, Crown, LockKeyhole, RefreshCcw, Users } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useI18n } from "@/components/i18n-provider";
import {
  getAdminTournamentRoster,
  mutateAdminTournamentRoster,
  PlatformApiError,
  platformApiMessage,
  type AdminRosterMutationOperation
} from "@/lib/platform-api";
import type {
  PlatformAdminRoster,
  PlatformAdminRosterMember
} from "@/lib/platform-types";

type AdminRosterPanelProps = {
  slug: string;
  formatDate: (value: string) => string;
};

const capabilityByOperation: Record<AdminRosterMutationOperation, keyof PlatformAdminRoster["capabilities"]> = {
  "add-player": "can_add_player",
  "remove-player": "can_remove_player",
  "move-player": "can_move_player",
  "replace-player": "can_replace_player",
  "change-captain": "can_change_captain"
};

function isRosterPayload(value: unknown): value is PlatformAdminRoster {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const candidate = value as Partial<PlatformAdminRoster>;
  return typeof candidate.state_version === "number" && Array.isArray(candidate.teams);
}

function memberLabel(member: PlatformAdminRosterMember): string {
  return member.handle ? `${member.display_name} · @${member.handle}` : member.display_name;
}

function slotLabel(member: PlatformAdminRosterMember): string {
  if (member.roster_role === "captain") {
    return "C";
  }
  return member.roster_role === "substitute" ? "S" : String(member.slot_number);
}

export function AdminRosterPanel({ slug, formatDate }: AdminRosterPanelProps) {
  const { t } = useI18n();
  const [roster, setRoster] = useState<PlatformAdminRoster | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [actionError, setActionError] = useState("");
  const [success, setSuccess] = useState("");
  const [busyOperation, setBusyOperation] = useState<AdminRosterMutationOperation | null>(null);
  const [reason, setReason] = useState("");
  const [addUserId, setAddUserId] = useState("");
  const [addTeamKey, setAddTeamKey] = useState("");
  const [addSlot, setAddSlot] = useState("1");
  const [selectedMemberKey, setSelectedMemberKey] = useState("");
  const [destinationTeamKey, setDestinationTeamKey] = useState("");
  const [destinationSlot, setDestinationSlot] = useState("1");
  const [replacementUserId, setReplacementUserId] = useState("");
  const [override, setOverride] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setIsLoading(true);
    setLoadError("");
    setActionError("");
    setSuccess("");
    void getAdminTournamentRoster(slug, controller.signal)
      .then((value) => {
        if (!active) {
          return;
        }
        if (!isRosterPayload(value)) {
          setRoster(null);
          setLoadError(t("admin.rosterLoadFailed"));
          return;
        }
        setRoster(value);
      })
      .catch((error: unknown) => {
        if (active && !controller.signal.aborted) {
          setLoadError(platformApiMessage(error, t("admin.rosterLoadFailed")));
        }
      })
      .finally(() => {
        if (active) {
          setIsLoading(false);
        }
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [slug, t]);

  useEffect(() => {
    if (!roster) {
      return;
    }
    setAddTeamKey((current) => (
      current && roster.teams.some((team) => team.team_key === current)
        ? current
        : roster.teams[0]?.team_key ?? ""
    ));
    setDestinationTeamKey((current) => (
      current && roster.teams.some((team) => team.team_key === current)
        ? current
        : roster.teams[0]?.team_key ?? ""
    ));
    setAddUserId((current) => (
      current && roster.unassigned_participants.some((player) => player.user_id === current)
        ? current
        : roster.unassigned_participants[0]?.user_id ?? ""
    ));
    setReplacementUserId((current) => (
      current && roster.unassigned_participants.some((player) => player.user_id === current)
        ? current
        : roster.unassigned_participants[0]?.user_id ?? ""
    ));
  }, [roster]);

  const selectedMember = useMemo(() => {
    if (!roster || !selectedMemberKey) {
      return null;
    }
    for (const team of roster.teams) {
      const member = team.members.find((item) => item.id === selectedMemberKey);
      if (member) {
        return { member, team };
      }
    }
    return null;
  }, [roster, selectedMemberKey]);

  const canRun = (operation: AdminRosterMutationOperation): boolean => {
    if (!roster || busyOperation !== null) {
      return false;
    }
    return roster.capabilities[capabilityByOperation[operation]] === true
      && (!roster.capabilities.requires_override || (roster.capabilities.can_override && override));
  };

  const runMutation = async (
    operation: AdminRosterMutationOperation,
    fields: Record<string, unknown>
  ) => {
    if (!roster || !canRun(operation)) {
      return;
    }
    if (reason.trim().length < 3) {
      setActionError(t("admin.rosterReason"));
      return;
    }
    setBusyOperation(operation);
    setActionError("");
    setSuccess("");
    try {
      const updated = await mutateAdminTournamentRoster(slug, operation, {
        expected_state_version: roster.state_version,
        reason: reason.trim(),
        override: roster.capabilities.requires_override && override,
        ...fields
      });
      setRoster(updated);
      setReason("");
      setSuccess(t("admin.rosterSaved"));
      setSelectedMemberKey("");
    } catch (error: unknown) {
      if (error instanceof PlatformApiError && error.status === 409) {
        setActionError(t("admin.rosterStale"));
      } else {
        setActionError(platformApiMessage(error, t("admin.rosterLoadFailed")));
      }
    } finally {
      setBusyOperation(null);
    }
  };

  if (isLoading) {
    return <section className="admin-roster-panel" data-testid="admin-roster-panel"><div className="admin-empty">{t("common.loading")}</div></section>;
  }
  if (!roster) {
    return (
      <section className="admin-roster-panel" data-testid="admin-roster-panel">
        <div className="admin-roster-panel-header"><Users size={16} /><span>{t("admin.rosterControlTitle")}</span></div>
        <div className="admin-empty">{loadError || t("admin.rosterNoMaterialized")}</div>
      </section>
    );
  }

  const selectedTeam = selectedMember?.team ?? null;
  const isBlocked = Boolean(roster.capabilities.blocked_reason);
  const allMembers = roster.teams.flatMap((team) => team.members.map((member) => ({ member, team })));

  return (
    <section className="admin-roster-panel" data-testid="admin-roster-panel">
      <div className="admin-roster-panel-header">
        <div><Users size={16} /><span>{t("admin.rosterControlTitle")}</span></div>
        <span className="admin-roster-version">v{roster.state_version}</span>
      </div>

      <div className="admin-roster-meta">
        <span>{t("admin.rosterSource")}: <code>{roster.source_assignment_run_id ?? "—"}</code></span>
        <span>{roster.locked ? <LockKeyhole size={13} /> : null}{roster.locked ? t("admin.rosterLocked") : t("admin.rosterOpen")}</span>
        {roster.last_modified_at ? <span>{formatDate(roster.last_modified_at)}</span> : null}
      </div>
      {roster.manually_modified ? <div className="admin-callout info">{t("admin.rosterManualModified")}</div> : null}
      {roster.bracket.exists ? (
        <div className="admin-callout info">{t("admin.rosterBracketImpact", { matches: roster.bracket.match_count, started: roster.bracket.started_count })}</div>
      ) : null}
      {isBlocked ? (
        <div className="admin-callout danger"><AlertTriangle size={15} />{t("admin.rosterBlocked")}: {roster.capabilities.blocked_reason}</div>
      ) : null}

      <div className="admin-roster-teams">
        <div className="admin-roster-section-title">{t("admin.rosterTeams")}</div>
        {roster.teams.length === 0 ? <div className="admin-empty">{t("admin.rosterNoTeams")}</div> : null}
        {roster.teams.map((team) => (
          <div className="admin-roster-team" key={team.id}>
            <div className="admin-roster-team-head">
              <strong>{team.name}</strong><span>{team.team_key} · {team.starter_average_strength.toFixed(1)}</span>
            </div>
            <div className="admin-roster-members">
              {team.members.map((member) => (
                <button
                  className={member.id === selectedMemberKey ? "admin-roster-member selected" : "admin-roster-member"}
                  key={member.id}
                  onClick={() => {
                    setSelectedMemberKey(member.id);
                    setDestinationTeamKey(team.team_key);
                    setDestinationSlot(String(member.slot_number === 6 ? 1 : member.slot_number));
                  }}
                  type="button"
                >
                  <span className="admin-roster-slot">{slotLabel(member)}</span>
                  <span><strong>{memberLabel(member)}</strong><small>{member.assigned_role ?? "—"} · {member.strength.toFixed(1)}</small></span>
                  {member.roster_role === "captain" ? <Crown size={14} /> : null}
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>

      <div className="admin-roster-form">
        <div className="admin-roster-section-title">{t("admin.rosterAddPlayer")}</div>
        <div className="admin-roster-form-grid">
          <label>{t("admin.rosterPlayer")}
            <select value={addUserId} onChange={(event) => setAddUserId(event.target.value)}>
              <option value="">{t("admin.rosterSelectPlayer")}</option>
              {roster.unassigned_participants.map((player) => <option key={player.user_id} value={player.user_id}>{player.display_name}</option>)}
            </select>
          </label>
          <label>{t("admin.rosterDestinationTeam")}
            <select value={addTeamKey} onChange={(event) => setAddTeamKey(event.target.value)}>
              {roster.teams.map((team) => <option key={team.team_key} value={team.team_key}>{team.name}</option>)}
            </select>
          </label>
          <label>{t("admin.rosterSlot")}
            <select value={addSlot} onChange={(event) => setAddSlot(event.target.value)}>{[1, 2, 3, 4, 5, 6].map((slot) => <option key={slot} value={slot}>{slot === 6 ? "S" : slot}</option>)}</select>
          </label>
          <button className="secondary-button" disabled={!addUserId || !canRun("add-player")} onClick={() => void runMutation("add-player", { user_id: addUserId, team_key: addTeamKey, slot_number: Number(addSlot) })} type="button">{busyOperation === "add-player" ? <RefreshCcw className="spin" size={14} /> : null}{t("admin.rosterApply")}</button>
        </div>
      </div>

      {selectedMember && selectedTeam ? (
        <div className="admin-roster-form admin-roster-actions">
          <div className="admin-roster-section-title">{t("admin.rosterSelected", { name: memberLabel(selectedMember.member) })}</div>
          <div className="admin-roster-form-grid">
            <label>{t("admin.rosterDestinationTeam")}
              <select value={destinationTeamKey} onChange={(event) => setDestinationTeamKey(event.target.value)}>{roster.teams.map((team) => <option key={team.team_key} value={team.team_key}>{team.name}</option>)}</select>
            </label>
            <label>{t("admin.rosterSlot")}
              <select value={destinationSlot} onChange={(event) => setDestinationSlot(event.target.value)}>{[1, 2, 3, 4, 5, 6].map((slot) => <option key={slot} value={slot}>{slot === 6 ? "S" : slot}</option>)}</select>
            </label>
            <label>{t("admin.rosterReplacement")}
              <select value={replacementUserId} onChange={(event) => setReplacementUserId(event.target.value)}><option value="">{t("admin.rosterSelectPlayer")}</option>{roster.unassigned_participants.map((player) => <option key={player.user_id} value={player.user_id}>{player.display_name}</option>)}</select>
            </label>
          </div>
          <div className="admin-roster-button-row">
            <button className="secondary-button" disabled={!canRun("move-player") || selectedMember.member.roster_role === "captain"} onClick={() => void runMutation("move-player", { team_key: selectedTeam.team_key, user_id: selectedMember.member.user_id, destination_team_key: destinationTeamKey, destination_slot: Number(destinationSlot) })} type="button"><ArrowRight size={14} />{t("admin.rosterMovePlayer")}</button>
            <button className="secondary-button" disabled={!replacementUserId || !canRun("replace-player")} onClick={() => void runMutation("replace-player", { team_key: selectedTeam.team_key, slot_number: selectedMember.member.slot_number, replacement_user_id: replacementUserId })} type="button">{t("admin.rosterReplacePlayer")}</button>
            <button className="secondary-button" disabled={!canRun("change-captain") || selectedMember.member.roster_role === "captain"} onClick={() => void runMutation("change-captain", { team_key: selectedTeam.team_key, user_id: selectedMember.member.user_id })} type="button"><Crown size={14} />{t("admin.rosterChangeCaptain")}</button>
            <button className="secondary-button danger" disabled={!canRun("remove-player") || selectedMember.member.roster_role === "captain"} onClick={() => void runMutation("remove-player", { team_key: selectedTeam.team_key, user_id: selectedMember.member.user_id })} type="button">{t("admin.rosterRemovePlayer")}</button>
          </div>
        </div>
      ) : <div className="admin-empty admin-roster-selection-hint">{t("admin.rosterSelectMemberHint")}</div>}

      <label className="admin-note-field admin-roster-reason">
        {t("admin.rosterReason")}
        <textarea maxLength={1000} placeholder={t("admin.rosterReasonPlaceholder")} value={reason} onChange={(event) => setReason(event.target.value)} />
      </label>
      {roster.capabilities.requires_override && roster.capabilities.can_override ? (
        <label className="admin-roster-override"><input checked={override} onChange={(event) => setOverride(event.target.checked)} type="checkbox" />{t("admin.rosterOverride")}</label>
      ) : null}
      {roster.capabilities.requires_override && roster.capabilities.can_override ? <p className="admin-roster-hint">{t("admin.rosterOverrideHint")}</p> : null}
      {actionError ? <div className="admin-feedback error">{actionError}</div> : null}
      {success ? <div className="admin-feedback success">{success}</div> : null}
      {!selectedMember && allMembers.length > 0 ? <div className="admin-roster-selection-hint">{t("admin.rosterSelectMemberHint")}</div> : null}
    </section>
  );
}
