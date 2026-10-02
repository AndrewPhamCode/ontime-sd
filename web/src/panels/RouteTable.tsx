import { useState } from 'react';
import { api, type RouteComparison } from '../api/client';
import { formatCount, formatMinutes, formatSignedMinutes } from '../lib/format';
import { useApi } from '../lib/useApi';
import { Card, Empty, Failed, Pending } from './Card';

type SortKey = 'mts' | 'ours' | 'delta' | 'n';

/** Per route, where we gain and where we lose. A table rather than a chart:
 *  the job here is identity and lookup across many rows, which a chart does badly. */
export function RouteTable({ horizon }: { horizon: number }) {
  const [sort, setSort] = useState<SortKey>('mts');
  const { data, error, loading } = useApi<RouteComparison[]>(
    () => api.routes(horizon),
    [horizon],
  );

  if (loading)
    return (
      <Card title="By route">
        <Pending what="route breakdown" />
      </Card>
    );
  if (error)
    return (
      <Card title="By route">
        <Failed error={error} />
      </Card>
    );
  if (!data || data.length === 0) {
    return (
      <Card title="By route">
        <Empty what="routes with enough observations" />
      </Card>
    );
  }

  const sorted = [...data].sort((a, b) => {
    switch (sort) {
      case 'ours':
        return (b.model_mae_seconds ?? 0) - (a.model_mae_seconds ?? 0);
      case 'delta':
        return (b.improvement_seconds ?? 0) - (a.improvement_seconds ?? 0);
      case 'n':
        return b.n - a.n;
      default:
        return b.mts_mae_seconds - a.mts_mae_seconds;
    }
  });

  const header = (key: SortKey, label: string) => (
    <th>
      <button
        onClick={() => setSort(key)}
        className="toggle"
        style={{
          padding: '2px 8px',
          fontSize: 11,
          textTransform: 'uppercase',
          letterSpacing: '0.05em',
          fontWeight: sort === key ? 600 : 400,
        }}
      >
        {label}
      </button>
    </th>
  );

  return (
    <Card
      title={`By route, at ${horizon} minutes ahead`}
      note="Rail is predicted far better than road: the trolley lines sit near the top of the easy end because they do not sit in traffic. Routes with fewer than 100 observations are omitted rather than shown as noise."
    >
      <div style={{ maxHeight: 420, overflowY: 'auto' }}>
        <table>
          <thead>
            <tr>
              <th>Route</th>
              {header('n', 'N')}
              {header('mts', 'MTS')}
              {header('ours', 'Ours')}
              {header('delta', 'Gain')}
            </tr>
          </thead>
          <tbody>
            {sorted.map((route) => (
              <tr key={route.route_id}>
                <td>
                  <strong>{route.route_id}</strong>
                  {route.route_name ? (
                    <span style={{ color: 'var(--ink-muted)' }}> {route.route_name}</span>
                  ) : null}
                </td>
                <td>{formatCount(route.n)}</td>
                <td>{formatMinutes(route.mts_mae_seconds)}</td>
                <td>{formatMinutes(route.model_mae_seconds)}</td>
                <td
                  style={{
                    color:
                      (route.improvement_seconds ?? 0) > 0
                        ? 'var(--status-good)'
                        : 'var(--ink-secondary)',
                  }}
                >
                  {formatSignedMinutes(route.improvement_seconds)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="note">
        Click a column heading to sort. Error in minutes; gain is MTS minus ours, so
        positive means we are ahead on that route.
      </p>
    </Card>
  );
}
