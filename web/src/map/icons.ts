/** Map artwork, drawn to a canvas and registered with MapLibre.
 *
 *  Drawn rather than shipped as image files so the colours come from the same
 *  validated palette as the charts and can follow dark mode. Everything is
 *  rendered at twice scale and registered with `pixelRatio: 2`, so it stays sharp
 *  on a retina display.
 */

export const ICON_PIN = 'stop-pin';
export const ICON_PIN_SELECTED = 'stop-pin-selected';
export const ICON_BUS = 'vehicle-bus';
export const ICON_TROLLEY = 'vehicle-trolley';

const SCALE = 2;

function canvas(
  width: number,
  height: number,
): [HTMLCanvasElement, CanvasRenderingContext2D] {
  const element = document.createElement('canvas');
  element.width = width * SCALE;
  element.height = height * SCALE;
  const context = element.getContext('2d');
  if (!context) throw new Error('2d canvas context unavailable');
  context.scale(SCALE, SCALE);
  return [element, context];
}

function toImage(element: HTMLCanvasElement): ImageData {
  const context = element.getContext('2d');
  if (!context) throw new Error('2d canvas context unavailable');
  return context.getImageData(0, 0, element.width, element.height);
}

/** A teardrop pin whose point sits exactly on the stop. The hole in the middle is
 *  what separates a pin from a blob at small sizes. */
function drawPin(fill: string, ring: string, size: number): ImageData {
  const width = size;
  const height = size * 1.35;
  const [element, context] = canvas(width, height);

  const radius = width / 2;
  const centreY = radius;

  context.beginPath();
  context.arc(radius, centreY, radius - 1.5, Math.PI, 0, false);
  context.quadraticCurveTo(width - 2.5, height * 0.62, radius, height - 1);
  context.quadraticCurveTo(2.5, height * 0.62, 1.5, centreY);
  context.closePath();

  context.fillStyle = fill;
  context.fill();
  context.lineWidth = 1.5;
  context.strokeStyle = ring;
  context.stroke();

  context.beginPath();
  context.arc(radius, centreY, radius * 0.36, 0, Math.PI * 2);
  context.fillStyle = ring;
  context.fill();

  return toImage(element);
}

/** A vehicle seen from above, so rotating it to the direction of travel reads
 *  naturally. The lighter band is the windscreen, which is what tells a viewer
 *  which end is the front. */
function drawVehicle(body: string, glass: string, trolley: boolean): ImageData {
  const width = 22;
  const height = 32;
  const [element, context] = canvas(width, height);

  const left = 4;
  const right = width - 4;
  const bodyWidth = right - left;

  // Wheels first, so they sit under the body and read as sticking out, which is
  // what makes the shape a vehicle rather than a capsule.
  context.fillStyle = 'rgba(30,30,30,0.72)';
  for (const y of [7.5, height - 13]) {
    context.beginPath();
    context.roundRect(left - 1.8, y, 2.6, 6, 1.2);
    context.fill();
    context.beginPath();
    context.roundRect(right - 0.8, y, 2.6, 6, 1.2);
    context.fill();
  }

  context.beginPath();
  context.roundRect(left, 2, bodyWidth, height - 4, trolley ? 2.5 : 4.5);
  context.fillStyle = body;
  context.fill();
  context.lineWidth = 1.3;
  context.strokeStyle = '#ffffff';
  context.stroke();

  // Windscreen across the front, which is the top before rotation.
  context.beginPath();
  context.roundRect(left + 1.8, 3.6, bodyWidth - 3.6, 4.6, 1.6);
  context.fillStyle = glass;
  context.fill();

  // Side windows, drawn as a pair of light strips.
  context.fillStyle = 'rgba(255,255,255,0.4)';
  for (let i = 0; i < 3; i += 1) {
    context.beginPath();
    context.roundRect(left + 1.6, 11 + i * 5.2, bodyWidth - 3.2, 3.2, 1);
    context.fill();
  }

  return toImage(element);
}

export interface IconColors {
  stop: string;
  stopSelected: string;
  bus: string;
  trolley: string;
  ring: string;
}

/** Register every icon. Safe to call again after a theme change: existing images
 *  are replaced rather than duplicated. */
export function registerIcons(
  map: {
    addImage: (id: string, image: ImageData, options?: { pixelRatio?: number }) => void;
    hasImage: (id: string) => boolean;
    removeImage: (id: string) => void;
  },
  colors: IconColors,
): void {
  const images: [string, ImageData][] = [
    [ICON_PIN, drawPin(colors.stop, colors.ring, 22)],
    [ICON_PIN_SELECTED, drawPin(colors.stopSelected, colors.ring, 30)],
    [ICON_BUS, drawVehicle(colors.bus, 'rgba(255,255,255,0.85)', false)],
    [ICON_TROLLEY, drawVehicle(colors.trolley, 'rgba(255,255,255,0.85)', true)],
  ];

  for (const [id, image] of images) {
    if (map.hasImage(id)) map.removeImage(id);
    map.addImage(id, image, { pixelRatio: SCALE });
  }
}
