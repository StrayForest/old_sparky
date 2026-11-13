"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, Check, Palette, RotateCcw, Save } from "lucide-react";
import { useCspNonce } from "@/components/security/csp-nonce-provider";
import { useI18n } from "@/components/i18n-provider";
import {
  appearanceDefaults,
  appearanceGroups,
  appearanceCss,
  resolveAppearanceColors,
  type AppearancePalette
} from "@/lib/appearance-palette";
import { PlatformApiError, platformApiMessage, platformApiRequest } from "@/lib/platform-api";
import type { PlatformAppearance } from "@/lib/platform-types";

type EditablePalette = Record<string, string>;

export function AdminAppearancePage() {
  const { t } = useI18n();
  const nonce = useCspNonce();
  const [saved, setSaved] = useState<PlatformAppearance | null>(null);
  const [draft, setDraft] = useState<EditablePalette>({ ...appearanceDefaults });
  const [hexDraft, setHexDraft] = useState<Record<string, string>>({ ...appearanceDefaults });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [conflict, setConflict] = useState(false);
  const [hsv, setHsv] = useState<Record<string, { h: number; s: number; v: number }>>({});
  const [activeColor, setActiveColor] = useState("background");

  const loadSaved = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError("");
    try {
      const response = await platformApiRequest<PlatformAppearance>("/admin/appearance", { signal });
      const editable = pickEditableColors(response.colors);
      setSaved(response);
      setDraft(editable);
      setHexDraft(editable);
      setHsv({});
      setConflict(false);
    } catch (loadError) {
      if (!signal?.aborted) setError(platformApiMessage(loadError, t("admin.appearance.loadFailed")));
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    const controller = new AbortController();
    void loadSaved(controller.signal);
    return () => controller.abort();
  }, [loadSaved]);

  const dirty = useMemo(() => {
    if (!saved) return false;
    const baseline = pickEditableColors(saved.colors);
    return Object.keys(appearanceDefaults).some((key) => draft[key] !== baseline[key]);
  }, [draft, saved]);
  const hasInvalidHex = useMemo(
    () => Object.keys(appearanceDefaults).some((key) => normalizeHex(hexDraft[key]) !== draft[key]),
    [draft, hexDraft]
  );
  const resolvedDraft = useMemo(() => resolveAppearanceColors(draft), [draft]);
  const contrastIsLow = useMemo(() => contrastRatio(resolvedDraft.foreground, resolvedDraft.background) < 4.5, [resolvedDraft]);

  function setValidColor(key: string, value: string) {
    const normalized = normalizeHex(value);
    setHexDraft((current) => ({ ...current, [key]: value }));
    if (normalized) {
      setDraft((current) => ({ ...current, [key]: normalized }));
      setHsv((current) => ({ ...current, [key]: hexToHsv(normalized) }));
    }
  }

  function setChannel(key: string, channel: "h" | "s" | "v", value: number) {
    const nextHsv = { ...(hsv[key] ?? hexToHsv(draft[key])), [channel]: value };
    setHsv((current) => ({ ...current, [key]: nextHsv }));
    setValidColor(key, hsvToHex(nextHsv));
  }

  function resetDraft() {
    const defaults = { ...appearanceDefaults };
    setDraft(defaults);
    setHexDraft(defaults);
    setHsv({});
    setError("");
    setNotice(t("admin.appearance.resetNotice"));
  }

  async function saveDraft() {
    if (!saved || !dirty || hasInvalidHex || saving) return;
    const normalized = normalizeEditableColors(draft);
    if (!normalized) {
      setError(t("admin.appearance.invalidColor"));
      return;
    }
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const response = await platformApiRequest<PlatformAppearance>("/admin/appearance", {
        method: "PUT",
        body: JSON.stringify({ colors: normalized, expected_revision: saved.revision })
      });
      const editable = pickEditableColors(response.colors);
      setSaved(response);
      setDraft(editable);
      setHexDraft(editable);
      setHsv({});
      setConflict(false);
      setNotice(t("admin.appearance.saved"));
    } catch (saveError) {
      if (saveError instanceof PlatformApiError && saveError.status === 409) {
        setConflict(true);
        setError(t("admin.appearance.conflict"));
      } else {
        setError(platformApiMessage(saveError, t("admin.appearance.saveFailed")));
      }
    } finally {
      setSaving(false);
    }
  }

  const groupKeys = [
    { id: "theme", title: t("admin.appearance.siteGroup"), copy: t("admin.appearance.siteGroupCopy") },
    { id: "system", title: t("admin.appearance.systemGroup"), copy: t("admin.appearance.systemGroupCopy") }
  ] as const;
  const labels: Record<string, string> = {
    background: t("admin.appearance.background"), accent: t("admin.appearance.accent"), foreground: t("admin.appearance.foreground"),
    secondary: t("admin.appearance.secondary"), warning: t("admin.appearance.warning"), danger: t("admin.appearance.danger"), success: t("admin.appearance.success")
  };
  return <section className="ops-page admin-appearance-page" data-testid="admin-appearance-page">
    <style nonce={nonce ?? undefined}>{appearanceCss(resolvedDraft, '[data-appearance-preview="true"]')}</style>
    <div className="ops-page-title-row"><div><span className="ops-kicker">{t("admin.appearance.kicker")}</span><h1>{t("admin.appearance.title")}</h1><p>{t("admin.appearance.description")}</p></div><div className="appearance-mark"><Palette size={22} /></div></div>
    {loading ? <div className="ops-panel appearance-state" role="status">{t("admin.appearance.loading")}</div> : null}
    {!loading && error && !saved ? <div className="ops-panel appearance-state appearance-error" role="alert"><span>{error}</span><button className="ops-button ops-button-secondary" type="button" onClick={() => void loadSaved()}>{t("admin.appearance.retry")}</button></div> : null}
    {saved ? <>
      <div className="appearance-toolbar">
        <div className="appearance-status" aria-live="polite">{error ? <span className="appearance-status-error">{error}</span> : notice ? <span>{notice}</span> : dirty ? <span>{t("admin.appearance.unsaved")}</span> : <span>{t("admin.appearance.upToDate")}</span>}</div>
        <div className="appearance-actions">
          {conflict ? <button className="ops-button ops-button-secondary" type="button" disabled={loading} onClick={() => void loadSaved()}><RotateCcw size={15} />{t("admin.appearance.reloadLatest")}</button> : null}
          <button className="ops-button ops-button-secondary" type="button" disabled={!dirty || saving} onClick={resetDraft}><RotateCcw size={15} />{t("admin.appearance.reset")}</button>
          <button className="ops-button ops-button-primary" type="button" disabled={!dirty || hasInvalidHex || saving} onClick={() => void saveDraft()}><Save size={15} />{saving ? t("admin.appearance.saving") : t("admin.appearance.save")}</button>
        </div>
      </div>
      {contrastIsLow ? <div className="appearance-warning" role="status"><AlertTriangle size={16} />{t("admin.appearance.lowContrast")}</div> : null}
      <div className="appearance-workspace">
        <div className="appearance-editor">
          {groupKeys.map((group) => <section className="ops-panel appearance-group" key={group.id}>
            <div className="ops-section-title"><h2>{group.title}</h2><p>{group.copy}</p></div>
            <div className="appearance-color-list">{appearanceGroups[group.id].map((key) => {
              const invalidHex = normalizeHex(hexDraft[key]) !== draft[key];
              const hexErrorId = `appearance-${key}-hex-error`;
              const currentHsv = hsv[key] ?? hexToHsv(draft[key]);
              return <label className={`appearance-color-row${activeColor === key ? " is-active" : ""}`} key={key}>
              <span className="appearance-color-name"><span className="appearance-color-dot"><input type="color" aria-label={labels[key]} value={draft[key]} disabled={saving} onFocus={() => setActiveColor(key)} onChange={(event) => setValidColor(key, event.currentTarget.value)} /></span><span><strong>{labels[key]}</strong><small>{key === "background" || key === "accent" || key === "foreground" ? t("admin.appearance.seedColor") : t("admin.appearance.systemColor")}</small></span></span>
              <span className="appearance-hex-control"><span className="appearance-range-pair"><input type="range" min="0" max="359" step="1" aria-label={t("admin.appearance.hue", { color: labels[key] })} value={currentHsv.h} disabled={saving} onFocus={() => setActiveColor(key)} onChange={(event) => setChannel(key, "h", Number(event.currentTarget.value))} /><input type="range" min="0" max="100" step="1" aria-label={t("admin.appearance.saturation", { color: labels[key] })} value={currentHsv.s} disabled={saving} onFocus={() => setActiveColor(key)} onChange={(event) => setChannel(key, "s", Number(event.currentTarget.value))} /><input type="range" min="0" max="100" step="1" aria-label={t("admin.appearance.brightness", { color: labels[key] })} value={currentHsv.v} disabled={saving} onFocus={() => setActiveColor(key)} onChange={(event) => setChannel(key, "v", Number(event.currentTarget.value))} /></span><span className="appearance-hex-entry"><input className="appearance-hex-input" aria-label={t("admin.appearance.hexLabel", { color: labels[key] })} aria-invalid={invalidHex} aria-describedby={invalidHex ? hexErrorId : undefined} inputMode="text" autoCapitalize="characters" spellCheck={false} maxLength={7} value={hexDraft[key]} disabled={saving} onFocus={() => setActiveColor(key)} onChange={(event) => setValidColor(key, event.currentTarget.value)} onBlur={() => { const normalized = normalizeHex(hexDraft[key]); if (normalized) setHexDraft((current) => ({ ...current, [key]: normalized })); }} />{invalidHex ? <small className="appearance-hex-error" id={hexErrorId} role="alert">{t("admin.appearance.invalidColor")}</small> : null}</span></span>
            </label>; })}</div>
          </section>)}
        </div>
        <section className="ops-panel appearance-preview-panel" aria-label={t("admin.appearance.previewTitle")}>
          <div className="ops-section-title"><h2>{t("admin.appearance.previewTitle")}</h2><p>{t("admin.appearance.previewCopy")}</p></div>
      <div className="appearance-preview" data-appearance-preview="true">
            <div className="appearance-preview-bar"><Palette size={17} /><strong>OLD SPARKY</strong><span>{t("admin.appearance.previewBadge")}</span></div>
            <div className="appearance-preview-content"><span className="appearance-preview-chip">{t("admin.appearance.previewAccent")}</span><h3>{t("admin.appearance.previewHeading")}</h3><p>{t("admin.appearance.previewBody")}</p><div className="appearance-preview-buttons"><button type="button">{t("admin.appearance.previewButton")}</button><span>{t("admin.appearance.previewMuted")}</span></div><div className="appearance-preview-states"><span className="is-success"><Check size={13} />{t("admin.appearance.success")}</span><span className="is-warning">{t("admin.appearance.warning")}</span><span className="is-danger">{t("admin.appearance.danger")}</span></div></div>
          </div>
          <div className="appearance-preview-note">{t("admin.appearance.previewLocalOnly")}</div>
        </section>
      </div>
    </> : null}
  </section>;
}

function pickEditableColors(colors: AppearancePalette): EditablePalette {
  return Object.fromEntries(Object.keys(appearanceDefaults).map((key) => [key, colors[key] ?? appearanceDefaults[key]]));
}

function normalizeEditableColors(colors: EditablePalette): EditablePalette | null {
  const normalized = Object.fromEntries(Object.keys(appearanceDefaults).map((key) => [key, normalizeHex(colors[key]) ?? ""]));
  return Object.values(normalized).every(Boolean) ? normalized : null;
}

function normalizeHex(value: string | undefined): string | null {
  if (!value) return null;
  const candidate = value.startsWith("#") ? value : `#${value}`;
  return /^#[0-9a-f]{6}$/iu.test(candidate) ? candidate.toUpperCase() : null;
}

function hexToHsv(hex: string) {
  const normalized = normalizeHex(hex) ?? "#000000";
  const r = Number.parseInt(normalized.slice(1, 3), 16) / 255;
  const g = Number.parseInt(normalized.slice(3, 5), 16) / 255;
  const b = Number.parseInt(normalized.slice(5, 7), 16) / 255;
  const max = Math.max(r, g, b); const min = Math.min(r, g, b); const delta = max - min;
  let h = 0;
  if (delta) h = max === r ? ((g - b) / delta) % 6 : max === g ? (b - r) / delta + 2 : (r - g) / delta + 4;
  return { h: Math.round((h * 60 + 360) % 360), s: max ? Math.round(delta / max * 100) : 0, v: Math.round(max * 100) };
}

function hsvToHex({ h, s, v }: { h: number; s: number; v: number }) {
  const saturation = s / 100; const value = v / 100; const chroma = value * saturation; const x = chroma * (1 - Math.abs((h / 60) % 2 - 1)); const m = value - chroma;
  const rgb = h < 60 ? [chroma, x, 0] : h < 120 ? [x, chroma, 0] : h < 180 ? [0, chroma, x] : h < 240 ? [0, x, chroma] : h < 300 ? [x, 0, chroma] : [chroma, 0, x];
  return `#${rgb.map((channel) => Math.round((channel + m) * 255).toString(16).padStart(2, "0")).join("")}`.toUpperCase();
}

function contrastRatio(foreground: string, background: string): number {
  const luminance = (hex: string) => {
    const channels = [1, 3, 5].map((index) => Number.parseInt(hex.slice(index, index + 2), 16) / 255).map((value) => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
    return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
  };
  const values = [luminance(foreground), luminance(background)].sort((a, b) => b - a);
  return (values[0] + 0.05) / (values[1] + 0.05);
}
