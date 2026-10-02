/** Arrival time presentation and the ETA correction, kept here so the arithmetic
 *  is testable without a browser. */

export const BASIS_STOP_ROUTE = 'stop_and_route';
export const BASIS_ROUTE = 'route';
export const BASIS_NONE = 'none';

/** Minutes from now, rounded the way a rider reads a departure board. */
export function minutesUntil(iso: string, now: Date = new Date()): number {
  return (new Date(iso).getTime() - now.getTime()) / 60000;
}

/** "Due", "4 min", "1 hr 5 min". Anything already past reads as due rather than
 *  as a negative number, because a bus that is a little late is still coming. */
export function formatWait(iso: string, now: Date = new Date()): string {
  const minutes = minutesUntil(iso, now);
  if (minutes < 1) return 'Due';
  const whole = Math.round(minutes);
  if (whole < 60) return `${whole} min`;
  const hours = Math.floor(whole / 60);
  const rest = whole % 60;
  return rest === 0 ? `${hours} hr` : `${hours} hr ${rest} min`;
}

/** Clock time, for the secondary line under the countdown. */
export function formatArrivalClock(iso: string): string {
  return new Date(iso).toLocaleTimeString('en-US', {
    hour: 'numeric',
    minute: '2-digit',
  });
}

/** How the correction should be described to a rider.
 *
 *  Phrased as what MTS does rather than what we do, because that is the thing the
 *  rider can act on, and the sample size is always included so a thin correction
 *  is visible as thin.
 */
export function explainCorrection(
  correctionSeconds: number | null | undefined,
  basis: string,
  sample: number | null | undefined,
): string | null {
  if (correctionSeconds == null || basis === BASIS_NONE) return null;

  const magnitude = Math.abs(correctionSeconds);
  if (magnitude < 20) return null;

  const amount = formatDuration(magnitude);
  const direction = correctionSeconds > 0 ? 'early' : 'late';
  const scope = basis === BASIS_STOP_ROUTE ? 'at this stop' : 'on this route';
  const evidence = sample ? ` over ${sample.toLocaleString('en-US')} arrivals` : '';
  return `MTS runs about ${amount} ${direction} ${scope}${evidence}`;
}

/** "1m 11s", "45s", "2m". Compact enough for one line of justification. */
export function formatDuration(seconds: number): string {
  const whole = Math.round(Math.abs(seconds));
  if (whole < 60) return `${whole}s`;
  const minutes = Math.floor(whole / 60);
  const rest = whole % 60;
  return rest === 0 ? `${minutes}m` : `${minutes}m ${rest}s`;
}

/** Whether the corrected estimate differs enough from MTS to be worth showing as
 *  a separate number. Below this the two agree and a second figure is noise. */
export const MEANINGFUL_CORRECTION_SECONDS = 20;

export function correctionIsMeaningful(
  correctionSeconds: number | null | undefined,
): boolean {
  return (
    correctionSeconds != null &&
    Math.abs(correctionSeconds) >= MEANINGFUL_CORRECTION_SECONDS
  );
}

/** Trolley lines are drawn differently from buses. GTFS route_type 0 is a tram or
 *  light rail, 3 is a bus. */
export function isTrolley(routeType: number | null | undefined): boolean {
  return routeType === 0;
}

/** Bearing in degrees from one coordinate to another, for pointing the bus icon.
 *  The feed sends no bearing, so it is derived from movement. */
export function headingBetween(
  from: readonly [number, number],
  to: readonly [number, number],
): number | null {
  const [lon1, lat1] = from;
  const [lon2, lat2] = to;
  const dLon = ((lon2 - lon1) * Math.PI) / 180;
  const y = Math.sin(dLon) * Math.cos((lat2 * Math.PI) / 180);
  const x =
    Math.cos((lat1 * Math.PI) / 180) * Math.sin((lat2 * Math.PI) / 180) -
    Math.sin((lat1 * Math.PI) / 180) * Math.cos((lat2 * Math.PI) / 180) * Math.cos(dLon);
  if (Math.abs(x) < 1e-12 && Math.abs(y) < 1e-12) return null;
  return ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
}
