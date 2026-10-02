import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { api, type DistributionBucket } from '../api/client';
import { SOURCE_COLORS, SOURCE_LABELS, formatCount } from '../lib/format';
import { useApi } from '../lib/useApi';
import { Card, Empty, Failed, Pending } from './Card';

interface Row {
  bucket: string;
  mts: number;
  lgbm: number;
}

/** Where the errors actually sit, not just their average.
 *
 *  Only two series, ours against MTS. Putting all four on a ten-bucket histogram
 *  would be unreadable, and the baselines are already compared above. */
export function ErrorDistribution({ horizon }: { horizon: number }) {
  const { data, error, loading } = useApi<DistributionBucket[]>(
    () => api.distribution(horizon),
    [horizon],
  );

  if (loading)
    return (
      <Card title="Distribution of error">
        <Pending what="the histogram" />
      </Card>
    );
  if (error)
    return (
      <Card title="Distribution of error">
        <Failed error={error} />
      </Card>
    );
  if (!data || data.length === 0) {
    return (
      <Card title="Distribution of error">
        <Empty what="scored predictions" />
      </Card>
    );
  }

  const buckets = new Map<number, Row>();
  for (const entry of data) {
    if (entry.source !== 'mts' && entry.source !== 'lgbm') continue;
    const existing = buckets.get(entry.upper_bound_seconds) ?? {
      bucket: `${entry.upper_bound_seconds / 60} min`,
      mts: 0,
      lgbm: 0,
    };
    existing[entry.source] = entry.n;
    buckets.set(entry.upper_bound_seconds, existing);
  }
  const rows = [...buckets.entries()].sort((a, b) => a[0] - b[0]).map(([, row]) => row);
  const total = rows.reduce((sum, row) => sum + row.mts, 0);

  return (
    <Card
      title={`Distribution of error at ${horizon} minutes ahead`}
      note="How often each predictor lands in each error band, counting arrivals. The x axis is absolute error, up to that many minutes. An average hides whether error is evenly spread or driven by a bad tail."
      aside={<span className="pill">{formatCount(total)} arrivals</span>}
    >
      <div style={{ height: 280 }}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={rows}
            margin={{ top: 8, right: 8, bottom: 16, left: 0 }}
            barGap={2}
          >
            <CartesianGrid stroke="var(--grid)" vertical={false} />
            <XAxis
              dataKey="bucket"
              stroke="var(--axis)"
              tick={{ fill: 'var(--ink-muted)', fontSize: 12 }}
            />
            <YAxis
              stroke="var(--axis)"
              tick={{ fill: 'var(--ink-muted)', fontSize: 12 }}
              tickFormatter={(value: number) => formatCount(value)}
            />
            <Tooltip
              contentStyle={{
                background: 'var(--surface)',
                border: '1px solid var(--border)',
                borderRadius: 8,
                color: 'var(--ink)',
                fontSize: 13,
              }}
              formatter={(value, name) => [
                formatCount(Number(value)),
                name === 'mts' ? SOURCE_LABELS.mts : SOURCE_LABELS.lgbm,
              ]}
            />
            <Legend
              formatter={(value) => (value === 'mts' ? 'MTS' : 'Ours')}
              verticalAlign="top"
              align="right"
              height={24}
              wrapperStyle={{ fontSize: 13, color: 'var(--ink-secondary)' }}
            />
            <Bar
              dataKey="lgbm"
              fill={SOURCE_COLORS.lgbm}
              radius={[4, 4, 0, 0]}
              isAnimationActive={false}
            />
            <Bar
              dataKey="mts"
              fill={SOURCE_COLORS.mts}
              radius={[4, 4, 0, 0]}
              isAnimationActive={false}
            />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </Card>
  );
}
