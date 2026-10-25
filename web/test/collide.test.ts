import { describe, expect, it } from "vitest";
import { blockingWalls, canStand, footprint, footprintsOverlap, hitsWalls, pointInPolygon } from "../src/room/collide";
import { wallPieces, rectOf, Layout } from "../src/layout";
// @ts-ignore: plain JS module
import { estimateLayout } from "../mock/estimate.mjs";

const l: Layout = estimateLayout({ listing_id: "c", bedrooms: 2, bathrooms: 1, square_feet: 900 });
const pieces = wallPieces(l);
const bed = rectOf(l.rooms.find((r) => r.room_id === "bed_1")!);
const centre: [number, number] = [(bed.x0 + bed.x1) / 2, (bed.z0 + bed.z1) / 2];

describe("furniture against walls", () => {
  it("footprints turn with yaw", () => {
    const f = footprint([0, 0], 90, [-1, -0.25], [1, 0.25]);
    const xs = f.map((p) => p[0]), zs = f.map((p) => p[1]);
    expect(Math.max(...xs) - Math.min(...xs)).toBeCloseTo(0.5, 5);
    expect(Math.max(...zs) - Math.min(...zs)).toBeCloseTo(2, 5);
    expect(pointInPolygon([0, 0], f)).toBe(true);
  });

  it("a chair in the middle of a bedroom is clear; pushed into a wall it isn't", () => {
    const walls = blockingWalls(pieces, 0.8);
    expect(hitsWalls(footprint(centre, 0, [-0.27, -0.26], [0.27, 0.26]), walls)).toBe(false);
    expect(hitsWalls(footprint([bed.x0 + 0.1, centre[1]], 0, [-0.27, -0.26], [0.27, 0.26]), walls)).toBe(true);
    // Turned 45° near a wall it collides sooner than straight.
    const near: [number, number] = [bed.x0 + 0.06 + 0.29, centre[1]];
    expect(hitsWalls(footprint(near, 0, [-0.27, -0.26], [0.27, 0.26]), walls)).toBe(false);
    expect(hitsWalls(footprint(near, 45, [-0.27, -0.26], [0.27, 0.26]), walls)).toBe(true);
  });

  it("the wall below a window still blocks furniture", () => {
    const win = l.openings.find((o) => o.kind === "window" && o.room_ids[0] === "bed_1")!;
    const atWindow: [number, number] = [(win.a[0] + win.b[0]) / 2, win.a[1] + 0.2];
    const box = footprint(atWindow, 0, [-0.3, -0.19], [0.3, 0.19]);
    expect(hitsWalls(box, blockingWalls(pieces, 0.5))).toBe(true);
    expect(hitsWalls(footprint([atWindow[0], win.a[1] + 0.3], 0, [-0.3, -0.19], [0.3, 0.19]), blockingWalls(pieces, 0.5))).toBe(false);
  });

  it("overlapping footprints are detected", () => {
    const a = footprint([0, 0], 0, [-0.5, -0.5], [0.5, 0.5]);
    expect(footprintsOverlap(a, footprint([0.8, 0], 30, [-0.5, -0.5], [0.5, 0.5]))).toBe(true);
    expect(footprintsOverlap(a, footprint([2, 0], 0, [-0.5, -0.5], [0.5, 0.5]))).toBe(false);
  });
});

describe("walking", () => {
  const body = blockingWalls(pieces, 1.7);
  it("you can stand in a room and in a doorway, not inside a wall", () => {
    expect(canStand(centre, body)).toBe(true);
    const door = l.openings.find((o) => o.kind === "door" && o.room_ids.includes("bed_1"))!;
    expect(canStand([(door.a[0] + door.b[0]) / 2, door.a[1]], body, 0.2)).toBe(true);
    expect(canStand([bed.x0 + 0.05, centre[1]], body)).toBe(false);
  });
});
