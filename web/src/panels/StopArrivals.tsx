import { useEffect, useState } from 'react';
import { api, type StopUpcoming } from '../api/client';
import {
  correctionIsMeaningful,
  explainCorrection,
  formatArrivalClock,
  formatWait,
  isTrolley,
} from '../lib/eta';

/** Live arrivals at a stop: what MTS says, and what we think it really means.
 *
 *  The corrected figure is MTS's prediction adjusted by the bias measured for this
 *  stop and route. It is a statistical correction, not live model inference, and
 *  the panel says so rather than implying a model is running.
 */
export function StopArrivals({
  stopId,
  onShowEvidence,
}: {
  stopId: string | null;
  onShowEvidence: () => void;
}) {
  const [data, setData] = useState<StopUpcoming | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [tick, setTick] = useState(0);

  // Re-render every 15s so the countdowns stay honest between fetches.
  useEffect(() => {
    const timer = setInterval(() => setTick((value) => value + 1), 15_000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!stopId) {
      setData(null);
      return;
    }
    let active = true;
    setLoading(true);
    const load = () =>
      api
        .upcoming(stopId)
        .then((result) => {
          if (active) {
            setData(result);
            setError(null);
            setLoading(false);
          }
        })
        .catch((cause: unknown) => {
          if (active) {
            setError(cause instanceof Error ? cause.message : 'request failed');
            setLoading(false);
          }
        });
    void load();
    const timer = setInterval(load, 20_000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [stopId]);

  if (!stopId) {
    return (
      <div className="sheet-empty">
        <h2>Pick a stop</h2>
        <p>
          Tap a pin to see what is coming. We show the official MTS estimate and our own,
          corrected using how wrong MTS has actually been at that stop.
        </p>
      </div>
    );
  }

  if (loading && !data) return <div className="sheet-empty">Loading arrivals…</div>;
  if (error) return <div className="sheet-empty">Could not load arrivals: {error}</div>;
  if (!data) return null;

  const now = new Date();
  void tick;

  return (
    <div className="sheet-body">
      <header className="sheet-head">
        <h2>{data.stop_name ?? data.stop_id}</h2>
        <p className="sheet-sub">Stop {data.stop_id}</p>
      </header>

      {data.arrivals.length === 0 ? (
        <p className="sheet-empty">
          Nothing due here right now. Late evening and early morning stops can be quiet for
          a while.
        </p>
      ) : (
        <ul className="arrivals">
          {data.arrivals.map((arrival) => {
            const corrected =
              arrival.corrected_arrival &&
              correctionIsMeaningful(arrival.correction_seconds)
                ? arrival.corrected_arrival
                : null;
            const why = explainCorrection(
              arrival.correction_seconds,
              arrival.correction_basis,
              arrival.correction_sample,
            );
            return (
              <li key={`${arrival.trip_id}-${arrival.stop_sequence}`}>
                <div className="arrival-top">
                  <span
                    className={`route-badge${isTrolley(arrival.route_type) ? ' trolley' : ''}`}
                  >
                    {arrival.route_short_name ?? arrival.route_id ?? '?'}
                  </span>
                  <span className="headsign">
                    {arrival.headsign ?? 'Destination unknown'}
                  </span>
                  <span className="eta-main">
                    {formatWait(corrected ?? arrival.mts_arrival, now)}
                  </span>
                </div>
                <div className="arrival-detail">
                  <span>
                    MTS says {formatWait(arrival.mts_arrival, now)}
                    <span className="clock">
                      {' '}
                      ({formatArrivalClock(arrival.mts_arrival)})
                    </span>
                  </span>
                  {corrected ? (
                    <span className="ours">
                      ours {formatWait(corrected, now)}
                      <span className="clock"> ({formatArrivalClock(corrected)})</span>
                    </span>
                  ) : (
                    <span className="ours muted">no correction, not enough history</span>
                  )}
                </div>
                {why ? (
                  <button className="why" onClick={onShowEvidence}>
                    {why} · see how we measured that
                  </button>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}

      <p className="sheet-foot">
        Our estimate adjusts the live MTS prediction by the error we have measured at this
        stop. It is a statistical correction from recorded history, not a live model.
      </p>
    </div>
  );
}
