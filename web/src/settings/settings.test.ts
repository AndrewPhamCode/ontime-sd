import { describe, expect, it } from 'vitest';
import {
  DEFAULTS,
  STORAGE_KEY,
  coerceSettings,
  isModified,
  readStored,
  weakenedBy,
} from './settings';

/** A localStorage stand-in, including the throwing case a private window gives. */
function storage(value: string | null, throws = false): Pick<Storage, 'getItem'> {
  return {
    getItem: () => {
      if (throws) throw new Error('storage disabled');
      return value;
    },
  };
}

describe('coerceSettings', () => {
  it('returns the defaults for anything that is not an object', () => {
    for (const input of [null, undefined, 42, 'nope', []]) {
      expect(coerceSettings(input)).toEqual(DEFAULTS);
    }
  });

  it('keeps values that are allowed', () => {
    const stored = { ...DEFAULTS, horizon: 20, compare: 'segment_mean', clock24: true };
    expect(coerceSettings(stored)).toMatchObject({
      horizon: 20,
      compare: 'segment_mean',
      clock24: true,
    });
  });

  it('falls back per field rather than wholesale', () => {
    // horizon is not an offered choice, compare is not a known predictor, and
    // showVehicles is the wrong type. place is valid and must survive.
    const result = coerceSettings({
      horizon: 7,
      compare: 'wishful_thinking',
      showVehicles: 'yes',
      place: 'downtown',
    });
    expect(result.horizon).toBe(DEFAULTS.horizon);
    expect(result.compare).toBe(DEFAULTS.compare);
    expect(result.showVehicles).toBe(DEFAULTS.showVehicles);
    expect(result.place).toBe('downtown');
  });

  it('rejects a horizon the API would not accept even though it is a number', () => {
    expect(coerceSettings({ horizon: 999 }).horizon).toBe(DEFAULTS.horizon);
    expect(coerceSettings({ labelFilter: -1 }).labelFilter).toBe(DEFAULTS.labelFilter);
    expect(coerceSettings({ minSample: 0 }).minSample).toBe(DEFAULTS.minSample);
  });
});

describe('readStored', () => {
  it('uses the defaults when nothing is stored', () => {
    expect(readStored(storage(null))).toEqual(DEFAULTS);
    expect(readStored(undefined)).toEqual(DEFAULTS);
  });

  it('round-trips a persisted value', () => {
    const saved = JSON.stringify({ ...DEFAULTS, horizon: 5, place: 'all' });
    expect(readStored(storage(saved))).toMatchObject({ horizon: 5, place: 'all' });
  });

  it('survives malformed JSON instead of throwing', () => {
    expect(readStored(storage('{"horizon":'))).toEqual(DEFAULTS);
    expect(readStored(storage('null'))).toEqual(DEFAULTS);
  });

  it('survives storage that throws on access', () => {
    expect(readStored(storage(null, true))).toEqual(DEFAULTS);
  });

  it('reads the versioned key, so an older schema is simply ignored', () => {
    expect(STORAGE_KEY).toContain('v1');
  });
});

describe('isModified', () => {
  it('is false for the defaults and true for any single change', () => {
    expect(isModified(DEFAULTS)).toBe(false);
    expect(isModified({ ...DEFAULTS, clock24: !DEFAULTS.clock24 })).toBe(true);
    expect(isModified({ ...DEFAULTS, horizon: 1 })).toBe(true);
  });
});

describe('weakenedBy', () => {
  it('says nothing at the defaults', () => {
    expect(weakenedBy(DEFAULTS)).toEqual([]);
  });

  it('warns when the label filter is loosened', () => {
    const reasons = weakenedBy({ ...DEFAULTS, labelFilter: 900 });
    expect(reasons).toHaveLength(1);
    expect(reasons[0]).toContain('label filter');
  });

  it('warns when corrections are allowed on thin evidence', () => {
    const reasons = weakenedBy({ ...DEFAULTS, minSample: 1 });
    expect(reasons).toHaveLength(1);
    expect(reasons[0]).toContain('1 observed arrival');
  });

  it('does not warn when a setting moves in the stricter direction', () => {
    // A tighter filter and more required evidence make the result harder to
    // achieve, not easier, so there is nothing to disclose.
    expect(weakenedBy({ ...DEFAULTS, labelFilter: 60, minSample: 100 })).toEqual([]);
  });

  it('reports both reasons at once', () => {
    expect(weakenedBy({ ...DEFAULTS, labelFilter: 600, minSample: 5 })).toHaveLength(2);
  });

  it('ignores presentation settings, which cannot weaken a measurement', () => {
    expect(
      weakenedBy({
        ...DEFAULTS,
        clock24: true,
        showVehicles: false,
        showStopLabels: false,
        place: 'all',
        refreshMs: 0,
      }),
    ).toEqual([]);
  });
});
