import { api, type DataQuality as DataQualityData } from '../api/client';
import { coverageStatus, formatCount } from '../lib/format';
import { useParams } from '../settings/useParams';
import { useApi } from '../lib/useApi';
import { Card, Failed, Pending } from './Card';

const STATUS_COLOR = {
  good: 'var(--status-good)',
  warning: 'var(--status-warning)',
  critical: 'var(--status-critical)',
} as const;

const STATUS_ICON = { good: '●', warning: '▲', critical: '■' } as const;

/** The unflattering panel, shown rather than buried in a README.
 *
 *  Coverage is the share of the day the collector was actually running, and it is
 *  poor: the machine is a laptop that sleeps. Label quality is how much GPS
 *  evidence sits behind each inferred arrival. Both bound what any number on this
 *  page can mean, so both are stated. */
export function DataQuality() {
  const params = useParams();
  const { data, error, loading } = useApi<DataQualityData>(
    () => api.dataQuality(params),
    [params],
  );

  if (loading)
    return (
      <Card title="Data quality">
        <Pending what="coverage" />
      </Card>
    );
  if (error)
    return (
      <Card title="Data quality">
        <Failed error={error} />
      </Card>
    );
  if (!data) return null;

  const worstCoverage = Math.max(...data.coverage.map((d) => 100 - d.coverage_pct), 0);

  return (
    <Card
      title="Data quality, stated rather than hidden"
      note="Every figure above rests on these. Collection runs on a laptop, arrival times are inferred from GPS, and both facts limit what the metric can prove."
      aside={
        <span className="pill">{formatCount(data.arrivals_total)} arrivals total</span>
      }
    >
      <h3 style={{ fontSize: 13, color: 'var(--ink-secondary)', marginBottom: 8 }}>
        Collection coverage per service day
      </h3>
      <div style={{ display: 'grid', gap: 7 }}>
        {data.coverage.map((day) => {
          const status = coverageStatus(day.coverage_pct);
          return (
            <div
              key={day.day}
              style={{ display: 'grid', gridTemplateColumns: '98px 1fr 132px', gap: 10 }}
            >
              <span style={{ fontSize: 13, color: 'var(--ink-secondary)' }}>{day.day}</span>
              <div style={{ background: 'var(--grid)', borderRadius: 4, height: 16 }}>
                <div
                  style={{
                    width: `${Math.max(Math.min(day.coverage_pct, 100), 0)}%`,
                    height: '100%',
                    background: STATUS_COLOR[status],
                    borderRadius: 4,
                  }}
                />
              </div>
              <span style={{ fontSize: 13, color: 'var(--ink-secondary)' }}>
                <span aria-hidden="true" style={{ color: STATUS_COLOR[status] }}>
                  {STATUS_ICON[status]}
                </span>{' '}
                {day.coverage_pct.toFixed(0)}% · {day.hours_lost.toFixed(1)}h lost
              </span>
            </div>
          );
        })}
      </div>

      <h3 style={{ fontSize: 13, color: 'var(--ink-secondary)', margin: '20px 0 8px' }}>
        Label quality: GPS evidence behind each inferred arrival
      </h3>
      <table>
        <thead>
          <tr>
            <th>Gap between surrounding GPS fixes</th>
            <th>Arrivals</th>
            <th>Share</th>
          </tr>
        </thead>
        <tbody>
          {data.label_bands.map((band) => (
            <tr key={band.band}>
              <td>{band.band}</td>
              <td>{formatCount(band.n)}</td>
              <td>{band.pct.toFixed(1)}%</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3 style={{ fontSize: 13, color: 'var(--ink-secondary)', margin: '20px 0 4px' }}>
        What this means for the numbers above
      </h3>
      <ul className="caveats">
        {data.caveats.map((caveat) => (
          <li key={caveat}>{caveat}</li>
        ))}
        <li>
          Worst day lost {worstCoverage.toFixed(0)}% of its collection window, so the
          per-hour breakdowns are thin in the hours the machine was asleep.
        </li>
      </ul>
    </Card>
  );
}
