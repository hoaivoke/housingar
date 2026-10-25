// Stand-in for the Mac's estimated floor plan (housingar.room.v1), used by the mock API and tests.
// The real generator is the Mac service's; this one only has to be valid and plausible.

const r3 = (v) => Math.round(v * 1000) / 1000;
const rect = (x0, z0, x1, z1) => [[r3(x0), r3(z1)], [r3(x1), r3(z1)], [r3(x1), r3(z0)], [r3(x0), r3(z0)]];

export function estimateLayout(listing) {
  const beds = Math.max(0, Math.round(listing.bedrooms ?? 1));
  const baths = Math.max(1, Math.round(listing.bathrooms ?? 1));
  const area = listing.square_feet ? listing.square_feet * 0.092903 : ({ 0: 40, 1: 60, 2: 85, 3: 110 }[beds] ?? 110 + 25 * (beds - 3));
  const rooms = [];
  const openings = [];
  let n = 0;
  const id = (p) => `${p}_${++n}`;
  const door = (rooms_, a, b) => openings.push({ opening_id: id("door"), kind: "door", room_ids: rooms_, a, b, height_m: 2.03, sill_m: 0 });
  const win = (room, a, b) => openings.push({ opening_id: id("window"), kind: "window", room_ids: [room], a, b, height_m: 1.2, sill_m: 0.9 });

  if (beds === 0) {
    const D = Math.sqrt(area / 1.35), W = area / D, side = 2.0, bathD = 2.4;
    rooms.push({ room_id: "main", name: "Main room", kind: "living", polygon: rect(0, 0, W - side, D) });
    rooms.push({ room_id: "bath_1", name: "Bathroom", kind: "bathroom", polygon: rect(W - side, 0, W, bathD) });
    rooms.push({ room_id: "kitchen", name: "Kitchen", kind: "kitchen", polygon: rect(W - side, bathD, W, D) });
    door(["main", "bath_1"], [r3(W - side), 0.8], [r3(W - side), 1.6]);
    door(["main", "kitchen"], [r3(W - side), r3(bathD + 0.3)], [r3(W - side), r3(Math.min(D - 0.3, bathD + 1.7))]);
    door(["main"], [r3((W - side) / 2 - 0.45), r3(D)], [r3((W - side) / 2 + 0.45), r3(D)]);
    win("main", [r3((W - side) / 2 - 0.8), 0], [r3((W - side) / 2 + 0.8), 0]);
    return finish(listing, rooms, openings);
  }

  const bedW = 3.4, bathW = 1.7;
  let d1 = 3.4;
  const W = beds * bedW + baths * bathW;
  let d2 = (area - W * d1) / W;
  if (d2 < 2.8) { d2 = 2.8; d1 = Math.max(2.6, (area - W * d2) / W); }
  // Top row: bedrooms then bathrooms. Bottom row: kitchen then living.
  let x = 0;
  const top = [];
  for (let i = 1; i <= beds; i++) { top.push({ room_id: `bed_${i}`, name: beds === 1 ? "Bedroom" : `Bedroom ${i}`, kind: "bedroom", x0: x, x1: x + bedW }); x += bedW; }
  for (let i = 1; i <= baths; i++) { top.push({ room_id: `bath_${i}`, name: baths === 1 ? "Bathroom" : `Bathroom ${i}`, kind: "bathroom", x0: x, x1: x + bathW }); x += bathW; }
  for (const t of top) rooms.push({ room_id: t.room_id, name: t.name, kind: t.kind, polygon: rect(t.x0, 0, t.x1, d1) });
  const kW = Math.min(3.2, W * 0.4);
  rooms.push({ room_id: "kitchen", name: "Kitchen", kind: "kitchen", polygon: rect(0, d1, kW, d1 + d2) });
  rooms.push({ room_id: "living", name: "Living room", kind: "living", polygon: rect(kW, d1, W, d1 + d2) });
  const bottom = [{ id: "kitchen", x0: 0, x1: kW }, { id: "living", x0: kW, x1: W }];
  for (const t of top) {
    const w = t.kind === "bathroom" ? 0.75 : 0.85;
    // Put the door where the room overlaps the bottom room with the longest shared wall.
    const shared = bottom.map((b) => ({ b, lo: Math.max(t.x0, b.x0), hi: Math.min(t.x1, b.x1) })).filter((s) => s.hi - s.lo > w + 0.2)
      .sort((p, q) => (q.hi - q.lo) - (p.hi - p.lo))[0];
    const c = (shared.lo + shared.hi) / 2;
    door([t.room_id, shared.b.id], [r3(c - w / 2), r3(d1)], [r3(c + w / 2), r3(d1)]);
    if (t.kind === "bedroom") win(t.room_id, [r3((t.x0 + t.x1) / 2 - 0.6), 0], [r3((t.x0 + t.x1) / 2 + 0.6), 0]);
  }
  const zb = r3(d1 + d2);
  door(["kitchen", "living"], [r3(kW), r3(d1 + 0.4)], [r3(kW), r3(Math.min(d1 + d2 - 0.4, d1 + 2.0))]);
  door(["living"], [r3(W - 1.2), zb], [r3(W - 0.3), zb]);
  const lw = W - kW;
  if (lw - 1.8 >= 0.6) win("living", [r3(kW + 0.3), zb], [r3(kW + 0.3 + Math.min(1.6, lw - 1.8)), zb]);
  else win("living", [r3(W), r3(d1 + 0.4)], [r3(W), r3(d1 + Math.min(d2 - 0.4, 1.6))]);
  win("kitchen", [r3(kW / 2 - 0.5), zb], [r3(kW / 2 + 0.5), zb]);
  return finish(listing, rooms, openings);
}

function finish(listing, rooms, openings) {
  const total = rooms.reduce((s, r) => {
    const xs = r.polygon.map((p) => p[0]), zs = r.polygon.map((p) => p[1]);
    return s + (Math.max(...xs) - Math.min(...xs)) * (Math.max(...zs) - Math.min(...zs));
  }, 0);
  return {
    schema: "housingar.room.v1", listing_id: listing.listing_id, estimated: true, source: "estimated", units: "meters",
    ceiling_height_m: 2.44, total_area_m2: Math.round(total * 100) / 100, rooms, openings,
  };
}
