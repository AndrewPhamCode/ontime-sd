import { api, type StopDetail as StopDetailData } from '../api/client';
import { SOURCE_COLORS, formatClock, formatSignedMinutes } from '../lib/format';
import { useApi } from '../lib/useApi';
import { Failed, Pending } from './Card';

/** One stop, concretely: scheduled, what MTS said, what we said, what happened.
 *
 *  This is the panel that makes the aggregate believable. A visitor can see an
 *  individual case and check the arithmetic. */
export function StopDetail({
  stopId,
  horizon,
}: {
  stopId: string | null;
  horizon: number;
}) {
  const { data, error, loading } = useApi<StopDetailData | null>(
    () => (stopId ? api.stopDetail(stopId, horizon) : Promise.resolve(null)),
    [stopId, horizon],
  );

  if (!stopId) {
    return (
      <p className="empty">
        Click a stop on the map to see individual arrivals: what was scheduled, what MTS
        predicted {horizon} minutes out, what we predicted, and what actually happened.
      </p>
    );
  }
  if (loading) return <Pending what={`arrivals at ${stopId}`} />;
  if (error) return <Failed error={error} />;
  if (!data || data.recent.length === 0) {
    return (
      <p className="empty">
        No scored arrivals at this stop in the evaluation window. Try a busier stop.
      </p>
    );
  }

  return (
    <div>
      <h3 style={{ fontSize: 15, marginBottom: 2 }}>{data.stop_name ?? data.stop_id}</h3>
      <p className="note" style={{ marginTop: 0 }}>
        Stop {data.stop_id} · predictions as they stood {horizon} minutes before each
        arrival
      </p>
      <div style={{ maxHeight: 320, overflowY: 'auto' }}>
        <table>
          <thead>
            <tr>
              <th>Route</th>
              <th>Scheduled</th>
              <th>
                <span
                  className="swatch"
                  style={{ background: SOURCE_COLORS.mts }}
                  aria-hidden="true"
                />
                MTS
              </th>
              <th>
                <span
                  className="swatch"
                  style={{ background: SOURCE_COLORS.lgbm }}
                  aria-hidden="true"
                />
                Ours
              </th>
              <th>Actual</th>
              <th>MTS off by</th>
              <th>We were off by</th>
            </tr>
          </thead>
          <tbody>
            {data.recent.map((arrival) => (
              <tr key={`${arrival.trip_id}-${arrival.stop_sequence}-${arrival.arrived_at}`}>
                <td>{arrival.route_id ?? '—'}</td>
                <td>{formatClock(arrival.scheduled_at)}</td>
                <td>{formatClock(arrival.mts_predicted)}</td>
                <td>{formatClock(arrival.model_predicted)}</td>
                <td>
                  <strong>{formatClock(arrival.arrived_at)}</strong>
                </td>
                <td>{formatSignedMinutes(arrival.mts_error_seconds)}</td>
                <td>{formatSignedMinutes(arrival.model_error_seconds)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="note">
        Times are local. Error columns are in minutes, signed: negative means the prediction
        was earlier than the bus actually arrived.
      </p>
    </div>
  );
}
