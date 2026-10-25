// Floor-plan geometry for housingar.room.v1 layouts (docs/listings_v1.md).
// Plan coordinates: metres, x to the right, z down the plan (toward the viewer in 3D), y up.
// Rooms are axis-aligned rectangles that tile the apartment. Pure functions, no DOM.

export type Vec2 = [number, number];
export type RoomKind = "living" | "kitchen" | "bedroom" | "bathroom" | "hall" | "closet" | "other";

export interface Room {
  room_id: string;
  name: string;
  kind: RoomKind;
  polygon: Vec2[];
}

export interface Opening {
  opening_id: string;
  kind: "door" | "window";
  room_ids: string[];
  a: Vec2;
  b: Vec2;
  height_m: number;
  sill_m: number;
}

export interface Layout {
  schema: "housingar.room.v1";
  listing_id: string;
  estimated: boolean;
  source: "estimated" | "user_edited";
  units: "meters";
  ceiling_height_m: number;
  total_area_m2: number;
  rooms: Room[];
  openings: Opening[];
}

export interface Rect { x0: number; z0: number; x1: number; z1: number }

export const MIN_ROOM_M = 0.6;
export const MAX_ROOM_M = 20;
const EPS = 1e-3;

export function rectOf(room: Room): Rect {
  const xs = room.polygon.map((p) => p[0]);
  const zs = room.polygon.map((p) => p[1]);
  return { x0: Math.min(...xs), z0: Math.min(...zs), x1: Math.max(...xs), z1: Math.max(...zs) };
}

/** Counter-clockwise seen from above (+y): with x right and z toward the viewer, that is x0z1 → x1z1 → x1z0 → x0z0. */
export function polygonOf(r: Rect): Vec2[] {
  return [[r.x0, r.z1], [r.x1, r.z1], [r.x1, r.z0], [r.x0, r.z0]];
}

export function area(r: Rect): number {
  return (r.x1 - r.x0) * (r.z1 - r.z0);
}

export function bounds(layout: Layout): Rect {
  const rs = layout.rooms.map(rectOf);
  return {
    x0: Math.min(...rs.map((r) => r.x0)),
    z0: Math.min(...rs.map((r) => r.z0)),
    x1: Math.max(...rs.map((r) => r.x1)),
    z1: Math.max(...rs.map((r) => r.z1)),
  };
}

export function totalArea(layout: Layout): number {
  return layout.rooms.reduce((s, r) => s + area(rectOf(r)), 0);
}

/**
 * A wall line segment. `axis` is the coordinate that is constant along it: "x" = a wall running
 * along z at x = c, "z" = a wall running along x at z = c. [t0, t1] is its extent along the other
 * axis. `exterior` walls have rooms on one side only; `outward` points away from them.
 */
export interface Wall {
  axis: "x" | "z";
  c: number;
  t0: number;
  t1: number;
  exterior: boolean;
  rooms: string[];
  outward: Vec2 | null;
}

interface Edge { axis: "x" | "z"; c: number; t0: number; t1: number; room: string; side: 1 | -1 }

function edgesOf(room: Room): Edge[] {
  const r = rectOf(room);
  // side: +1 if the room lies on the + side of the line, -1 on the − side.
  return [
    { axis: "x", c: r.x0, t0: r.z0, t1: r.z1, room: room.room_id, side: 1 },
    { axis: "x", c: r.x1, t0: r.z0, t1: r.z1, room: room.room_id, side: -1 },
    { axis: "z", c: r.z0, t0: r.x0, t1: r.x1, room: room.room_id, side: 1 },
    { axis: "z", c: r.z1, t0: r.x0, t1: r.x1, room: room.room_id, side: -1 },
  ];
}

const key = (axis: string, c: number) => `${axis}:${Math.round(c * 1000)}`;

/** Every wall of the plan, split wherever the rooms on either side change. */
export function walls(layout: Layout): Wall[] {
  const lines = new Map<string, Edge[]>();
  for (const room of layout.rooms) {
    for (const e of edgesOf(room)) {
      const k = key(e.axis, e.c);
      if (!lines.has(k)) lines.set(k, []);
      lines.get(k)!.push(e);
    }
  }
  const out: Wall[] = [];
  for (const edges of lines.values()) {
    const cuts = [...new Set(edges.flatMap((e) => [e.t0, e.t1]).map((t) => Math.round(t * 1000) / 1000))].sort((a, b) => a - b);
    for (let i = 0; i + 1 < cuts.length; i++) {
      const t0 = cuts[i], t1 = cuts[i + 1];
      if (t1 - t0 < EPS) continue;
      const mid = (t0 + t1) / 2;
      const covering = edges.filter((e) => e.t0 - EPS <= mid && mid <= e.t1 + EPS);
      if (covering.length === 0) continue;
      const plus = covering.some((e) => e.side === 1);
      const minus = covering.some((e) => e.side === -1);
      const axis = edges[0].axis;
      const exterior = !(plus && minus);
      let outward: Vec2 | null = null;
      if (exterior) {
        const s = plus ? -1 : 1; // empty side
        outward = axis === "x" ? [s, 0] : [0, s];
      }
      out.push({ axis, c: edges[0].c, t0, t1, exterior, rooms: [...new Set(covering.map((e) => e.room))], outward });
    }
  }
  return out;
}

/** The opening's position on a wall line, or null if it isn't axis-aligned. */
export function openingLine(o: Opening): { axis: "x" | "z"; c: number; t0: number; t1: number } | null {
  if (Math.abs(o.a[0] - o.b[0]) < EPS) {
    return { axis: "x", c: o.a[0], t0: Math.min(o.a[1], o.b[1]), t1: Math.max(o.a[1], o.b[1]) };
  }
  if (Math.abs(o.a[1] - o.b[1]) < EPS) {
    return { axis: "z", c: o.a[1], t0: Math.min(o.a[0], o.b[0]), t1: Math.max(o.a[0], o.b[0]) };
  }
  return null;
}

/** A solid piece of wall for 3D: segment a–b on the plan, from height y0 to y1. */
export interface WallPiece { a: Vec2; b: Vec2; y0: number; y1: number; exterior: boolean; outward: Vec2 | null }

export const DOOR_HEIGHT_M = 2.03;

/** Walls with door and window gaps cut out, as boxes to extrude. */
export function wallPieces(layout: Layout): WallPiece[] {
  const H = layout.ceiling_height_m;
  const pieces: WallPiece[] = [];
  const point = (w: Wall, t: number): Vec2 => (w.axis === "x" ? [w.c, t] : [t, w.c]);
  for (const w of walls(layout)) {
    const holes = layout.openings
      .map((o) => ({ o, l: openingLine(o) }))
      .filter(({ l }) => l && l.axis === w.axis && Math.abs(l.c - w.c) < EPS && l.t1 > w.t0 + EPS && l.t0 < w.t1 - EPS)
      .map(({ o, l }) => ({ o, t0: Math.max(l!.t0, w.t0), t1: Math.min(l!.t1, w.t1) }))
      .sort((p, q) => p.t0 - q.t0);
    let t = w.t0;
    const add = (t0: number, t1: number, y0: number, y1: number) => {
      if (t1 - t0 > EPS && y1 - y0 > EPS) pieces.push({ a: point(w, t0), b: point(w, t1), y0, y1, exterior: w.exterior, outward: w.outward });
    };
    for (const h of holes) {
      add(t, h.t0, 0, H);
      if (h.o.kind === "door") {
        add(h.t0, h.t1, Math.min(h.o.height_m || DOOR_HEIGHT_M, H), H);
      } else {
        const sill = h.o.sill_m ?? 0.9;
        const top = Math.min(sill + (h.o.height_m || 1.2), H);
        add(h.t0, h.t1, 0, sill);
        add(h.t0, h.t1, top, H);
      }
      t = Math.max(t, h.t1);
    }
    add(t, w.t1, 0, H);
  }
  return pieces;
}

function clone(layout: Layout): Layout {
  return JSON.parse(JSON.stringify(layout));
}

/**
 * Drag a wall line: every room edge on line `axis = c` that is connected (collinear and
 * touching) to the point at `t` moves to `newC`, so neighbouring rooms stay tiled. The move is
 * clamped so no affected room becomes smaller than MIN_ROOM_M. Openings on that line move with
 * it; openings on walls that got shorter are pulled back inside them. Returns the new layout.
 */
export function moveWall(layout: Layout, axis: "x" | "z", c: number, t: number, newC: number): Layout {
  const next = clone(layout);
  const rects = next.rooms.map((r) => ({ room: r, r: rectOf(r) }));
  const on = (r: Rect, which: "lo" | "hi") => {
    const v = axis === "x" ? (which === "lo" ? r.x0 : r.x1) : (which === "lo" ? r.z0 : r.z1);
    return Math.abs(v - c) < EPS;
  };
  const span = (r: Rect): [number, number] => (axis === "x" ? [r.z0, r.z1] : [r.x0, r.x1]);
  // Grow the connected run of edges along the line starting from the grabbed point.
  const candidates = rects.filter(({ r }) => on(r, "lo") || on(r, "hi"));
  let lo = t, hi = t;
  const chosen = new Set<number>();
  let grew = true;
  while (grew) {
    grew = false;
    candidates.forEach(({ r }, i) => {
      if (chosen.has(i)) return;
      const [s0, s1] = span(r);
      if (s0 <= hi + EPS && s1 >= lo - EPS) {
        chosen.add(i);
        lo = Math.min(lo, s0);
        hi = Math.max(hi, s1);
        grew = true;
      }
    });
  }
  if (chosen.size === 0) return next;
  // Clamp: rooms whose low edge moves must keep their size, and likewise for high edges.
  let min = -Infinity, max = Infinity;
  for (const i of chosen) {
    const { r } = candidates[i];
    if (on(r, "lo")) max = Math.min(max, (axis === "x" ? r.x1 : r.z1) - MIN_ROOM_M);
    if (on(r, "hi")) min = Math.max(min, (axis === "x" ? r.x0 : r.z0) + MIN_ROOM_M);
  }
  // Whole millimetres, rounded inward at the limits so no room ends up under the minimum.
  const lowest = Math.ceil(min * 1000 - 1e-6) / 1000, highest = Math.floor(max * 1000 + 1e-6) / 1000;
  if (lowest > highest) return next;
  const target = Math.min(highest, Math.max(lowest, Math.round(newC * 1000) / 1000));
  for (const i of chosen) {
    const { room, r } = candidates[i];
    const n = { ...r };
    if (axis === "x") {
      if (on(r, "lo")) n.x0 = target;
      if (on(r, "hi")) n.x1 = target;
    } else {
      if (on(r, "lo")) n.z0 = target;
      if (on(r, "hi")) n.z1 = target;
    }
    room.polygon = polygonOf(n);
  }
  for (const o of next.openings) {
    const l = openingLine(o);
    if (l && l.axis === axis && Math.abs(l.c - c) < EPS && l.t1 >= lo - EPS && l.t0 <= hi + EPS) {
      if (axis === "x") { o.a[0] = target; o.b[0] = target; } else { o.a[1] = target; o.b[1] = target; }
    }
  }
  fitOpenings(next);
  next.total_area_m2 = Math.round(totalArea(next) * 100) / 100;
  return next;
}

/** Contiguous runs [t0, t1] of the given walls along one line. */
function runs(ws: Wall[]): Array<[number, number]> {
  const out: Array<[number, number]> = [];
  for (const w of [...ws].sort((p, q) => p.t0 - q.t0)) {
    const last = out[out.length - 1];
    if (last && w.t0 <= last[1] + EPS) last[1] = Math.max(last[1], w.t1);
    else out.push([w.t0, w.t1]);
  }
  return out;
}

/** Pull each opening back inside the wall it sits on (shortening it if that wall got shorter). */
export function fitOpenings(layout: Layout): void {
  const ws = walls(layout);
  for (const o of layout.openings) {
    const l = openingLine(o);
    if (!l) continue;
    const between = o.room_ids.length > 1;
    const line = ws.filter((w) => w.axis === l.axis && Math.abs(w.c - l.c) < EPS &&
      o.room_ids.every((r) => w.rooms.includes(r)) && (between ? !w.exterior : w.exterior));
    const rs = runs(line);
    if (rs.length === 0) continue;
    const mid = (l.t0 + l.t1) / 2;
    const run = rs.find(([a, b]) => a - EPS <= mid && mid <= b + EPS) ??
      rs.reduce((best, r) => (Math.abs((r[0] + r[1]) / 2 - mid) < Math.abs((best[0] + best[1]) / 2 - mid) ? r : best));
    const [s0, s1] = run;
    const margin = Math.min(0.1, (s1 - s0) / 4);
    const width = Math.min(l.t1 - l.t0, s1 - s0 - 2 * margin);
    const t0 = Math.min(Math.max(l.t0, s0 + margin), s1 - margin - width);
    const t1 = t0 + width;
    if (l.axis === "x") { o.a = [l.c, round(t0)]; o.b = [l.c, round(t1)]; } else { o.a = [round(t0), l.c]; o.b = [round(t1), l.c]; }
  }
}

function round(v: number) {
  return Math.round(v * 1000) / 1000;
}

/** Resize a room by moving its right and bottom walls (neighbours follow). */
export function setRoomSize(layout: Layout, roomId: string, width: number | null, depth: number | null): Layout {
  let next = layout;
  const room = () => next.rooms.find((r) => r.room_id === roomId)!;
  if (width != null) {
    const r = rectOf(room());
    next = moveWall(next, "x", r.x1, (r.z0 + r.z1) / 2, r.x0 + width);
  }
  if (depth != null) {
    const r = rectOf(room());
    next = moveWall(next, "z", r.z1, (r.x0 + r.x1) / 2, r.z0 + depth);
  }
  return next;
}

/** Problems that would make the Mac refuse the plan (docs/listings_v1.md), in plain words. */
export function validate(layout: Layout): string[] {
  const errors: string[] = [];
  if (layout.rooms.length === 0) errors.push("The plan has no rooms.");
  if (layout.rooms.length > 20) errors.push("A plan can have at most 20 rooms.");
  if (layout.openings.length > 60) errors.push("A plan can have at most 60 doors and windows.");
  const rects = layout.rooms.map((r) => ({ r: rectOf(r), room: r }));
  for (const { r, room } of rects) {
    if (room.polygon.length !== 4) errors.push(`${room.name} isn't a rectangle.`);
    const w = r.x1 - r.x0, d = r.z1 - r.z0;
    if (w < MIN_ROOM_M - EPS || d < MIN_ROOM_M - EPS) errors.push(`${room.name} is smaller than ${MIN_ROOM_M} m.`);
    if (w > MAX_ROOM_M + EPS || d > MAX_ROOM_M + EPS) errors.push(`${room.name} is larger than ${MAX_ROOM_M} m.`);
    const n = room.name.trim();
    if (n.length < 1 || n.length > 40) errors.push("Room names need 1–40 characters.");
  }
  for (let i = 0; i < rects.length; i++) {
    for (let j = i + 1; j < rects.length; j++) {
      const a = rects[i].r, b = rects[j].r;
      const ox = Math.min(a.x1, b.x1) - Math.max(a.x0, b.x0);
      const oz = Math.min(a.z1, b.z1) - Math.max(a.z0, b.z0);
      if (ox > EPS && oz > EPS) errors.push(`${rects[i].room.name} overlaps ${rects[j].room.name}.`);
    }
  }
  // The same rules the Mac checks before it saves a plan.
  const rect = new Map(layout.rooms.map((room) => [room.room_id, rectOf(room)]));
  const name = (id: string) => layout.rooms.find((room) => room.room_id === id)?.name ?? id;
  const outer = layout.rooms.length ? bounds(layout) : null;
  const graph = new Map(layout.rooms.map((room) => [room.room_id, new Set<string>()]));
  const entries = new Set<string>();
  const windowed = new Set<string>();
  for (const o of layout.openings) {
    const ids = o.room_ids;
    if (ids.length < 1 || ids.length > 2 || new Set(ids).size !== ids.length || ids.some((id) => !rect.has(id))) {
      errors.push(`A ${o.kind} refers to a room that isn't in the plan.`);
      continue;
    }
    const [r0, r1] = ids.map((id) => rect.get(id)!);
    const where = ids.length === 2 ? `between ${name(ids[0])} and ${name(ids[1])}` : `in ${name(ids[0])}`;
    if (o.height_m + o.sill_m > layout.ceiling_height_m + EPS) errors.push(`The ${o.kind} ${where} is taller than the ceiling.`);
    if (o.kind === "door" && o.sill_m > EPS) errors.push(`The door ${where} doesn't start at the floor.`);
    if (Math.hypot(o.b[0] - o.a[0], o.b[1] - o.a[1]) < MIN_OPENING_M) {
      errors.push(`The ${o.kind} ${where} is too narrow; make the wall it's on longer.`);
      continue;
    }
    if (ids.length === 2) {
      if (o.kind === "window") errors.push(`The window ${where} has to be on an outside wall.`);
      else if (!onRectSide(r0, o.a, o.b) || !onRectSide(r1, o.a, o.b)) errors.push(`${name(ids[0])} and ${name(ids[1])} no longer share a wall for their door.`);
      else { graph.get(ids[0])!.add(ids[1]); graph.get(ids[1])!.add(ids[0]); }
    } else if (!onRectSide(r0, o.a, o.b) || !outer || !onRectSide(outer, o.a, o.b)) {
      errors.push(`The ${o.kind} ${where} is no longer on an outside wall.`);
    } else if (o.kind === "door") entries.add(ids[0]);
    else windowed.add(ids[0]);
  }
  const reached = new Set(entries);
  const queue = [...entries];
  while (queue.length) for (const next of graph.get(queue.pop()!) ?? []) if (!reached.has(next)) { reached.add(next); queue.push(next); }
  if (entries.size === 0 && layout.rooms.length) errors.push("The plan needs a door to the outside.");
  else for (const room of layout.rooms) if (!reached.has(room.room_id)) errors.push(`${room.name} can't be reached through a door.`);
  for (const room of layout.rooms) {
    if ((room.kind === "living" || room.kind === "bedroom") && !windowed.has(room.room_id)) {
      errors.push(`${room.name} has no window to the outside, so it can't be a ${room.kind === "living" ? "living room" : "bedroom"}.`);
    }
  }
  return errors;
}

/** The shortest door or window the Mac accepts. */
export const MIN_OPENING_M = 0.1;

function onRectSide(r: Rect, a: Vec2, b: Vec2): boolean {
  if (Math.abs(a[0] - b[0]) < EPS) {
    return (Math.abs(a[0] - r.x0) < EPS || Math.abs(a[0] - r.x1) < EPS) && Math.min(a[1], b[1]) >= r.z0 - EPS && Math.max(a[1], b[1]) <= r.z1 + EPS;
  }
  if (Math.abs(a[1] - b[1]) < EPS) {
    return (Math.abs(a[1] - r.z0) < EPS || Math.abs(a[1] - r.z1) < EPS) && Math.min(a[0], b[0]) >= r.x0 - EPS && Math.max(a[0], b[0]) <= r.x1 + EPS;
  }
  return false;
}

/** Rooms with a window on an outside wall (only these can be living rooms or bedrooms). */
export function roomsWithWindows(layout: Layout): Set<string> {
  return new Set(layout.openings.filter((o) => o.kind === "window" && o.room_ids.length === 1).map((o) => o.room_ids[0]));
}

/** Which room a plan point is in. */
export function roomAt(layout: Layout, x: number, z: number): Room | null {
  return layout.rooms.find((room) => {
    const r = rectOf(room);
    return x >= r.x0 && x <= r.x1 && z >= r.z0 && z <= r.z1;
  }) ?? null;
}

/**
 * One open room of the listing's size, for when the Mac can't estimate rooms
 * (say five bedrooms in 600 sq ft). It follows the same rules, so it saves once edited.
 */
export function starterLayout(listingId: string, squareFeet: number | null): Layout {
  const areaM2 = Math.min(Math.max((squareFeet ?? 450) * 0.09290304, 9), 400);
  const width = Math.round(Math.min(20, Math.max(3, Math.sqrt(areaM2 * 1.25))) * 100) / 100;
  const depth = Math.round(Math.min(20, Math.max(3, areaM2 / width)) * 100) / 100;
  return {
    schema: "housingar.room.v1", listing_id: listingId, estimated: true, source: "estimated", units: "meters",
    ceiling_height_m: 2.44, total_area_m2: width * depth,
    rooms: [{ room_id: "main", name: "Open room", kind: "living", polygon: polygonOf({ x0: 0, z0: 0, x1: width, z1: depth }) }],
    openings: [
      { opening_id: "door_entry", kind: "door", room_ids: ["main"], a: [0.4, depth], b: [1.3, depth], height_m: DOOR_HEIGHT_M, sill_m: 0 },
      { opening_id: "window_1", kind: "window", room_ids: ["main"], a: [width / 2 - 0.6, 0], b: [width / 2 + 0.6, 0], height_m: 1.2, sill_m: 0.9 },
    ],
  };
}
