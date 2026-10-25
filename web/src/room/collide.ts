// 2D collision on the floor plan (metres): furniture footprints and the walking camera against
// walls. Pure functions so they can be tested without WebGL.
import type { Vec2, WallPiece } from "../layout";

export const WALL_HALF = 0.06;

/** Corners of an object's footprint: local bounds [min, max] (x, z) turned by yaw, at position. */
export function footprint(pos: Vec2, yawDeg: number, min: Vec2, max: Vec2): Vec2[] {
  const a = (yawDeg * Math.PI) / 180;
  const c = Math.cos(a), s = Math.sin(a);
  // three.js rotation about +y: x' = x cos + z sin, z' = −x sin + z cos.
  return ([[min[0], min[1]], [max[0], min[1]], [max[0], max[1]], [min[0], max[1]]] as Vec2[])
    .map(([x, z]) => [pos[0] + x * c + z * s, pos[1] - x * s + z * c] as Vec2);
}

function cross(o: Vec2, a: Vec2, b: Vec2) {
  return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
}

export function segmentsIntersect(p1: Vec2, p2: Vec2, q1: Vec2, q2: Vec2): boolean {
  const d1 = cross(q1, q2, p1), d2 = cross(q1, q2, p2), d3 = cross(p1, p2, q1), d4 = cross(p1, p2, q2);
  return ((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) && ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0));
}

export function pointSegmentDistance(p: Vec2, a: Vec2, b: Vec2): number {
  const dx = b[0] - a[0], dz = b[1] - a[1];
  const len2 = dx * dx + dz * dz;
  const t = len2 ? Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dz) / len2)) : 0;
  return Math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dz));
}

export function segmentDistance(p1: Vec2, p2: Vec2, q1: Vec2, q2: Vec2): number {
  if (segmentsIntersect(p1, p2, q1, q2)) return 0;
  return Math.min(pointSegmentDistance(p1, q1, q2), pointSegmentDistance(p2, q1, q2), pointSegmentDistance(q1, p1, p2), pointSegmentDistance(q2, p1, p2));
}

export function pointInPolygon(p: Vec2, poly: Vec2[]): boolean {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, zi] = poly[i], [xj, zj] = poly[j];
    if ((zi > p[1]) !== (zj > p[1]) && p[0] < ((xj - xi) * (p[1] - zi)) / (zj - zi) + xi) inside = !inside;
  }
  return inside;
}

/** Wall pieces that block something standing on the floor up to `height`. */
export function blockingWalls(pieces: WallPiece[], height: number): WallPiece[] {
  return pieces.filter((p) => p.y0 < height - 0.02 && p.y1 > 0.02);
}

/** Does a footprint polygon touch any of these walls (thickness included)? */
export function hitsWalls(corners: Vec2[], walls: WallPiece[], clearance = 0.005): boolean {
  for (const w of walls) {
    if (pointInPolygon(w.a, corners) || pointInPolygon(w.b, corners)) return true;
    for (let i = 0; i < corners.length; i++) {
      if (segmentDistance(corners[i], corners[(i + 1) % corners.length], w.a, w.b) < WALL_HALF + clearance) return true;
    }
  }
  return false;
}

/** Can a person (circle of `radius`) stand at p? */
export function canStand(p: Vec2, walls: WallPiece[], radius = 0.22): boolean {
  return walls.every((w) => pointSegmentDistance(p, w.a, w.b) >= WALL_HALF + radius);
}

/** Do two footprints overlap? */
export function footprintsOverlap(a: Vec2[], b: Vec2[]): boolean {
  if (a.some((p) => pointInPolygon(p, b)) || b.some((p) => pointInPolygon(p, a))) return true;
  for (let i = 0; i < a.length; i++) for (let j = 0; j < b.length; j++) {
    if (segmentsIntersect(a[i], a[(i + 1) % a.length], b[j], b[(j + 1) % b.length])) return true;
  }
  return false;
}
