import { describe, expect, it } from 'vitest';
import {
  bestSourceAt,
  byHorizon,
  coverageStatus,
  deltaVsMts,
  formatCount,
  formatMinutes,
  formatSignedMinutes,
  isSource,
  toHorizonSeries,
  toMinutes,
  type HeadlineRow,
} from './format';

function row(source: string, horizon: number, maeSeconds: number): HeadlineRow {
  return {
    source,
    horizon_minutes: horizon,
    n: 100,
    mae_seconds: maeSeconds,
    median_seconds: maeSeconds * 0.8,
    p90_seconds: maeSeconds * 1.8,
    bias_seconds: -10,
  };
}

describe('minutes formatting', () => {
  it('converts seconds to minutes at two places', () => {
    // The metric is reported in minutes, so 104 seconds is 1.73.
    expect(toMinutes(104)).toBe(1.73);
    expect(formatMinutes(104)).toBe('1.73');
  });

  it('renders an em dash rather than NaN for missing values', () => {
    expect(formatMinutes(null)).toBe('—');
    expect(formatMinutes(undefined)).toBe('—');
    expect(formatMinutes(Number.NaN)).toBe('—');
  });

  it('keeps the sign for bias, where direction is the point', () => {
    expect(formatSignedMinutes(-27)).toBe('-0.45');
    expect(formatSignedMinutes(27)).toBe('+0.45');
    expect(formatSignedMinutes(0)).toBe('0.00');
  });

  it('groups counts', () => {
    expect(formatCount(50591)).toBe('50,591');
    expect(formatCount(null)).toBe('—');
  });
});

describe('source identification', () => {
  it('accepts the four known sources and nothing else', () => {
    expect(isSource('lgbm')).toBe(true);
    expect(isSource('mts')).toBe(true);
    expect(isSource('something_else')).toBe(false);
  });

  it('ignores unknown sources when indexing, rather than throwing', () => {
    // A new predictor added server-side must not break the page.
    const indexed = byHorizon([row('mts', 10, 100), row('mystery', 10, 50)]);
    expect(Object.keys(indexed.get(10) ?? {})).toEqual(['mts']);
  });
});

describe('chart series', () => {
  it('produces one point per horizon with a key per source', () => {
    const series = toHorizonSeries([
      row('mts', 10, 106),
      row('lgbm', 10, 104),
      row('mts', 1, 56),
      row('lgbm', 1, 53),
    ]);

    expect(series).toEqual([
      { horizon: 1, mts: 0.93, lgbm: 0.88 },
      { horizon: 10, mts: 1.77, lgbm: 1.73 },
    ]);
  });

  it('sorts horizons numerically, not lexically', () => {
    const series = toHorizonSeries([
      row('mts', 20, 1),
      row('mts', 5, 1),
      row('mts', 1, 1),
      row('mts', 10, 1),
    ]);
    expect(series.map((p) => p.horizon)).toEqual([1, 5, 10, 20]);
  });
});

describe('delta against MTS', () => {
  it('is positive when we are better', () => {
    const [first] = deltaVsMts([row('mts', 10, 106), row('lgbm', 10, 104)]);
    expect(first?.delta).toBe(2);
  });

  it('is negative when we are worse', () => {
    const [first] = deltaVsMts([row('mts', 10, 100), row('lgbm', 10, 130)]);
    expect(first?.delta).toBe(-30);
  });

  it('skips horizons missing either side rather than inventing a zero', () => {
    expect(deltaVsMts([row('mts', 10, 100)])).toEqual([]);
  });
});

describe('best source', () => {
  it('picks the lowest MAE', () => {
    const rows = [row('mts', 10, 106), row('lgbm', 10, 104), row('segment_mean', 10, 126)];
    expect(bestSourceAt(rows, 10)).toBe('lgbm');
  });

  it('returns null for a horizon with no data', () => {
    expect(bestSourceAt([row('mts', 10, 100)], 20)).toBeNull();
  });
});

describe('coverage status', () => {
  it('flags the real measured values honestly', () => {
    // These are the actual per-day figures: 64%, 38%, 35%, 61%.
    expect(coverageStatus(64.4)).toBe('warning');
    expect(coverageStatus(37.5)).toBe('critical');
    expect(coverageStatus(95)).toBe('good');
  });
});
