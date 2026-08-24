"use client";

import Link from "next/link";
import { ArrowLeft, Crown, ExternalLink, Info, RefreshCw, Users } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { TournamentCard } from "@/components/tournaments/tournament-card";
import { TournamentRegistrationActions } from "@/components/tournaments/tournament-registration-actions";
import { useI18n } from "@/components/i18n-provider";
import { CspImage } from "@/components/media/csp-image";
import { deadlockRankIconPath, deadlockRankPlaceholderPath } from "@/lib/deadlock";
import { isActiveParticipantStatus } from "@/lib/tournament-model";
import type { Registration, Team, TournamentDetail } from "@/lib/types";

type TournamentDetailViewProps = {
  tournament: TournamentDetail;
  actorUserId: string | null;
};

export function TournamentDetailView({ tournament, actorUserId }: TournamentDetailViewProps) {
  const { t } = useI18n();
  const [detail, setDetail] = useState<TournamentDetail>(tournament);
  const [selectedOpponentId, setSelectedOpponentId] = useState<string | null>(null);
  const initialReadyChoice = detail.readyCheckState?.active_round?.current_user_choice
    ?? detail.readyCheckState?.latest_round?.current_user_choice
    ?? null;
  const [readyChoice, setReadyChoice] = useState<string | null>(initialReadyChoice);

  useEffect(() => {
    setDetail(tournament);
  }, [tournament]);

  useEffect(() => {
    setReadyChoice(initialReadyChoice);
  }, [initialReadyChoice]);

  const currentTeam = useMemo(
    () => actorUserId
      ? detail.teams.find(
          (team) => team.members.some((member) => member.userId === actorUserId)
        ) ?? null
      : null,
    [actorUserId, detail.teams]
  );
  const opponents = useMemo(
    () => detail.teams.filter((team) => team.id !== currentTeam?.id),
    [currentTeam?.id, detail.teams]
  );
  const selectedOpponent = opponents.find((team) => team.id === selectedOpponentId) ?? null;
  const registered = Boolean(
    actorUserId
    && (
      isActiveParticipantStatus(detail.currentUserParticipantStatus)
      || detail.registrations.some(
        (registration) => (
          registration.userId === actorUserId
          && isActiveParticipantStatus(registration.status)
        )
      )
    )
  );
  const checkedIn = Boolean(
    actorUserId
    && (
      readyChoice === "yes"
      || detail.registrations.some(
        (registration) => registration.userId === actorUserId && registration.checkInStatus === "checked_in"
      )
    )
  );
  const teamsFormed = detail.teams.length > 0;
  const blockedByOtherTournament = Boolean(
    detail.activeCommitment
    && detail.activeCommitment.tournamentId !== detail.id
  );
  const bracketReady = detail.bracket.status === "ready" || detail.bracket.revision > 0;

  return (
    <>
      <section className="top-detail-grid">
        <TournamentCard interactive={false} tournament={detail} />
        <section className="panel panel-pad description-panel">
          <h2 className="panel-title"><Info size={17} />{t("tournament.descriptionTitle")}</h2>
          <p className="description-text tournament-description-text">
            {detail.description || `${detail.title} - турнир Deadlock для игроков платформы.`}
          </p>
        </section>
      </section>

      <TournamentRegistrationActions
        tournament={detail}
        actorUserId={actorUserId}
        onRegistrationChange={(registration, previous) => {
          setDetail((current) => updateCurrentUserRegistration(current, actorUserId, registration, previous));
        }}
        onReadyChoiceChange={setReadyChoice}
      />

      <section className="info-grid">
        <div className="panel bracket-panel">
          <div className="bracket-panel-title">
            <ReferenceBracketIcon />
            <div className="small-title">{t("tournament.bracketTitle")}</div>
          </div>
          <div className="bracket-format-group">
            <div className="bracket-format-summary">
              <span>{t("tournament.matchesLabel")}</span>
              <strong>{detail.matchFormat.toUpperCase()}</strong>
            </div>
            <div className="bracket-format-summary">
              <span>{t("tournament.finalLabel")}</span>
              <strong>{detail.finalFormat.toUpperCase()}</strong>
            </div>
          </div>
          <Link
            aria-disabled={!bracketReady}
            aria-label={t("tournament.openBracketShort")}
            className={bracketReady ? "outline-button bracket-open-link" : "outline-button bracket-open-link disabled-link"}
            href={`/tournaments/${detail.slug}/bracket`}
            onClick={(event) => {
              if (!bracketReady) {
                event.preventDefault();
              }
            }}
          >
            {t("tournament.openBracket")}
            <ExternalLink aria-hidden="true" size={15} />
          </Link>
        </div>
        <div className="panel next-panel">
          <div>
            <div className="small-title">{t("tournament.nextStepsTitle")}</div>
            <div className="check-list">
              <CheckItem label={registered ? t("tournament.nextStepRegistered") : t("tournament.nextStepRegister")} done={registered} />
              <CheckItem label={checkedIn ? t("tournament.nextStepReadyDone") : t("tournament.nextStepReady")} done={checkedIn} />
              <CheckItem label={teamsFormed ? t("tournament.nextStepTeamsDone") : t("tournament.nextStepTeams")} done={teamsFormed} />
            </div>
          </div>
        </div>
      </section>

      {registered && !teamsFormed && blockedByOtherTournament && detail.activeCommitment ? (
        <section className="panel team-unassigned-panel" data-testid="tournament-commitment-blocked">
          <Users size={22} aria-hidden="true" />
          <div>
            <div className="small-text">
              {t("tournament.commitmentBlocked", {
                team: detail.activeCommitment.teamName,
                tournament: detail.activeCommitment.tournamentName,
              })}
            </div>
          </div>
        </section>
      ) : registered && teamsFormed && currentTeam ? (
        <section className="team-grid">
          <TeamTable
            actorUserId={actorUserId}
            title={t("tournament.myTeam")}
            team={currentTeam}
            tournamentSlug={detail.slug}
          />
          {selectedOpponent ? (
            <TeamTable
              title={selectedOpponent.name}
              team={selectedOpponent}
              actorUserId={actorUserId}
              tournamentSlug={detail.slug}
              headerAction={(
                <button className="team-control roster-back-btn" onClick={() => setSelectedOpponentId(null)} type="button">
                  <ArrowLeft size={14} aria-hidden="true" />
                  {t("common.back")}
                </button>
              )}
            />
          ) : (
            <OpponentTeamsTable teams={opponents} onSelectTeam={setSelectedOpponentId} />
          )}
        </section>
      ) : registered && teamsFormed ? (
        <section className="panel team-unassigned-panel" data-testid="tournament-team-unassigned">
          <Users size={22} aria-hidden="true" />
          <div>
            <div className="small-title">{t("tournament.notAssignedTitle")}</div>
          </div>
        </section>
      ) : null}

    </>
  );
}

function updateCurrentUserRegistration(
  tournament: TournamentDetail,
  actorUserId: string | null,
  registration: Registration | null,
  previous: Registration | null
): TournamentDetail {
  if (!actorUserId) {
    return tournament;
  }

  const wasRegistered = Boolean(
    isActiveParticipantStatus(tournament.currentUserParticipantStatus)
    || tournament.registrations.some(
      (item) => item.userId === actorUserId && isActiveParticipantStatus(item.status)
    )
    || (previous && isActiveParticipantStatus(previous.status))
  );
  const isRegistered = Boolean(registration && isActiveParticipantStatus(registration.status));

  const registrations = registration
    ? upsertRegistration(tournament.registrations, registration)
    : tournament.registrations.filter(
        (item) => item.id !== previous?.id && item.userId !== actorUserId
      );
  const participantCount = Math.max(
    0,
    tournament.participantCount + (isRegistered && !wasRegistered ? 1 : 0) - (!isRegistered && wasRegistered ? 1 : 0)
  );

  return {
    ...tournament,
    registrations,
    participantCount,
    currentUserParticipantStatus: registration?.status ?? null
  };
}

function upsertRegistration(registrations: Registration[], registration: Registration): Registration[] {
  const existingIndex = registrations.findIndex(
    (item) => item.id === registration.id || item.userId === registration.userId
  );
  if (existingIndex === -1) {
    return [...registrations, registration];
  }
  return registrations.map((item, index) => index === existingIndex ? registration : item);
}

function ReferenceBracketIcon() {
  return (
    <svg className="bracket-icon" width="54" height="54" viewBox="0 0 64 64" fill="none" aria-hidden="true">
      <path d="M10 8h14v12H10zM10 26h14v12H10zM10 44h14v12H10zM24 14h10v18h8M24 32h18M24 50h10V32M42 26h12v12H42z" stroke="currentColor" strokeWidth="3" />
    </svg>
  );
}

function CheckItem({ label, done }: { label: string; done: boolean }) {
  const { t } = useI18n();
  return (
    <div className="check-item">
      <span className={done ? "dot done" : "dot wait"} />
      <span>{label}</span>
      <span className={done ? "check-state done" : "check-state wait"}>{done ? t("common.done") : t("common.notDone")}</span>
    </div>
  );
}

function TeamTable({
  actorUserId,
  title,
  team,
  tournamentSlug,
  headerAction,
}: {
  actorUserId: string | null;
  title: string;
  team: Team | null;
  tournamentSlug: string;
  headerAction?: ReactNode;
}) {
  const { t } = useI18n();
  return (
    <div className="panel team-panel">
      <div className="team-header">
        <div className="team-title"><Users size={18} />{title}</div>
        {headerAction ? <div className="team-header-control">{headerAction}</div> : null}
      </div>
      <table className="table">
        <tbody>
          {team?.members.length ? team.members.map((member) => (
            <tr key={member.userId}>
              <td className="player-cell">
                <span className="team-player-identity">
                  <span className="team-player-avatar">
                    {member.avatarUrl ? (
                      <CspImage alt="" height={32} src={member.avatarUrl} width={32} />
                    ) : avatarInitials(member.handle)}
                    {member.isCaptain ? (
                      <span className="team-player-role-icon team-player-role-icon-captain" role="img" aria-label="Капитан">
                        <Crown aria-hidden="true" size={14} strokeWidth={2.5} />
                      </span>
                    ) : member.isSubstitute ? (
                      <span className="team-player-role-icon team-player-role-icon-substitute" role="img" aria-label="Замена">
                        <RefreshCw aria-hidden="true" size={13} strokeWidth={2.5} />
                      </span>
                    ) : null}
                  </span>
                  <span>{member.handle}</span>
                </span>
                {member.rank ? (
                  <span className="team-player-rank" title={[member.rank, member.subrank].filter(Boolean).join(" ")}>
                    <CspImage
                      alt=""
                      height={34}
                      onError={(event) => {
                        event.currentTarget.onerror = null;
                        event.currentTarget.src = deadlockRankPlaceholderPath;
                      }}
                      src={deadlockRankIconPath(member.rank)}
                      width={34}
                    />
                    {member.subrank ? <span>{member.subrank}</span> : null}
                  </span>
                ) : null}
              </td>
              <td className="team-action-cell">
                {member.userId === actorUserId ? (
                  <Link className="team-control" href="/profile/me">{t("tournament.viewProfile")}</Link>
                ) : (
                  <Link
                    className="team-control"
                    href={`/tournaments/${encodeURIComponent(tournamentSlug)}/profiles/${encodeURIComponent(member.userId)}`}
                  >
                    {t("tournament.viewProfile")}
                  </Link>
                )}
              </td>
            </tr>
          )) : (
            <tr><td className="player-cell" colSpan={3}>{t("tournament.teamsNotFormed")}</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function OpponentTeamsTable({ teams, onSelectTeam }: { teams: Team[]; onSelectTeam: (teamId: string) => void }) {
  const { t } = useI18n();
  return (
    <div className="panel team-panel opponent-team-panel">
      <div className="team-header">
        <div className="team-title">
          <Users size={18} />
          <span className="opponent-title-full">{t("tournament.opponentTeams")}</span>
          <span className="opponent-title-short">{t("tournament.opponentsShort")}</span>
        </div>
        <div className="team-header-control">
          <span className="team-badge team-control">{t("tournament.teamsCountBadge", { count: teams.length })}</span>
        </div>
      </div>
      <div className="opponents-scroll">
        <table className="table">
          <tbody>
            {teams.length ? teams.map((team, index) => (
              <tr key={team.id}>
                <td className="player-cell"><span className={`team-list-mark mark-${(index % 4) + 1}`} />{team.name}</td>
                <td className="team-action-cell">
                  <button className="team-control" onClick={() => onSelectTeam(team.id)} type="button">
                    {t("tournament.roster")}
                  </button>
                </td>
              </tr>
            )) : (
              <tr><td className="player-cell" colSpan={2}>{t("tournament.noOpponentTeams")}</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function avatarInitials(handle: string): string {
  return handle.trim().slice(0, 2).toUpperCase() || "?";
}
