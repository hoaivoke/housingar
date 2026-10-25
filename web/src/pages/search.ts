import { h } from "../dom";
import { api, dayText, Listing, placeName, SearchQuery, SearchResult } from "../api";
import { money, roomsText } from "../units";
import { PlanView } from "../plan/planView";
import { heroLayout } from "../plan/heroLayout";
import type { Page } from "../main";

const KEY = "roomfit.search";

interface Saved { near: string; useLocation: boolean; lat?: number; lon?: number; radius: string; maxRent: string; bedrooms: string }

function load(): Saved | null {
  try { return JSON.parse(localStorage.getItem(KEY) || "null"); } catch { return null; }
}
function save(s: Saved) {
  try { localStorage.setItem(KEY, JSON.stringify(s)); } catch { /* private mode */ }
}

/** Results are kept for the session so Back from a listing doesn't search again. */
let lastResult: { query: string; result: SearchResult } | null = null;

export function searchPage(): Page {
  const saved = load();
  const near = h("input", { id: "near", name: "near", type: "text", autocomplete: "street-address", placeholder: "Address, city or ZIP code", value: saved?.near ?? "", required: true }) as HTMLInputElement;
  const select = (id: string, label: string, options: Array<[string, string]>, value: string) =>
    h("label.field", { for: id }, h("span.field-label", label),
      h("select", { id, name: id }, ...options.map(([v, t]) => h("option", { value: v, selected: v === value }, t))));
  const maxRent = select("max_rent", "Rent up to", [["", "Any rent"], ["800", "$800"], ["1000", "$1,000"], ["1200", "$1,200"], ["1500", "$1,500"], ["2000", "$2,000"], ["2500", "$2,500"], ["3500", "$3,500"]], saved?.maxRent ?? "");
  const beds = select("bedrooms", "Bedrooms", [["", "Any"], ["0", "Studio"], ["1", "1"], ["2", "2"], ["3+", "3 or more"]], saved?.bedrooms ?? "");
  const radius = select("radius", "Within", [["1", "1 mile"], ["3", "3 miles"], ["5", "5 miles"], ["10", "10 miles"], ["25", "25 miles"]], saved?.radius ?? "10");
  const results = h("section.results-area", { "aria-live": "polite" });
  let coords: { lat: number; lon: number } | null = saved?.useLocation && saved.lat != null ? { lat: saved.lat, lon: saved.lon! } : null;

  const locate = h("button.link-button", { type: "button" }, "Use my location") as HTMLButtonElement;
  locate.addEventListener("click", () => {
    if (!navigator.geolocation) return show(error("This browser can't share its location. Type an address instead."));
    locate.disabled = true;
    locate.textContent = "Finding you…";
    navigator.geolocation.getCurrentPosition((pos) => {
      coords = { lat: pos.coords.latitude, lon: pos.coords.longitude };
      near.value = "My location";
      locate.disabled = false;
      locate.textContent = "Use my location";
      run();
    }, () => {
      locate.disabled = false;
      locate.textContent = "Use my location";
      show(error("Location isn't available here (browsers only share it over https or localhost). Type an address instead."));
    }, { timeout: 10000 });
  });
  near.addEventListener("input", () => { coords = null; });

  const form = h("form.search", { role: "search", "aria-label": "Find rentals" },
    h("label.field.grow", { for: "near" }, h("span.field-label", "Where"), near),
    maxRent, beds, radius,
    h("button.primary", { type: "submit" }, "Search"),
  ) as HTMLFormElement;
  form.addEventListener("submit", (e) => { e.preventDefault(); run(); });

  function query(): SearchQuery {
    const v = (id: string) => (form.querySelector(`#${id}`) as HTMLSelectElement).value;
    return {
      ...(coords ? { lat: coords.lat, lon: coords.lon } : { near: near.value.trim() }),
      radius_mi: Number(v("radius")),
      max_rent: v("max_rent") ? Number(v("max_rent")) : null,
      bedrooms: v("bedrooms") || null,
    };
  }

  function show(...nodes: Node[]) {
    results.replaceChildren(...nodes);
  }

  async function run() {
    const q = query();
    if (!coords && !q.near) { near.focus(); return; }
    const v = (id: string) => (form.querySelector(`#${id}`) as HTMLSelectElement).value;
    save({ near: near.value, useLocation: !!coords, lat: coords?.lat, lon: coords?.lon, radius: v("radius"), maxRent: v("max_rent"), bedrooms: v("bedrooms") });
    const key = JSON.stringify(q);
    if (lastResult?.query === key) return render(lastResult.result);
    show(h("p.status", `Searching rentals near ${coords ? "you" : q.near}…`));
    try {
      const r = await api.search(q);
      lastResult = { query: key, result: r };
      render(r);
    } catch (e) {
      show(error((e as Error).message, run));
    }
  }

  function render(r: SearchResult, byLocation = !!coords) {
    const nodes: Node[] = [];
    if (r.source === "sample") {
      nodes.push(h("p.notice", "These are made-up sample listings. Add a RentCast key on the Mac to see real rentals."));
    } else if (r.source === "dataset") {
      const d = r.dataset ?? {};
      nodes.push(h("p.notice",
        `Saved listings from ${d.source ?? "a listings site"}${d.collected ? `, as listed on ${dayText(d.collected)}` : ""}. `,
        "Prices and rooms change, so open the listing to check before you apply."));
    }
    // Say it the way the person typed it (the Mac's label is normalised, or just numbers for a location).
    const typed = query().near;
    const where = byLocation ? " near you" : typed ? ` near ${typed}` : "";
    if (!r.listings.length) {
      nodes.push(h("div.empty", h("h2", `No rentals match${where}`),
        h("p", r.source === "dataset" && r.center?.lat == null
          ? `The saved listings cover ${r.dataset?.name?.replace(/^.*?around /, "") ?? "one area"}. Try a ZIP code or town there, such as 24061.`
          : "Try a larger distance, a higher rent or any number of bedrooms.")));
      return show(...nodes);
    }
    const n = r.listings.length;
    // The Mac returns at most 100, nearest first.
    nodes.push(h("h2.results-title", n >= 100 ? `The 100 closest places${where.replace(/^ near /, " to ")}` : `${n} ${n === 1 ? "place" : "places"}${where}`,
      r.cached ? h("span.muted", "from an earlier search today") : null));
    nodes.push(h("ol.results", ...r.listings.map(row)));
    show(...nodes);
  }

  const hero = h("div.hero-plan", { "aria-hidden": "true" });
  hero.append(new PlanView(heroLayout, { editable: false, unit: "ft", selected: "bed_1" }).svg);

  const el = h("main.page.search-page",
    h("section.hero",
      h("div.hero-text",
        h("h1", "Find a place to rent, then see your own furniture in it"),
        h("p.lede", "Search rentals nearby, open one as a floor plan you can adjust, and walk through it in 3D with the things you scanned on your phone."),
        form,
        h("p.locate", "Or ", locate, "."),
      ),
      hero,
    ),
    results,
  );

  if (lastResult) render(lastResult.result);
  else if (saved && (saved.near || saved.useLocation)) run();
  return { el, title: "Find a place" };
}

function row(l: Listing): HTMLElement {
  const a = l.address;
  return h("li",
    h("a.result", { href: `#/listing/${encodeURIComponent(l.listing_id)}` },
      h("span.result-rent", money(l.rent_monthly, l.currency), h("span.per", " a month"),
        l.rent_per === "bedroom" ? h("span.per.per-bed", "per bedroom") : null),
      h("span.result-main",
        h("span.result-rooms", roomsText(l.bedrooms, l.bathrooms), l.square_feet ? `, ${l.square_feet.toLocaleString("en-US")} sq ft` : ""),
        h("span.result-address", [placeName(l) !== a.line1 ? placeName(l) : null, a.line1, a.line2, a.city].filter(Boolean).join(", "))),
      h("span.result-side",
        l.distance_mi != null ? h("span.result-distance", `${l.distance_mi.toFixed(1)} mi`) : null,
        l.sample ? h("span.tag", "Sample") : h("span.result-type", l.property_type)),
    ));
}

export function error(text: string, retry?: () => void): HTMLElement {
  return h("div.error", { role: "alert" }, h("p", text), retry ? h("button.secondary", { type: "button", onclick: retry }, "Try again") : null);
}
