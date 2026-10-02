import { useEffect, useState } from 'react';

export interface AsyncState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
}

/** Minimal fetch-on-mount hook. No cache library: every panel loads once, and the
 *  API already caches its own aggregates. */
export function useApi<T>(
  load: () => Promise<T>,
  deps: readonly unknown[] = [],
): AsyncState<T> {
  const [state, setState] = useState<AsyncState<T>>({
    data: null,
    error: null,
    loading: true,
  });

  useEffect(() => {
    let active = true;
    setState({ data: null, error: null, loading: true });
    load()
      .then((data) => {
        if (active) setState({ data, error: null, loading: false });
      })
      .catch((cause: unknown) => {
        if (active) {
          setState({
            data: null,
            error: cause instanceof Error ? cause.message : 'request failed',
            loading: false,
          });
        }
      });
    return () => {
      active = false;
    };
    // Dependencies are supplied by the caller on purpose: each panel declares
    // exactly what should retrigger its fetch.
  }, deps);

  return state;
}
