import "@fontsource-variable/archivo/standard.css";
import "./styles.css";
import { h } from "./dom";
import { searchPage } from "./pages/search";
import { listingPage } from "./pages/listing";
import { roomPage } from "./pages/room";
import { stuffPage } from "./pages/stuff";

export interface Page {
  el: HTMLElement;
  title: string;
  dispose?: () => void;
}

const app = document.getElementById("app")!;
let current: Page | null = null;

function header(active: string) {
  const link = (href: string, text: string, key: string) =>
    h("a", { href, class: active === key ? "nav-link active" : "nav-link", "aria-current": active === key ? "page" : null }, text);
  return h("header.site-header",
    h("a.wordmark", { href: "#/" }, "Roomfit"),
    h("nav", { "aria-label": "Main" }, link("#/", "Find a place", "search"), link("#/stuff", "My stuff", "stuff")),
  );
}

async function route() {
  const hash = location.hash.replace(/^#/, "") || "/";
  const parts = hash.split("/").filter(Boolean).map(decodeURIComponent);
  current?.dispose?.();
  let page: Page;
  let active = "search";
  try {
    if (parts[0] === "listing" && parts[1]) page = await listingPage(parts[1]);
    else if (parts[0] === "room" && parts[1]) page = await roomPage(parts[1]);
    else if (parts[0] === "stuff") { page = await stuffPage(); active = "stuff"; }
    else page = searchPage();
  } catch (e) {
    page = { title: "Something went wrong", el: h("main.page", h("h1", "That page didn't load"), h("p", (e as Error).message), h("p", h("a", { href: "#/" }, "Back to search"))) };
  }
  current = page;
  document.title = `${page.title} – Roomfit`;
  app.replaceChildren(header(active), page.el);
  page.el.querySelector<HTMLElement>("h1")?.setAttribute("tabindex", "-1");
  if (!parts.length) return;
  window.scrollTo(0, 0);
}

window.addEventListener("hashchange", route);
route();
