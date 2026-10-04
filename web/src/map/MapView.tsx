import { useEffect, useRef, useState } from 'react';
import maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import type { Stop, Vehicle } from '../api/client';
import { headingBetween, isTrolley } from '../lib/eta';
import {
  ICON_BUS,
  ICON_PIN,
  ICON_PIN_SELECTED,
  ICON_TROLLEY,
  registerIcons,
} from './icons';

export interface MapPlace {
  center: [number, number];
  zoom: number;
  label: string;
}

/** Opens on UCSD and La Jolla Shores: the densest corner of the network that
 *  still fits one screen, with 183 observed stops and 13 routes. */
export const PLACES: Record<string, MapPlace> = {
  ucsd: { center: [-117.238, 32.872], zoom: 13.1, label: 'UCSD & La Jolla' },
  downtown: { center: [-117.1611, 32.7157], zoom: 13.4, label: 'Downtown' },
  all: { center: [-117.14, 32.78], zoom: 10.2, label: 'All San Diego' },
};

const BASEMAP = 'https://tiles.openfreemap.org/styles/positron';
const GLIDE_MS = 2200;
const FRAME_MS = 60;

// Below this, a position change is GPS jitter rather than travel, and rotating
// the icon to match would make stationary buses spin.
const MIN_HEADING_METRES = 12;

interface Props {
  stops: readonly Stop[];
  vehicles: readonly Vehicle[];
  selected: string | null;
  onSelect: (stopId: string) => void;
  place: MapPlace;
  flyTo: { center: [number, number]; zoom: number } | null;
  showVehicles: boolean;
  showStopLabels: boolean;
}

type Tween = {
  from: [number, number];
  to: [number, number];
  started: number;
  heading: number;
};

const lerp = (a: number, b: number, t: number) => a + (b - a) * t;
const easeOut = (t: number) => 1 - (1 - t) * (1 - t);

function metresBetween(a: readonly [number, number], b: readonly [number, number]): number {
  const dx = (b[0] - a[0]) * 93_675;
  const dy = (b[1] - a[1]) * 111_320;
  return Math.hypot(dx, dy);
}

export function MapView({
  stops,
  vehicles,
  selected,
  onSelect,
  place,
  flyTo,
  showVehicles,
  showStopLabels,
}: Props) {
  const container = useRef<HTMLDivElement | null>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const [ready, setReady] = useState(false);
  const tweens = useRef(new Map<string, Tween>());

  // Visibility is toggled on the live layers rather than by rebuilding the map,
  // so flipping a switch does not drop the viewport or the icon atlas.
  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready) return;
    instance.setLayoutProperty('vehicles', 'visibility', showVehicles ? 'visible' : 'none');
    instance.setLayoutProperty(
      'stops',
      'text-field',
      showStopLabels ? ['step', ['zoom'], '', 13.5, ['get', 'stop_name']] : '',
    );
  }, [ready, showVehicles, showStopLabels]);

  useEffect(() => {
    if (!container.current || map.current) return;
    const instance = new maplibregl.Map({
      container: container.current,
      style: BASEMAP,
      center: PLACES['ucsd']!.center,
      zoom: PLACES['ucsd']!.zoom,
      attributionControl: { compact: true },
    });
    instance.addControl(
      new maplibregl.NavigationControl({ showCompass: false }),
      'bottom-right',
    );
    instance.addControl(
      new maplibregl.GeolocateControl({ trackUserLocation: false }),
      'bottom-right',
    );

    instance.on('load', () => {
      instance.resize();

      const styles = getComputedStyle(document.documentElement);
      const token = (name: string, fallback: string) =>
        styles.getPropertyValue(name).trim() || fallback;

      // MapLibre paint properties are WebGL, so they need real colours rather
      // than CSS custom properties. Resolved once from the same tokens.
      const labelInk = token('--ink-secondary', '#52514e');
      const labelHalo = token('--surface', '#fcfcfb');

      registerIcons(instance, {
        stop: token('--series-lgbm', '#2a78d6'),
        stopSelected: token('--series-mts', '#eb6834'),
        bus: token('--status-good', '#0ca30c'),
        trolley: token('--series-violet', '#4a3aa7'),
        ring: token('--surface', '#fcfcfb'),
      });

      instance.addSource('stops', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      });
      instance.addLayer({
        id: 'stops',
        type: 'symbol',
        source: 'stops',
        layout: {
          'icon-image': ICON_PIN,
          // The point of a pin sits on the place it marks.
          'icon-anchor': 'bottom',
          'icon-size': ['interpolate', ['linear'], ['zoom'], 10, 0.5, 14, 0.85, 17, 1],
          'icon-allow-overlap': true,
          // Names appear once the map is at neighbourhood scale. Below that the
          // labels would collide into noise, so pins alone carry the map.
          'text-field': ['step', ['zoom'], '', 13.5, ['get', 'stop_name']],
          'text-font': ['Noto Sans Regular'],
          'text-size': ['interpolate', ['linear'], ['zoom'], 13.5, 10, 17, 12.5],
          'text-offset': [0, 0.45],
          'text-anchor': 'top',
          'text-optional': true,
          'text-max-width': 9,
          // Busier stops win when two labels cannot both fit.
          'symbol-sort-key': ['-', 0, ['get', 'arrivals']],
        },
        paint: {
          'text-color': labelInk,
          'text-halo-color': labelHalo,
          'text-halo-width': 1.6,
        },
      });
      instance.addLayer({
        id: 'stop-selected',
        type: 'symbol',
        source: 'stops',
        filter: ['==', ['get', 'stop_id'], ''],
        layout: {
          'icon-image': ICON_PIN_SELECTED,
          'icon-anchor': 'bottom',
          'icon-size': ['interpolate', ['linear'], ['zoom'], 10, 0.6, 17, 1.05],
          'icon-allow-overlap': true,
        },
      });

      instance.addSource('vehicles', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      });
      instance.addLayer({
        id: 'vehicles',
        type: 'symbol',
        source: 'vehicles',
        layout: {
          'icon-image': ['case', ['get', 'trolley'], ICON_TROLLEY, ICON_BUS],
          'icon-size': ['interpolate', ['linear'], ['zoom'], 10, 0.5, 14, 0.8, 17, 1],
          'icon-allow-overlap': true,
          'icon-rotate': ['get', 'heading'],
          // The vehicle turns with the road; its route number stays readable.
          'icon-rotation-alignment': 'map',
          'text-rotation-alignment': 'viewport',
          'text-field': ['step', ['zoom'], '', 12, ['get', 'route']],
          'text-font': ['Noto Sans Bold'],
          'text-size': 11,
          'text-offset': [0, 1.35],
          'text-anchor': 'top',
          'text-allow-overlap': false,
          'text-optional': true,
        },
        paint: {
          'text-color': labelInk,
          'text-halo-color': labelHalo,
          'text-halo-width': 1.8,
        },
      });

      const popup = new maplibregl.Popup({
        closeButton: false,
        closeOnClick: false,
        offset: 14,
      });

      instance.on('click', 'stops', (event) => {
        const stopId = event.features?.[0]?.properties?.['stop_id'];
        if (typeof stopId === 'string') onSelect(stopId);
      });
      // mousemove, not mouseenter: vehicles draw with icon-allow-overlap, so
      // moving from one bus to the one beside it never leaves the layer and
      // mouseenter does not fire again. The popup would keep the first bus's
      // route and position while the cursor sat on a different bus.
      for (const layer of ['stops', 'vehicles']) {
        instance.on('mousemove', layer, (event) => {
          instance.getCanvas().style.cursor = layer === 'stops' ? 'pointer' : '';
          const props = event.features?.[0]?.properties;
          if (!props) return;
          const html =
            layer === 'stops'
              ? `<strong>${String(props['stop_name'] ?? '')}</strong>`
              : `<strong>Route ${String(props['route'] ?? '?')}</strong><br/>vehicle ${String(props['vehicle_id'] ?? '')}`;
          popup.setLngLat(event.lngLat).setHTML(html).addTo(instance);
        });
        instance.on('mouseleave', layer, () => {
          instance.getCanvas().style.cursor = '';
          popup.remove();
        });
      }

      setReady(true);
    });

    map.current = instance;
    const observer = new ResizeObserver(() => instance.resize());
    observer.observe(container.current);
    return () => {
      observer.disconnect();
      instance.remove();
      map.current = null;
      setReady(false);
    };
  }, [onSelect]);

  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready) return;
    instance.easeTo({ center: place.center, zoom: place.zoom, duration: 800 });
  }, [place, ready]);

  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready || !flyTo) return;
    instance.flyTo({ center: flyTo.center, zoom: flyTo.zoom, duration: 900 });
  }, [flyTo, ready]);

  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready) return;
    const source = instance.getSource('stops') as maplibregl.GeoJSONSource | undefined;
    source?.setData({
      type: 'FeatureCollection',
      features: stops.map((stop) => ({
        type: 'Feature' as const,
        geometry: { type: 'Point' as const, coordinates: [stop.lon, stop.lat] },
        properties: {
          stop_id: stop.stop_id,
          stop_name: stop.stop_name ?? stop.stop_id,
          arrivals: stop.arrivals,
        },
      })),
    });
  }, [stops, ready]);

  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready) return;

    const now = performance.now();
    const next = new Map<string, Tween>();
    for (const vehicle of vehicles) {
      const previous = tweens.current.get(vehicle.vehicle_id);
      const target: [number, number] = [vehicle.lon, vehicle.lat];
      const elapsed = previous ? Math.min((now - previous.started) / GLIDE_MS, 1) : 1;
      const current: [number, number] = previous
        ? [
            lerp(previous.from[0], previous.to[0], easeOut(elapsed)),
            lerp(previous.from[1], previous.to[1], easeOut(elapsed)),
          ]
        : target;

      // Keep the previous heading when the vehicle has barely moved, so a bus
      // waiting at a stop does not spin on GPS noise.
      const moved = metresBetween(current, target);
      const heading =
        moved >= MIN_HEADING_METRES
          ? (headingBetween(current, target) ?? previous?.heading ?? 0)
          : (previous?.heading ?? 0);

      next.set(vehicle.vehicle_id, { from: current, to: target, started: now, heading });
    }
    tweens.current = next;

    const source = instance.getSource('vehicles') as maplibregl.GeoJSONSource | undefined;
    if (!source) return;

    const paint = (): boolean => {
      const elapsed = Math.min((performance.now() - now) / GLIDE_MS, 1);
      const eased = easeOut(elapsed);
      source.setData({
        type: 'FeatureCollection',
        features: vehicles.map((vehicle) => {
          const tween = tweens.current.get(vehicle.vehicle_id);
          const coordinates: [number, number] = tween
            ? [
                lerp(tween.from[0], tween.to[0], eased),
                lerp(tween.from[1], tween.to[1], eased),
              ]
            : [vehicle.lon, vehicle.lat];
          return {
            type: 'Feature' as const,
            geometry: { type: 'Point' as const, coordinates },
            properties: {
              vehicle_id: vehicle.vehicle_id,
              route: vehicle.route_short_name ?? vehicle.route_id ?? '',
              trolley: isTrolley(vehicle.route_type),
              heading: tween?.heading ?? 0,
            },
          };
        }),
      });
      return elapsed >= 1;
    };

    paint();
    const timer = setInterval(() => {
      if (paint()) clearInterval(timer);
    }, FRAME_MS);
    return () => clearInterval(timer);
  }, [vehicles, ready]);

  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready) return;
    if (instance.getLayer('stop-selected')) {
      instance.setFilter('stop-selected', ['==', ['get', 'stop_id'], selected ?? '']);
    }
  }, [selected, ready]);

  return <div ref={container} style={{ position: 'absolute', inset: 0 }} />;
}
