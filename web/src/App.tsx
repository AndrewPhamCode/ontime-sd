import { useCallback, useEffect, useState } from 'react';
import { api, type Stop, type Window } from './api/client';
import { HORIZONS, formatCount } from './lib/format';
import { useApi } from './lib/useApi';
import { useVehicles } from './lib/useVehicles';
import { Card } from './panels/Card';
import { DataQuality } from './panels/DataQuality';
import { DeltaPanel } from './panels/DeltaPanel';
import { ErrorDistribution } from './panels/ErrorDistribution';
import { Headline } from './panels/Headline';
import { HorizonChart } from './panels/HorizonChart';
import { MethodNote } from './panels/MethodNote';
import { RouteTable } from './panels/RouteTable';
import { StopDetail } from './panels/StopDetail';
import { StopMap, VIEWS } from './panels/StopMap';

type Theme = 'light' | 'dark';

function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(() =>
    window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light',
  );
  useEffect(() => {
    document.documentElement.dataset['theme'] = theme;
  }, [theme]);
  return [theme, () => setTheme((current) => (current === 'dark' ? 'light' : 'dark'))];
}

export function App() {
  const [theme, toggleTheme] = useTheme();
  const [horizon, setHorizon] = useState<number>(10);
  const [selectedStop, setSelectedStop] = useState<string | null>(null);
  const [viewKey, setViewKey] = useState<'ucsd' | 'all'>('ucsd');

  const window_ = useApi<Window>(() => api.window());
  const stops = useApi<Stop[]>(() => api.stops());
  // Polled rather than fetched once, so the buses actually move.
  const vehicles = useVehicles(10_000);
  const view = VIEWS[viewKey]!;

  const onSelect = useCallback((stopId: string) => setSelectedStop(stopId), []);

  return (
    <div className="page">
      <div className="masthead">
        <div>
          <h1>OnTime SD</h1>
          <p className="subtitle">
            Can a model predict San Diego MTS arrivals better than MTS does? This page
            measures the agency's own predictions against arrivals reconstructed from raw
            GPS, then puts a gradient boosted model beside them. Honest answer so far:
            parity, not a win.
          </p>
        </div>
        <button className="toggle" onClick={toggleTheme}>
          {theme === 'dark' ? 'Light' : 'Dark'} mode
        </button>
      </div>

      {window_.data ? (
        <p className="mono" style={{ marginTop: 12 }}>
          evaluation window {window_.data.test_from} → {window_.data.test_to} ·{' '}
          {window_.data.source_of_window}
          {stops.data ? ` · ${formatCount(stops.data.length)} stops observed` : ''}
          {vehicles.vehicles.length
            ? ` · ${formatCount(vehicles.vehicles.length)} vehicles reporting now`
            : ''}
        </p>
      ) : null}

      <Headline />
      <HorizonChart />
      <DeltaPanel />

      <div className="controls" style={{ marginTop: 20 }}>
        <label htmlFor="horizon">Lead time for the panels below</label>
        <select
          id="horizon"
          value={horizon}
          onChange={(event) => setHorizon(Number(event.target.value))}
        >
          {HORIZONS.map((value) => (
            <option key={value} value={value}>
              {value} minutes ahead
            </option>
          ))}
        </select>
      </div>

      <ErrorDistribution horizon={horizon} />
      <RouteTable horizon={horizon} />

      <Card
        title="Live map: predicted against what actually happened"
        note="Green dots are buses reporting right now, refreshed every 10 seconds. Blue dots are stops we have observed arrivals at. Click one to see, for each recent arrival, the time MTS predicted and the time the bus actually turned up."
        aside={
          <span className="pill">
            <span aria-hidden="true" style={{ color: 'var(--status-good)' }}>
              ●
            </span>{' '}
            {formatCount(vehicles.vehicles.length)} vehicles live
          </span>
        }
      >
        <div className="controls">
          {(['ucsd', 'all'] as const).map((key) => (
            <button
              key={key}
              className="toggle"
              onClick={() => setViewKey(key)}
              style={{
                fontWeight: viewKey === key ? 600 : 400,
                borderColor: viewKey === key ? 'var(--series-lgbm)' : 'var(--border)',
              }}
            >
              {VIEWS[key]!.label}
            </button>
          ))}
          {vehicles.error ? (
            <span style={{ fontSize: 13, color: 'var(--status-critical)' }}>
              live feed unavailable: {vehicles.error}
            </span>
          ) : null}
        </div>
        <div className="two-col">
          <StopMap
            stops={stops.data ?? []}
            vehicles={vehicles.vehicles}
            selected={selectedStop}
            onSelect={onSelect}
            view={view}
          />
          <StopDetail stopId={selectedStop} horizon={horizon} />
        </div>
      </Card>

      <DataQuality />
      <MethodNote />

      <p className="note" style={{ marginTop: 28 }}>
        Collector, schedule loader, arrival inference, evaluation and model are all in the
        repository, with every architectural decision and its rejected alternatives
        recorded. Data from San Diego Metropolitan Transit System.
      </p>
    </div>
  );
}
