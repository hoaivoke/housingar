import { h } from "../dom";
import { api, isRough } from "../api";
import { length } from "../units";
import type { Page } from "../main";
import { error } from "./search";

export async function stuffPage(): Promise<Page> {
  const list = h("div");
  const el = h("main.page.stuff-page",
    h("h1", "My stuff"),
    h("p.lede", "Things you scanned with the phone app. Open a listing's 3D plan to place them."),
    list);
  try {
    const { items } = await api.library();
    if (!items.length) {
      list.append(h("div.empty", h("h2", "Nothing here yet"),
        h("p", "In the phone app, press Scan stuff, tap the things you want to keep and walk around them. They appear here once the Mac has built them.")));
    } else {
      list.append(h("ul.stuff-grid", ...items.map((i) => h("li.stuff",
        i.thumbnail_url ? h("img", { src: i.thumbnail_url, alt: "", loading: "lazy" }) : h("div.stuff-noimg"),
        h("h2", i.name),
        h("p.muted", i.dimensions_m ? `${length(i.dimensions_m[0])} wide, ${length(i.dimensions_m[1])} tall, ${length(i.dimensions_m[2])} deep` : "Size unknown"),
        isRough(i) ? h("p.tag.warn", "Rough model") : null))));
    }
  } catch (e) {
    list.append(error((e as Error).message));
  }
  return { el, title: "My stuff" };
}
