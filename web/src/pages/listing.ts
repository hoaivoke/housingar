import { h } from "../dom";
import { arButton } from "../ar";
import { api, ApiError, dayText, placeName, rentPer } from "../api";
import { areaText, getUnit, length, money, roomsText } from "../units";
import { PlanView } from "../plan/planView";
import { bounds, totalArea } from "../layout";
import type { Page } from "../main";

/** "5125550100" → "(512) 555-0100"; anything else as given. */
export function phoneText(p: string): string {
  const d = p.replace(/\D/g, "").replace(/^1(?=\d{10}$)/, "");
  return d.length === 10 ? `(${d.slice(0, 3)}) ${d.slice(3, 6)}-${d.slice(6)}` : p;
}

export async function listingPage(id: string): Promise<Page> {
  const [l, layout] = await Promise.all([api.listing(id), api.layout(id).catch((e) => (e instanceof ApiError && e.status === 422 ? "starter" as const : null))]);
  const a = l.address;
  const facts: Array<[string, string]> = [
    ["Rooms", roomsText(l.bedrooms, l.bathrooms)],
    ["Size", l.square_feet ? `${l.square_feet.toLocaleString("en-US")} sq ft` : "Not listed"],
    ["Type", l.property_type],
  ];
  if (l.distance_mi != null) facts.push(["Distance", `${l.distance_mi.toFixed(1)} miles from your search`]);
  if (l.days_on_market != null) facts.push(["Listed", l.days_on_market === 0 ? "Today" : `${l.days_on_market} days ago`]);

  // A sample's links would only search for a made-up address.
  const links = l.sample ? [] : [
    l.links.listing_url && h("a.button.primary", { href: l.links.listing_url, target: "_blank", rel: "noopener" }, "Open the listing"),
    l.links.search_url && h("a.button", { href: l.links.search_url, target: "_blank", rel: "noopener", class: l.links.listing_url ? "secondary" : "primary" }, "Find this listing online"),
    l.links.maps_url && h("a.button.secondary", { href: l.links.maps_url, target: "_blank", rel: "noopener" }, "Open in Maps"),
  ].filter(Boolean) as HTMLElement[];

  const c = l.contact;
  const contact = c && (c.name || c.phone || c.email || c.office)
    ? h("section.contact", h("h2", "Contact"),
      h("p", [c.name, c.office].filter(Boolean).join(", ")),
      c.phone ? h("p", h("a", { href: `tel:${c.phone.replace(/[^\d+]/g, "")}` }, phoneText(c.phone))) : null,
      c.email ? h("p", h("a", { href: `mailto:${c.email}` }, c.email)) : null)
    : null;

  let plan: HTMLElement;
  if (layout === "starter") {
    plan = h("section.plan-card", h("h2", "Floor plan"),
      h("p", "The Mac couldn't work out rooms from this listing's size and room count. You can still start from one open room of its size."),
      h("a.button.primary.wide", { href: `#/room/${encodeURIComponent(id)}` }, "Plan and furnish in 3D"));
  } else if (layout) {
    const b = bounds(layout);
    const view = new PlanView(layout, { editable: false, unit: getUnit(), selected: null });
    plan = h("section.plan-card",
      h("div.plan-card-head",
        h("h2", layout.estimated ? "Estimated floor plan" : "Your floor plan"),
        h("p.muted", `${length(b.x1 - b.x0)} × ${length(b.z1 - b.z0)}, ${areaText(totalArea(layout))}.`,
          layout.estimated ? " Worked out from the listing's size and rooms; you can change it." : " Adjusted by you.")),
      h("a.plan-card-figure", { href: `#/room/${encodeURIComponent(id)}`, "aria-label": "Open the plan in 3D" }, view.svg),
      arButton(id),
      h("p.muted.small", "AR opens the installed Housing AR app on your Pixel, at full size. Estimated room sizes can be adjusted in the plan."),
      h("a.button.primary.wide", { href: `#/room/${encodeURIComponent(id)}` }, "Plan and furnish in 3D"));
  } else {
    plan = h("section.plan-card", h("h2", "Floor plan"), h("p", "The Mac couldn't make a floor plan for this listing."));
  }

  const el = h("main.page.listing-page",
    h("p.back", h("a", { href: "#/" }, "Back to results")),
    h("div.listing-grid",
      h("div.listing-info",
        h("h1", { class: placeName(l).length > 48 ? "long" : "" }, placeName(l)),
        h("p.listing-city", [placeName(l) !== a.line1 ? a.line1 + (a.line2 ? `, ${a.line2}` : "") : null, a.city, [a.state, a.zip].filter(Boolean).join(" ")].filter(Boolean).join(", ")),
        h("p.listing-rent", money(l.rent_monthly, l.currency), h("span.per", rentPer(l))),
        l.rent_per === "bedroom" ? h("p.muted.small", "Rented by the bedroom: you sign for one bedroom and share the rest of the place.") : null,
        l.sample ? h("p.notice", "This is a made-up sample listing, so there's nothing to rent or contact.") : null,
        h("dl.facts", ...facts.flatMap(([k, v]) => [h("dt", k), h("dd", v)])),
        links.length ? h("div.actions", ...links) : null,
        !l.links.listing_url && !l.sample ? h("p.muted.small", "The listing service doesn't give a direct page link, so this searches the web for the address.") : null,
        l.source === "dataset" && l.collected ? h("p.muted.small", `Price and rooms as listed on ${dayText(l.collected)}; the listing shows today's.`) : null,
        contact,
      ),
      plan,
    ),
  );
  return { el, title: placeName(l) };
}
