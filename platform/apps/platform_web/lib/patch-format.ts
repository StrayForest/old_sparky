const SOURCE_DATE_SUFFIX = /\s*(?:[-–—|]\s*)?\d{1,2}[-./]\d{1,2}[-./]\d{4}\s*$/u;

export function formatPatchTitle(title: string): string {
  return title.replace(SOURCE_DATE_SUFFIX, "").trim();
}

export function formatPatchDate(publishedAt: string): string {
  const date = new Date(publishedAt);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  const parts = new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    timeZone: "UTC"
  }).formatToParts(date);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.day}-${values.month}-${values.year}`;
}
