/** Typed access to the API.
 *
 *  Types come from `schema.d.ts`, which is generated from the API's own OpenAPI
 *  document by `npm run gen:types`. Renaming a field on the Python side therefore
 *  breaks the type check rather than quietly rendering `undefined` on screen,
 *  which is the main thing TypeScript is here to do.
 */
import type { components } from './schema';

type Schemas = components['schemas'];

export type Headline = Schemas['Headline'];
export type SourceHorizon = Schemas['SourceHorizon'];
export type Window = Schemas['Window'];
export type RouteComparison = Schemas['RouteComparison'];
export type DistributionBucket = Schemas['DistributionBucket'];
export type DataQuality = Schemas['DataQuality'];
export type ModelRun = Schemas['ModelRun'];
export type Stop = Schemas['Stop'];
export type StopDetail = Schemas['StopDetail'];
export type Vehicle = Schemas['Vehicle'];
export type StopUpcoming = Schemas['StopUpcoming'];
export type UpcomingArrival = Schemas['UpcomingArrival'];
export type StopSearchResult = Schemas['StopSearchResult'];

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function get<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: { accept: 'application/json' } });
  if (!response.ok) {
    throw new ApiError(response.status, `${path} returned ${response.status}`);
  }
  return (await response.json()) as T;
}

/** Parameters that change what the API computes.
 *
 *  These mirror the settings exactly. They are always sent explicitly rather
 *  than relying on the server defaults, so what is on screen is always a
 *  function of what the panel says, and the API's own caching is keyed on the
 *  same values.
 */
export interface Params {
  horizon: number;
  minSample: number;
  labelFilter: number;
  compare: string;
}

const query = (parts: Record<string, string | number>) =>
  Object.entries(parts)
    .map(([key, value]) => `${key}=${encodeURIComponent(String(value))}`)
    .join('&');

export const api = {
  headline: (p: Params) =>
    get<Headline>(`/api/headline?${query({ max_ping_gap: p.labelFilter })}`),
  window: () => get<Window>('/api/window'),
  routes: (p: Params) =>
    get<RouteComparison[]>(
      `/api/routes?${query({
        horizon: p.horizon,
        max_ping_gap: p.labelFilter,
        compare: p.compare,
      })}`,
    ),
  distribution: (p: Params) =>
    get<DistributionBucket[]>(
      `/api/error-distribution?${query({
        horizon: p.horizon,
        max_ping_gap: p.labelFilter,
      })}`,
    ),
  dataQuality: (p: Params) =>
    get<DataQuality>(`/api/data-quality?${query({ max_ping_gap: p.labelFilter })}`),
  modelRun: () => get<ModelRun | null>('/api/model-run'),
  stops: () => get<Stop[]>('/api/stops?limit=1500'),
  stopDetail: (stopId: string, p: Params) =>
    get<StopDetail>(
      `/api/stops/${encodeURIComponent(stopId)}?${query({
        horizon: p.horizon,
        compare: p.compare,
      })}`,
    ),
  vehicles: () => get<Vehicle[]>('/api/vehicles'),
  upcoming: (stopId: string, p: Params) =>
    get<StopUpcoming>(
      `/api/stops/${encodeURIComponent(stopId)}/upcoming?${query({
        horizon: p.horizon,
        min_sample: p.minSample,
      })}`,
    ),
  searchStops: (query_: string) =>
    get<StopSearchResult[]>(`/api/stops/search?q=${encodeURIComponent(query_)}`),
};
