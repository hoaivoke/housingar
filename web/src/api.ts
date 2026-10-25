// Client for the Mac service (docs/listings_v1.md). The site is served by the service itself,
// so every URL is same-origin; in development Vite proxies /v1 to it.
import type { Layout } from "./layout";

export interface Listing {
  listing_id: string;
  source: "rentcast" | "sample" | "dataset" | string;
  sample: boolean;
  title: string;
  /** Saved-dataset listings: the property or post name, its floor plan, and when it was collected. */
  name?: string | null;
  plan_name?: string | null;
  collected?: string | null;
  /** "bedroom" when one person rents one bedroom (by-the-bed leases, rooms); else the whole place. */
  rent_per?: "bedroom" | "unit" | null;
  address: { line1: string; line2: string | null; city: string; state: string; zip: string; formatted: string };
  lat: number;
  lon: number;
  distance_mi: number | null;
  rent_monthly: number;
  currency: string;
  bedrooms: number;
  bathrooms: number;
  square_feet: number | null;
  property_type: string;
  listed_date: string | null;
  days_on_market: number | null;
  contact: { name?: string | null; phone?: string | null; email?: string | null; office?: string | null } | null;
  links: { search_url: string | null; maps_url: string | null; listing_url: string | null };
  photos: string[];
}

export interface SearchResult {
  source: string;
  cached: boolean;
  center: { lat: number | null; lon: number | null; label: string } | null;
  listings: Listing[];
  /** Present when the listings come from the Mac's saved listings file. */
  dataset?: { name?: string | null; source?: string | null; source_url?: string | null; collected?: string | null; note?: string | null };
}

/** The name to show for a listing: the property's name when it has one, else the street. */
export function placeName(l: Listing): string {
  const street = l.address.line1 + (l.address.line2 ? `, ${l.address.line2}` : "");
  return l.name && l.name.trim() && l.name.trim() !== l.address.line1 ? l.name.trim() : street;
}

/** "a month" or "a month per bedroom". */
export function rentPer(l: Listing): string {
  return l.rent_per === "bedroom" ? " a month per bedroom" : " a month";
}

/** "2026-10-25" → "Oct 25, 2026". */
export function dayText(iso: string | null | undefined): string {
  if (!iso || !/^\d{4}-\d{2}-\d{2}/.test(iso)) return iso ?? "";
  return new Date(iso.slice(0, 10) + "T12:00:00Z").toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
}

export interface SearchQuery {
  near?: string;
  lat?: number;
  lon?: number;
  radius_mi?: number;
  max_rent?: number | null;
  bedrooms?: string | null;
}

export interface LibraryItem {
  item_id: string;
  name: string;
  dimensions_m: [number, number, number] | null;
  mesh_url: string | null;
  thumbnail_url: string | null;
  method: string | null;
  low_quality: boolean;
  created: string | null;
}

/** Depth-only reconstructions (the older method) are blobby; say so next to them. */
export function isRough(i: LibraryItem): boolean {
  return i.low_quality || i.method === "depth_fusion";
}

export interface Placement {
  placement_id: string;
  item_id: string;
  name: string;
  position: [number, number, number];
  yaw_deg: number;
}

export interface Furniture {
  schema: "housingar.room_furniture.v1";
  listing_id: string;
  placements: Placement[];
  /** Server-owned; left out when saving. */
  updated?: string | null;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...init });
  } catch {
    throw new ApiError(0, "Can't reach the Mac. Check that the service is running, then try again.");
  }
  const text = await res.text();
  let data: any = null;
  try { data = text ? JSON.parse(text) : null; } catch { /* not JSON */ }
  if (!res.ok) {
    const detail = data?.detail ?? (text || res.statusText);
    throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data as T;
}

export const api = {
  search(q: SearchQuery): Promise<SearchResult> {
    const p = new URLSearchParams();
    if (q.lat != null && q.lon != null) { p.set("lat", String(q.lat)); p.set("lon", String(q.lon)); } else if (q.near) p.set("near", q.near);
    if (q.radius_mi) p.set("radius_mi", String(q.radius_mi));
    if (q.max_rent) p.set("max_rent", String(q.max_rent));
    if (q.bedrooms) p.set("bedrooms", q.bedrooms);
    p.set("limit", "100");
    return call(`/v1/listings?${p}`);
  },
  listing: (id: string) => call<Listing>(`/v1/listings/${encodeURIComponent(id)}`),
  layout: (id: string) => call<Layout>(`/v1/listings/${encodeURIComponent(id)}/layout`),
  saveLayout: (id: string, l: Layout) => call<Layout>(`/v1/listings/${encodeURIComponent(id)}/layout`, { method: "PUT", body: JSON.stringify(l) }),
  resetLayout: (id: string) => call<Layout>(`/v1/listings/${encodeURIComponent(id)}/layout`, { method: "DELETE" }),
  furniture: (id: string) => call<Furniture>(`/v1/listings/${encodeURIComponent(id)}/furniture`),
  saveFurniture: (id: string, f: Furniture) => call<Furniture>(`/v1/listings/${encodeURIComponent(id)}/furniture`, { method: "PUT", body: JSON.stringify(f) }),
  library: async () => {
    const r = await call<{ items: LibraryItem[] }>("/v1/library");
    // Photo-quality models first, then newest first.
    const rank = (i: LibraryItem) => (isRough(i) ? 1 : 0);
    r.items.sort((a, b) => rank(a) - rank(b) || (b.created ?? "").localeCompare(a.created ?? ""));
    return r;
  },
};
