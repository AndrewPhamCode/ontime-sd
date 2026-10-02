import { api, type Headline as HeadlineData } from '../api/client';
import { deltaVsMts, formatCount, formatSignedMinutes } from '../lib/format';
import { useParams } from '../settings/useParams';
import { useApi } from '../lib/useApi';
import { Card, Empty, Failed, Pending } from './Card';

/** How far ahead of MTS we actually are, per lead time.
 *
 *  This panel exists to make the honest answer unmissable: the bars are tiny.
 *  Direction uses status colour, and every bar carries an icon and a signed
 *  number, so the state never rests on colour alone. */
export function DeltaPanel() {
  const params = useParams();
  const { data, error, loading } = useApi<HeadlineData>(
    () => api.headline(params),
    [params],
  );

  if (loading)
    return (
      <Card title="Are we actually better?">
        <Pending what="the comparison" />
      </Card>
    );
  if (error)
    return (
      <Card title="Are we actually better?">
        <Failed error={error} />
      </Card>
    );

  const deltas = data ? deltaVsMts(data.rows) : [];
  if (deltas.length === 0) {
    return (
      <Card title="Are we actually better?">
        <Empty what="model predictions" />
      </Card>
    );
  }

  const widest = Math.max(...deltas.map((d) => Math.abs(d.delta)), 30);

  return (
    <Card
      title="Are we actually better?"
      note="Our error subtracted from MTS's, per lead time. Positive means we are ahead. The bars are small on purpose: this is parity, not a win, and a few percent on five days of data is inside the noise."
    >
      <div style={{ display: 'grid', gap: 10 }}>
        {deltas.map(({ horizon, delta }) => {
          const better = delta > 0;
          const color = better ? 'var(--status-good)' : 'var(--status-critical)';
          const width = `${Math.min((Math.abs(delta) / widest) * 100, 100)}%`;
          return (
            <div
              key={horizon}
              style={{ display: 'grid', gridTemplateColumns: '72px 1fr 160px', gap: 10 }}
            >
              <span style={{ color: 'var(--ink-secondary)', fontSize: 13 }}>
                {horizon} min
              </span>
              <div
                style={{
                  background: 'var(--grid)',
                  borderRadius: 4,
                  position: 'relative',
                  height: 18,
                }}
              >
                <div
                  style={{
                    width,
                    height: '100%',
                    background: color,
                    borderRadius: 4,
                  }}
                />
              </div>
              <span style={{ fontSize: 13, color: 'var(--ink-secondary)' }}>
                <span aria-hidden="true">{better ? '▲' : '▼'}</span>{' '}
                {better ? 'better by' : 'worse by'} {formatSignedMinutes(Math.abs(delta))}{' '}
                min
              </span>
            </div>
          );
        })}
      </div>
      <p className="note">
        Measured on {formatCount(deltas[0]?.n)} arrivals per lead time. Bar length is scaled
        to the largest difference shown, which is itself under half a minute.
      </p>
    </Card>
  );
}
