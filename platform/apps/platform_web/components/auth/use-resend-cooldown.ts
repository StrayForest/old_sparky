"use client";

import { useCallback, useEffect, useState } from "react";

const defaultCooldownSeconds = 60;

export function useResendCooldown() {
  const [availableAt, setAvailableAt] = useState(0);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const initialNow = Date.now();
    if (availableAt <= initialNow) {
      if (availableAt !== 0) {
        setAvailableAt(0);
        setNow(initialNow);
      }
      return;
    }
    const interval = window.setInterval(() => {
      const nextNow = Date.now();
      setNow(nextNow);
      if (availableAt <= nextNow) {
        window.clearInterval(interval);
        setAvailableAt(0);
      }
    }, 250);
    return () => window.clearInterval(interval);
  }, [availableAt]);

  const secondsRemaining = Math.max(0, Math.ceil((availableAt - now) / 1_000));
  const start = useCallback((seconds = defaultCooldownSeconds) => {
    const normalizedSeconds = Math.max(1, Math.ceil(seconds));
    const nextNow = Date.now();
    setNow(nextNow);
    setAvailableAt(nextNow + normalizedSeconds * 1_000);
  }, []);

  return {
    isCoolingDown: secondsRemaining > 0,
    secondsRemaining,
    start
  };
}
