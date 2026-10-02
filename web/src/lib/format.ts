/** Formatting and data shaping. Kept separate from components because this is
 *  where the bugs live, and it is testable without a DOM. */

export const SOURCES = ['lgbm', 'mts', 'segment_mean', 'persist_delay'] as const;
export type Source = (typeof SOURCES)[number];

/** Display names. `lgbm` is deliberately labelled as ours so a visitor can tell
 *  which series is the subject without reading the legend twice. */
export const SOURCE_LABELS: Record<Source, string> = {
  lgbm: 'Ours (LightGBM)',
  mts: 'MTS official',
  segment_mean: 'Baseline: segment mean',
  persist_delay: 'Baseline: persist delay',
};

/** CSS variables, so light and dark steps resolve in one place. */
export const SOURCE_COLORS: Record<Source, string> = {
  lgbm: 'var(--series-lgbm)',
  mts: 'var(--series-mts)',
  segment_mean: 'var(--series-segment)',
  persist_delay: 'var(--series-persist)',
};

export const HORIZONS = [1, 5, 10, 20] as const;

export function isSource(value: string): value is Source {
  return (SOURCES as readonly string[]).includes(value);
}

/** Seconds as minutes to two places. The metric is reported in minutes. */
export function toMinutes(seconds: number): number {
  return Math.round((seconds / 60) * 100) / 100;
}

export function formatMinutes(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '—';
  return toMinutes(seconds).toFixed(2);
}

/** Signed, for bias and deltas, where direction is the point. */
export function formatSignedMinutes(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '—';
  const minutes = toMinutes(seconds);
  return `${minutes > 0 ? '+' : ''}${minutes.toFixed(2)}`;
}

export function formatCount(n: number | null | undefined): string {
  if (n === null || n === undefined) return '—';
  return n.toLocaleString('en-US');
}

export function formatClock(iso: string | null | undefined): string {
  if (!iso) return '—';
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return '—';
  return parsed.toLocaleTimeString('en-US', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

export interface HeadlineRow {
  source: string;
  horizon_minutes: number;
  n: number;
  mae_seconds: number;
  median_seconds: number;
  p90_seconds: number;
  bias_seconds: number;
}

/** Index headline rows by horizon then source, which is how every panel reads them. */
export function byHorizon(
  rows: readonly HeadlineRow[],
): Map<number, Partial<Record<Source, HeadlineRow>>> {
  const out = new Map<number, Partial<Record<Source, HeadlineRow>>>();
  for (const row of rows) {
    if (!isSource(row.source)) continue;
    const bucket = out.get(row.horizon_minutes) ?? {};
    bucket[row.source] = row;
    out.set(row.horizon_minutes, bucket);
  }
  return out;
}

export interface HorizonPoint {
  horizon: number;
  lgbm?: number;
  mts?: number;
  segment_mean?: number;
  persist_delay?: number;
}

/** Chart series: one point per horizon, one key per source, in minutes. */
export function toHorizonSeries(rows: readonly HeadlineRow[]): HorizonPoint[] {
  const indexed = byHorizon(rows);
  return [...indexed.keys()]
    .sort((a, b) => a - b)
    .map((horizon) => {
      const bucket = indexed.get(horizon) ?? {};
      const point: HorizonPoint = { horizon };
      for (const source of SOURCES) {
        const row = bucket[source];
        if (row) point[source] = toMinutes(row.mae_seconds);
      }
      return point;
    });
}

/** How much better than MTS we are, per horizon, in seconds. Positive is better. */
export function deltaVsMts(
  rows: readonly HeadlineRow[],
): { horizon: number; delta: number; n: number }[] {
  const indexed = byHorizon(rows);
  const out: { horizon: number; delta: number; n: number }[] = [];
  for (const horizon of [...indexed.keys()].sort((a, b) => a - b)) {
    const bucket = indexed.get(horizon) ?? {};
    const mts = bucket.mts;
    const ours = bucket.lgbm;
    if (!mts || !ours) continue;
    out.push({
      horizon,
      delta: mts.mae_seconds - ours.mae_seconds,
      n: mts.n,
    });
  }
  return out;
}

/** Which source has the lowest MAE at a horizon. Drives the bold cell. */
export function bestSourceAt(rows: readonly HeadlineRow[], horizon: number): Source | null {
  const bucket = byHorizon(rows).get(horizon);
  if (!bucket) return null;
  let best: Source | null = null;
  let lowest = Number.POSITIVE_INFINITY;
  for (const source of SOURCES) {
    const row = bucket[source];
    if (row && row.mae_seconds < lowest) {
      lowest = row.mae_seconds;
      best = source;
    }
  }
  return best;
}

/** Coverage below this reads as a warning rather than a neutral number. */
export const COVERAGE_WARN_PCT = 80;
export const COVERAGE_CRITICAL_PCT = 50;

export function coverageStatus(pct: number): 'good' | 'warning' | 'critical' {
  if (pct < COVERAGE_CRITICAL_PCT) return 'critical';
  if (pct < COVERAGE_WARN_PCT) return 'warning';
  return 'good';
}
