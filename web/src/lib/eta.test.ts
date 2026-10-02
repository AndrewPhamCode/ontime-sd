import { describe, expect, it } from 'vitest';
import {
  BASIS_NONE,
  BASIS_ROUTE,
  BASIS_STOP_ROUTE,
  correctionIsMeaningful,
  explainCorrection,
  formatDuration,
  formatWait,
  headingBetween,
  isTrolley,
  minutesUntil,
} from './eta';

const NOW = new Date('2026-10-02T20:00:00Z');
const inMinutes = (m: number) => new Date(NOW.getTime() + m * 60_000).toISOString();

describe('waiting time', () => {
  it('counts minutes from now', () => {
    expect(minutesUntil(inMinutes(7), NOW)).toBeCloseTo(7);
  });

  it('reads as a departure board', () => {
    expect(formatWait(inMinutes(7), NOW)).toBe('7 min');
    expect(formatWait(inMinutes(0.4), NOW)).toBe('Due');
    expect(formatWait(inMinutes(75), NOW)).toBe('1 hr 15 min');
    expect(formatWait(inMinutes(120), NOW)).toBe('2 hr');
  });

  it('shows a slightly late bus as due rather than a negative number', () => {
    // A bus one minute past its estimate is still coming.
    expect(formatWait(inMinutes(-1), NOW)).toBe('Due');
  });
});

describe('durations', () => {
  it('is compact enough for one line', () => {
    expect(formatDuration(45)).toBe('45s');
    expect(formatDuration(131)).toBe('2m 11s');
    expect(formatDuration(120)).toBe('2m');
    expect(formatDuration(-131)).toBe('2m 11s');
  });
});

describe('explaining the correction', () => {
  it('describes what MTS does, with the evidence behind it', () => {
    // The real measurement at Gilman Dr & Eucalyptus Grove, route 30.
    expect(explainCorrection(131, BASIS_STOP_ROUTE, 91)).toBe(
      'MTS runs about 2m 11s early at this stop over 91 arrivals',
    );
  });

  it('says route rather than stop when it fell back a level', () => {
    expect(explainCorrection(70, BASIS_ROUTE, 400)).toContain('on this route');
  });

  it('says late when MTS predicts later than the bus arrives', () => {
    expect(explainCorrection(-90, BASIS_STOP_ROUTE, 50)).toContain('late');
  });

  it('explains nothing when there was no basis', () => {
    expect(explainCorrection(null, BASIS_NONE, null)).toBeNull();
  });

  it('stays quiet about a correction too small to matter', () => {
    // Under 20 seconds, the two estimates agree and a second line is noise.
    expect(explainCorrection(11, BASIS_STOP_ROUTE, 500)).toBeNull();
  });
});

describe('whether to show a second number at all', () => {
  it('shows one only when the correction is big enough to act on', () => {
    expect(correctionIsMeaningful(131)).toBe(true);
    expect(correctionIsMeaningful(-45)).toBe(true);
    expect(correctionIsMeaningful(9)).toBe(false);
    expect(correctionIsMeaningful(null)).toBe(false);
  });
});

describe('vehicle kind', () => {
  it('treats GTFS route_type 0 as a trolley and 3 as a bus', () => {
    expect(isTrolley(0)).toBe(true);
    expect(isTrolley(3)).toBe(false);
    expect(isTrolley(null)).toBe(false);
  });
});

describe('heading, derived because the feed sends none', () => {
  it('points north when travelling north', () => {
    expect(headingBetween([-117.2, 32.8], [-117.2, 32.9])).toBeCloseTo(0, 0);
  });

  it('points east when travelling east', () => {
    expect(headingBetween([-117.3, 32.8], [-117.2, 32.8])).toBeCloseTo(90, 0);
  });

  it('points south when travelling south', () => {
    expect(headingBetween([-117.2, 32.9], [-117.2, 32.8])).toBeCloseTo(180, 0);
  });

  it('has no opinion when the vehicle has not moved', () => {
    // Otherwise a stationary bus would spin on GPS jitter.
    expect(headingBetween([-117.2, 32.8], [-117.2, 32.8])).toBeNull();
  });
});
