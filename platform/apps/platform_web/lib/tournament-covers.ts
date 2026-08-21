export const TOURNAMENT_COVER_TEMPLATES = [
  { label: "Шаблон 1", url: "/assets/tournament-covers/tournament-cover-template-1-v1.webp" },
  { label: "Шаблон 2", url: "/assets/tournament-covers/tournament-cover-template-2-v1.webp" },
  { label: "Шаблон 3", url: "/assets/tournament-covers/tournament-cover-template-3-v1.webp" }
] as const;

export const DEFAULT_TOURNAMENT_COVER_URL = TOURNAMENT_COVER_TEMPLATES[0].url;
export const TOURNAMENT_COVER_ASSET_REVISION = "20260725-2";
export const TOURNAMENT_COVER_UPLOAD_MAX_BYTES = 5 * 1024 * 1024;
export const TOURNAMENT_COVER_UPLOAD_HINT =
  "Рекомендуемый размер: 1200x240 - JPG, PNG или WebP до 5 МБ";

export const TOURNAMENT_COVER_UPLOAD_TYPES = [
  "image/jpeg",
  "image/png",
  "image/webp"
] as const;

export function tournamentCoverAssetUrl(url: string | null | undefined): string {
  const resolvedUrl = url || DEFAULT_TOURNAMENT_COVER_URL;
  const isSharedTemplate = TOURNAMENT_COVER_TEMPLATES.some((template) => template.url === resolvedUrl);
  return isSharedTemplate
    ? `${resolvedUrl}?rev=${TOURNAMENT_COVER_ASSET_REVISION}`
    : resolvedUrl;
}
