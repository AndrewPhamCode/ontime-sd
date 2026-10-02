import { useEffect, useRef, useState } from 'react';
import maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import type { Stop, Vehicle } from '../api/client';

export interface MapView {
  center: [number, number];
  zoom: number;
  label: string;
}

/** Default view. UCSD and La Jolla Shores is the densest corner of the network
 *  that still fits on one screen: 183 observed stops and 13 routes. */
export const VIEWS: Record<string, MapView> = {
  ucsd: { center: [-117.238, 32.872], zoom: 12.6, label: 'UCSD & La Jolla Shores' },
  all: { center: [-117.16, 32.75], zoom: 9.9, label: 'All San Diego' },
};

interface Props {
  stops: readonly Stop[];
  vehicles: readonly Vehicle[];
  selected: string | null;
  onSelect: (stopId: string) => void;
  view: MapView;
}

// The one external runtime dependency on this page. No API key required.
const BASEMAP = 'https://tiles.openfreemap.org/styles/positron';

// Vehicles are eased from their previous position to the new one rather than
// jumping, so the map reads as movement. The upstream feed updates every 30s.
const GLIDE_MS = 2200;
const FRAME_MS = 50;

type Tween = { from: [number, number]; to: [number, number]; started: number };

function lerp(a: number, b: number, t: number): number {
  return a + (b - a) * t;
}

function easeOut(t: number): number {
  return 1 - (1 - t) * (1 - t);
}

export function StopMap({ stops, vehicles, selected, onSelect, view }: Props) {
  const container = useRef<HTMLDivElement | null>(null);
  const map = useRef<maplibregl.Map | null>(null);
  // State, not a ref: the data effects must re-run once the style has loaded.
  // With a ref, data arriving before the load event was never applied and the map
  // rendered empty.
  const [ready, setReady] = useState(false);
  const tweens = useRef(new Map<string, Tween>());

  useEffect(() => {
    if (!container.current || map.current) return;
    const instance = new maplibregl.Map({
      container: container.current,
      style: BASEMAP,
      center: VIEWS['ucsd']!.center,
      zoom: VIEWS['ucsd']!.zoom,
      attributionControl: { compact: true },
    });
    instance.addControl(
      new maplibregl.NavigationControl({ showCompass: false }),
      'top-right',
    );

    instance.on('load', () => {
      // The container is sized by CSS grid, so it may have changed since the map
      // was constructed. Without this the canvas keeps its initial dimensions.
      instance.resize();

      instance.addSource('stops', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      });
      instance.addLayer({
        id: 'stops',
        type: 'circle',
        source: 'stops',
        paint: {
          'circle-radius': ['interpolate', ['linear'], ['zoom'], 10, 2.6, 15, 6],
          'circle-color': '#2a78d6',
          'circle-opacity': 0.7,
          'circle-stroke-width': 1,
          'circle-stroke-color': '#fcfcfb',
        },
      });
      instance.addLayer({
        id: 'stop-selected',
        type: 'circle',
        source: 'stops',
        filter: ['==', ['get', 'stop_id'], ''],
        paint: {
          'circle-radius': 10,
          'circle-color': '#eb6834',
          'circle-stroke-width': 2.5,
          'circle-stroke-color': '#fcfcfb',
        },
      });

      instance.addSource('vehicles', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      });
      instance.addLayer({
        id: 'vehicles',
        type: 'circle',
        source: 'vehicles',
        paint: {
          'circle-radius': 5,
          'circle-color': '#0ca30c',
          'circle-stroke-width': 1.5,
          'circle-stroke-color': '#fcfcfb',
        },
      });

      const popup = new maplibregl.Popup({
        closeButton: false,
        closeOnClick: false,
        offset: 10,
      });

      instance.on('click', 'stops', (event) => {
        const stopId = event.features?.[0]?.properties?.['stop_id'];
        if (typeof stopId === 'string') onSelect(stopId);
      });
      instance.on('mouseenter', 'stops', (event) => {
        instance.getCanvas().style.cursor = 'pointer';
        const props = event.features?.[0]?.properties;
        if (props) {
          popup
            .setLngLat(event.lngLat)
            .setHTML(
              `<strong>${String(props['stop_name'] ?? '')}</strong><br/>` +
                `${Number(props['arrivals'] ?? 0).toLocaleString()} observed arrivals`,
            )
            .addTo(instance);
        }
      });
      instance.on('mouseleave', 'stops', () => {
        instance.getCanvas().style.cursor = '';
        popup.remove();
      });
      instance.on('mouseenter', 'vehicles', (event) => {
        const props = event.features?.[0]?.properties;
        if (props) {
          popup
            .setLngLat(event.lngLat)
            .setHTML(
              `<strong>Vehicle ${String(props['vehicle_id'] ?? '')}</strong><br/>` +
                `route ${String(props['route_id'] ?? 'unassigned')}`,
            )
            .addTo(instance);
        }
      });
      instance.on('mouseleave', 'vehicles', () => popup.remove());

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
    instance.easeTo({ center: view.center, zoom: view.zoom, duration: 700 });
  }, [view, ready]);

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

  /** Glide vehicles from their previous position to the new one. */
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
      next.set(vehicle.vehicle_id, { from: current, to: target, started: now });
    }
    tweens.current = next;

    const source = instance.getSource('vehicles') as maplibregl.GeoJSONSource | undefined;
    if (!source) return;

    // Returns true once the glide has finished, so the caller can stop the timer.
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
              route_id: vehicle.route_id ?? 'unassigned',
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

  return <div className="map-shell" ref={container} />;
}
