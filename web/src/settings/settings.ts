/** The settings model, kept free of React so it can be tested directly.
 *
 *  Two groups, because the app has two audiences. The prediction settings change
 *  how the numbers are computed and are the decisions this project argues about;
 *  the map settings change how the thing is used. Defaults are exactly the
 *  constants the API applies when no parameter is sent, so a fresh viewer and a
 *  reset viewer see the same figures the README quotes.
 */

export const SOURCE_KEYS = ['lgbm', 'segment_mean', 'persist_delay', 'mts'] as const;
export type SourceKey = (typeof SOURCE_KEYS)[number];

export const PLACE_KEYS = ['ucsd', 'downtown', 'all'] as const;
export type PlaceKey = (typeof PLACE_KEYS)[number];

export const HORIZON_CHOICES = [1, 5, 10, 20] as const;
export const SAMPLE_CHOICES = [1, 5, 10, 30, 100] as const;
/** Seconds of GPS ping gap tolerated in an arrival used for scoring. */
export const LABEL_FILTER_CHOICES = [60, 180, 600, 900] as const;
export const REFRESH_CHOICES = [5_000, 10_000, 30_000, 0] as const;

export interface Settings {
  /** Which horizon's measured bias drives the live correction. */
  horizon: number;
  /** Arrivals required at a stop and route before its bias is trusted. */
  minSample: number;
  /** Maximum GPS ping gap, in seconds, for an arrival to count as a label. */
  labelFilter: number;
  /** Which predictor is shown beside MTS. */
  compare: SourceKey;

  place: PlaceKey;
  /** Vehicle poll interval in ms; 0 means do not poll. */
  refreshMs: number;
  showVehicles: boolean;
  showStopLabels: boolean;
  clock24: boolean;
}

export const DEFAULTS: Settings = {
  horizon: 10,
  minSample: 10,
  labelFilter: 180,
  compare: 'lgbm',
  place: 'ucsd',
  refreshMs: 10_000,
  showVehicles: true,
  showStopLabels: true,
  clock24: false,
};

export const STORAGE_KEY = 'ontime-sd.settings.v1';

function oneOf<T>(choices: readonly T[], value: unknown, fallback: T): T {
  return choices.includes(value as T) ? (value as T) : fallback;
}

function bool(value: unknown, fallback: boolean): boolean {
  return typeof value === 'boolean' ? value : fallback;
}

/** Build a valid Settings from anything at all.
 *
 *  Every field is validated against its allowed values rather than merely
 *  spread over the defaults. A key left over from an older version of the app,
 *  a hand-edited localStorage entry, or a truncated JSON blob therefore yields
 *  the default for that one field instead of putting an unusable value into a
 *  query string or white-screening the app.
 */
export function coerceSettings(raw: unknown): Settings {
  if (raw === null || typeof raw !== 'object') return { ...DEFAULTS };
  const input = raw as Record<string, unknown>;
  return {
    horizon: oneOf(HORIZON_CHOICES, input['horizon'], DEFAULTS.horizon),
    minSample: oneOf(SAMPLE_CHOICES, input['minSample'], DEFAULTS.minSample),
    labelFilter: oneOf(LABEL_FILTER_CHOICES, input['labelFilter'], DEFAULTS.labelFilter),
    compare: oneOf(SOURCE_KEYS, input['compare'], DEFAULTS.compare),
    place: oneOf(PLACE_KEYS, input['place'], DEFAULTS.place),
    refreshMs: oneOf(REFRESH_CHOICES, input['refreshMs'], DEFAULTS.refreshMs),
    showVehicles: bool(input['showVehicles'], DEFAULTS.showVehicles),
    showStopLabels: bool(input['showStopLabels'], DEFAULTS.showStopLabels),
    clock24: bool(input['clock24'], DEFAULTS.clock24),
  };
}

export function readStored(storage: Pick<Storage, 'getItem'> | undefined): Settings {
  if (!storage) return { ...DEFAULTS };
  try {
    const raw = storage.getItem(STORAGE_KEY);
    if (!raw) return { ...DEFAULTS };
    return coerceSettings(JSON.parse(raw));
  } catch {
    // Private-mode storage throws, and malformed JSON throws. Neither is worth
    // breaking the page over.
    return { ...DEFAULTS };
  }
}

export function isModified(settings: Settings): boolean {
  return (Object.keys(DEFAULTS) as (keyof Settings)[]).some(
    (key) => settings[key] !== DEFAULTS[key],
  );
}

/** Settings that make the measured result look better than the honest one.
 *
 *  Loosening the label filter admits arrivals whose true time we barely know,
 *  which charges our own interpolation error to every predictor. Dropping the
 *  minimum sample fits a correction to a handful of observations. Both produce
 *  numbers, so the UI has to say when they are in effect.
 */
export function weakenedBy(settings: Settings): string[] {
  const reasons: string[] = [];
  if (settings.labelFilter > DEFAULTS.labelFilter) {
    reasons.push(
      `the label filter is at ${Math.round(settings.labelFilter / 60)} min instead of ` +
        `${Math.round(DEFAULTS.labelFilter / 60)}, so arrivals we only know roughly are ` +
        `being scored`,
    );
  }
  if (settings.minSample < DEFAULTS.minSample) {
    reasons.push(
      `corrections are allowed on as few as ${settings.minSample} observed ` +
        `arrival${settings.minSample === 1 ? '' : 's'}, which fits them to noise`,
    );
  }
  return reasons;
}
