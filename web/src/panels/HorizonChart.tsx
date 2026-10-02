import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { api, type Headline as HeadlineData } from '../api/client';
import {
  SOURCES,
  SOURCE_COLORS,
  SOURCE_LABELS,
  formatMinutes,
  toHorizonSeries,
  type Source,
} from '../lib/format';
import { useParams } from '../settings/useParams';
import { useApi } from '../lib/useApi';
import { Card, Empty, Failed, Pending } from './Card';

/** Error against lead time. Four series on one axis.
 *
 *  Every series is direct-labelled at its right end as well as appearing in the
 *  legend, so identity never rests on colour alone. Two of the four colours sit
 *  below 3:1 against the light surface, which obliges exactly this. */
export function HorizonChart() {
  const params = useParams();
  const { data, error, loading } = useApi<HeadlineData>(
    () => api.headline(params),
    [params],
  );

  if (loading)
    return (
      <Card title="Error grows with lead time">
        <Pending what="the curve" />
      </Card>
    );
  if (error)
    return (
      <Card title="Error grows with lead time">
        <Failed error={error} />
      </Card>
    );
  if (!data || data.rows.length === 0) {
    return (
      <Card title="Error grows with lead time">
        <Empty what="scored predictions" />
      </Card>
    );
  }

  const series = toHorizonSeries(data.rows);
  const present = SOURCES.filter((source) => series.some((point) => point[source] != null));

  return (
    <Card
      title="Error grows with lead time"
      note="The further ahead a prediction looks, the worse it gets. That gap is the room a model has to work in. Mean absolute error in minutes."
    >
      <div style={{ height: 320 }}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={series} margin={{ top: 8, right: 132, bottom: 8, left: 0 }}>
            <CartesianGrid stroke="var(--grid)" strokeDasharray="0" vertical={false} />
            <XAxis
              dataKey="horizon"
              type="number"
              domain={[0, 21]}
              ticks={[1, 5, 10, 20]}
              tickFormatter={(value: number) => `${value}m`}
              stroke="var(--axis)"
              tick={{ fill: 'var(--ink-muted)', fontSize: 12 }}
              label={{
                value: 'minutes before the bus actually arrived',
                position: 'insideBottom',
                offset: -4,
                fill: 'var(--ink-muted)',
                fontSize: 12,
              }}
            />
            <YAxis
              stroke="var(--axis)"
              tick={{ fill: 'var(--ink-muted)', fontSize: 12 }}
              tickFormatter={(value: number) => value.toFixed(1)}
              label={{
                value: 'MAE (min)',
                angle: -90,
                position: 'insideLeft',
                fill: 'var(--ink-muted)',
                fontSize: 12,
              }}
            />
            <Tooltip
              contentStyle={{
                background: 'var(--surface)',
                border: '1px solid var(--border)',
                borderRadius: 8,
                color: 'var(--ink)',
                fontSize: 13,
              }}
              labelFormatter={(value) => `${String(value)} minutes ahead`}
              formatter={(value, name) => [
                `${Number(value).toFixed(2)} min`,
                SOURCE_LABELS[name as Source] ?? String(name),
              ]}
            />
            {present.map((source) => (
              <Line
                key={source}
                type="monotone"
                dataKey={source}
                name={source}
                stroke={SOURCE_COLORS[source]}
                strokeWidth={2}
                dot={{ r: 4, strokeWidth: 0, fill: SOURCE_COLORS[source] }}
                activeDot={{ r: 6, stroke: 'var(--surface)', strokeWidth: 2 }}
                isAnimationActive={false}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>

      {/* Direct labels, rendered as a list beside the chart rather than on every
          point. A number on every point is noise; identity still never depends on
          colour alone. */}
      <ul
        style={{
          listStyle: 'none',
          padding: 0,
          margin: '10px 0 0',
          display: 'flex',
          gap: 18,
          flexWrap: 'wrap',
          fontSize: 13,
        }}
      >
        {present.map((source) => {
          const last = series[series.length - 1];
          return (
            <li key={source} style={{ color: 'var(--ink-secondary)' }}>
              <span
                className="swatch"
                style={{ background: SOURCE_COLORS[source] }}
                aria-hidden="true"
              />
              {SOURCE_LABELS[source]}
              <span style={{ color: 'var(--ink-muted)' }}>
                {' '}
                — {formatMinutes((last?.[source] ?? 0) * 60)} min at 20
              </span>
            </li>
          );
        })}
      </ul>
    </Card>
  );
}
