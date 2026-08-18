"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export interface PollingState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  refresh: () => void;
}

export function usePolling<T>(
  loader: (signal: AbortSignal) => Promise<T>,
  intervalMs: number,
): PollingState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const controller = useRef<AbortController | null>(null);
  const mounted = useRef(false);
  const safeInterval = Number.isFinite(intervalMs) ? Math.max(1_000, intervalMs) : 2_000;

  const refresh = useCallback(() => {
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    void loader(next.signal)
      .then((value) => {
        if (!next.signal.aborted && mounted.current) {
          setData(value);
          setError(null);
        }
      })
      .catch((reason: unknown) => {
        if (!next.signal.aborted && mounted.current) {
          setError(reason instanceof Error ? reason.message : "Dashboard request failed");
        }
      })
      .finally(() => {
        if (!next.signal.aborted && mounted.current) {
          setLoading(false);
        }
      });
  }, [loader]);

  useEffect(() => {
    mounted.current = true;
    refresh();
    const timer = window.setInterval(refresh, safeInterval);
    return () => {
      mounted.current = false;
      window.clearInterval(timer);
      controller.current?.abort();
    };
  }, [refresh, safeInterval]);

  return { data, error, loading, refresh };
}
