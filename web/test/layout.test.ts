import { describe, expect, it } from "vitest";
import {
  Layout, bounds, moveWall, rectOf, setRoomSize, totalArea, validate, wallPieces, walls, openingLine, roomAt, roomsWithWindows, DOOR_HEIGHT_M, MIN_ROOM_M,
} from "../src/layout";
// @ts-ignore: plain JS module
import { estimateLayout } from "../mock/estimate.mjs";

const listing = (bedrooms: number, bathrooms: number, square_feet: number | null) =>
  ({ listing_id: `l_${bedrooms}_${bathrooms}_${square_feet}`, bedrooms, bathrooms, square_feet });

const cases: Array<[number, number, number | null]> = [[0, 1, 450], [0, 1, null], [1, 1, 650], [1, 1, null], [2, 1, 900], [2, 2, null], [3, 2, 1250], [4, 2, null], [2, 1, 600]];

describe("estimated plans from the mock are valid", () => {
  for (const [b, ba, sf] of cases) {
    it(`${b} bed ${ba} bath ${sf ?? "no"} sq ft`, () => {
      const l: Layout = estimateLayout(listing(b, ba, sf));
      expect(validate(l)).toEqual([]);
      expect(l.rooms.filter((r) => r.kind === "bedroom")).toHaveLength(b);
      // Exterior walls trace the outline exactly.
      const bb = bounds(l);
      const exterior = walls(l).filter((w) => w.exterior).reduce((s, w) => s + (w.t1 - w.t0), 0);
      expect(exterior).toBeCloseTo(2 * (bb.x1 - bb.x0 + bb.z1 - bb.z0), 3);
      expect(totalArea(l)).toBeCloseTo((bb.x1 - bb.x0) * (bb.z1 - bb.z0), 3);
      if (sf) expect(Math.abs(totalArea(l) - sf * 0.092903) / (sf * 0.092903)).toBeLessThan(0.1);
      // Every room has a door.
      for (const r of l.rooms) expect(l.openings.some((o) => o.kind === "door" && o.room_ids.includes(r.room_id))).toBe(true);
    });
  }
});

describe("walls and openings", () => {
  const l: Layout = estimateLayout(listing(2, 1, 900));

  it("interior walls are shared once, exterior walls point outwards", () => {
    const ws = walls(l);
    const bb = bounds(l);
    for (const w of ws.filter((w) => w.exterior)) {
      const mid = w.axis === "x" ? [w.c, (w.t0 + w.t1) / 2] : [(w.t0 + w.t1) / 2, w.c];
      const out = [mid[0] + w.outward![0] * 0.05, mid[1] + w.outward![1] * 0.05];
      expect(out[0] < bb.x0 || out[0] > bb.x1 || out[1] < bb.z0 || out[1] > bb.z1).toBe(true);
    }
    for (const w of ws.filter((w) => !w.exterior)) expect(w.rooms.length).toBeGreaterThanOrEqual(2);
  });

  it("doors leave a walk-through gap and windows a gap between sill and head", () => {
    const pieces = wallPieces(l);
    for (const o of l.openings) {
      const line = openingLine(o)!;
      const mid = (line.t0 + line.t1) / 2;
      const at = pieces.filter((p) => {
        const ax = Math.abs(p.a[0] - p.b[0]) < 1e-3 ? "x" : "z";
        if (ax !== line.axis) return false;
        const c = ax === "x" ? p.a[0] : p.a[1];
        const t0 = ax === "x" ? Math.min(p.a[1], p.b[1]) : Math.min(p.a[0], p.b[0]);
        const t1 = ax === "x" ? Math.max(p.a[1], p.b[1]) : Math.max(p.a[0], p.b[0]);
        return Math.abs(c - line.c) < 1e-3 && t0 <= mid && mid <= t1;
      });
      const solidAt = (y: number) => at.some((p) => p.y0 <= y && y <= p.y1);
      if (o.kind === "door") {
        expect(solidAt(1.0)).toBe(false);
        expect(solidAt(DOOR_HEIGHT_M + 0.2)).toBe(true);
      } else {
        expect(solidAt(0.4)).toBe(true);
        expect(solidAt(1.5)).toBe(false);
        expect(solidAt(2.3)).toBe(true);
      }
    }
  });

  it("roomAt finds the room under a point", () => {
    const bed = rectOf(l.rooms.find((r) => r.room_id === "bed_1")!);
    expect(roomAt(l, (bed.x0 + bed.x1) / 2, (bed.z0 + bed.z1) / 2)!.room_id).toBe("bed_1");
    expect(roomAt(l, -5, -5)).toBeNull();
  });
});

describe("editing", () => {
  const l: Layout = estimateLayout(listing(2, 1, 900));
  const bed = () => rectOf(l.rooms.find((r) => r.room_id === "bed_1")!);

  it("dragging the wall between bedrooms and living space moves both sides and its doors", () => {
    const z = bed().z1;
    const moved = moveWall(l, "z", z, 1.0, z + 0.5);
    expect(validate(moved)).toEqual([]);
    expect(rectOf(moved.rooms.find((r) => r.room_id === "bed_1")!).z1).toBeCloseTo(z + 0.5, 3);
    expect(rectOf(moved.rooms.find((r) => r.room_id === "living")!).z0).toBeCloseTo(z + 0.5, 3);
    expect(totalArea(moved)).toBeCloseTo(totalArea(l), 3);
    const d = moved.openings.find((o) => o.room_ids.includes("bed_1") && o.kind === "door")!;
    expect(d.a[1]).toBeCloseTo(z + 0.5, 3);
  });

  it("moves are clamped so rooms stay at least 0.6 m", () => {
    const moved = moveWall(l, "z", bed().z1, 1.0, -10);
    expect(rectOf(moved.rooms.find((r) => r.room_id === "bed_1")!).z1).toBeCloseTo(0.6, 3);
    expect(validate(moved)).toEqual([]);
  });

  it("an exterior wall grows the apartment", () => {
    const bb = bounds(l);
    const moved = moveWall(l, "z", bb.z1, (bb.x0 + bb.x1) / 2, bb.z1 + 1);
    expect(totalArea(moved)).toBeCloseTo(totalArea(l) + (bb.x1 - bb.x0), 3);
    expect(validate(moved)).toEqual([]);
  });

  it("typing a room size moves its right and bottom walls; neighbours follow", () => {
    const b = bed();
    const resized = setRoomSize(l, "bed_1", 4.0, 3.0);
    const nb = rectOf(resized.rooms.find((r) => r.room_id === "bed_1")!);
    expect(nb.x1 - nb.x0).toBeCloseTo(4.0, 3);
    expect(nb.z1 - nb.z0).toBeCloseTo(3.0, 3);
    expect(nb.x0).toBeCloseTo(b.x0, 3);
    expect(validate(resized)).toEqual([]);
    // Openings were pulled back onto shortened walls.
    for (const o of resized.openings) expect(openingLine(o)).not.toBeNull();
  });

  it("validation explains overlaps and stray openings", () => {
    const bad: Layout = JSON.parse(JSON.stringify(l));
    bad.rooms[1].polygon = bad.rooms[0].polygon.map((p) => [p[0] + 0.5, p[1]]) as any;
    bad.openings.push({ opening_id: "x", kind: "window", room_ids: ["bed_1"], a: [0.3, 1.234], b: [0.9, 1.234], height_m: 1.2, sill_m: 0.9 });
    const errors = validate(bad);
    expect(errors.some((e) => e.includes("overlaps"))).toBe(true);
    expect(errors.some((e) => e.includes("no longer on an outside wall"))).toBe(true);
  });
});

// Plans exactly as the Mac service generates them (saved from its sample listings).
import macLayouts from "./fixtures/mac-layouts.json";

describe("the Mac's plans", () => {
  const plans = macLayouts as unknown as Layout[];

  it("are all valid here", () => {
    expect(plans.length).toBeGreaterThan(10);
    for (const p of plans) expect(validate(p), p.listing_id).toEqual([]);
  });

  it("flags a bedroom without an outside window", () => {
    const p = structuredClone(plans.find((x) => x.rooms.some((r) => r.kind === "hall"))!);
    const hall = p.rooms.find((r) => r.kind === "hall")!;
    hall.kind = "bedroom";
    expect(validate(p).join(" ")).toMatch(/no window to the outside/);
    expect(roomsWithWindows(p).has(hall.room_id)).toBe(false);
  });

  it("flags a room you can't reach and a door that's too narrow", () => {
    const p = structuredClone(plans.find((x) => x.rooms.length >= 5)!);
    const inner = p.openings.find((o) => o.kind === "door" && o.room_ids.length === 2)!;
    p.openings = p.openings.filter((o) => o !== inner);
    expect(validate(p).join(" ")).toMatch(/can't be reached through a door/);
    const q = structuredClone(plans.find((x) => x.rooms.length >= 5)!);
    const door = q.openings.find((o) => o.kind === "door" && o.room_ids.length === 2)!;
    door.b = door.a[0] === door.b[0] ? [door.a[0], door.a[1] + 0.05] : [door.a[0] + 0.05, door.a[1]];
    expect(validate(q).join(" ")).toMatch(/too narrow/);
  });

  it("keep every room at least 0.6 m when a wall is pushed past the limit", () => {
    for (const p of plans) {
      for (const w of walls(p)) {
        for (const d of [-25, 25]) {
          const next = moveWall(p, w.axis, w.c, (w.t0 + w.t1) / 2, w.c + d);
          for (const r of next.rooms) {
            const b = rectOf(r);
            expect(b.x1 - b.x0).toBeGreaterThanOrEqual(MIN_ROOM_M - 1e-9);
            expect(b.z1 - b.z0).toBeGreaterThanOrEqual(MIN_ROOM_M - 1e-9);
          }
        }
      }
    }
  });
});

describe("the one-room starter plan", () => {
  it("is valid for small, missing and huge sizes", async () => {
    const { starterLayout } = await import("../src/layout");
    for (const sqft of [null, 120, 450, 900, 3000, 9000]) {
      const p = starterLayout("x", sqft);
      expect(validate(p), String(sqft)).toEqual([]);
      expect(Math.abs(totalArea(p) - p.total_area_m2)).toBeLessThan(1e-9);
    }
  });
});
