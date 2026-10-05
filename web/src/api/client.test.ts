import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from './client';
import { DEFAULTS } from '../settings/settings';

const params = {
  horizon: DEFAULTS.horizon,
  minSample: DEFAULTS.minSample,
  labelFilter: DEFAULTS.labelFilter,
  compare: DEFAULTS.compare,
  timeBand: DEFAULTS.timeBand,
};

/** Capture the URL a client method requests, without a server. */
function capture(): { urls: string[] } {
  const urls: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) => {
      urls.push(url);
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
    }),
  );
  return { urls };
}

afterEach(() => vi.unstubAllGlobals());

describe('api query strings', () => {
  it('sends the label filter wherever it changes the result', async () => {
    const { urls } = capture();
    await api.headline({ ...params, labelFilter: 900 });
    await api.distribution({ ...params, labelFilter: 900 });
    await api.dataQuality({ ...params, labelFilter: 900 });
    await api.routes({ ...params, labelFilter: 900 });
    expect(urls).toHaveLength(4);
    for (const url of urls) expect(url).toContain('max_ping_gap=900');
  });

  it('sends the horizon and the predictor to the endpoints that take them', async () => {
    const { urls } = capture();
    await api.routes({ ...params, horizon: 20, compare: 'segment_mean' });
    expect(urls[0]).toContain('horizon=20');
    expect(urls[0]).toContain('compare=segment_mean');
  });

  it('sends the horizon and minimum sample on upcoming arrivals', async () => {
    const { urls } = capture();
    await api.upcoming('10839', { ...params, horizon: 5, minSample: 1 });
    expect(urls[0]).toContain('horizon=5');
    expect(urls[0]).toContain('min_sample=1');
  });

  it('sends the predictor on a stop detail', async () => {
    const { urls } = capture();
    await api.stopDetail('10839', { ...params, compare: 'persist_delay' });
    expect(urls[0]).toContain('compare=persist_delay');
  });

  // The settings are what the viewer is looking at, so they always travel with
  // the request rather than relying on the server's defaults matching.
  it('always states its parameters rather than defaulting server side', async () => {
    const { urls } = capture();
    await api.headline(params);
    expect(urls[0]).toContain('max_ping_gap=');
  });

  it('escapes a stop id instead of pasting it into the path', async () => {
    const { urls } = capture();
    await api.upcoming('a b/c', params);
    expect(urls[0]).toContain('a%20b%2Fc');
  });
});

describe('time of day band', () => {
  it('sends the band on every endpoint whose numbers it changes', async () => {
    const { urls } = capture();
    await api.headline({ ...params, timeBand: 'pm_rush' });
    await api.routes({ ...params, timeBand: 'pm_rush' });
    await api.distribution({ ...params, timeBand: 'pm_rush' });
    expect(urls).toHaveLength(3);
    for (const url of urls) expect(url).toContain('time_band=pm_rush');
  });

  it('sends the default band rather than omitting the parameter', async () => {
    // Omitting it would work, since the API defaults to all day, but then the
    // request for all-day numbers and the request for a band would differ in
    // shape rather than in one value, which is harder to read in a network log.
    const { urls } = capture();
    await api.headline(params);
    expect(urls[0]).toContain('time_band=all');
  });
});
