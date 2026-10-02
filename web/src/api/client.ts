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

export const api = {
  headline: () => get<Headline>('/api/headline'),
  window: () => get<Window>('/api/window'),
  routes: (horizon: number) => get<RouteComparison[]>(`/api/routes?horizon=${horizon}`),
  distribution: (horizon: number) =>
    get<DistributionBucket[]>(`/api/error-distribution?horizon=${horizon}`),
  dataQuality: () => get<DataQuality>('/api/data-quality'),
  modelRun: () => get<ModelRun | null>('/api/model-run'),
  stops: () => get<Stop[]>('/api/stops?limit=1500'),
  stopDetail: (stopId: string, horizon: number) =>
    get<StopDetail>(`/api/stops/${encodeURIComponent(stopId)}?horizon=${horizon}`),
  vehicles: () => get<Vehicle[]>('/api/vehicles'),
};
