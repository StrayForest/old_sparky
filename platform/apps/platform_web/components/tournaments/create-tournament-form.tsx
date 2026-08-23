"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { Calendar, Check, CheckCircle, ChevronDown, ChevronUp, Circle, Clock, Copy, Infinity as InfinityIcon, Info, Lock, RefreshCcw, Upload } from "lucide-react";
import type { FormEvent } from "react";
import { useI18n } from "@/components/i18n-provider";
import { CspImage } from "@/components/media/csp-image";
import { TournamentCard } from "@/components/tournaments/tournament-card";
import { copyTextToClipboard } from "@/lib/clipboard";
import { deadlockRankIconPath, deadlockRankPlaceholderPath } from "@/lib/deadlock";
import {
  checkTournamentInviteCode,
  createTournament,
  PlatformApiError,
  platformApiMessage,
  platformApiRequest,
  suggestTournamentInviteCode,
  uploadTournamentBanner,
  waitForOwnedMedia
} from "@/lib/platform-api";
import { ranks, sortRanksByStrengthDesc } from "@/lib/tournament-model";
import {
  DEFAULT_TOURNAMENT_COVER_URL,
  TOURNAMENT_COVER_TEMPLATES,
  tournamentCoverAssetUrl,
  TOURNAMENT_COVER_UPLOAD_HINT,
  TOURNAMENT_COVER_UPLOAD_MAX_BYTES,
  TOURNAMENT_COVER_UPLOAD_TYPES
} from "@/lib/tournament-covers";
import type { TournamentCreatePayload } from "@/lib/platform-api";
import type { PlatformMediaDescriptor, PlatformProfile, PlatformUser } from "@/lib/platform-types";
import type { TournamentSummary } from "@/lib/types";
import { z } from "@/lib/zod";

type CreateFormValues = {
  title: string;
  organizerName: string;
  description: string;
  visibility: "public" | "private";
  inviteCode: string;
  registrationClosesDate: string;
  registrationClosesAt: string;
  checkInStartsDate: string;
  checkInStartsAt: string;
  teamsFormDate: string;
  teamsFormAt: string;
  startsDate: string;
  startsAt: string;
  maxParticipants: string;
  maxTeams: string;
  allowedRankCodes: string[];
  matchFormat: "bo1" | "bo3" | "bo5";
  finalFormat: "bo1" | "bo3" | "bo5";
};

const MAX_TITLE_LENGTH = 25;
const MAX_DESCRIPTION_LENGTH = 200;
const MAX_DESCRIPTION_LINES = 10;
const MAX_DESCRIPTION_LINE_LENGTH = 70;
const COPY_CONFIRMATION_MS = 3000;
const MAX_MANUAL_PARTICIPANTS = 99_999;
const UNLIMITED_PARTICIPANTS = 999_999_999;
const TEAM_COUNT_CHOICES = [2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192] as const;
const INVALID_TITLE_CHARACTERS = /[^A-Za-z0-9 .,'!?&#():+\-_/]/g;
const TITLE_PLACEHOLDER = "Например, Old Sparky Cup";
const DESCRIPTION_PLACEHOLDER = "Трансляция будет на твиче - OldSparky\nДругие соцсети - @OldSparky\nПризовой фонд - 10 денег";
const createSchema = z.object({
  title: z.string().trim().min(3).max(MAX_TITLE_LENGTH).regex(/^[A-Za-z0-9][A-Za-z0-9 .,'!?&#():+\-_/]*$/),
  description: z.string().trim().max(MAX_DESCRIPTION_LENGTH).refine(
    (value) => countDescriptionLines(value) <= MAX_DESCRIPTION_LINES
  ),
  visibility: z.enum(["public", "private"]),
  inviteCode: z.string().trim().min(10).max(24),
  maxTeams: z.coerce.number().int().min(2).max(8192),
  allowedRankCodes: z.array(z.string()).min(1)
});

export function CreateTournamentForm({
  currentUser,
  serverNowIso
}: {
  currentUser: PlatformUser;
  serverNowIso: string;
}) {
  const { t } = useI18n();
  const router = useRouter();
  const parsedServerNowMs = Date.parse(serverNowIso);
  const serverNowMs = Number.isFinite(parsedServerNowMs) ? parsedServerNowMs : Date.now();
  const inviteCodeTouchedRef = useRef(false);
  const inviteCopyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const inviteRequestGenerationRef = useRef(0);
  const submitInFlightRef = useRef(false);
  const [values, setValues] = useState<CreateFormValues>(() => ({
    ...buildDefaultValues(serverNowMs),
    organizerName: currentUser.display_name,
    inviteCode: fallbackInviteCode()
  }));
  const [organizerAvatarUrl, setOrganizerAvatarUrl] = useState<string | null>(null);
  const [organizerAvatarMedia, setOrganizerAvatarMedia] = useState<PlatformMediaDescriptor | null>(null);
  const [coverFile, setCoverFile] = useState<File | null>(null);
  const [coverPreviewUrl, setCoverPreviewUrl] = useState<string>(DEFAULT_TOURNAMENT_COVER_URL);
  const [selectedCoverTemplateUrl, setSelectedCoverTemplateUrl] = useState<string>(DEFAULT_TOURNAMENT_COVER_URL);
  const [status, setStatus] = useState<"idle" | "saving" | "error" | "invalid">("idle");
  const [formMessage, setFormMessage] = useState<string | null>(null);
  const [createdTournamentSlug, setCreatedTournamentSlug] = useState<string | null>(null);
  const [inviteCodeStatus, setInviteCodeStatus] = useState<"idle" | "checking" | "available" | "taken" | "unknown">("idle");
  const [inviteCopied, setInviteCopied] = useState(false);
  const canCreatePublic = canUserCreatePublic(currentUser);
  const privateMonthlyLimit = currentUser.private_tournament_monthly_limit ?? 1;
  const privateMonthlyRemaining = currentUser.private_tournament_monthly_remaining ?? privateMonthlyLimit;
  const privateTournamentCredits = currentUser.private_tournament_credits ?? 0;
  const canCreatePrivate = privateMonthlyRemaining > 0 || privateTournamentCredits > 0;

  useEffect(() => {
    let cancelled = false;
    void platformApiRequest<PlatformProfile>("/profiles/me")
      .then((profile) => {
        if (!cancelled) {
          setOrganizerAvatarUrl(profile.avatar_url ?? null);
          setOrganizerAvatarMedia(profile.avatar_media ?? null);
        }
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => () => {
    inviteRequestGenerationRef.current += 1;
    if (inviteCopyTimerRef.current) {
      clearTimeout(inviteCopyTimerRef.current);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    const requestGeneration = ++inviteRequestGenerationRef.current;

    void generateInviteCode().then((suggestion) => {
      if (cancelled || requestGeneration !== inviteRequestGenerationRef.current || inviteCodeTouchedRef.current) {
        return;
      }
      setValues((current) => ({ ...current, inviteCode: suggestion.code }));
      setInviteCodeStatus(suggestion.verified ? "available" : "unknown");
    });

    return () => {
      cancelled = true;
    };
  }, []);

  async function handleCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitInFlightRef.current || createdTournamentSlug) {
      return;
    }
    submitInFlightRef.current = true;
    setStatus("saving");
    setFormMessage(null);

    try {
      const normalizedValues = {
        ...values,
        inviteCode: normalizeInviteCode(values.inviteCode)
      };
      if (normalizedValues.inviteCode !== values.inviteCode) {
        setValues(normalizedValues);
      }
      const parsed = createSchema.safeParse(normalizedValues);
      if (!parsed.success) {
        setStatus("invalid");
        setFormMessage("Проверьте название, код приглашения, расписание и допустимые ранги.");
        if (!normalizedValues.inviteCode || normalizedValues.inviteCode.length < 10) {
          setInviteCodeStatus("taken");
          setFormMessage(t("organizer.inviteCodeMinimum"));
        }
        return;
      }
      const canCreateSelectedTournament = normalizedValues.visibility === "public"
        ? canCreatePublic
        : canCreatePrivate;
      if (!canCreateSelectedTournament) {
        setStatus("invalid");
        setFormMessage(normalizedValues.visibility === "public"
          ? t("organizer.publicCreationContact")
          : t("organizer.privateAllowanceExhausted"));
        return;
      }
      if (!isScheduleValid(normalizedValues)) {
        setStatus("invalid");
        setFormMessage("Проверьте порядок расписания: закрытие регистрации, подтверждение, команды, старт.");
        return;
      }
      if (!isScheduleInFuture(normalizedValues, serverNowMs)) {
        setStatus("invalid");
        setFormMessage(t("organizer.scheduleMustBeFuture"));
        return;
      }

      const inviteAvailable = await verifyInviteCode(normalizedValues.inviteCode);
      if (!inviteAvailable) {
        setStatus("invalid");
        return;
      }

      const payload = buildPayload(normalizedValues, selectedCoverTemplateUrl);
      const result = await createTournament(
        process.env.NEXT_PUBLIC_PLATFORM_ACTOR_USER_ID ?? "u_lisalexy",
        payload
      );

      if (!result) {
        setStatus("error");
        setFormMessage("Не удалось создать турнир. Проверьте данные и попробуйте еще раз.");
        return;
      }

      setCreatedTournamentSlug(result.slug);
      if (coverFile && !await uploadCreatedTournamentBanner(result.slug, coverFile)) {
        return;
      }

      router.push(`/tournaments/${result.slug}`);
    } catch (error) {
      if (error instanceof PlatformApiError && error.status === 409) {
        setStatus("invalid");
        if (error.message.toLowerCase().includes("private tournament")) {
          setFormMessage(t("organizer.privateAllowanceExhausted"));
        } else {
          setInviteCodeStatus("taken");
          setFormMessage("Код приглашения занят.");
        }
        return;
      }
      if (error instanceof PlatformApiError && error.status === 422) {
        setStatus("invalid");
        setFormMessage(t("organizer.scheduleMustBeFuture"));
        return;
      }
      setStatus("error");
      setFormMessage("Не удалось создать турнир. Проверьте данные и попробуйте еще раз.");
    } finally {
      submitInFlightRef.current = false;
    }
  }

  async function uploadCreatedTournamentBanner(slug: string, file: File): Promise<boolean> {
    setStatus("saving");
    setFormMessage(t("organizer.bannerAccepted"));
    try {
      const accepted = await uploadTournamentBanner(slug, file);
      const descriptor = await waitForOwnedMedia(accepted.asset_id, {
        onStatus: (current) => {
          setFormMessage(current.status === "processing"
            ? t("organizer.bannerProcessing")
            : current.status === "pending"
              ? t("organizer.bannerQueued")
              : current.status === "ready"
                ? t("organizer.bannerReady")
                : t("organizer.bannerFailed"));
        }
      });
      if (descriptor.status === "failed") {
        setStatus("error");
        setFormMessage(t("organizer.bannerFailedRetry"));
        return false;
      }
      return true;
    } catch (error) {
      setStatus("error");
      setFormMessage(platformApiMessage(error, t("organizer.bannerFailedRetry")));
      return false;
    }
  }

  function update(next: Partial<CreateFormValues>) {
    if (submitInFlightRef.current) {
      return;
    }
    setValues((current) => ({ ...current, ...next }));
    setStatus("idle");
    setFormMessage(null);
  }

  function updateInviteCode(value: string) {
    if (submitInFlightRef.current) {
      return;
    }
    inviteCodeTouchedRef.current = true;
    inviteRequestGenerationRef.current += 1;
    resetInviteCopyState();
    update({ inviteCode: normalizeInviteCode(value) });
    setInviteCodeStatus("idle");
  }

  function updateSchedule(next: Partial<CreateFormValues>) {
    if (submitInFlightRef.current) {
      return;
    }
    setValues((current) => normalizeSchedule({ ...current, ...next }, serverNowMs));
    setStatus("idle");
    setFormMessage(null);
  }

  function toggleRank(rankCode: string) {
    if (submitInFlightRef.current) {
      return;
    }
    setValues((current) => {
      const exists = current.allowedRankCodes.includes(rankCode);
      const allowedRankCodes = exists
        ? current.allowedRankCodes.filter((code) => code !== rankCode)
        : sortRanksByStrengthDesc([...current.allowedRankCodes, rankCode]);
      return { ...current, allowedRankCodes };
    });
    setStatus("idle");
    setFormMessage(null);
  }

  function updateCover(file: File | null): boolean {
    if (submitInFlightRef.current) {
      return false;
    }
    if (file && file.size > TOURNAMENT_COVER_UPLOAD_MAX_BYTES) {
      setStatus("invalid");
      setFormMessage(t("organizer.bannerTooLarge"));
      return false;
    }
    if (file && !TOURNAMENT_COVER_UPLOAD_TYPES.includes(file.type as typeof TOURNAMENT_COVER_UPLOAD_TYPES[number])) {
      setStatus("invalid");
      setFormMessage("Используйте обложку в формате JPG, PNG или WebP.");
      return false;
    }
    if (coverPreviewUrl?.startsWith("blob:")) {
      URL.revokeObjectURL(coverPreviewUrl);
    }
    setCoverFile(file);
    setSelectedCoverTemplateUrl(file ? "" : DEFAULT_TOURNAMENT_COVER_URL);
    setCoverPreviewUrl(file ? URL.createObjectURL(file) : DEFAULT_TOURNAMENT_COVER_URL);
    setStatus("idle");
    setFormMessage(null);
    return true;
  }

  function selectCoverTemplate(url: string) {
    if (submitInFlightRef.current) {
      return;
    }
    if (coverPreviewUrl?.startsWith("blob:")) {
      URL.revokeObjectURL(coverPreviewUrl);
    }
    setCoverFile(null);
    setSelectedCoverTemplateUrl(url);
    setCoverPreviewUrl(url);
    setStatus("idle");
    setFormMessage(null);
  }

  function stepMaxParticipants(delta: number) {
    const currentValue = values.maxParticipants === String(UNLIMITED_PARTICIPANTS)
      ? 0
      : Number(values.maxParticipants || 0);
    update({ maxParticipants: String(Math.min(MAX_MANUAL_PARTICIPANTS, Math.max(1, currentValue + delta))) });
  }

  function updateMaxParticipants(value: string) {
    const parsedValue = Number(value);
    update({
      maxParticipants: value && Number.isFinite(parsedValue)
        ? String(Math.min(MAX_MANUAL_PARTICIPANTS, Math.max(1, parsedValue)))
        : String(UNLIMITED_PARTICIPANTS)
    });
  }

  function updateMaxTeams(value: string) {
    const digits = value.replace(/\D/g, "").slice(0, 4);
    update({ maxTeams: digits ? String(Math.min(8192, Number(digits))) : "" });
  }

  function normalizeMaxTeams() {
    update({ maxTeams: String(roundTeamCountUp(Number(values.maxTeams || 2))) });
  }

  function stepMaxTeams(direction: -1 | 1) {
    if (!values.maxTeams || values.maxTeams === "8192") {
      update({ maxTeams: String(TEAM_COUNT_CHOICES[0]) });
      return;
    }
    const normalized = roundTeamCountUp(Number(values.maxTeams || 2));
    const index = TEAM_COUNT_CHOICES.indexOf(normalized as typeof TEAM_COUNT_CHOICES[number]);
    const nextIndex = Math.min(TEAM_COUNT_CHOICES.length - 1, Math.max(0, index + direction));
    update({ maxTeams: String(TEAM_COUNT_CHOICES[nextIndex]) });
  }

  async function refreshInviteCode() {
    if (submitInFlightRef.current) {
      return;
    }
    inviteCodeTouchedRef.current = true;
    resetInviteCopyState();
    const requestGeneration = ++inviteRequestGenerationRef.current;
    setInviteCodeStatus("checking");
    const suggestion = await generateInviteCode();
    if (requestGeneration !== inviteRequestGenerationRef.current || submitInFlightRef.current) {
      return;
    }
    setValues((current) => ({ ...current, inviteCode: suggestion.code }));
    setInviteCodeStatus(suggestion.verified ? "available" : "unknown");
  }

  async function copyInviteCode() {
    if (inviteCopied || !values.inviteCode) {
      return;
    }
    if (!await copyTextToClipboard(values.inviteCode)) {
      return;
    }
    if (inviteCopyTimerRef.current) {
      clearTimeout(inviteCopyTimerRef.current);
    }
    setInviteCopied(true);
    inviteCopyTimerRef.current = setTimeout(() => {
      setInviteCopied(false);
      inviteCopyTimerRef.current = null;
    }, COPY_CONFIRMATION_MS);
  }

  function resetInviteCopyState() {
    if (inviteCopyTimerRef.current) {
      clearTimeout(inviteCopyTimerRef.current);
      inviteCopyTimerRef.current = null;
    }
    setInviteCopied(false);
  }

  async function verifyInviteCode(code: string): Promise<boolean> {
    const normalizedCode = normalizeInviteCode(code);
    const requestGeneration = ++inviteRequestGenerationRef.current;
    if (normalizedCode.length < 10) {
      if (requestGeneration === inviteRequestGenerationRef.current) {
        setInviteCodeStatus("taken");
        setFormMessage(t("organizer.inviteCodeMinimum"));
      }
      return false;
    }
    setInviteCodeStatus("checking");
    try {
      const result = await checkTournamentInviteCode(normalizedCode);
      if (requestGeneration !== inviteRequestGenerationRef.current) {
        return result.available;
      }
      setValues((current) => ({ ...current, inviteCode: result.code || normalizedCode }));
      if (!result.available) {
        setInviteCodeStatus("taken");
        setFormMessage("Код приглашения занят.");
        return false;
      }
      setInviteCodeStatus("available");
      return true;
    } catch {
      if (requestGeneration === inviteRequestGenerationRef.current) {
        setInviteCodeStatus("unknown");
      }
      return true;
    }
  }

  const preview = buildPreview(values, coverPreviewUrl, organizerAvatarUrl, organizerAvatarMedia);
  const unlimitedParticipants = values.maxParticipants === String(UNLIMITED_PARTICIPANTS);
  const inviteCodeInvalid = inviteCodeStatus === "taken";
  const scheduleValid = isScheduleValid(values) && isScheduleInFuture(values, serverNowMs);
  const scheduleMinimums = getScheduleMinimums(values, serverNowMs);
  const checklistItems = [
    {
      label: "Название и описание",
      done: values.title.trim().length >= 3 && values.description.trim().length > 0
    },
    {
      label: "Обложка турнира",
      done: Boolean(coverFile || selectedCoverTemplateUrl)
    },
    {
      label: "Видимость и код приглашения",
      done: values.inviteCode.length >= 10 && !inviteCodeInvalid
    },
    {
      label: "Расписание",
      done: scheduleValid
    },
    {
      label: "Допустимые ранги",
      done: values.allowedRankCodes.length > 0
    },
    {
      label: "Форматы матчей",
      done: Boolean(values.matchFormat && values.finalFormat)
    }
  ];

  return (
    <div className="create-layout" data-server-now={serverNowIso}>
      <form className="form-stack" id="create-tournament-form" aria-label="Форма создания турнира" aria-busy={status === "saving"} onSubmit={handleCreate}>
        <fieldset className="create-form-fields" disabled={status === "saving"}>
          <article className="panel panel-pad create-main-panel">
            <h2 className="panel-title"><span>A.</span> Основное</h2>
            <div className="field-grid grid-2 create-main-grid">
              <label className="field create-title-field">
                <span className="label">Название турнира <span className="counter">{values.title.length}/{MAX_TITLE_LENGTH}</span></span>
                <input
                  className="input"
                  maxLength={MAX_TITLE_LENGTH}
                  placeholder={TITLE_PLACEHOLDER}
                  value={values.title}
                  onChange={(event) => update({ title: normalizeTournamentTitle(event.target.value) })}
                />
                <div className="account-hint create-empty-hint" aria-hidden="true">&nbsp;</div>
              </label>
              <label className="field create-organizer-field">
                <span className="label">Организатор</span>
                <input
                  className="input"
                  value={values.organizerName}
                  onChange={(event) => update({ organizerName: event.target.value })}
                  readOnly
                />
                <div className="account-hint">
                  Взято из имени аккаунта
                </div>
              </label>
              <label className="field create-description-field">
                <span className="label">Краткое описание <span className="counter">{values.description.length}/{MAX_DESCRIPTION_LENGTH} · {countDescriptionLines(values.description)}/{MAX_DESCRIPTION_LINES}</span></span>
                <textarea
                  className="textarea create-description-textarea"
                  maxLength={MAX_DESCRIPTION_LENGTH}
                  placeholder={DESCRIPTION_PLACEHOLDER}
                  value={values.description}
                  onChange={(event) => update({ description: normalizeTournamentDescription(event.target.value) })}
                />
              </label>
            </div>
          </article>

          <article className="panel panel-pad">
            <h2 className="panel-title"><span>B.</span> Обложка турнира</h2>
            <div className="cover-upload-frame">
              <div className="cover-box">
                {coverPreviewUrl ? (
                  <CspImage
                    alt=""
                    className="cover-preview-image"
                    fill
                    loading="eager"
                    src={tournamentCoverAssetUrl(coverPreviewUrl)}
                  />
                ) : null}
              </div>
              <div className="cover-upload-footer">
                <div className="hint cover-upload-hint">
                  <span>{TOURNAMENT_COVER_UPLOAD_HINT}</span>
                </div>
                <div className="cover-choice-actions">
                  <label className="upload-button">
                    <Upload size={18} aria-hidden="true" />
                    Загрузить обложку
                    <input
                      accept="image/jpeg,image/png,image/webp"
                      aria-label="Загрузить обложку турнира"
                      onChange={(event) => {
                        if (!updateCover(event.target.files?.[0] ?? null)) {
                          event.currentTarget.value = "";
                        }
                      }}
                      className="cover-file-input"
                      type="file"
                    />
                  </label>
                  {TOURNAMENT_COVER_TEMPLATES.map((template) => (
                    <button
                      className={selectedCoverTemplateUrl === template.url ? "cover-template-button active" : "cover-template-button"}
                      key={template.url}
                      type="button"
                      onClick={() => selectCoverTemplate(template.url)}
                    >
                      {template.label}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          </article>

          <article className="panel panel-pad create-participation-panel">
            <h2 className="panel-title panel-title-with-info">
              <span className="visibility-title-text"><span>C.</span> Видимость и участие</span>
              <span className="visibility-info-wrap">
                <button className="visibility-info-button" type="button" aria-label="Информация о доступе к турнирам">i</button>
                <span className="visibility-tooltip" role="tooltip">
                  <span>{t("organizer.privateMonthlyAllowance", { remaining: privateMonthlyRemaining, limit: privateMonthlyLimit })}</span>
                  {(currentUser.private_tournament_credits ?? 0) > 0 ? (
                    <span>{t("organizer.additionalPrivateCredits", { count: currentUser.private_tournament_credits ?? 0 })}</span>
                  ) : null}
                  <span>
                    {canCreatePublic
                      ? t("organizer.publicCreationAllowed")
                      : t("organizer.publicCreationContact")}
                  </span>
                </span>
              </span>
            </h2>
            <div className="field-grid grid-4 participation-grid">
              <div className="field">
                <span className="label">Видимость турнира</span>
                <div className="segmented visibility-segmented">
                  <button
                    className={`segment visibility-segment${values.visibility === "public" ? " active" : ""}${canCreatePublic ? "" : " locked"}`}
                    type="button"
                    disabled={!canCreatePublic}
                    onClick={() => update({ visibility: "public" })}
                    aria-label={canCreatePublic ? "Публичный турнир" : "Публичный турнир заблокирован"}
                  >
                    {canCreatePublic ? null : <Lock size={14} aria-hidden="true" />}
                    Публичный
                  </button>
                  <button
                    aria-label={canCreatePrivate ? "Приватный" : "Приватный турнир недоступен"}
                    className={`segment visibility-segment${values.visibility === "private" ? " active" : ""}${canCreatePrivate ? "" : " locked"}`}
                    disabled={!canCreatePrivate}
                    type="button"
                    onClick={() => update({ visibility: "private" })}
                  >
                    Приватный
                  </button>
                </div>
              </div>
              <label className={inviteCodeInvalid ? "field invite-code-field invalid" : "field invite-code-field"}>
                <span className="label">Код приглашения</span>
                <span className="inline-group">
                  <input
                    className="input"
                    maxLength={24}
                    value={values.inviteCode}
                    onChange={(event) => updateInviteCode(event.target.value)}
                    onBlur={() => void verifyInviteCode(values.inviteCode)}
                  />
                  <span className="invite-code-actions">
                    <button className="icon-button" aria-label="Сгенерировать" type="button" onClick={() => void refreshInviteCode()}><RefreshCcw size={18} /></button>
                    <button
                      className={inviteCopied ? "icon-button copied" : "icon-button"}
                      aria-label={inviteCopied ? "Скопировано" : "Скопировать"}
                      disabled={inviteCopied || !values.inviteCode}
                      type="button"
                      onClick={() => void copyInviteCode()}
                    >
                      {inviteCopied ? <Check aria-hidden="true" size={17} /> : <Copy aria-hidden="true" size={17} />}
                    </button>
                  </span>
                </span>
              </label>
              <label className="field team-count-field">
                <span className="label">Макс. команд</span>
                <span className="number-row">
                  <span className="number-control team-count-control">
                    <input
                      aria-label="Макс. команд"
                      className="input"
                      inputMode="numeric"
                      max={8192}
                      min={2}
                      type="number"
                      value={values.maxTeams === "8192" ? "" : values.maxTeams}
                      onBlur={normalizeMaxTeams}
                      onChange={(event) => updateMaxTeams(event.target.value)}
                    />
                    <span className="arrows" aria-label="Управление лимитом команд">
                      <button type="button" onClick={() => stepMaxTeams(1)} aria-label="Увеличить количество команд"><ChevronUp aria-hidden="true" size={14} /></button>
                      <button type="button" onClick={() => stepMaxTeams(-1)} aria-label="Уменьшить количество команд"><ChevronDown aria-hidden="true" size={14} /></button>
                    </span>
                  </span>
                  <button
                    aria-label="Максимум команд"
                    className={values.maxTeams === "8192" ? "limit-button infinity-limit-button active" : "limit-button infinity-limit-button"}
                    title="Максимум команд"
                    type="button"
                    onClick={() => update({ maxTeams: "8192" })}
                  >
                    <InfinityIcon aria-hidden="true" className="infinity-symbol" size={28} strokeWidth={2.8} />
                  </button>
                </span>
              </label>
              <label className="field">
                <span className="label">Макс. регистраций</span>
                <span className="number-row">
                  <span className="number-control">
                    <input
                      className="input"
                      max={MAX_MANUAL_PARTICIPANTS}
                      min={1}
                      type="number"
                      value={unlimitedParticipants ? "" : values.maxParticipants}
                      onChange={(event) => updateMaxParticipants(event.target.value)}
                    />
                    <span className="arrows" aria-label="Управление лимитом участников">
                      <button type="button" onClick={() => stepMaxParticipants(1)} aria-label="Увеличить лимит"><ChevronUp aria-hidden="true" size={14} /></button>
                      <button type="button" onClick={() => stepMaxParticipants(-1)} aria-label="Уменьшить лимит"><ChevronDown aria-hidden="true" size={14} /></button>
                    </span>
                  </span>
                  <button
                    aria-label="Без ограничений"
                    className={unlimitedParticipants ? "limit-button infinity-limit-button active" : "limit-button infinity-limit-button"}
                    title="Без ограничений"
                    type="button"
                    onClick={() => update({ maxParticipants: String(UNLIMITED_PARTICIPANTS) })}
                  ><InfinityIcon aria-hidden="true" className="infinity-symbol" size={28} strokeWidth={2.8} /></button>
                </span>
              </label>
            </div>
          </article>

          <article className="panel panel-pad">
            <h2 className="panel-title"><span>D.</span> Расписание</h2>
            <div className="field-grid schedule-grid">
              <ScheduleField label="1. Закрытие регистрации" dateValue={values.registrationClosesDate} timeValue={values.registrationClosesAt} minDate={scheduleMinimums.registration.date} minTime={scheduleMinimums.registration.time} onDateChange={(registrationClosesDate) => updateSchedule({ registrationClosesDate })} onTimeChange={(registrationClosesAt) => updateSchedule({ registrationClosesAt })} />
              <ScheduleField label="2. Подтверждение участия" dateValue={values.checkInStartsDate} timeValue={values.checkInStartsAt} minDate={scheduleMinimums.checkIn.date} minTime={scheduleMinimums.checkIn.time} onDateChange={(checkInStartsDate) => updateSchedule({ checkInStartsDate })} onTimeChange={(checkInStartsAt) => updateSchedule({ checkInStartsAt })} />
              <ScheduleField label="3. Формирование команд" dateValue={values.teamsFormDate} timeValue={values.teamsFormAt} minDate={scheduleMinimums.teams.date} minTime={scheduleMinimums.teams.time} onDateChange={(teamsFormDate) => updateSchedule({ teamsFormDate })} onTimeChange={(teamsFormAt) => updateSchedule({ teamsFormAt })} />
              <ScheduleField label="4. Начало турнира" dateValue={values.startsDate} timeValue={values.startsAt} minDate={scheduleMinimums.start.date} minTime={scheduleMinimums.start.time} onDateChange={(startsDate) => updateSchedule({ startsDate })} onTimeChange={(startsAt) => updateSchedule({ startsAt })} />
            </div>
          </article>

          <article className="panel panel-pad">
            <h2 className="panel-title"><span>E.</span> Допустимые ранги</h2>
            <div className="rank-select-layout">
              <div className="rank-grid">
                {ranks.map((rank) => (
                  <button
                    className={values.allowedRankCodes.includes(rank.code) ? "rank-pill active" : "rank-pill"}
                    type="button"
                    key={rank.code}
                    onClick={() => toggleRank(rank.code)}
                  >
                    <CspImage
                      alt=""
                      className="rank-icon"
                      height={40}
                      onError={(event) => {
                        event.currentTarget.onerror = null;
                        event.currentTarget.src = deadlockRankPlaceholderPath;
                      }}
                      src={deadlockRankIconPath(rank.code)}
                      width={40}
                    />
                    <span>{rank.label}</span>
                  </button>
                ))}
              </div>
              <div className="rank-actions">
                <button className={values.allowedRankCodes.length === ranks.length ? "secondary-button active" : "secondary-button"} type="button" onClick={() => update({ allowedRankCodes: ranks.map((rank) => rank.code) })}>Все ранги</button>
                <button className="secondary-button" type="button" onClick={() => update({ allowedRankCodes: [] })}>Очистить выбор</button>
              </div>
            </div>
          </article>

          <article className="panel panel-pad">
            <h2 className="panel-title"><span>F.</span> Форматы</h2>
            <div className="field-grid grid-2">
              <label className="field">
                <span className="label">Формат матчей</span>
                <span className="filter-control select-control create-select-control">
                  <select className="filter-select" value={values.matchFormat} onChange={(event) => update({ matchFormat: event.target.value as CreateFormValues["matchFormat"] })}>
                    <option value="bo1">BO1 (до 1 победы)</option>
                    <option value="bo3">BO3 (до 2 побед)</option>
                    <option value="bo5">BO5 (до 3 побед)</option>
                  </select>
                  <ChevronDown size={17} aria-hidden="true" />
                </span>
              </label>
              <label className="field">
                <span className="label">Формат финала</span>
                <span className="filter-control select-control create-select-control">
                  <select className="filter-select" value={values.finalFormat} onChange={(event) => update({ finalFormat: event.target.value as CreateFormValues["finalFormat"] })}>
                    <option value="bo1">BO1 (до 1 победы)</option>
                    <option value="bo3">BO3 (до 2 побед)</option>
                    <option value="bo5">BO5 (до 3 побед)</option>
                  </select>
                  <ChevronDown size={17} aria-hidden="true" />
                </span>
              </label>
            </div>
          </article>
        </fieldset>
        <style jsx>{`
          .create-form-fields {
            border: 0;
            margin: 0;
            min-width: 0;
            padding: 0;
            display: contents;
          }
        `}</style>
      </form>

      <aside className="side-stack" aria-label="Предпросмотр и подсказки">
        <article className="panel panel-pad organizer-checklist">
          <h2 className="panel-title">Чек-лист организатора</h2>
          <div className="checklist-body">
            {checklistItems.map((item) => (
              <div className={item.done ? "check-item done" : "check-item pending"} key={item.label}>
                {item.done ? <CheckCircle size={18} aria-hidden="true" /> : <Circle size={18} aria-hidden="true" />}
                <div className="check-title">{item.label}</div>
              </div>
            ))}
          </div>
        </article>
        <article className="panel panel-pad create-preview-panel">
          <h2 className="panel-title">Предпросмотр карточки</h2>
          <TournamentCard tournament={preview} />
        </article>
      </aside>

      <article className="panel info-strip create-submit-panel">
        <div className="info-note">
          <Info size={20} aria-hidden="true" />
          {formMessage
            ?? (status === "error"
              ? "Не удалось создать турнир. Проверьте данные и попробуйте еще раз."
              : status === "invalid"
                ? "Проверьте название, расписание и допустимые ранги."
                : "Убедитесь, что параметры введены верно!")}
        </div>
        <button
          className="primary-button"
          disabled={status === "saving" || (values.visibility === "public" ? !canCreatePublic : !canCreatePrivate)}
          form="create-tournament-form"
          type={createdTournamentSlug ? "button" : "submit"}
          onClick={createdTournamentSlug && coverFile ? () => {
            void uploadCreatedTournamentBanner(createdTournamentSlug, coverFile).then((uploaded) => {
              if (uploaded) {
                router.push(`/tournaments/${createdTournamentSlug}`);
              }
            });
          } : undefined}
        >
          {status === "saving"
            ? t("organizer.bannerWorking")
            : createdTournamentSlug ? t("organizer.bannerRetry") : "Создать турнир"}
        </button>
      </article>
    </div>
  );
}

function ScheduleField({
  label,
  dateValue,
  timeValue,
  minDate,
  minTime,
  onDateChange,
  onTimeChange
}: {
  label: string;
  dateValue: string;
  timeValue: string;
  minDate?: string;
  minTime?: string;
  onDateChange: (value: string) => void;
  onTimeChange: (value: string) => void;
}) {
  return (
    <fieldset className="field schedule-field">
      <legend className="label">{label}</legend>
      <span className="date-time-row">
        <SchedulePicker
          icon="date"
          label="Дата"
          min={minDate}
          type="date"
          value={dateValue}
          onChange={onDateChange}
        />
        <SchedulePicker
          icon="time"
          label="Время"
          min={dateValue === minDate ? minTime : undefined}
          type="time"
          value={timeValue}
          onChange={onTimeChange}
        />
        <span className="tz-button" aria-label="Часовой пояс">МСК</span>
      </span>
    </fieldset>
  );
}

function SchedulePicker({
  icon,
  label,
  min,
  type,
  value,
  onChange
}: {
  icon: "date" | "time";
  label: string;
  min?: string;
  type: "date" | "time";
  value: string;
  onChange: (value: string) => void;
}) {
  const pickerRef = useRef<HTMLDivElement | null>(null);
  const selectedTimeRef = useRef<HTMLButtonElement | null>(null);
  const [open, setOpen] = useState(false);
  const [visibleMonth, setVisibleMonth] = useState(() => (
    monthStart(value || min || dateStringFromUtc(new Date()))
  ));

  useEffect(() => {
    if (type === "date" && value) {
      setVisibleMonth(monthStart(value));
    }
  }, [type, value]);

  useEffect(() => {
    if (!open) {
      return;
    }
    function closeFromOutside(event: PointerEvent) {
      if (!pickerRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    }
    function closeFromKeyboard(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setOpen(false);
      }
    }
    document.addEventListener("pointerdown", closeFromOutside);
    document.addEventListener("keydown", closeFromKeyboard);
    return () => {
      document.removeEventListener("pointerdown", closeFromOutside);
      document.removeEventListener("keydown", closeFromKeyboard);
    };
  }, [open]);

  useEffect(() => {
    if (open && type === "time") {
      selectedTimeRef.current?.scrollIntoView({ block: "nearest" });
    }
  }, [open, type]);

  const Icon = icon === "date" ? Calendar : Clock;
  const displayValue = value ? (type === "date" ? formatDisplayDate(value) : value) : "Выберите";
  const timeOptions = type === "time" ? buildTimeOptions(min) : [];
  const previousMonthDisabled = Boolean(min && monthStart(visibleMonth) <= monthStart(min));

  return (
    <div className="schedule-input" ref={pickerRef}>
      <span>{label}</span>
      <button
        aria-expanded={open}
        aria-haspopup={type === "date" ? "dialog" : "listbox"}
        aria-label={label}
        className={open ? "schedule-picker active" : "schedule-picker"}
        data-picker-min={min ?? ""}
        data-picker-type={type}
        data-picker-value={value}
        type="button"
        onClick={() => setOpen((current) => !current)}
      >
        <span className="schedule-display">{displayValue}</span>
        <Icon size={17} aria-hidden="true" />
      </button>
      {open && type === "date" ? (
        <div aria-label="Выбор даты" className="schedule-popover schedule-calendar" role="dialog">
          <div className="schedule-calendar-head">
            <button
              aria-label="Предыдущий месяц"
              disabled={previousMonthDisabled}
              type="button"
              onClick={() => setVisibleMonth(shiftMonth(visibleMonth, -1))}
            >
              ‹
            </button>
            <strong>{formatMonthLabel(visibleMonth)}</strong>
            <button
              aria-label="Следующий месяц"
              type="button"
              onClick={() => setVisibleMonth(shiftMonth(visibleMonth, 1))}
            >
              ›
            </button>
          </div>
          <div className="schedule-calendar-grid schedule-calendar-weekdays" aria-hidden="true">
            {WEEKDAY_LABELS.map((weekday) => <span key={weekday}>{weekday}</span>)}
          </div>
          <div className="schedule-calendar-grid">
            {buildCalendarCells(visibleMonth).map((dateValue, index) => dateValue ? (
              <button
                aria-label={formatDisplayDate(dateValue)}
                className={dateValue === value ? "selected" : ""}
                disabled={Boolean(min && dateValue < min)}
                key={dateValue}
                type="button"
                onClick={() => {
                  onChange(dateValue);
                  setOpen(false);
                }}
              >
                {Number(dateValue.slice(-2))}
              </button>
            ) : <span aria-hidden="true" key={`empty-${index}`} />)}
          </div>
        </div>
      ) : null}
      {open && type === "time" ? (
        <div aria-label="Выбор времени" className="schedule-popover schedule-time-options" role="listbox">
          {timeOptions.map((timeValue) => (
            <button
              aria-selected={timeValue === value}
              className={timeValue === value ? "selected" : ""}
              key={timeValue}
              ref={timeValue === value ? selectedTimeRef : undefined}
              role="option"
              type="button"
              onClick={() => {
                onChange(timeValue);
                setOpen(false);
              }}
            >
              {timeValue}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

const WEEKDAY_LABELS = ("Пн Вт Ср Чт Пт Вс").split(" ");

function buildTimeOptions(minTime?: string): string[] {
  const minimumMinutes = minTime ? roundTimeUpToTen(timeToMinutes(minTime)) : 0;
  const options: string[] = [];
  for (let minutes = minimumMinutes; minutes < 24 * 60; minutes += 10) {
    options.push(`${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`);
  }
  return options;
}

function timeToMinutes(value: string): number {
  const [hours = "0", minutes = "0"] = value.split(":");
  return Math.min(24 * 60 - 1, Math.max(0, Number(hours) * 60 + Number(minutes)));
}

function roundTimeUpToTen(minutes: number): number {
  return Math.ceil(minutes / 10) * 10;
}

function buildCalendarCells(monthValue: string): Array<string | null> {
  const [year, month] = monthValue.split("-").map(Number);
  const firstDay = new Date(Date.UTC(year, month - 1, 1));
  const leadingEmptyCells = (firstDay.getUTCDay() + 6) % 7;
  const daysInMonth = new Date(Date.UTC(year, month, 0)).getUTCDate();
  return [
    ...Array.from({ length: leadingEmptyCells }, () => null),
    ...Array.from({ length: daysInMonth }, (_, index) => (
      dateStringFromUtc(new Date(Date.UTC(year, month - 1, index + 1)))
    )),
  ];
}

function monthStart(dateValue: string): string {
  return `${dateValue.slice(0, 7)}-01`;
}

function shiftMonth(monthValue: string, delta: number): string {
  const [year, month] = monthValue.split("-").map(Number);
  return dateStringFromUtc(new Date(Date.UTC(year, month - 1 + delta, 1)));
}

function dateStringFromUtc(date: Date): string {
  return date.toISOString().slice(0, 10);
}

function formatMonthLabel(monthValue: string): string {
  const [year, month] = monthValue.split("-").map(Number);
  return new Intl.DateTimeFormat("ru-RU", {
    month: "long",
    timeZone: "UTC",
    year: "numeric",
  }).format(new Date(Date.UTC(year, month - 1, 1)));
}

function buildPayload(values: CreateFormValues, coverUrl: string | null): TournamentCreatePayload {
  const startsAt = toMoscowIso(values.startsDate, values.startsAt);

  return {
    title: values.title.trim(),
    description: values.description.trim(),
    cover_url: coverUrl,
    visibility: values.visibility,
    invite_code: normalizeInviteCode(values.inviteCode) || null,
    status: "registration_open",
    bracket_type: "single_elimination",
    match_format: values.matchFormat,
    final_format: values.finalFormat,
    participant_mode: "solo",
    allowed_rank_codes: values.allowedRankCodes,
    starts_at: startsAt,
    max_participants: values.maxParticipants ? Number(values.maxParticipants) : null,
    teams_count: roundTeamCountUp(Number(values.maxTeams || 2)),
    schedule: {
      registration_starts_at: null,
      registration_closes_at: toMoscowIso(values.registrationClosesDate, values.registrationClosesAt),
      check_in_starts_at: toMoscowIso(values.checkInStartsDate, values.checkInStartsAt),
      teams_form_at: toMoscowIso(values.teamsFormDate, values.teamsFormAt),
      starts_at: startsAt,
      timezone: "Europe/Moscow"
    }
  };
}

function buildDefaultValues(serverNowMs: number): CreateFormValues {
  const defaultDate = moscowPartsFromMs(serverNowMs + 24 * 60 * 60 * 1000).date;
  return {
    title: "",
    organizerName: "Организатор",
    description: "",
    visibility: "private",
    inviteCode: "",
    registrationClosesDate: defaultDate,
    registrationClosesAt: "18:00",
    checkInStartsDate: defaultDate,
    checkInStartsAt: "18:30",
    teamsFormDate: defaultDate,
    teamsFormAt: "19:00",
    startsDate: defaultDate,
    startsAt: "20:30",
    maxParticipants: String(UNLIMITED_PARTICIPANTS),
    maxTeams: "8192",
    allowedRankCodes: ranks.map((rank) => rank.code),
    matchFormat: "bo1",
    finalFormat: "bo3"
  };
}

function buildPreview(
  values: CreateFormValues,
  coverUrl: string | null,
  organizerAvatarUrl: string | null,
  organizerAvatarMedia: PlatformMediaDescriptor | null
): TournamentSummary {
  return {
    id: "preview",
    slug: "preview",
    title: values.title || "Название турнира",
    organizerUserId: null,
    organizerName: values.organizerName || "Организатор",
    organizerAvatarUrl,
    organizerAvatarMedia,
    coverUrl,
    coverMedia: null,
    startsAtIso: toMoscowIso(values.startsDate, values.startsAt),
    registrationClosesAtIso: toMoscowIso(values.registrationClosesDate, values.registrationClosesAt),
    startsAtLabel: `${formatDisplayDate(values.startsDate)}, ${values.startsAt} МСК`,
    registrationTimerLabel: "Рег. открыта",
    startTimerLabel: "Старт через",
    status: "registration_open",
    statusLabel: "Регистрация открыта",
    visibility: values.visibility,
    bracketType: "single_elimination",
    theme: "theme-teal",
    allowedRanks: sortRanksByStrengthDesc(values.allowedRankCodes),
    participantCount: 0,
    maxParticipants: values.maxParticipants ? Number(values.maxParticipants) : null,
    teamsCount: roundTeamCountUp(Number(values.maxTeams || 2))
  };
}

function roundTeamCountUp(value: number): typeof TEAM_COUNT_CHOICES[number] {
  const normalized = Number.isFinite(value) ? Math.max(2, Math.min(8192, Math.ceil(value))) : 2;
  return TEAM_COUNT_CHOICES.find((count) => count >= normalized) ?? TEAM_COUNT_CHOICES.at(-1)!;
}

function canUserCreatePublic(user: PlatformUser): boolean {
  return Boolean(
    user.can_create_public_tournaments
    || (user.public_tournament_credits ?? 0) > 0
    || user.roles.includes("admin")
    || user.roles.includes("superadmin")
  );
}

async function generateInviteCode(): Promise<{ code: string; verified: boolean }> {
  try {
    const result = await suggestTournamentInviteCode();
    return { code: result.code || fallbackInviteCode(), verified: Boolean(result.available) };
  } catch {
    return { code: fallbackInviteCode(), verified: false };
  }
}

function fallbackInviteCode(): string {
  const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  const bytes = new Uint32Array(10);
  if (typeof crypto !== "undefined" && crypto.getRandomValues) {
    crypto.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * alphabet.length);
    }
  }
  return Array.from(bytes, (value) => alphabet[value % alphabet.length]).join("");
}

function normalizeInviteCode(value: string): string {
  return value.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 24);
}

function normalizeTournamentTitle(value: string): string {
  return value.replace(INVALID_TITLE_CHARACTERS, "").replace(/^\s+/, "").slice(0, MAX_TITLE_LENGTH);
}

function normalizeTournamentDescription(value: string): string {
  const normalized = value.replace(/\r\n?/g, "\n").slice(0, MAX_DESCRIPTION_LENGTH);
  const lines = normalized
    .split("\n")
    .flatMap((line) => line.length > 0 ? chunkLine(line, MAX_DESCRIPTION_LINE_LENGTH) : [""])
    .slice(0, MAX_DESCRIPTION_LINES);
  return lines.join("\n").slice(0, MAX_DESCRIPTION_LENGTH);
}

function countDescriptionLines(value: string): number {
  return value.length === 0 ? 0 : value.split("\n").length;
}

function chunkLine(value: string, maxLength: number): string[] {
  const chunks: string[] = [];
  for (let index = 0; index < value.length; index += maxLength) {
    chunks.push(value.slice(index, index + maxLength));
  }
  return chunks;
}

function isScheduleValid(values: CreateFormValues): boolean {
  const registrationClose = parseMoscowMs(values.registrationClosesDate, values.registrationClosesAt);
  const checkInStart = parseMoscowMs(values.checkInStartsDate, values.checkInStartsAt);
  const teamsForm = parseMoscowMs(values.teamsFormDate, values.teamsFormAt);
  const startsAt = parseMoscowMs(values.startsDate, values.startsAt);
  if ([registrationClose, checkInStart, teamsForm, startsAt].some((value) => value === null)) {
    return false;
  }
  return (
    checkInStart! >= registrationClose!
    && teamsForm! - checkInStart! >= 10 * 60 * 1000
    && teamsForm! <= startsAt!
  );
}

function isScheduleInFuture(values: CreateFormValues, serverNowMs: number): boolean {
  return [
    parseMoscowMs(values.registrationClosesDate, values.registrationClosesAt),
    parseMoscowMs(values.checkInStartsDate, values.checkInStartsAt),
    parseMoscowMs(values.teamsFormDate, values.teamsFormAt),
    parseMoscowMs(values.startsDate, values.startsAt)
  ].every((value) => value !== null && value > serverNowMs);
}

function normalizeSchedule(values: CreateFormValues, serverNowMs: number): CreateFormValues {
  const next = { ...values };
  let registrationClose = parseMoscowMs(next.registrationClosesDate, next.registrationClosesAt);
  if (registrationClose === null || registrationClose <= serverNowMs) {
    const parts = moscowPartsFromMs(nextTenMinuteBoundary(serverNowMs));
    next.registrationClosesDate = parts.date;
    next.registrationClosesAt = parts.time;
    registrationClose = parseMoscowMs(next.registrationClosesDate, next.registrationClosesAt);
  }
  const checkInStart = parseMoscowMs(next.checkInStartsDate, next.checkInStartsAt);
  if (registrationClose !== null && (checkInStart === null || checkInStart < registrationClose)) {
    const parts = moscowPartsFromMs(registrationClose);
    next.checkInStartsDate = parts.date;
    next.checkInStartsAt = parts.time;
  }

  const normalizedCheckInStart = parseMoscowMs(next.checkInStartsDate, next.checkInStartsAt);
  const teamsForm = parseMoscowMs(next.teamsFormDate, next.teamsFormAt);
  if (normalizedCheckInStart !== null) {
    const minTeamsForm = normalizedCheckInStart + 10 * 60 * 1000;
    if (teamsForm === null || teamsForm < minTeamsForm) {
      const parts = moscowPartsFromMs(minTeamsForm);
      next.teamsFormDate = parts.date;
      next.teamsFormAt = parts.time;
    }
  }

  const normalizedTeamsForm = parseMoscowMs(next.teamsFormDate, next.teamsFormAt);
  const startsAt = parseMoscowMs(next.startsDate, next.startsAt);
  if (normalizedTeamsForm !== null && (startsAt === null || startsAt < normalizedTeamsForm)) {
    const parts = moscowPartsFromMs(normalizedTeamsForm);
    next.startsDate = parts.date;
    next.startsAt = parts.time;
  }

  return next;
}

function getScheduleMinimums(values: CreateFormValues, serverNowMs: number): {
  registration: { date?: string; time?: string };
  checkIn: { date?: string; time?: string };
  teams: { date?: string; time?: string };
  start: { date?: string; time?: string };
} {
  const registrationClose = parseMoscowMs(values.registrationClosesDate, values.registrationClosesAt);
  const checkInStart = parseMoscowMs(values.checkInStartsDate, values.checkInStartsAt);
  const teamsForm = parseMoscowMs(values.teamsFormDate, values.teamsFormAt);

  return {
    registration: moscowPartsFromMs(nextTenMinuteBoundary(serverNowMs)),
    checkIn: registrationClose === null ? {} : moscowPartsFromMs(registrationClose),
    teams: checkInStart === null ? {} : moscowPartsFromMs(checkInStart + 10 * 60 * 1000),
    start: teamsForm === null ? {} : moscowPartsFromMs(teamsForm)
  };
}

function nextTenMinuteBoundary(timestampMs: number): number {
  const intervalMs = 10 * 60 * 1000;
  return Math.floor(timestampMs / intervalMs) * intervalMs + intervalMs;
}

function moscowPartsFromMs(timestampMs: number): { date: string; time: string } {
  const localIso = new Date(timestampMs + 3 * 60 * 60 * 1000).toISOString();
  return {
    date: localIso.slice(0, 10),
    time: localIso.slice(11, 16)
  };
}

function parseMoscowMs(dateValue: string, timeValue: string): number | null {
  if (!dateValue || !timeValue) {
    return null;
  }
  const timestamp = new Date(toMoscowIso(dateValue, timeValue)).getTime();
  return Number.isFinite(timestamp) ? timestamp : null;
}

function toMoscowIso(dateValue: string, timeValue: string): string {
  const [year = "2026", month = "06", day = "07"] = dateValue.split("-");
  const [hours = "00", minutes = "00"] = timeValue.split(":");
  return `${year.padStart(4, "20")}-${month.padStart(2, "0")}-${day.padStart(2, "0")}T${hours.padStart(2, "0")}:${minutes.padStart(2, "0")}:00+03:00`;
}

function formatDisplayDate(dateValue: string): string {
  const [year = "2026", month = "06", day = "07"] = dateValue.split("-");
  return `${day.padStart(2, "0")}.${month.padStart(2, "0")}.${year.padStart(4, "20")}`;
}
