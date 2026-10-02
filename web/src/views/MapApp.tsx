import { useCallback, useState } from 'react';
import { api, type Stop, type StopSearchResult } from '../api/client';
import { useApi } from '../lib/useApi';
import { useVehicles } from '../lib/useVehicles';
import { MapView, PLACES, type MapPlace } from '../map/MapView';
import { StopArrivals } from '../panels/StopArrivals';
import { StopSearch } from '../panels/StopSearch';

/** The app. Map first, arrivals beside it on desktop and underneath on a phone. */
export function MapApp({ onShowEvidence }: { onShowEvidence: () => void }) {
  const [place, setPlace] = useState<MapPlace>(PLACES['ucsd']!);
  const [flyTo, setFlyTo] = useState<{ center: [number, number]; zoom: number } | null>(
    null,
  );
  const [selected, setSelected] = useState<string | null>(null);

  const stops = useApi<Stop[]>(() => api.stops());
  const vehicles = useVehicles(10_000);

  const onSelect = useCallback((stopId: string) => setSelected(stopId), []);
  const onPick = useCallback((stop: StopSearchResult) => {
    setSelected(stop.stop_id);
    setFlyTo({ center: [stop.lon, stop.lat], zoom: 15.5 });
  }, []);

  return (
    <div className="app-stage">
      <div className="map-stage">
        <MapView
          stops={stops.data ?? []}
          vehicles={vehicles.vehicles}
          selected={selected}
          onSelect={onSelect}
          place={place}
          flyTo={flyTo}
        />

        <div className="map-overlay">
          <StopSearch onPick={onPick} />
          <div className="place-buttons">
            {(['ucsd', 'downtown', 'all'] as const).map((key) => (
              <button
                key={key}
                className="toggle"
                onClick={() => setPlace(PLACES[key]!)}
                style={{
                  fontWeight: place.label === PLACES[key]!.label ? 600 : 400,
                  borderColor:
                    place.label === PLACES[key]!.label
                      ? 'var(--series-lgbm)'
                      : 'var(--border)',
                }}
              >
                {PLACES[key]!.label}
              </button>
            ))}
          </div>
        </div>

        <div className="map-legend">
          <span>
            <i className="dot stop" /> stop
          </span>
          <span>
            <i className="dot bus" /> bus
          </span>
          <span>
            <i className="dot trolley" /> trolley
          </span>
          <span className="muted">
            {vehicles.vehicles.length} vehicles live
            {vehicles.error ? ' · feed unavailable' : ''}
          </span>
        </div>
      </div>

      <aside className="sheet">
        <StopArrivals stopId={selected} onShowEvidence={onShowEvidence} />
      </aside>
    </div>
  );
}
