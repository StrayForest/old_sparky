"use client";

import { useEffect, useState } from "react";

type TournamentCountdownLabelProps = {
  targetIso?: string | null;
  fallbackLabel: string;
  prefix: string;
  elapsedLabel: string;
};

export function TournamentCountdownLabel({
  targetIso,
  fallbackLabel,
  prefix,
  elapsedLabel
}: TournamentCountdownLabelProps) {
  const targetMs = parseTargetMs(targetIso);
  const [nowMs, setNowMs] = useState<number | null>(null);

  useEffect(() => {
    if (targetMs === null) {
      return;
    }

    const update = () => setNowMs(Date.now());
    update();
    const intervalId = window.setInterval(update, 1000);
    return () => window.clearInterval(intervalId);
  }, [targetMs]);

  if (targetMs === null) {
    return <span>{fallbackLabel}</span>;
  }

  if (nowMs === null) {
    return <span suppressHydrationWarning>{prefix} --:--:--</span>;
  }

  return <span suppressHydrationWarning>{formatCountdownLabel(targetMs, nowMs, prefix, elapsedLabel)}</span>;
}

function parseTargetMs(value: string | null | undefined): number | null {
  if (!value) {
    return null;
  }

  const timestamp = new Date(value).getTime();
  return Number.isFinite(timestamp) ? timestamp : null;
}

function formatCountdownLabel(targetMs: number, nowMs: number, prefix: string, elapsedLabel: string): string {
  const remainingSeconds = Math.floor((targetMs - nowMs) / 1000);
  if (remainingSeconds <= 0) {
    return elapsedLabel;
  }

  return `${prefix} ${formatDuration(remainingSeconds)}`;
}

function formatDuration(totalSeconds: number): string {
  const days = Math.floor(totalSeconds / 86_400);
  const hours = Math.floor((totalSeconds % 86_400) / 3_600);
  const minutes = Math.floor((totalSeconds % 3_600) / 60);
  const seconds = totalSeconds % 60;
  const time = `${pad(hours)}:${pad(minutes)}:${pad(seconds)}`;

  return days > 0 ? `${days} д. ${time}` : time;
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}
