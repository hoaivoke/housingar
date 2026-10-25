#!/usr/bin/env node
// Stand-in for the Mac service's listing, layout, furniture and library API (docs/listings_v1.md),
// for building and testing the website without the Mac. Optionally serves the built site.
//
//   node mock/server.mjs [--port 8000] [--dist dist] [--library ../phone-library2] [--real] [--delay 0]
//
// --real makes listings look like RentCast results (sample: false, fictional 555-01xx contacts).

import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { estimateLayout } from "./estimate.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const args = Object.fromEntries(process.argv.slice(2).reduce((acc, a, i, all) => {
  if (a.startsWith("--")) acc.push([a.slice(2), all[i + 1] && !all[i + 1].startsWith("--") ? all[i + 1] : true]);
  return acc;
}, []));
const port = Number(args.port ?? 8000);
const dist = args.dist ? path.resolve(args.dist) : null;
const libraryDir = path.resolve(args.library ?? path.join(here, "..", "..", "phone-library2"));
const real = !!args.real;
const delay = Number(args.delay ?? 0);

const places = {
  blacksburg: [37.2296, -80.4139, "Blacksburg, VA"],
  christiansburg: [37.1299, -80.4089, "Christiansburg, VA"],
  roanoke: [37.271, -79.9414, "Roanoke, VA"],
};

function rng(seed) {
  let s = seed >>> 0;
  return () => ((s = (s * 1664525 + 1013904223) >>> 0) / 2 ** 32);
}

const streets = ["Maple Hollow Ln", "Ridgeview Ct", "Old Mill Rd", "Larkspur Way", "Cedar Bend Dr", "Foxglove St", "Willow Run", "Quarry Hill Rd", "Bramble Ct", "Harvest Ln", "Stonegate Dr", "Juniper Pl", "Orchard Row", "Kestrel Ave", "Millbrook Ter"];
const types = ["Apartment", "Apartment", "Apartment", "Townhouse", "Single Family", "Condo"];

const store = new Map(); // listing_id → listing
const layouts = new Map();
const furniture = new Map();

function makeListings(center, label) {
  const rand = rng(Math.round(center[0] * 1000) * 31 + Math.round(center[1] * 1000));
  const out = [];
  for (let i = 0; i < 15; i++) {
    const beds = [0, 1, 1, 2, 2, 2, 3, 3, 4][Math.floor(rand() * 9)];
    const baths = beds >= 3 ? 2 : beds === 2 && rand() > 0.5 ? 2 : 1;
    const sqft = rand() > 0.2 ? Math.round((beds === 0 ? 430 : 520 + beds * 260) * (0.85 + rand() * 0.35) / 10) * 10 : null;
    const rent = Math.round(((beds === 0 ? 780 : 650 + beds * 420) * (0.85 + rand() * 0.4)) / 5) * 5;
    const ang = rand() * Math.PI * 2, dist = 0.2 + rand() * 4.6; // miles
    const lat = center[0] + (dist / 69) * Math.cos(ang);
    const lon = center[1] + (dist / (69 * Math.cos(center[0] * Math.PI / 180))) * Math.sin(ang);
    const city = label.split(",")[0];
    const line1 = `${100 + Math.floor(rand() * 1800)} ${streets[i % streets.length]}`;
    const line2 = beds <= 2 && rand() > 0.4 ? `Apt ${1 + Math.floor(rand() * 4)}${String.fromCharCode(65 + Math.floor(rand() * 4))}` : null;
    const zip = "24060";
    const formatted = `${line1}${line2 ? ", " + line2 : ""}, ${city}, VA ${zip}`;
    const id = `${real ? "rc" : "sample"}_${Math.round(center[0] * 100)}_${Math.round(-center[1] * 100)}_${i + 1}`;
    const type = types[Math.floor(rand() * types.length)];
    const listing = {
      listing_id: id,
      source: real ? "rentcast" : "sample",
      sample: !real,
      title: `${beds === 0 ? "Studio" : `${beds} bd`} · ${baths} ba ${type.toLowerCase()}`,
      address: { line1, line2, city, state: "VA", zip, formatted },
      lat: Math.round(lat * 1e5) / 1e5,
      lon: Math.round(lon * 1e5) / 1e5,
      distance_mi: Math.round(dist * 10) / 10,
      rent_monthly: rent,
      currency: "USD",
      bedrooms: beds,
      bathrooms: baths,
      square_feet: sqft,
      property_type: type,
      listed_date: real ? new Date(Date.now() - Math.floor(rand() * 40) * 864e5).toISOString().slice(0, 10) : null,
      days_on_market: real ? Math.floor(rand() * 40) : null,
      contact: real ? { name: "Jordan Example", phone: `(555) 010-${String(10 + i).padStart(4, "0")}`, email: "leasing@example.com", office: "Example Realty" } : null,
      links: real ? {
        search_url: `https://www.google.com/search?q=${encodeURIComponent(formatted + " for rent")}`,
        maps_url: `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(formatted)}`,
        listing_url: null,
      } : { search_url: null, maps_url: null, listing_url: null },
      photos: [],
    };
    out.push(listing);
    store.set(id, listing);
  }
  return out;
}

function send(res, code, body, type = "application/json") {
  const data = typeof body === "string" || Buffer.isBuffer(body) ? body : JSON.stringify(body);
  res.writeHead(code, { "Content-Type": type, "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET,PUT,POST,DELETE,PATCH", "Access-Control-Allow-Headers": "Content-Type" });
  res.end(data);
}

function body(req) {
  return new Promise((resolve, reject) => {
    let s = "";
    req.on("data", (c) => (s += c));
    req.on("end", () => { try { resolve(s ? JSON.parse(s) : {}); } catch (e) { reject(e); } });
  });
}

function libraryItems() {
  try {
    return JSON.parse(fs.readFileSync(path.join(libraryDir, "index.json"), "utf8")).map((it) => ({
      ...it,
      mesh_url: `/v1/library/${it.item_id}/object.glb`,
      thumbnail_url: `/v1/library/${it.item_id}/thumbnail.jpg`,
      source: { scan_id: "mock", proposal_id: it.item_id },
    }));
  } catch {
    return [];
  }
}

const mime = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2", ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".json": "application/json", ".webmanifest": "application/manifest+json" };

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://x");
  const p = url.pathname;
  if (req.method === "OPTIONS") return send(res, 204, "");
  if (delay) await new Promise((r) => setTimeout(r, delay));
  try {
    if (p === "/health") return send(res, 200, { status: "ok", backend: "mock", model: "mock", device: "cpu" });
    if (p === "/v1/listings" && req.method === "GET") {
      const q = url.searchParams;
      let center, label;
      if (q.get("lat") && q.get("lon")) {
        center = [Number(q.get("lat")), Number(q.get("lon"))];
        label = "Your location";
      } else {
        const near = (q.get("near") || "").toLowerCase();
        if (!near.trim()) return send(res, 400, { detail: "Give an address (near) or lat and lon." });
        if (near.includes("nowhere")) return send(res, 200, { source: real ? "rentcast" : "sample", cached: false, center: null, listings: [] });
        const key = Object.keys(places).find((k) => near.includes(k)) ?? "blacksburg";
        [center, label] = [places[key].slice(0, 2), places[key][2]];
      }
      const radius = Math.min(25, Number(q.get("radius_mi") || 5));
      const minRent = q.get("min_rent") ? Number(q.get("min_rent")) : null;
      const maxRent = q.get("max_rent") ? Number(q.get("max_rent")) : null;
      const beds = q.get("bedrooms");
      let list = makeListings(center, label.includes("location") ? "Blacksburg, VA" : label);
      list = list.filter((l) => l.distance_mi <= radius && (minRent == null || l.rent_monthly >= minRent) && (maxRent == null || l.rent_monthly <= maxRent) &&
        (beds == null || beds === "" || (beds.endsWith("+") ? l.bedrooms >= Number(beds.slice(0, -1)) : l.bedrooms === Number(beds))));
      list.sort((a, b) => a.distance_mi - b.distance_mi);
      return send(res, 200, { source: real ? "rentcast" : "sample", cached: false, center: { lat: center[0], lon: center[1], label }, listings: list.slice(0, Number(q.get("limit") || 40)) });
    }
    let m;
    if ((m = p.match(/^\/v1\/listings\/([^/]+)$/))) {
      const l = store.get(m[1]);
      return l ? send(res, 200, l) : send(res, 404, { detail: "Unknown listing" });
    }
    if ((m = p.match(/^\/v1\/listings\/([^/]+)\/layout$/))) {
      const l = store.get(m[1]);
      if (!l) return send(res, 404, { detail: "Unknown listing" });
      if (req.method === "GET") return send(res, 200, layouts.get(l.listing_id) ?? estimateLayout(l));
      if (req.method === "PUT") {
        const b = await body(req);
        if (b.schema !== "housingar.room.v1" || !Array.isArray(b.rooms) || b.rooms.length === 0) return send(res, 422, { detail: "Invalid layout" });
        layouts.set(l.listing_id, b);
        return send(res, 200, b);
      }
      if (req.method === "DELETE") { layouts.delete(l.listing_id); return send(res, 200, estimateLayout(l)); }
    }
    if ((m = p.match(/^\/v1\/listings\/([^/]+)\/furniture$/))) {
      const l = store.get(m[1]);
      if (!l) return send(res, 404, { detail: "Unknown listing" });
      if (req.method === "GET") return send(res, 200, furniture.get(l.listing_id) ?? { schema: "housingar.room_furniture.v1", listing_id: l.listing_id, placements: [], updated: null });
      if (req.method === "PUT") {
        const b = await body(req);
        const ids = new Set(libraryItems().map((i) => i.item_id));
        if (!Array.isArray(b.placements) || b.placements.some((pl) => !ids.has(pl.item_id))) return send(res, 422, { detail: "Placements must use items from My stuff" });
        const saved = { ...b, updated: new Date().toISOString() };
        furniture.set(l.listing_id, saved);
        return send(res, 200, saved);
      }
    }
    if (p === "/v1/library") return send(res, 200, { items: libraryItems() });
    if ((m = p.match(/^\/v1\/library\/([^/]+)\/(object\.glb|thumbnail\.jpg)$/))) {
      const f = path.join(libraryDir, m[1], m[2]);
      if (!fs.existsSync(f)) return send(res, 404, { detail: "Item file missing" });
      return send(res, 200, fs.readFileSync(f), m[2].endsWith(".glb") ? "model/gltf-binary" : "image/jpeg");
    }
    if (p.startsWith("/v1/")) return send(res, 404, { detail: "Not found" });
    if (dist) {
      let f = path.join(dist, decodeURIComponent(p));
      if (!f.startsWith(dist) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) f = path.join(dist, "index.html");
      return send(res, 200, fs.readFileSync(f), mime[path.extname(f)] ?? "application/octet-stream");
    }
    send(res, 404, { detail: "Not found" });
  } catch (e) {
    send(res, 500, { detail: String(e) });
  }
});

server.listen(port, "127.0.0.1", () => console.log(`mock API on http://127.0.0.1:${port} (library: ${libraryDir}${dist ? ", site: " + dist : ""}${real ? ", RentCast-like" : ", sample"})`));
