/** Trip plan presentation, kept out of the component so it is testable without a
 *  browser. The times themselves come from the API; this only decides how a
 *  rider reads them. */
import { formatDuration, formatWait, minutesUntil } from './eta';

/** The API's arrive-by window, mirrored so the input cannot ask for more. */
export const MAX_ARRIVE_BY_HOURS = 24;

/** An `<input type="time">` value as the next time that clock reading occurs.
 *
 *  "10:00" typed at 22:00 means tomorrow morning, not a time already gone, which
 *  is what a rider setting an alarm-like deadline expects. Interpreted in the
 *  browser's timezone, which for this app's riders is San Diego's.
 */
export function arriveByIso(hhmm: string, now: Date = new Date()): string | null {
  const match = /^(\d{1,2}):(\d{2})$/.exec(hhmm.trim());
  if (!match) return null;
  const hours = Number(match[1]);
  const minutes = Number(match[2]);
  if (hours > 23 || minutes > 59) return null;
  const target = new Date(now);
  target.setHours(hours, minutes, 0, 0);
  if (target.getTime() <= now.getTime()) target.setDate(target.getDate() + 1);
  return target.toISOString();
}

/** Walking minutes, never shown as zero: "0 min walk" reads like a bug. */
export function walkMinutes(seconds: number): number {
  return Math.max(1, Math.round(seconds / 60));
}

/** The headline of an option: how long until you have to set off. */
export function leaveLabel(leaveByIso: string, now: Date = new Date()): string {
  if (minutesUntil(leaveByIso, now) < 1) return 'Leave now';
  return `Leave in ${formatWait(leaveByIso, now)}`;
}

/** Which estimate the departure time is, in words a rider can weigh. */
export function describeDeparture(basis: string): string {
  if (basis === 'corrected') return 'live, our estimate';
  if (basis === 'mts') return 'live, MTS estimate';
  return 'timetable';
}

/** Why the margin is what it is. A measured margin says where it came from, and
 *  the default says it is a default, so neither is mistaken for the other. */
export function describeMargin(seconds: number, basis: string): string {
  const amount = formatDuration(seconds);
  if (basis === 'measured') {
    return `${amount} early, because 1 in 10 buses on this route beat the estimate by that much`;
  }
  return `${amount} early, a standard buffer since there is no live data for this bus yet`;
}

/** Identity of a request, so a result is only ever shown for the inputs that
 *  produced it. Without this, changing the destination would leave the old
 *  answer on screen, looking current, until the new one arrived. */
export function planKey(
  from: { lat: number; lon: number } | null,
  to: { lat: number; lon: number } | null,
  arriveBy: string | null,
): string | null {
  if (!from || !to) return null;
  const point = (p: { lat: number; lon: number }) =>
    `${p.lat.toFixed(5)},${p.lon.toFixed(5)}`;
  return `${point(from)}>${point(to)}@${arriveBy ?? 'now'}`;
}
