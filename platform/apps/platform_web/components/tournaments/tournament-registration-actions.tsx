"use client";

import { useEffect, useMemo, useState } from "react";
import { useI18n } from "@/components/i18n-provider";
import { leaveTournament, registerForTournament, setTournamentReadyCheckChoice } from "@/lib/platform-api";
import { isActiveParticipantStatus } from "@/lib/tournament-model";
import type { Registration, TournamentDetail } from "@/lib/types";

type StepState = {
  registration: Registration | null;
  readyCheckChoice: string | null;
  saving: "registration" | "leave" | "ready" | null;
  error: string | null;
  errorStep: "registration" | "ready" | null;
  removedRegistrationId: string | null;
};

type TournamentRegistrationActionsProps = {
  tournament: TournamentDetail;
  actorUserId: string | null;
  onRegistrationChange?: (registration: Registration | null, previous: Registration | null) => void;
  onReadyChoiceChange?: (choice: string | null) => void;
};

export function TournamentRegistrationActions({
  tournament,
  actorUserId,
  onRegistrationChange,
  onReadyChoiceChange
}: TournamentRegistrationActionsProps) {
  const { t } = useI18n();
  const initialRegistration = useMemo(
    () => currentUserRegistration(tournament, actorUserId),
    [actorUserId, tournament]
  );
  const initialReadyCheckChoice = useMemo(
    () => tournament.readyCheckState?.active_round?.current_user_choice
      ?? tournament.readyCheckState?.latest_round?.current_user_choice
      ?? (initialRegistration?.checkInStatus === "checked_in" || initialRegistration?.status === "checked_in" ? "yes" : null),
    [initialRegistration, tournament.readyCheckState]
  );
  const [state, setState] = useState<StepState>({
    registration: initialRegistration,
    readyCheckChoice: initialReadyCheckChoice,
    saving: null,
    error: null,
    errorStep: null,
    removedRegistrationId: null
  });
  const confirmationEndsAtMs = timestampMs(tournament.schedule?.checkInEndsAt);
  const [nowMs, setNowMs] = useState<number | null>(null);

  useEffect(() => {
    setNowMs(Date.now());
    if (confirmationEndsAtMs === null) {
      return;
    }
    const delay = confirmationEndsAtMs - Date.now();
    if (delay <= 0) {
      return;
    }
    const timeoutId = window.setTimeout(() => setNowMs(Date.now()), delay + 25);
    return () => window.clearTimeout(timeoutId);
  }, [confirmationEndsAtMs]);

  useEffect(() => {
    setState((current) => {
      if (!initialRegistration) {
        if (current.removedRegistrationId) {
          return current;
        }
        if (current.registration) {
          return {
            ...current,
            registration: null,
            readyCheckChoice: initialReadyCheckChoice
          };
        }
        return current;
      }
      if (current.removedRegistrationId === initialRegistration.id) {
        return current;
      }
      if (current.registration?.id === initialRegistration?.id) {
        return current.readyCheckChoice === initialReadyCheckChoice
          ? current
          : { ...current, readyCheckChoice: initialReadyCheckChoice };
      }
      return {
        ...current,
        registration: initialRegistration,
        readyCheckChoice: initialReadyCheckChoice,
        removedRegistrationId: null
      };
    });
  }, [initialReadyCheckChoice, initialRegistration]);

  const registered = Boolean(state.registration);
  const readyCheckActive = tournament.readyCheckState?.active_round?.status === "active";
  const readyCheckClosed = Boolean(
    (nowMs !== null && confirmationEndsAtMs !== null && nowMs >= confirmationEndsAtMs)
    || (
      !readyCheckActive
      && tournament.readyCheckState?.latest_round
      && tournament.readyCheckState.latest_round.status !== "active"
    )
  );
  const checkedIn = state.readyCheckChoice === "yes";
  const teamsFormed = tournament.teams.length > 0;
  const hasRegistrationAccess = Boolean(
    tournament.visibility !== "private"
    || tournament.currentUserHasInviteAccess
    || (actorUserId && actorUserId === tournament.organizerUserId)
  );
  const canRegister = Boolean(
    actorUserId
    && hasRegistrationAccess
    && tournament.status === "registration_open"
    && !registered
    && !teamsFormed
  );
  const canCancelRegistration = Boolean(
    actorUserId
    && registered
    && !checkedIn
    && !teamsFormed
    && (tournament.status === "registration_open" || tournament.status === "registration_closed")
  );
  const canToggleReady = Boolean(actorUserId && registered && readyCheckActive && !readyCheckClosed);
  const registrationIsStatus = Boolean(
    state.saving === null
    && ((registered && !canCancelRegistration) || (!registered && teamsFormed))
  );
  const readyIsStatus = checkedIn && !canToggleReady && state.saving !== "ready";
  const inviteAccessRequired = Boolean(actorUserId && !registered && !teamsFormed && !hasRegistrationAccess);

  async function handleRegister() {
    if (state.saving || !actorUserId || registered || !canRegister) {
      return;
    }

    const previous = state.registration;
    const optimistic: Registration = {
      id: "optimistic-registration",
      userId: actorUserId,
      status: "registered",
      checkInStatus: "pending",
      registeredAt: new Date().toISOString(),
      checkedInAt: null
    };
    setState((current) => ({
      ...current,
      registration: optimistic,
      saving: "registration",
      error: null,
      errorStep: null,
      removedRegistrationId: null
    }));
    const result = await registerForTournament(tournament.slug).catch(() => null);
    setState((current) => result
      ? { ...current, registration: result, saving: null, error: null, errorStep: null, removedRegistrationId: null }
      : {
          ...current,
          registration: previous,
          saving: null,
          error: t("tournament.registrationActionFailed"),
          errorStep: "registration",
          removedRegistrationId: null
        });
    if (result) {
      onRegistrationChange?.(result, previous);
    }
  }

  async function handleLeave() {
    if (state.saving || !actorUserId || !registered || checkedIn) {
      return;
    }

    const previous = state.registration;
    setState((current) => ({
      ...current,
      registration: previous,
      saving: "leave",
      error: null,
      errorStep: null,
      removedRegistrationId: null
    }));
    const result = await leaveTournament(tournament.slug).catch(() => false);
    setState((current) => result
      ? {
          ...current,
          registration: null,
          readyCheckChoice: null,
          saving: null,
          error: null,
          errorStep: null,
          removedRegistrationId: previous?.id ?? "removed-registration"
        }
      : {
          ...current,
          registration: previous,
          saving: null,
          error: t("tournament.registrationCancelFailed"),
          errorStep: "registration",
          removedRegistrationId: null
        });
    if (result) {
      onRegistrationChange?.(null, previous);
    }
  }

  async function handleReadyToggle() {
    if (state.saving || !actorUserId || !registered || !canToggleReady) {
      return;
    }

    const previousChoice = state.readyCheckChoice;
    const nextChoice = checkedIn ? "no" : "yes";
    setState((current) => ({
      ...current,
      readyCheckChoice: nextChoice,
      saving: "ready",
      error: null,
      errorStep: null
    }));
    const result = await setTournamentReadyCheckChoice(tournament.slug, nextChoice).catch(() => null);
    setState((current) => result
      ? {
          ...current,
          readyCheckChoice: result.current_user_choice ?? nextChoice,
          saving: null,
          error: null,
          errorStep: null
        }
      : {
          ...current,
          readyCheckChoice: previousChoice,
          saving: null,
          error: t("tournament.readyActionFailed"),
          errorStep: "ready"
        });
    if (result) {
      onReadyChoiceChange?.(result.current_user_choice ?? nextChoice);
    }
  }

  return (
    <section className="panel steps-panel" data-testid="registration-steps">
      <div className={`step ${registered ? "done" : canRegister ? "active" : ""}`}>
        {!actorUserId ? (
          <div aria-disabled="true" className="disabled-action">{t("tournament.stepSignInAction")}</div>
        ) : inviteAccessRequired ? (
          <div aria-disabled="true" className="disabled-action">{t("tournament.visibilityInvite")}</div>
        ) : registrationIsStatus ? (
          <div className="status-action">{registrationActionLabel({ registered, canCancelRegistration, teamsFormed, saving: state.saving, t })}</div>
        ) : (
          <button
            className="primary-action"
            type="button"
            onClick={registered ? handleLeave : handleRegister}
            disabled={state.saving !== null || (registered ? !canCancelRegistration : !canRegister)}
            aria-busy={state.saving === "registration" || state.saving === "leave"}
          >
            {registrationActionLabel({ registered, canCancelRegistration, teamsFormed, saving: state.saving, t })}
          </button>
        )}
        <div className="step-note">
          {state.errorStep === "registration" && state.error
            ? state.error
            : inviteAccessRequired
              ? t("info.faq.private.answer")
              : t("tournament.stepRegistrationOpenUntil", { time: scheduleLabel(tournament, "registrationClosesAt") })}
        </div>
      </div>
      <div className="arrow" />
      <div className={`step ${checkedIn ? "done" : canToggleReady ? "active" : ""}`}>
        {readyIsStatus ? (
          <div className="status-action">{readyActionLabel({
            checkedIn,
            canToggleReady,
            readyCheckClosed,
            saving: state.saving === "ready",
            t
          })}</div>
        ) : (
          <button
            className={checkedIn || canToggleReady ? "primary-action" : "disabled-action"}
            type="button"
            onClick={handleReadyToggle}
            disabled={!canToggleReady || state.saving === "ready"}
            aria-busy={state.saving === "ready"}
          >
            {readyActionLabel({
              checkedIn,
              canToggleReady,
              readyCheckClosed,
              saving: state.saving === "ready",
              t
            })}
          </button>
        )}
        <div className="step-note">
          {state.errorStep === "ready" && state.error
            ? state.error
            : t("tournament.stepReadyWindow", {
                start: scheduleLabel(tournament, "checkInStartsAt"),
                end: scheduleLabel(tournament, "checkInEndsAt")
              })}
        </div>
      </div>
      <div className="arrow" />
      <div className={`step ${teamsFormed ? "done" : ""}`}>
        <div className="status-action">
          {teamsFormed ? t("tournament.stepFormedAction") : t("tournament.stepWaitingTeamsAction")}
        </div>
        <div className="step-note team-auto-text">{t("tournament.stepTeamsFormAt", { time: approximateScheduleLabel(tournament, "teamsFormAt") })}</div>
      </div>
    </section>
  );
}

function currentUserRegistration(
  tournament: TournamentDetail,
  actorUserId: string | null
): Registration | null {
  if (!actorUserId) {
    return null;
  }

  const status = tournament.currentUserParticipantStatus;
  if (status && !isActiveParticipantStatus(status)) {
    return null;
  }

  const listedRegistration = tournament.registrations.find(
    (registration) => (
      registration.userId === actorUserId
      && isActiveParticipantStatus(registration.status)
    )
  );
  if (listedRegistration) {
    return listedRegistration;
  }

  if (!isActiveParticipantStatus(status)) {
    return null;
  }
  const confirmed = status === "confirmed" || status === "checked_in";

  return {
    id: `current-user-registration:${tournament.id}:${actorUserId}`,
    userId: actorUserId,
    displayName: actorUserId,
    entryType: tournament.participantMode,
    teamName: null,
    status,
    checkInStatus: confirmed ? "checked_in" : "pending",
    registeredAt: "",
    checkedInAt: null
  };
}

type RegistrationActionLabelInput = {
  registered: boolean;
  canCancelRegistration: boolean;
  teamsFormed: boolean;
  saving: StepState["saving"];
  t: ReturnType<typeof useI18n>["t"];
};

function registrationActionLabel({ registered, canCancelRegistration, teamsFormed, saving, t }: RegistrationActionLabelInput): string {
  if (saving === "registration") {
    return t("tournament.stepRegisteringAction");
  }
  if (saving === "leave") {
    return t("tournament.stepCancelRegistrationSavingAction");
  }
  if (registered && canCancelRegistration) {
    return t("tournament.stepCancelRegistrationAction");
  }
  if (registered) {
    return t("tournament.stepRegisteredAction");
  }
  if (teamsFormed) {
    return t("tournament.stepRegistrationClosedAction");
  }
  return t("tournament.stepRegisterAction");
}

type ReadyActionLabelInput = {
  checkedIn: boolean;
  canToggleReady: boolean;
  readyCheckClosed: boolean;
  saving: boolean;
  t: ReturnType<typeof useI18n>["t"];
};

function readyActionLabel({
  checkedIn,
  canToggleReady,
  readyCheckClosed,
  saving,
  t
}: ReadyActionLabelInput): string {
  if (saving) {
    return checkedIn
      ? t("tournament.stepReadySavingAction")
      : t("tournament.stepReadyCancelSavingAction");
  }
  if (checkedIn) {
    return canToggleReady
      ? t("tournament.stepReadyCancelAction")
      : t("tournament.stepReadyDoneAction");
  }
  if (readyCheckClosed) {
    return t("tournament.stepReadyClosedAction");
  }
  return t("tournament.stepReadyAction");
}

function scheduleLabel(tournament: TournamentDetail, field: "registrationClosesAt" | "checkInStartsAt" | "checkInEndsAt" | "teamsFormAt"): string {
  const value = tournament.schedule?.[field];
  if (!value) {
    return "по расписанию";
  }

  return `${new Intl.DateTimeFormat("ru-RU", {
    day: "numeric",
    month: "long",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: tournament.schedule?.timezone || "Europe/Moscow"
  }).format(new Date(value))} ${tournament.schedule?.timezone === "Europe/Moscow" ? "МСК" : tournament.schedule?.timezone ?? ""}`;
}

function approximateScheduleLabel(tournament: TournamentDetail, field: "teamsFormAt"): string {
  return scheduleLabel(tournament, field).replace(" в ", " в ~");
}

function timestampMs(value: string | null | undefined): number | null {
  if (!value) {
    return null;
  }
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : null;
}
