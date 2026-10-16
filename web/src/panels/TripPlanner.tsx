import { useEffect, useState, type ReactNode } from 'react';
import { api, type Place, type TripOption, type TripPlan } from '../api/client';
import { formatArrivalClock, isTrolley } from '../lib/eta';
import {
  arriveByIso,
  describeDeparture,
  describeMargin,
  leaveLabel,
  planKey,
  walkMinutes,
} from '../lib/trip';
import { useSettings } from '../settings/SettingsContext';
import { StopSearch } from './StopSearch';

export type PickTarget = 'from' | 'to';

type Located = 'idle' | 'locating' | 'denied' | 'unavailable';

/** Leave-now plans move with the clock and with live estimates, so they refresh
 *  while the panel is open. Slower than the arrivals panel because a plan is
 *  read once and acted on, not watched. */
const REFRESH_MS = 30_000;

/** Where to go, which stop to walk to, and when to be there. Direct rides only.
 *
 *  The times come from `/api/trip-plan`. "Be there by" is the departure minus a
 *  margin from how early buses on that route have actually run, which the card
 *  states, so a rider can see why it asks them to be early.
 */
export function TripPlanner({
  from,
  to,
  pick,
  onPick,
  onFrom,
  onTo,
  chosen,
  onChosen,
}: {
  from: Place | null;
  to: Place | null;
  pick: PickTarget | null;
  onPick: (target: PickTarget | null) => void;
  onFrom: (place: Place) => void;
  onTo: (place: Place) => void;
  chosen: TripOption | null;
  onChosen: (option: TripOption | null) => void;
}) {
  const { settings } = useSettings();
  const [arriveAt, setArriveAt] = useState('');
  const [located, setLocated] = useState<Located>('idle');
  const [result, setResult] = useState<{
    key: string;
    plan: TripPlan | null;
    error: string | null;
  } | null>(null);
  const [tick, setTick] = useState(0);

  const arriveBy = arriveAt ? arriveByIso(arriveAt) : null;
  const key = planKey(from, to, arriveBy);

  const locate = () => {
    if (!('geolocation' in navigator)) {
      setLocated('unavailable');
      return;
    }
    setLocated('locating');
    navigator.geolocation.getCurrentPosition(
      (position) => {
        setLocated('idle');
        onFrom({
          lat: position.coords.latitude,
          lon: position.coords.longitude,
          label: 'My location',
        });
      },
      (failure) =>
        setLocated(failure.code === failure.PERMISSION_DENIED ? 'denied' : 'unavailable'),
      { enableHighAccuracy: true, timeout: 10_000, maximumAge: 60_000 },
    );
  };

  // Start from where the rider is, once, when the panel first opens without a start.
  // Deliberately mount only: re-running when `from` changes would override a start
  // the rider just chose by hand.
  useEffect(() => {
    if (!from) locate();
  }, []);

  useEffect(() => {
    const timer = setInterval(() => setTick((value) => value + 1), 15_000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!key || !from || !to) {
      setResult(null);
      return;
    }
    let active = true;
    const load = () =>
      api
        .tripPlan(from, to, arriveBy)
        .then((plan) => {
          if (active) setResult({ key, plan, error: null });
        })
        .catch((cause: unknown) => {
          if (active) {
            setResult({
              key,
              plan: null,
              error: cause instanceof Error ? cause.message : 'request failed',
            });
          }
        });
    void load();
    const timer = setInterval(load, REFRESH_MS);
    return () => {
      active = false;
      clearInterval(timer);
    };
    // `key` is the identity of from, to and arriveBy, so it alone decides a refetch.
    // Listing the objects too would refetch whenever a parent re-renders them.
  }, [key]);

  // A result for other inputs is never shown as if it answered these ones.
  const current = result && result.key === key ? result : null;
  const options = current?.plan?.options ?? [];

  // Keep the highlighted option in step with the answer on screen.
  useEffect(() => {
    if (options.length === 0) {
      if (chosen) onChosen(null);
      return;
    }
    const still = chosen && options.find((option) => option.trip_id === chosen.trip_id);
    if (!still) onChosen(options[0]!);
    else if (still !== chosen) onChosen(still);
    // Runs per answer, not per highlight change, or picking a card would loop.
  }, [current]);

  const clock = (iso: string) => formatArrivalClock(iso, !settings.clock24);
  const now = new Date();
  void tick;

  return (
    <div className="sheet-body">
      <header className="sheet-head">
        <h2>Plan a trip</h2>
        <p className="sheet-sub">Direct rides only: one bus or trolley, no transfers.</p>
      </header>

      <TripField
        label="From"
        place={from}
        picking={pick === 'from'}
        onPick={() => onPick(pick === 'from' ? null : 'from')}
        onStop={onFrom}
      >
        <button className="toggle" onClick={locate} disabled={located === 'locating'}>
          {located === 'locating' ? 'Locating…' : 'My location'}
        </button>
      </TripField>
      {located === 'denied' || located === 'unavailable' ? (
        <p className="trip-note" role="status">
          {located === 'denied'
            ? 'Location is blocked for this site. Tap the map or search a stop to set your start.'
            : 'Could not find your location. Tap the map or search a stop to set your start.'}
        </p>
      ) : null}

      <TripField
        label="To"
        place={to}
        picking={pick === 'to'}
        onPick={() => onPick(pick === 'to' ? null : 'to')}
        onStop={onTo}
      />

      <div className="trip-arrive">
        <label>
          Arrive by{' '}
          <input
            type="time"
            value={arriveAt}
            onChange={(event) => setArriveAt(event.target.value)}
          />
        </label>
        {arriveAt ? (
          <button className="why" onClick={() => setArriveAt('')}>
            leave now instead
          </button>
        ) : (
          <span className="muted">optional, otherwise leave now</span>
        )}
      </div>

      {pick ? (
        <p className="trip-note" role="status">
          Tap the map to set your {pick === 'from' ? 'start' : 'destination'}.
        </p>
      ) : null}

      {!from || !to ? null : !current ? (
        <p className="sheet-empty">Finding rides…</p>
      ) : current.error ? (
        <p className="sheet-empty">Could not plan this trip: {current.error}</p>
      ) : options.length === 0 ? (
        <p className="sheet-empty">{current.plan?.message ?? 'No direct ride found.'}</p>
      ) : (
        <ul className="arrivals trip-options">
          {options.map((option) => (
            <li
              key={`${option.trip_id}-${option.board.stop_id}`}
              className={chosen?.trip_id === option.trip_id ? 'chosen' : undefined}
              onClick={() => onChosen(option)}
            >
              <div className="arrival-top">
                <span
                  className={`route-badge${isTrolley(option.route_type) ? ' trolley' : ''}`}
                >
                  {option.route_short_name ?? option.route_id ?? '?'}
                </span>
                <span className="headsign">{option.headsign ?? 'Destination unknown'}</span>
                <span className="eta-main">{leaveLabel(option.leave_by, now)}</span>
              </div>
              <ol className="trip-steps">
                <li>
                  Walk {walkMinutes(option.walk_to_stop_seconds)} min to{' '}
                  <strong>{option.board.stop_name ?? option.board.stop_id}</strong>, be
                  there by <strong>{clock(option.be_at_stop_by)}</strong>
                </li>
                <li>
                  Departs {clock(option.departs_at)}{' '}
                  <span className="muted">
                    ({describeDeparture(option.departure_basis)})
                  </span>
                  , {option.stops_ridden} {option.stops_ridden === 1 ? 'stop' : 'stops'}
                </li>
                <li>
                  Off at {option.alight.stop_name ?? option.alight.stop_id}{' '}
                  {clock(option.arrives_at)}, walk{' '}
                  {walkMinutes(option.walk_from_stop_seconds)} min, arrive{' '}
                  <strong>{clock(option.arrive_at_destination)}</strong>
                </li>
              </ol>
              <p className="trip-margin">
                Be there {describeMargin(option.margin_seconds, option.margin_basis)}.
              </p>
            </li>
          ))}
        </ul>
      )}

      <p className="sheet-foot">
        Walking times are straight line distance with an allowance for streets. Live times
        are used when MTS is tracking the bus; trips that have not started yet show the
        timetable.
      </p>
    </div>
  );
}

function TripField({
  label,
  place,
  picking,
  onPick,
  onStop,
  children,
}: {
  label: string;
  place: Place | null;
  picking: boolean;
  onPick: () => void;
  onStop: (place: Place) => void;
  children?: ReactNode;
}) {
  return (
    <div className="trip-field">
      <div className="trip-field-head">
        <span className="trip-field-label">{label}</span>
        <span className={place ? 'trip-place' : 'trip-place muted'}>
          {place ? place.label : 'not set'}
        </span>
      </div>
      <div className="trip-field-row">
        <StopSearch
          placeholder={
            label === 'From' ? 'Search a start stop' : 'Search a destination stop'
          }
          onPick={(stop) =>
            onStop({ lat: stop.lat, lon: stop.lon, label: stop.stop_name ?? stop.stop_id })
          }
        />
        <button className={`toggle${picking ? ' modified' : ''}`} onClick={onPick}>
          {picking ? 'Cancel' : 'Pick on map'}
        </button>
        {children}
      </div>
    </div>
  );
}
