import { useCallback, useMemo, useState } from 'react';
import {
  api,
  type Place,
  type Stop,
  type StopSearchResult,
  type TripOption,
} from '../api/client';
import { useApi } from '../lib/useApi';
import { useVehicles } from '../lib/useVehicles';
import { MapView, PLACES, type MapMarker, type MapPlace } from '../map/MapView';
import { useSettings } from '../settings/SettingsContext';
import { PLACE_KEYS } from '../settings/settings';
import { StopArrivals } from '../panels/StopArrivals';
import { StopSearch } from '../panels/StopSearch';
import { TripPlanner, type PickTarget } from '../panels/TripPlanner';

type Tab = 'arrivals' | 'plan';

/** The app. Map first, arrivals beside it on desktop and underneath on a phone. */
export function MapApp({ onShowEvidence }: { onShowEvidence: () => void }) {
  const { settings, set } = useSettings();
  // The place buttons and the settings default are the same control, so the map
  // opens where the setting says and the buttons move the setting.
  const place: MapPlace = PLACES[settings.place] ?? PLACES['ucsd']!;
  const [flyTo, setFlyTo] = useState<{ center: [number, number]; zoom: number } | null>(
    null,
  );
  const [selected, setSelected] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>('arrivals');

  // Trip state lives here rather than in the planner because the map sets it too:
  // a tap in pick mode is a start or a destination.
  const [from, setFrom] = useState<Place | null>(null);
  const [to, setTo] = useState<Place | null>(null);
  const [pick, setPick] = useState<PickTarget | null>(null);
  const [chosen, setChosen] = useState<TripOption | null>(null);

  const stops = useApi<Stop[]>(() => api.stops());
  const vehicles = useVehicles(settings.showVehicles ? settings.refreshMs : 0);

  // A stop tapped while planning is still a request to see that stop's arrivals.
  const onSelect = useCallback((stopId: string) => {
    setSelected(stopId);
    setTab('arrivals');
  }, []);
  const onPick = useCallback((stop: StopSearchResult) => {
    setSelected(stop.stop_id);
    setTab('arrivals');
    setFlyTo({ center: [stop.lon, stop.lat], zoom: 15.5 });
  }, []);

  const planning = tab === 'plan';
  const onMapClick = useMemo(() => {
    if (!planning || !pick) return null;
    return ([lon, lat]: [number, number]) => {
      const place = { lat, lon, label: 'Point on the map' };
      if (pick === 'from') setFrom(place);
      else setTo(place);
      setPick(null);
    };
  }, [planning, pick]);

  const markers = useMemo<MapMarker[]>(() => {
    if (!planning) return [];
    const out: MapMarker[] = [];
    if (from)
      out.push({
        kind: 'from',
        lon: from.lon,
        lat: from.lat,
        title: `Start: ${from.label}`,
      });
    if (to)
      out.push({ kind: 'to', lon: to.lon, lat: to.lat, title: `Destination: ${to.label}` });
    if (chosen) {
      out.push({
        kind: 'board',
        lon: chosen.board.lon,
        lat: chosen.board.lat,
        title: `Board at ${chosen.board.stop_name ?? chosen.board.stop_id}`,
      });
      out.push({
        kind: 'alight',
        lon: chosen.alight.lon,
        lat: chosen.alight.lat,
        title: `Get off at ${chosen.alight.stop_name ?? chosen.alight.stop_id}`,
      });
    }
    return out;
  }, [planning, from, to, chosen]);

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
          showVehicles={settings.showVehicles}
          showStopLabels={settings.showStopLabels}
          onMapClick={onMapClick}
          markers={markers}
        />

        <div className="map-overlay">
          <StopSearch onPick={onPick} />
          <div className="place-buttons">
            {PLACE_KEYS.map((key) => (
              <button
                key={key}
                className="toggle"
                onClick={() => set('place', key)}
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
            {settings.showVehicles
              ? `${vehicles.vehicles.length} vehicles live`
              : 'vehicles hidden'}
            {settings.refreshMs === 0 && settings.showVehicles ? ' · not refreshing' : ''}
            {vehicles.error ? ' · feed unavailable' : ''}
          </span>
        </div>
      </div>

      <aside className="sheet">
        <div className="sheet-tabs" role="tablist">
          {(
            [
              ['arrivals', 'Stop arrivals'],
              ['plan', 'Plan a trip'],
            ] as const
          ).map(([key, label]) => (
            <button
              key={key}
              role="tab"
              aria-selected={tab === key}
              className={tab === key ? 'active' : undefined}
              onClick={() => {
                setTab(key);
                setPick(null);
              }}
            >
              {label}
            </button>
          ))}
        </div>
        {planning ? (
          <TripPlanner
            from={from}
            to={to}
            pick={pick}
            onPick={setPick}
            onFrom={setFrom}
            onTo={setTo}
            chosen={chosen}
            onChosen={setChosen}
          />
        ) : (
          <StopArrivals stopId={selected} onShowEvidence={onShowEvidence} />
        )}
      </aside>
    </div>
  );
}
