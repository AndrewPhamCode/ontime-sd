import { describe, expect, it } from 'vitest';
import {
  arriveByIso,
  describeDeparture,
  describeMargin,
  leaveLabel,
  planKey,
  walkMinutes,
} from './trip';

// Local time, because the arrive-by input is read in the browser's timezone.
const NOW = new Date(2026, 9, 15, 20, 0, 0);
const inMinutes = (m: number) => new Date(NOW.getTime() + m * 60_000).toISOString();

describe('arrive by', () => {
  it('reads a later time today as today', () => {
    const iso = arriveByIso('21:30', NOW)!;
    const target = new Date(iso);
    expect(target.getDate()).toBe(15);
    expect(target.getHours()).toBe(21);
    expect(target.getMinutes()).toBe(30);
  });

  it('reads a time already gone as tomorrow, not the past', () => {
    const target = new Date(arriveByIso('08:15', NOW)!);
    expect(target.getDate()).toBe(16);
    expect(target.getHours()).toBe(8);
  });

  it('treats the current minute as tomorrow, since it cannot be met', () => {
    expect(new Date(arriveByIso('20:00', NOW)!).getDate()).toBe(16);
  });

  it('rejects input that is not a clock time', () => {
    expect(arriveByIso('', NOW)).toBeNull();
    expect(arriveByIso('25:00', NOW)).toBeNull();
    expect(arriveByIso('soon', NOW)).toBeNull();
  });
});

describe('option wording', () => {
  it('never shows a zero minute walk', () => {
    expect(walkMinutes(10)).toBe(1);
    expect(walkMinutes(503)).toBe(8);
  });

  it('says leave now once the leave time has come', () => {
    expect(leaveLabel(inMinutes(0.5), NOW)).toBe('Leave now');
    expect(leaveLabel(inMinutes(-2), NOW)).toBe('Leave now');
    expect(leaveLabel(inMinutes(6), NOW)).toBe('Leave in 6 min');
  });

  it('names which estimate the departure is', () => {
    expect(describeDeparture('corrected')).toBe('live, our estimate');
    expect(describeDeparture('mts')).toBe('live, MTS estimate');
    expect(describeDeparture('scheduled')).toBe('timetable');
  });

  it('keeps a measured margin and the default distinguishable', () => {
    expect(describeMargin(200, 'measured')).toMatch(/^3m 20s early, because 1 in 10/);
    expect(describeMargin(120, 'default')).toMatch(/standard buffer/);
  });
});

describe('request identity', () => {
  const a = { lat: 32.877, lon: -117.237 };
  const b = { lat: 32.7547, lon: -117.1997 };

  it('needs both ends', () => {
    expect(planKey(a, null, null)).toBeNull();
    expect(planKey(null, b, null)).toBeNull();
  });

  it('changes when any input changes, so a stale answer is never shown as current', () => {
    const base = planKey(a, b, null);
    expect(planKey(b, a, null)).not.toBe(base);
    expect(planKey(a, b, '2026-10-16T03:00:00.000Z')).not.toBe(base);
    expect(planKey({ ...a }, { ...b }, null)).toBe(base);
  });
});
