import manifest from "../node_modules/.cache/platform/appearance_palette.json";

export type AppearancePalette = Record<string, string>;
export type AppearanceGroup = "theme" | "system";

export const appearanceDefaults = manifest.defaults as AppearancePalette;
export const appearanceGroups = manifest.groups as Record<AppearanceGroup, string[]>;
export const appearanceDerived = manifest.derived as unknown as Record<string,
  | { mix: [string, string, number] }
  | { contrast: { against: string; light: string; dark: string; threshold: number } }
>;

function isHexColor(value: string): boolean {
  return /^#[0-9a-f]{6}$/iu.test(value);
}

function mixColors(first: string, second: string, amount: number): string {
  const a = [1, 3, 5].map((index) => Number.parseInt(first.slice(index, index + 2), 16));
  const b = [1, 3, 5].map((index) => Number.parseInt(second.slice(index, index + 2), 16));
  return `#${a.map((channel, index) => Math.round(channel * (1 - amount) + b[index] * amount).toString(16).padStart(2, "0")).join("")}`.toUpperCase();
}

function luminance(hex: string): number {
  const channels = [1, 3, 5].map((index) => Number.parseInt(hex.slice(index, index + 2), 16) / 255)
    .map((channel) => channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4);
  return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
}

export function resolveAppearanceColors(editable: AppearancePalette): AppearancePalette {
  const colors: AppearancePalette = {};
  for (const key of Object.keys(appearanceDefaults)) {
    const color = editable[key];
    if (!isHexColor(color ?? "")) throw new Error(`Invalid appearance color: ${key}`);
    colors[key] = color.toUpperCase();
  }
  const pending = new Map(Object.entries(appearanceDerived));
  while (pending.size) {
    let progressed = false;
    for (const [key, recipe] of pending) {
      if ("mix" in recipe) {
        const [firstRole, secondRole, amount] = recipe.mix;
        const first = colors[firstRole] ?? firstRole;
        const second = colors[secondRole] ?? secondRole;
        if ((colors[firstRole] || isHexColor(first)) && (colors[secondRole] || isHexColor(second))) {
          colors[key] = mixColors(first, second, amount);
          pending.delete(key);
          progressed = true;
        }
      } else {
        const color = colors[recipe.contrast.against];
        if (color) {
          colors[key] = luminance(color) < recipe.contrast.threshold ? recipe.contrast.light : recipe.contrast.dark;
          pending.delete(key);
          progressed = true;
        }
      }
    }
    if (!progressed) throw new Error("Appearance palette derivation has an unknown or cyclic role.");
  }
  return colors;
}

export function appearanceCss(colors: AppearancePalette, selector = ":root"): string {
  const roles: Record<string, string> = {
    background: "--ui-canvas",
    backgroundDeep: "--ui-canvas-deep",
    surface: "--ui-surface",
    surfaceStrong: "--ui-surface-strong",
    surfaceSoft: "--ui-surface-soft",
    surfaceHover: "--ui-surface-hover",
    border: "--ui-border",
    borderStrong: "--ui-border-strong",
    accent: "--ui-primary",
    onPrimary: "--ui-on-primary",
    accentBright: "--ui-primary-bright",
    accentDeep: "--ui-primary-deep",
    accentSoft: "--ui-primary-soft",
    secondary: "--ui-secondary",
    secondarySoft: "--ui-secondary-soft",
    warning: "--ui-warning",
    warningSoft: "--ui-warning-soft",
    danger: "--ui-danger",
    dangerSoft: "--ui-danger-soft",
    success: "--ui-success",
    successSoft: "--ui-success-soft",
    foreground: "--ui-text",
    foregroundSoft: "--ui-text-soft",
    foregroundMuted: "--ui-text-muted"
  };
  const declarations = (palette: AppearancePalette) => Object.entries(roles)
    .map(([key, name]) => `${name}:${palette[key] ?? "#000000"};`)
    .join("");
  const colorScheme = luminance(colors.background ?? appearanceDefaults.background) < 0.42 ? "dark" : "light";
  const rootRules = `${selector}{${declarations(colors)}color-scheme:${colorScheme};}`;
  if (selector !== ":root") return rootRules;
  const safeAdminColors = resolveAppearanceColors(appearanceDefaults);
  return `${rootRules}.ops-shell{${declarations(safeAdminColors)}}`;
}
