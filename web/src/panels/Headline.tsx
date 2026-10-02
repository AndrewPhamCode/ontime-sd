import { api, type Headline as HeadlineData } from '../api/client';
import {
  SOURCES,
  SOURCE_COLORS,
  SOURCE_LABELS,
  bestSourceAt,
  byHorizon,
  formatCount,
  formatMinutes,
  formatSignedMinutes,
} from '../lib/format';
import { useApi } from '../lib/useApi';
import { Card, Empty, Failed, Pending } from './Card';

/** The headline. Our model is shown beside MTS and both baselines in the same
 *  table, because the honest result is parity and a single figure would hide
 *  that. N appears on every row. */
export function Headline() {
  const { data, error, loading } = useApi<HeadlineData>(() => api.headline());

  if (loading)
    return (
      <Card title="Prediction error">
        <Pending what="the headline" />
      </Card>
    );
  if (error)
    return (
      <Card title="Prediction error">
        <Failed error={error} />
      </Card>
    );
  if (!data || data.rows.length === 0) {
    return (
      <Card title="Prediction error">
        <Empty what="scored predictions" />
      </Card>
    );
  }

  const indexed = byHorizon(data.rows);
  const horizons = [...indexed.keys()].sort((a, b) => a - b);
  const tiles = horizons.map((horizon) => {
    const bucket = indexed.get(horizon) ?? {};
    return { horizon, mts: bucket.mts, ours: bucket.lgbm };
  });

  return (
    <Card
      title="Prediction error by lead time"
      note={`Mean absolute error in minutes, over ${data.window.test_from} to ${data.window.test_to}. Restricted to arrivals whose GPS evidence is within ${data.window.test_from ? data.label_filter_seconds : 0} seconds, so this measures the predictors rather than our own interpolation. Lower is better.`}
      aside={<span className="pill">N = {formatCount(tiles[0]?.mts?.n)} per horizon</span>}
    >
      <div className="tiles">
        {tiles.map(({ horizon, mts, ours }) => (
          <div className="tile" key={horizon}>
            <div className="label">{horizon} min ahead</div>
            <div className="value">
              {formatMinutes(ours?.mae_seconds)}
              <span className="unit">min</span>
            </div>
            <div className="sub">MTS {formatMinutes(mts?.mae_seconds)} min</div>
          </div>
        ))}
      </div>

      <table style={{ marginTop: 18 }}>
        <thead>
          <tr>
            <th>Predictor</th>
            {horizons.map((h) => (
              <th key={h}>{h} min</th>
            ))}
            <th>p90 @10</th>
            <th>Bias @10</th>
          </tr>
        </thead>
        <tbody>
          {SOURCES.map((source) => {
            const at10 = indexed.get(10)?.[source];
            return (
              <tr key={source}>
                <td>
                  <span
                    className="swatch"
                    style={{ background: SOURCE_COLORS[source] }}
                    aria-hidden="true"
                  />
                  {SOURCE_LABELS[source]}
                </td>
                {horizons.map((horizon) => {
                  const row = indexed.get(horizon)?.[source];
                  const isBest = bestSourceAt(data.rows, horizon) === source;
                  return (
                    <td key={horizon} className={isBest ? 'best' : undefined}>
                      {formatMinutes(row?.mae_seconds)}
                    </td>
                  );
                })}
                <td>{formatMinutes(at10?.p90_seconds)}</td>
                <td>{formatSignedMinutes(at10?.bias_seconds)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="note">
        Bold is the lowest error at that lead time. Bias is signed: negative means
        predicting the bus arrives earlier than it does.
      </p>
    </Card>
  );
}
