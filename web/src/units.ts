// Lengths are metres everywhere in data; people in the US read feet and inches.
export type Unit = "ft" | "m";

const KEY = "roomfit.unit";
export function getUnit(): Unit {
  try { return (localStorage.getItem(KEY) as Unit) || "ft"; } catch { return "ft"; }
}
export function setUnit(u: Unit) {
  try { localStorage.setItem(KEY, u); } catch { /* private mode */ }
}

/** 3.76 → `12′ 4″` (ft) or `3.76 m`. */
export function length(m: number, unit: Unit = getUnit()): string {
  if (unit === "m") return `${m.toFixed(2)} m`;
  let inches = Math.round(m / 0.0254);
  const ft = Math.floor(inches / 12);
  inches -= ft * 12;
  return inches ? `${ft}′ ${inches}″` : `${ft}′`;
}

export function areaText(m2: number, unit: Unit = getUnit()): string {
  return unit === "m" ? `${m2.toFixed(1)} m²` : `${Math.round(m2 / 0.092903)} sq ft`;
}

/**
 * Parses what someone types for a length: `12' 4"`, `12 4`, `12.5 ft`, `148 in`, `3.7 m`, `370 cm`.
 * A bare number is in the current unit (feet, or metres). Returns metres, or null.
 */
export function parseLength(text: string, unit: Unit = getUnit()): number | null {
  const s = text.trim().toLowerCase().replace(/[’′]/g, "'").replace(/[”″]/g, '"').replace(/,/g, ".");
  if (!s) return null;
  let m: RegExpMatchArray | null;
  if ((m = s.match(/^(\d+(?:\.\d+)?)\s*cm$/))) return Number(m[1]) / 100;
  if ((m = s.match(/^(\d+(?:\.\d+)?)\s*m$/))) return Number(m[1]);
  if ((m = s.match(/^(\d+(?:\.\d+)?)\s*(?:in|")$/))) return Number(m[1]) * 0.0254;
  if ((m = s.match(/^(\d+(?:\.\d+)?)\s*(?:ft|'|feet)\s*(?:(\d+(?:\.\d+)?)\s*(?:in|")?)?$/))) return Number(m[1]) * 0.3048 + Number(m[2] ?? 0) * 0.0254;
  if ((m = s.match(/^(\d+)\s+(\d+(?:\.\d+)?)$/)) && unit === "ft") return Number(m[1]) * 0.3048 + Number(m[2]) * 0.0254;
  if ((m = s.match(/^(\d+(?:\.\d+)?)$/))) return unit === "ft" ? Number(m[1]) * 0.3048 : Number(m[1]);
  return null;
}

export function money(v: number, currency = "USD"): string {
  return new Intl.NumberFormat("en-US", { style: "currency", currency, maximumFractionDigits: 0 }).format(v);
}

/** "2 bedrooms, 1 bath" / "Studio, 1 bath". */
export function roomsText(bedrooms: number, bathrooms: number): string {
  const b = bedrooms === 0 ? "Studio" : `${bedrooms} bedroom${bedrooms === 1 ? "" : "s"}`;
  const ba = `${bathrooms % 1 ? bathrooms.toFixed(1) : bathrooms} bath${bathrooms === 1 ? "" : "s"}`;
  return `${b}, ${ba}`;
}
