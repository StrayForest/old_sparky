"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { currentReadyCheckRound, useReadyCheckPhase } from "@/components/ready-check/ready-check-timer";
import { useI18n } from "@/components/i18n-provider";
import {
  leaveTournament,
  PlatformApiError,
  platformApiMessage,
  registerForTournament,
  setTournamentReadyCheckChoice
} from "@/lib/platform-api";
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
    () => currentReadyCheckRound(
      tournament.readyCheckState,
      tournament.schedule?.checkInStartsAt,
    )?.current_user_choice
      ?? (initialRegistration?.checkInStatus === "checked_in" || initialRegistration?.status === "checked_in" ? "yes" : null),
    [initialRegistration, tournament.readyCheckState, tournament.schedule?.checkInStartsAt]
  );
  const [state, setState] = useState<StepState>({
    registration: initialRegistration,
    readyCheckChoice: initialReadyCheckChoice,
    saving: null,
    error: null,
    errorStep: null,
    removedRegistrationId: null
  });
  const readyActionInFlight = useRef(false);
  const actionIdentity = `${tournament.id}:${tournament.slug}:${actorUserId ?? "anonymous"}`;
  const actionIdentityRef = useRef(actionIdentity);
  const actionGeneration = useRef(0);
  const activeActionController = useRef<AbortController | null>(null);
  const [readyRetryCooldown, setReadyRetryCooldown] = useState(false);
  const readyCheckPhase = useReadyCheckPhase(
    tournament.serverTime,
    tournament.schedule?.checkInStartsAt,
    tournament.schedule?.checkInEndsAt,
  );
  const [readyCheckTimerMounted, setReadyCheckTimerMounted] = useState(false);

  useEffect(() => {
    setReadyCheckTimerMounted(true);
    return () => setReadyCheckTimerMounted(false);
  }, []);

  useEffect(() => {
    if (actionIdentityRef.current === actionIdentity) {
      return;
    }
    actionIdentityRef.current = actionIdentity;
    actionGeneration.current += 1;
    activeActionController.current?.abort();
    activeActionController.current = null;
    readyActionInFlight.current = false;
    setReadyRetryCooldown(false);
    setState({
      registration: initialRegistration,
      readyCheckChoice: initialReadyCheckChoice,
      saving: null,
      error: null,
      errorStep: null,
      removedRegistrationId: null
    });
  }, [actionIdentity, initialReadyCheckChoice, initialRegistration]);

  useEffect(() => () => {
    actionGeneration.current += 1;
    activeActionController.current?.abort();
    activeActionController.current = null;
    readyActionInFlight.current = false;
  }, []);

  useEffect(() => {
    if (!readyRetryCooldown) {
      return;
    }
    const timeout = window.setTimeout(() => setReadyRetryCooldown(false), 1500);
    return () => window.clearTimeout(timeout);
  }, [readyRetryCooldown]);

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
  const readyCheckActive = readyCheckPhase === "active";
  const currentRound = currentReadyCheckRound(
    tournament.readyCheckState,
    tournament.schedule?.checkInStartsAt,
  );
  const readyCheckClosed = Boolean(
    readyCheckPhase === "finished"
    || (
      currentRound
      && currentRound.status !== "active"
    )
  );
  const checkedIn = state.readyCheckChoice === "yes";
  const teamsFormed = tournament.teams.length > 0;
  const inactiveParticipant = Boolean(
    actorUserId
    && tournament.currentUserParticipantStatus
    && !isActiveParticipantStatus(tournament.currentUserParticipantStatus)
  );
  const readOnlyBearer = Boolean(
    tournament.visibility === "private"
    && tournament.inviteCode
    && (!actorUserId || inactiveParticipant)
  );
  const hasRegistrationAccess = Boolean(
    tournament.visibility !== "private"
    || tournament.inviteCode
    || (actorUserId && actorUserId === tournament.organizerUserId)
  );
  const canRegister = Boolean(
    actorUserId
    && hasRegistrationAccess
    && tournament.status === "registration_open"
    && !registered
    && !teamsFormed
    && !inactiveParticipant
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
    && (inactiveParticipant || (registered && !canCancelRegistration) || (!registered && teamsFormed))
  );
  const readyIsStatus = checkedIn && !canToggleReady && state.saving !== "ready";
  const inviteAccessRequired = Boolean(
    actorUserId
    && !inactiveParticipant
    && !registered
    && !teamsFormed
    && !hasRegistrationAccess
  );

  function requestIsCurrent(
    generation: number,
    identity: string,
    controller: AbortController,
  ): boolean {
    return (
      !controller.signal.aborted
      && actionGeneration.current === generation
      && actionIdentityRef.current === identity
    );
  }

  async function handleRegister() {
    if (state.saving || !actorUserId || registered || !canRegister) {
      return;
    }

    const previous = state.registration;
    const requestSlug = tournament.slug;
    const requestInviteCode = tournament.inviteCode;
    const requestIdentity = actionIdentity;
    const requestGeneration = actionGeneration.current;
    const controller = new AbortController();
    activeActionController.current?.abort();
    activeActionController.current = controller;
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
    let result: Registration | null = null;
    try {
      result = await registerForTournament(requestSlug, requestInviteCode, controller.signal);
    } catch {
      // An aborted request belongs to an older tournament/session and must not
      // clear or replace state owned by the current view.
      if (!requestIsCurrent(requestGeneration, requestIdentity, controller)) {
        return;
      }
    }
    if (!requestIsCurrent(requestGeneration, requestIdentity, controller)) {
      return;
    }
    setState((current) => requestIsCurrent(requestGeneration, requestIdentity, controller)
      ? result
        ? { ...current, registration: result, saving: null, error: null, errorStep: null, removedRegistrationId: null }
        : {
            ...current,
            registration: previous,
            saving: null,
            error: t("tournament.registrationActionFailed"),
            errorStep: "registration",
            removedRegistrationId: null
          }
      : current);
    if (result && requestIsCurrent(requestGeneration, requestIdentity, controller)) {
      onRegistrationChange?.(result, previous);
    }
    if (activeActionController.current === controller) {
      activeActionController.current = null;
    }
  }

  async function handleLeave() {
    if (state.saving || !actorUserId || !registered || checkedIn) {
      return;
    }

    const previous = state.registration;
    const requestSlug = tournament.slug;
    const requestIdentity = actionIdentity;
    const requestGeneration = actionGeneration.current;
    const controller = new AbortController();
    activeActionController.current?.abort();
    activeActionController.current = controller;
    setState((current) => ({
      ...current,
      registration: previous,
      saving: "leave",
      error: null,
      errorStep: null,
      removedRegistrationId: null
    }));
    let result = false;
    try {
      result = await leaveTournament(requestSlug, controller.signal);
    } catch {
      if (!requestIsCurrent(requestGeneration, requestIdentity, controller)) {
        return;
      }
    }
    if (!requestIsCurrent(requestGeneration, requestIdentity, controller)) {
      return;
    }
    setState((current) => requestIsCurrent(requestGeneration, requestIdentity, controller)
      ? result
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
          }
      : current);
    if (result && requestIsCurrent(requestGeneration, requestIdentity, controller)) {
      onRegistrationChange?.(null, previous);
    }
    if (activeActionController.current === controller) {
      activeActionController.current = null;
    }
  }

  async function handleReadyToggle() {
    if (
      state.saving
      || readyActionInFlight.current
      || readyRetryCooldown
      || !actorUserId
      || !registered
      || !canToggleReady
    ) {
      return;
    }

    readyActionInFlight.current = true;
    const previousChoice = state.readyCheckChoice;
    const nextChoice = checkedIn ? "no" : "yes";
    const requestSlug = tournament.slug;
    const requestIdentity = actionIdentity;
    const requestGeneration = actionGeneration.current;
    const controller = new AbortController();
    activeActionController.current?.abort();
    activeActionController.current = controller;
    setState((current) => ({
      ...current,
      readyCheckChoice: nextChoice,
      saving: "ready",
      error: null,
      errorStep: null
    }));
    try {
      const result = await setTournamentReadyCheckChoice(requestSlug, nextChoice, controller.signal);
      if (!requestIsCurrent(requestGeneration, requestIdentity, controller)) {
        return;
      }
      setState((current) => requestIsCurrent(requestGeneration, requestIdentity, controller)
        ? result
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
            }
        : current);
      if (result && requestIsCurrent(requestGeneration, requestIdentity, controller)) {
        onReadyChoiceChange?.(result.current_user_choice ?? nextChoice);
      }
    } catch (error) {
      if (!requestIsCurrent(requestGeneration, requestIdentity, controller)) {
        return;
      }
      const overloaded = error instanceof PlatformApiError
        && error.status === 503
        && error.code === "READY_VOTE_OVERLOADED"
        && error.retryable;
      setReadyRetryCooldown(overloaded);
      setState((current) => ({
        ...current,
        readyCheckChoice: previousChoice,
        saving: null,
        error: overloaded
          ? t("tournament.readyActionBusy")
          : platformApiMessage(error, t("tournament.readyActionFailed")),
        errorStep: "ready"
      }));
    } finally {
      if (activeActionController.current === controller) {
        readyActionInFlight.current = false;
        activeActionController.current = null;
      }
    }
  }

  return (
    <section className="panel steps-panel" data-testid="registration-steps">
      <div className={`step ${registered ? "done" : canRegister ? "active" : ""}`}>
        {readOnlyBearer ? (
          <div aria-disabled="true" className="disabled-action" data-testid="tournament-read-only-registration">{t("tournament.stepInactiveParticipantAction")}</div>
        ) : !actorUserId ? (
          <div aria-disabled="true" className="disabled-action">{t("tournament.stepSignInAction")}</div>
        ) : inactiveParticipant ? (
          <div aria-disabled="true" className="disabled-action" data-testid="tournament-read-only-registration">{t("tournament.stepInactiveParticipantAction")}</div>
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
            : readOnlyBearer
              ? t("tournament.stepInactiveParticipantNote")
              : inviteAccessRequired
              ? t("info.faq.private.answer")
              : t("tournament.stepRegistrationOpenUntil", { time: scheduleLabel(tournament, "registrationClosesAt") })}
        </div>
      </div>
      <div className="arrow" />
      <div
        className={`step ${checkedIn ? "done" : canToggleReady ? "active" : ""}`}
        data-testid="ready-check-step"
        data-ready-check-phase={readyCheckPhase}
        data-ready-check-timer-mounted={readyCheckTimerMounted ? "true" : undefined}
      >
        {readOnlyBearer ? (
          <div aria-disabled="true" className="disabled-action" data-testid="tournament-read-only-workflow">{t("tournament.stepInactiveParticipantAction")}</div>
        ) : readyIsStatus ? (
          <div className="status-action">{readyActionLabel({
            checkedIn,
            canToggleReady,
            readyCheckClosed,
            busy: readyRetryCooldown,
            saving: state.saving === "ready",
            t
          })}</div>
        ) : (
          <button
            className={checkedIn || canToggleReady ? "primary-action" : "disabled-action"}
            type="button"
            onClick={handleReadyToggle}
            disabled={!canToggleReady || state.saving === "ready" || readyRetryCooldown}
            aria-busy={state.saving === "ready"}
          >
            {readyActionLabel({
              checkedIn,
              canToggleReady,
              readyCheckClosed,
              busy: readyRetryCooldown,
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
  busy: boolean;
  saving: boolean;
  t: ReturnType<typeof useI18n>["t"];
};

function readyActionLabel({
  checkedIn,
  canToggleReady,
  readyCheckClosed,
  busy,
  saving,
  t
}: ReadyActionLabelInput): string {
  if (busy) {
    return t("tournament.stepReadyBusyAction");
  }
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
