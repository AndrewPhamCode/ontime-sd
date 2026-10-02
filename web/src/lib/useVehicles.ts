import { useEffect, useState } from 'react';
import { api, type Vehicle } from '../api/client';

export interface VehicleFeed {
  vehicles: Vehicle[];
  updatedAt: number;
  error: string | null;
}

/** Poll live vehicle positions.
 *
 *  The feed itself only refreshes every 30 seconds upstream, so polling faster
 *  than that buys nothing. Ten seconds keeps the page responsive to a new poll
 *  landing without hammering the API.
 */
export function useVehicles(intervalMs = 10_000): VehicleFeed {
  const [feed, setFeed] = useState<VehicleFeed>({
    vehicles: [],
    updatedAt: 0,
    error: null,
  });

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const tick = async () => {
      try {
        const vehicles = await api.vehicles();
        if (active) setFeed({ vehicles, updatedAt: Date.now(), error: null });
      } catch (cause) {
        if (active) {
          setFeed((current) => ({
            ...current,
            error: cause instanceof Error ? cause.message : 'request failed',
          }));
        }
      }
      if (active) timer = setTimeout(tick, intervalMs);
    };

    void tick();
    return () => {
      active = false;
      if (timer) clearTimeout(timer);
    };
  }, [intervalMs]);

  return feed;
}
