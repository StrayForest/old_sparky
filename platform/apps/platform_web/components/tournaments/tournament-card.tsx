import Link from "next/link";
import { Calendar, Clock, PlayCircle, Users } from "lucide-react";
import { CspImage } from "@/components/media/csp-image";
import { PreparedMedia } from "@/components/media/prepared-media";
import { TournamentCountdownLabel } from "@/components/tournaments/tournament-countdown-label";
import { deadlockRankIconPath } from "@/lib/deadlock";
import { participantLimit, ranks, sortRanksByStrengthDesc } from "@/lib/tournament-model";
import { tournamentCoverAssetUrl } from "@/lib/tournament-covers";
import type { TournamentSummary } from "@/lib/types";

type TournamentCardProps = {
  tournament: TournamentSummary;
  interactive?: boolean;
};

export function TournamentCard({ tournament, interactive = true }: TournamentCardProps) {
  const coverUrl = tournamentCoverAssetUrl(tournament.coverUrl);
  const statusClassName = tournament.status.replaceAll("_", "-");
  const limit = participantLimit(tournament);
  const displayedRanks = sortRanksByStrengthDesc(
    tournament.allowedRanks.length ? tournament.allowedRanks : ranks.map((rank) => rank.code)
  );
  const progressClass = limit.kind === "limited" && limit.percent >= 95
    ? "red"
    : limit.kind === "limited" && limit.percent >= 80
      ? "yellow"
      : "green";
  const cardContent = (
    <>
      <div className="card-banner card-banner-cover">
        <PreparedMedia
          alt=""
          className="card-banner-media"
          descriptor={tournament.coverMedia}
          fallbackUrl={coverUrl}
          sizes="(max-width: 700px) 100vw, 560px"
        />
        <span
          className={`badge status tournament-status-${statusClassName}`}
          data-testid="tournament-status-badge"
        >
          {tournament.statusLabel}
        </span>
      </div>

      <div className="card-body">
        <div className="card-top">
          <div className="card-main-info">
            <h2 className="card-title">{tournament.title}</h2>
            <div className="card-date">
              <Calendar size={14} aria-hidden="true" />
              {tournament.startsAtLabel}
            </div>
          </div>
          <div className="card-timers">
            <div className={`timer registration registration-${tournament.status === "registration_open" ? "open" : "closed"}`}>
              <Clock size={15} aria-hidden="true" />
              <TournamentCountdownLabel
                targetIso={tournament.registrationClosesAtIso}
                fallbackLabel={tournament.registrationTimerLabel}
                prefix="Рег. открыта"
                elapsedLabel="Рег. закрыта"
              />
            </div>
            <div className="timer start">
              <PlayCircle size={15} aria-hidden="true" />
              <TournamentCountdownLabel
                targetIso={tournament.startsAtIso}
                fallbackLabel={tournament.startTimerLabel}
                prefix="Старт через"
                elapsedLabel="Старт начался"
              />
            </div>
          </div>
        </div>

        <div className="ranks-section">
          <div className="ranks-label">Допустимые ранги</div>
          <div className="ranks-row">
            {displayedRanks.map((rank) => (
              <CspImage
                alt=""
                className="rank-icon tournament-rank-icon"
                height={32}
                key={rank}
                loading="lazy"
                src={deadlockRankIconPath(rank)}
                width={32}
              />
            ))}
          </div>
        </div>

        <div className="tournament-card-meta">
          <div className="organizer">
            <div className={tournament.organizerAvatarUrl || tournament.organizerAvatarMedia ? "org-emblem org-emblem-avatar" : "org-emblem"}>
              {tournament.organizerAvatarUrl || tournament.organizerAvatarMedia ? (
                <PreparedMedia
                  alt=""
                  descriptor={tournament.organizerAvatarMedia}
                  fallbackUrl={tournament.organizerAvatarUrl}
                  height={40}
                  sizes="40px"
                  width={40}
                />
              ) : tournament.organizerName[0]}
            </div>
            <div>
              <div className="org-label">Организатор</div>
              <div className="org-name">{tournament.organizerName}</div>
            </div>
          </div>
          <div className="team-limit">
            <div className="team-limit-label">Макс. команд</div>
            <div className="team-limit-value" data-testid="team-limit-value"><Users aria-hidden="true" size={19} />{tournament.teamsCount}</div>
          </div>
          {limit.kind === "limited" ? (
            <div className="participants">
              <div className="participants-top">
                <span>Регистраций:</span>
                <span className="participants-value">{limit.current} / {limit.max}</span>
              </div>
              <progress
                aria-label="Заполнение регистраций"
                className={`progress ${progressClass}`}
                max={100}
                value={limit.percent}
              />
            </div>
          ) : (
            <div className="participants unlimited" data-testid="participant-limit-unlimited">
              <div className="participants-top">
                <span>Регистраций:</span>
                <span className="participants-value">{limit.current} / ∞</span>
              </div>
              <progress
                aria-label="Регистрации без ограничения"
                className="progress progress-muted muted"
                max={100}
                value={100}
              />
            </div>
          )}
        </div>
      </div>
    </>
  );

  const cardProps = {
    className: `tournament-card ${tournament.theme}`,
    "data-testid": "tournament-card"
  };

  if (!interactive || tournament.id === "preview") {
    return <article {...cardProps}>{cardContent}</article>;
  }

  return (
    <Link
      {...cardProps}
      aria-label={`Открыть турнир: ${tournament.title}`}
      href={`/tournaments/${tournament.slug}`}
    >
      {cardContent}
    </Link>
  );
}
