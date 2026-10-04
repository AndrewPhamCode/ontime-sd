import { useMemo } from 'react';
import type { Params } from '../api/client';
import { useSettings } from './SettingsContext';

/** The API parameters implied by the current settings.
 *
 *  Memoised on the five values that matter so it can be used directly in a
 *  `useApi` dependency list: a panel refetches when a setting it depends on
 *  moves, and not when an unrelated one does.
 */
export function useParams(): Params {
  const { settings } = useSettings();
  const { horizon, minSample, labelFilter, compare, timeBand } = settings;
  return useMemo(
    () => ({ horizon, minSample, labelFilter, compare, timeBand }),
    [horizon, minSample, labelFilter, compare, timeBand],
  );
}
