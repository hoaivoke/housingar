import { h, toast } from "../dom";
import { arButton } from "../ar";
import { api, ApiError, Furniture, LibraryItem, Placement, isRough, placeName } from "../api";
import { Layout, RoomKind, area, rectOf, roomsWithWindows, setRoomSize, starterLayout, validate } from "../layout";
import { Unit, areaText, getUnit, length, parseLength, setUnit } from "../units";
import { PlanView } from "../plan/planView";
import { RoomScene } from "../room/scene";
import type { Page } from "../main";

const KINDS: Array<[RoomKind, string]> = [["living", "Living room"], ["bedroom", "Bedroom"], ["kitchen", "Kitchen"], ["bathroom", "Bathroom"], ["hall", "Hall"], ["closet", "Closet"], ["other", "Other"]];

/** The Mac's plan, or a one-room starter when it can't estimate one (HTTP 422). */
async function layoutOrStarter(id: string): Promise<{ layout: Layout; starter: boolean }> {
  try {
    return { layout: await api.layout(id), starter: false };
  } catch (e) {
    if (!(e instanceof ApiError && e.status === 422)) throw e;
    const l = await api.listing(id);
    return { layout: starterLayout(id, l.square_feet), starter: true };
  }
}

export async function roomPage(id: string): Promise<Page> {
  const [listing, firstLayout, savedFurniture, library] = await Promise.all([
    api.listing(id), layoutOrStarter(id), api.furniture(id).catch(() => null), api.library().catch(() => ({ items: [] as LibraryItem[] })),
  ]);
  let layout: Layout = firstLayout.layout;
  let starter = firstLayout.starter;
  let unit: Unit = getUnit();
  let selectedRoom: string | null = null;
  let placements: Placement[] = savedFurniture?.placements ?? [];
  const items = library.items.filter((i) => i.mesh_url);

  // ---------------------------------------------------------------- save state
  const status = h("p.save-status", { role: "status", "aria-live": "polite" });
  const setStatus = (text: string, kind: "ok" | "busy" | "error" = "ok") => {
    status.textContent = text;
    status.dataset.kind = kind;
  };
  setStatus(starter ? "Simple plan: the Mac couldn't work out rooms for this listing, so this is one open room of its size." : layout.estimated ? "Estimated plan" : "Your plan");
  let layoutTimer = 0, furnitureTimer = 0;
  let layoutUnsaved = starter, furnitureUnsaved = false;
  const readyForAr = () => !layoutUnsaved && !furnitureUnsaved;
  const saveLayout = () => {
    layoutUnsaved = true;
    clearTimeout(layoutTimer);
    const errors = validate(layout);
    if (errors.length) return setStatus(`Not saved: ${errors[0]}`, "error");
    layoutTimer = window.setTimeout(async () => {
      setStatus("Saving…", "busy");
      try {
        const pending = JSON.stringify(layout);
        const saved = await api.saveLayout(id, { ...layout, estimated: false, source: "user_edited" });
        if (JSON.stringify(layout) === pending) { layout = saved; layoutUnsaved = false; }
        setStatus("Plan saved");
        starter = false;
        planHeading.textContent = "Your floor plan";
        resetButton.hidden = false;
      } catch (e) {
        setStatus(`Not saved: ${(e as Error).message}`, "error");
      }
    }, 700);
  };
  const saveFurniture = () => {
    furnitureUnsaved = true;
    clearTimeout(furnitureTimer);
    furnitureTimer = window.setTimeout(async () => {
      setStatus("Saving…", "busy");
      const body: Furniture = { schema: "housingar.room_furniture.v1", listing_id: id, placements };
      const pending = JSON.stringify(placements);
      try {
        await api.saveFurniture(id, body);
        if (JSON.stringify(placements) === pending) furnitureUnsaved = false;
        setStatus("Furniture saved");
      } catch (e) {
        setStatus(`Not saved: ${(e as Error).message}`, "error");
      }
    }, 600);
  };

  // ---------------------------------------------------------------- 3D
  const stage = h("div.stage");
  const selectedBar = h("div.selected-bar", { hidden: true });
  const message = (text: string) => toast(text);
  let scene: RoomScene;
  const sceneEvents = {
    onSelect: (p: Placement | null, overlapping: string | null) => {
      if (!p) { selectedBar.hidden = true; return; }
      selectedBar.hidden = false;
      selectedBar.replaceChildren(
        h("p.selected-name", p.name, overlapping ? h("span.warn-text", ` overlaps ${overlapping}`) : null),
        h("div.selected-actions",
          h("button.secondary", { type: "button", onclick: () => scene.rotateSelected(15), "aria-label": `Turn ${p.name} left` }, "Turn left"),
          h("button.secondary", { type: "button", onclick: () => scene.rotateSelected(-15), "aria-label": `Turn ${p.name} right` }, "Turn right"),
          h("button.secondary.danger", { type: "button", onclick: () => scene.removeSelected() }, "Remove"),
          h("button.secondary", { type: "button", onclick: () => scene.deselect() }, "Done")));
    },
    onChange: (next: Placement[], done: boolean) => {
      placements = next;
      planView.update(layout, { footprints: scene.footprints() });
      if (done) saveFurniture();
    },
    onMessage: message,
  };

  const overviewBtn = h("button.seg", { type: "button", "aria-pressed": "true" }, "Overview") as HTMLButtonElement;
  const walkBtn = h("button.seg", { type: "button", "aria-pressed": "false" }, "Walk through") as HTMLButtonElement;
  const lookBtn = h("button.secondary.look", { type: "button", hidden: true, "aria-pressed": "false" }, "Look around") as HTMLButtonElement;
  const walkPad = h("div.walk-pad", { hidden: true, "aria-label": "Walk" });
  const setMode = (mode: "overview" | "walk") => {
    scene.setMode(mode);
    overviewBtn.setAttribute("aria-pressed", String(mode === "overview"));
    walkBtn.setAttribute("aria-pressed", String(mode === "walk"));
    lookBtn.hidden = mode !== "walk" || !scene.gyroAvailable || !matchMedia("(pointer: coarse)").matches;
    walkPad.hidden = mode !== "walk" || !matchMedia("(pointer: coarse)").matches;
    hint.textContent = mode === "walk"
      ? (matchMedia("(pointer: coarse)").matches ? "Drag to look, tap the floor to walk there, or use the arrows." : "Drag to look. Walk with W A S D or the arrow keys, or click the floor.")
      : "Drag to turn the view, scroll or pinch to zoom. Drag furniture to move it.";
    if (mode === "overview") { lookBtn.setAttribute("aria-pressed", "false"); lookBtn.textContent = "Look around"; }
  };
  overviewBtn.addEventListener("click", () => setMode("overview"));
  walkBtn.addEventListener("click", () => setMode("walk"));
  lookBtn.addEventListener("click", async () => {
    if (scene.gyroActive) {
      scene.stopGyro();
      lookBtn.setAttribute("aria-pressed", "false");
      lookBtn.textContent = "Look around";
      return;
    }
    const problem = await scene.startGyro();
    if (problem) return message(problem);
    lookBtn.setAttribute("aria-pressed", "true");
    lookBtn.textContent = "Stop";
    message("Move your phone to look around. Tap the floor or use the arrows to walk.");
  });
  const pad = (label: string, text: string, forward: number, turn: number) => {
    const b = h("button.pad", { type: "button", "aria-label": label }, text) as HTMLButtonElement;
    const on = (e: Event) => { e.preventDefault(); scene.setMoveInput(forward, turn); };
    const off = () => scene.setMoveInput(0, 0);
    b.addEventListener("pointerdown", on);
    b.addEventListener("pointerup", off);
    b.addEventListener("pointerleave", off);
    b.addEventListener("pointercancel", off);
    return b;
  };
  walkPad.append(pad("Walk forward", "▲", 1, 0), pad("Turn left", "◀", 0, -1), pad("Turn right", "▶", 0, 1), pad("Walk back", "▼", -1, 0));
  const hint = h("p.stage-hint");

  // My stuff drawer.
  const drawer = h("div.drawer", { hidden: true, role: "dialog", "aria-label": "Add from My stuff" });
  const addBtn = h("button.primary", { type: "button", "aria-expanded": "false" }, "Add furniture") as HTMLButtonElement;
  const closeDrawer = () => { drawer.hidden = true; addBtn.setAttribute("aria-expanded", "false"); };
  addBtn.addEventListener("click", () => {
    const open = drawer.hidden;
    drawer.hidden = !open;
    addBtn.setAttribute("aria-expanded", String(open));
  });
  drawer.append(
    h("div.drawer-head", h("h2", "My stuff"), h("button.link-button", { type: "button", onclick: closeDrawer }, "Close")),
    items.length
      ? h("ul.drawer-list", ...items.map((it) => h("li",
        h("button.drawer-item", {
          type: "button",
          onclick: async (e: Event) => {
            const b = e.currentTarget as HTMLButtonElement;
            b.disabled = true;
            try {
              await scene.addItem(it);
              closeDrawer();
            } catch (err) {
              message(`Couldn't load ${it.name}: ${(err as Error).message}`);
            } finally {
              b.disabled = false;
            }
          },
        },
          it.thumbnail_url ? h("img", { src: it.thumbnail_url, alt: "", loading: "lazy" }) : h("span.stuff-noimg"),
          h("span.drawer-text", h("span.drawer-name", it.name),
            h("span.muted", it.dimensions_m ? `${length(it.dimensions_m[0], unit)} × ${length(it.dimensions_m[2], unit)}, ${length(it.dimensions_m[1], unit)} tall` : ""),
            isRough(it) ? h("span.tag.warn", "Rough model") : null)))))
      : h("p.muted", "Nothing in My stuff yet. Scan your things with the phone app first."),
  );

  const stageWrap = h("section.stage-wrap", { "aria-label": "3D view" },
    stage,
    h("div.stage-top", h("div.segmented", { role: "group", "aria-label": "View" }, overviewBtn, walkBtn), lookBtn),
    h("div.stage-bottom", selectedBar, h("div.stage-actions", walkPad, h("div.stage-right", hint, addBtn))),
    drawer,
  );

  // ---------------------------------------------------------------- plan
  const roomPanel = h("div.room-panel");
  const planView = new PlanView(layout, {
    editable: true, unit, selected: null,
    onSelect: (rid) => { selectedRoom = rid; planView.update(layout, { selected: rid }); renderPanel(); },
    onChange: (next, done) => {
      layout = next;
      liveScene(next);
      renderPanel(false);
      if (done) saveLayout();
    },
  });
  let pending: Layout | null = null;
  const liveScene = (l: Layout) => {
    if (pending) { pending = l; return; }
    pending = l;
    requestAnimationFrame(() => { scene.setLayout(pending!); pending = null; planView.update(layout, { footprints: scene.footprints() }); });
  };

  const planHeading = h("h2", starter ? "Simple floor plan" : layout.estimated ? "Estimated floor plan" : "Your floor plan");
  const resetButton = h("button.link-button", { type: "button", hidden: layout.estimated }, "Go back to the estimated plan") as HTMLButtonElement;
  resetButton.addEventListener("click", async () => {
    if (!confirm("Replace your changes with the estimated plan?")) return;
    try {
      try {
        layout = await api.resetLayout(id);
        starter = false;
      } catch (e) {
        if (!(e instanceof ApiError && e.status === 422)) throw e;
        layout = starterLayout(id, listing.square_feet);
        starter = true;
      }
      selectedRoom = null;
      layoutUnsaved = starter;
      planView.update(layout, { selected: null });
      scene.setLayout(layout);
      renderPanel();
      resetButton.hidden = true;
      setStatus(starter ? "Simple plan" : "Estimated plan");
      planHeading.textContent = starter ? "Simple floor plan" : "Estimated floor plan";
    } catch (e) {
      message((e as Error).message);
    }
  });

  const unitToggle = h("div.segmented.small", { role: "group", "aria-label": "Units" },
    ...(["ft", "m"] as Unit[]).map((u) => {
      const b = h("button.seg", { type: "button", "aria-pressed": String(u === unit) }, u === "ft" ? "Feet" : "Metres");
      b.addEventListener("click", () => {
        unit = u;
        setUnit(u);
        unitToggle.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
        planView.update(layout, { unit });
        renderPanel();
      });
      return b;
    }));

  function renderPanel(full = true) {
    const room = layout.rooms.find((r) => r.room_id === selectedRoom);
    if (!room) {
      roomPanel.replaceChildren(h("p.muted", "Select a room on the plan to change its size or name. Drag its walls, or type the sizes."));
      return;
    }
    const r = rectOf(room);
    if (!full) {
      // Only refresh the numbers while dragging, so focus and typing aren't disturbed.
      const w = roomPanel.querySelector<HTMLInputElement>("#room-width"), d = roomPanel.querySelector<HTMLInputElement>("#room-depth");
      if (w && document.activeElement !== w) w.value = length(r.x1 - r.x0, unit);
      if (d && document.activeElement !== d) d.value = length(r.z1 - r.z0, unit);
      const a = roomPanel.querySelector(".room-area");
      if (a) a.textContent = areaText(area(r), unit);
      return;
    }
    const name = h("input", { id: "room-name", type: "text", value: room.name, maxlength: 40 }) as HTMLInputElement;
    name.addEventListener("change", () => {
      const v = name.value.trim();
      if (!v) { name.value = room.name; return; }
      layout = { ...layout, rooms: layout.rooms.map((x) => (x.room_id === room.room_id ? { ...x, name: v } : x)) };
      planView.update(layout);
      saveLayout();
    });
    // Living rooms and bedrooms need a window to the outside (the Mac won't save one without).
    const lit = roomsWithWindows(layout).has(room.room_id);
    const kind = h("select", { id: "room-kind" }, ...KINDS.map(([k, t]) => {
      const needsWindow = (k === "living" || k === "bedroom") && !lit && k !== room.kind;
      return h("option", { value: k, selected: k === room.kind, disabled: needsWindow }, needsWindow ? `${t} (needs a window)` : t);
    })) as HTMLSelectElement;
    kind.addEventListener("change", () => {
      layout = { ...layout, rooms: layout.rooms.map((x) => (x.room_id === room.room_id ? { ...x, kind: kind.value as RoomKind } : x)) };
      planView.update(layout);
      scene.setLayout(layout);
      saveLayout();
    });
    const sizeInput = (idAttr: string, value: number, which: "w" | "d") => {
      const input = h("input", { id: idAttr, type: "text", inputmode: "decimal", value: length(value, unit), "aria-describedby": "size-help" }) as HTMLInputElement;
      input.addEventListener("change", () => {
        const m = parseLength(input.value, unit);
        if (m == null) { input.setCustomValidity("Type a length like 12' 4\" or 3.7 m."); input.reportValidity(); return; }
        input.setCustomValidity("");
        layout = setRoomSize(layout, room.room_id, which === "w" ? m : null, which === "d" ? m : null);
        planView.update(layout);
        scene.setLayout(layout);
        planView.update(layout, { footprints: scene.footprints() });
        renderPanel();
        saveLayout();
      });
      return input;
    };
    roomPanel.replaceChildren(
      h("div.panel-row",
        h("label.field.grow", { for: "room-name" }, h("span.field-label", "Name"), name),
        h("label.field", { for: "room-kind" }, h("span.field-label", "Kind"), kind)),
      h("div.panel-row",
        h("label.field", { for: "room-width" }, h("span.field-label", "Width"), sizeInput("room-width", r.x1 - r.x0, "w")),
        h("label.field", { for: "room-depth" }, h("span.field-label", "Depth"), sizeInput("room-depth", r.z1 - r.z0, "d")),
        h("p.room-area-wrap", h("span.field-label", "Area"), h("span.room-area", areaText(area(r), unit)))),
      h("p.muted.small", { id: "size-help" }, "Changing a size moves this room's right or bottom wall; the rooms next to it adjust."),
      arButton(id, room.room_id, readyForAr),
    );
  }
  renderPanel();

  // ---------------------------------------------------------------- page
  const tabs = h("div.segmented.view-tabs", { role: "tablist", "aria-label": "Show" });
  const planTab = h("button.seg", { type: "button", role: "tab", "aria-selected": "false" }, "Plan");
  const stageTab = h("button.seg", { type: "button", role: "tab", "aria-selected": "true" }, "3D");
  tabs.append(planTab, stageTab);

  const planSection = h("section.plan-pane", { "aria-label": "Floor plan" },
    h("div.pane-head", planHeading, unitToggle),
    h("div.plan-frame", planView.svg),
    roomPanel,
    h("div.pane-foot", status, resetButton));

  const el = h("main.room-page",
    h("div.room-head",
      h("div",
        h("p.back", h("a", { href: `#/listing/${encodeURIComponent(id)}` }, "Back to the listing")),
        h("h1", placeName(listing))),
      h("div", tabs, arButton(id, undefined, readyForAr))),
    h("div.room-body", { "data-show": "stage" }, planSection, stageWrap),
  );
  const body = el.querySelector(".room-body") as HTMLElement;
  const showTab = (which: "plan" | "stage") => {
    body.dataset.show = which;
    planTab.setAttribute("aria-selected", String(which === "plan"));
    stageTab.setAttribute("aria-selected", String(which === "stage"));
  };
  planTab.addEventListener("click", () => showTab("plan"));
  stageTab.addEventListener("click", () => showTab("stage"));

  scene = new RoomScene(stage, sceneEvents);
  scene.setLayout(layout);
  scene.setLibrary(library.items);
  setMode("overview");
  scene.setPlacements(placements).then(() => planView.update(layout, { footprints: scene.footprints() }));
  (window as any).__roomScene = scene; // for automated checks

  return {
    el,
    title: `Plan ${placeName(listing)}`,
    dispose: () => { scene.dispose(); clearTimeout(layoutTimer); clearTimeout(furnitureTimer); },
  };
}
