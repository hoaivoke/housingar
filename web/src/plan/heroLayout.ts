import type { Layout } from "../layout";

// A small two-bedroom plan for the search page's drawing (not a listing).
const r = (x0: number, z0: number, x1: number, z1: number): Array<[number, number]> => [[x0, z1], [x1, z1], [x1, z0], [x0, z0]];

export const heroLayout: Layout = {
  schema: "housingar.room.v1", listing_id: "hero", estimated: true, source: "estimated", units: "meters",
  ceiling_height_m: 2.44, total_area_m2: 74.8,
  rooms: [
    { room_id: "bed_1", name: "Bedroom", kind: "bedroom", polygon: r(0, 0, 3.6, 3.4) },
    { room_id: "bed_2", name: "Bedroom", kind: "bedroom", polygon: r(3.6, 0, 6.6, 3.4) },
    { room_id: "bath", name: "Bath", kind: "bathroom", polygon: r(6.6, 0, 8.4, 3.4) },
    { room_id: "kitchen", name: "Kitchen", kind: "kitchen", polygon: r(0, 3.4, 3.0, 8.0) },
    { room_id: "living", name: "Living room", kind: "living", polygon: r(3.0, 3.4, 8.4, 8.0) },
  ],
  openings: [
    { opening_id: "d1", kind: "door", room_ids: ["bed_1", "kitchen"], a: [1.2, 3.4], b: [2.05, 3.4], height_m: 2.03, sill_m: 0 },
    { opening_id: "d2", kind: "door", room_ids: ["bed_2", "living"], a: [4.6, 3.4], b: [5.45, 3.4], height_m: 2.03, sill_m: 0 },
    { opening_id: "d3", kind: "door", room_ids: ["bath", "living"], a: [7.1, 3.4], b: [7.85, 3.4], height_m: 2.03, sill_m: 0 },
    { opening_id: "d4", kind: "door", room_ids: ["kitchen", "living"], a: [3.0, 4.2], b: [3.0, 6.0], height_m: 2.03, sill_m: 0 },
    { opening_id: "d5", kind: "door", room_ids: ["living"], a: [7.2, 8.0], b: [8.1, 8.0], height_m: 2.03, sill_m: 0 },
    { opening_id: "w1", kind: "window", room_ids: ["bed_1"], a: [1.2, 0], b: [2.4, 0], height_m: 1.2, sill_m: 0.9 },
    { opening_id: "w2", kind: "window", room_ids: ["bed_2"], a: [4.5, 0], b: [5.7, 0], height_m: 1.2, sill_m: 0.9 },
    { opening_id: "w3", kind: "window", room_ids: ["living"], a: [3.6, 8.0], b: [5.4, 8.0], height_m: 1.2, sill_m: 0.9 },
    { opening_id: "w4", kind: "window", room_ids: ["kitchen"], a: [1.0, 8.0], b: [2.0, 8.0], height_m: 1.2, sill_m: 0.9 },
  ],
};
