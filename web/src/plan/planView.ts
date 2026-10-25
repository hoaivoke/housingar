// The floor plan as an architect's drawing: walls, door swings, windows, room names and
// dimension lines. Editable mode lets you drag the selected room's walls (neighbours follow).
import { s } from "../dom";
import {
  Layout, Room, Rect, bounds, moveWall, openingLine, rectOf, wallPieces, area, DOOR_HEIGHT_M,
} from "../layout";
import { Unit, areaText, length } from "../units";

export interface PlanOptions {
  editable: boolean;
  unit: Unit;
  selected: string | null;
  onSelect?: (roomId: string | null) => void;
  /** Called continuously while dragging (live) and once more with `done` when released. */
  onChange?: (layout: Layout, done: boolean) => void;
  /** Furniture footprints to draw (plan coordinates). */
  footprints?: Array<{ corners: Array<[number, number]>; selected: boolean }>;
}

const WALL = 0.12;
const PAD = 1.1;

const fills: Record<string, string> = {
  bedroom: "var(--room-bed)", living: "var(--room-living)", kitchen: "var(--room-kitchen)",
  bathroom: "var(--room-bath)", hall: "var(--room-hall)", closet: "var(--room-hall)", other: "var(--room-hall)",
};

export class PlanView {
  readonly svg: SVGSVGElement;
  private layout: Layout;
  private opts: PlanOptions;
  private drag: { axis: "x" | "z"; c: number; t: number; start: Layout; pointer: number } | null = null;

  constructor(layout: Layout, opts: PlanOptions) {
    this.layout = layout;
    this.opts = opts;
    this.svg = s("svg", { class: "plan", role: "img" });
    this.svg.addEventListener("pointerdown", (e) => {
      if (e.target === this.svg || (e.target as Element).classList.contains("plan-bg")) this.opts.onSelect?.(null);
    });
    this.render();
  }

  update(layout: Layout, opts: Partial<PlanOptions> = {}) {
    this.layout = layout;
    this.opts = { ...this.opts, ...opts };
    if (!this.drag) this.render();
  }

  private render() {
    const L = this.layout;
    const svg = this.svg;
    svg.replaceChildren();
    if (!L.rooms.length) return;
    const b = bounds(L);
    const w = b.x1 - b.x0, d = b.z1 - b.z0;
    svg.setAttribute("viewBox", `${b.x0 - PAD} ${b.z0 - PAD} ${w + 2 * PAD} ${d + 2 * PAD}`);
    svg.setAttribute("aria-label", `Floor plan, ${length(w, this.opts.unit)} by ${length(d, this.opts.unit)}, ${L.rooms.length} rooms`);
    const scale = Math.max(w, d);
    const fs = Math.max(0.24, Math.min(0.42, scale / 30));
    svg.style.setProperty("--fs", String(fs));

    svg.append(s("rect", { class: "plan-bg", x: b.x0 - PAD, y: b.z0 - PAD, width: w + 2 * PAD, height: d + 2 * PAD }));

    // Room floors (selectable).
    const floors = s("g", { class: "floors" });
    for (const room of L.rooms) {
      const r = rectOf(room);
      const sel = room.room_id === this.opts.selected;
      const el = s("rect", {
        class: `floor${sel ? " selected" : ""}`, x: r.x0, y: r.z0, width: r.x1 - r.x0, height: r.z1 - r.z0,
        fill: fills[room.kind] ?? fills.other, "data-room": room.room_id,
      });
      if (this.opts.onSelect) {
        el.addEventListener("pointerdown", (e) => {
          e.stopPropagation();
          this.opts.onSelect!(room.room_id);
        });
      }
      floors.append(el);
    }
    svg.append(floors);

    // Furniture footprints under the walls.
    if (this.opts.footprints?.length) {
      const g = s("g", { class: "footprints" });
      for (const f of this.opts.footprints) {
        g.append(s("polygon", { class: f.selected ? "fp selected" : "fp", points: f.corners.map((c) => c.join(",")).join(" ") }));
      }
      svg.append(g);
    }

    // Walls, windows and doors.
    const H = L.ceiling_height_m;
    const wallsG = s("g", { class: "walls" });
    for (const p of wallPieces(L)) {
      if (p.y0 > 1e-3) continue;
      const full = p.y1 >= H - 1e-3;
      wallsG.append(s("line", { class: full ? "wall" : "window", x1: p.a[0], y1: p.a[1], x2: p.b[0], y2: p.b[1], "stroke-width": full ? WALL : WALL * 0.9 }));
      if (!full) wallsG.append(s("line", { class: "window-glass", x1: p.a[0], y1: p.a[1], x2: p.b[0], y2: p.b[1] }));
    }
    for (const o of L.openings) {
      if (o.kind !== "door") continue;
      const l = openingLine(o);
      if (!l) continue;
      const wdt = l.t1 - l.t0;
      if (wdt > 1.15 || (o.height_m || DOOR_HEIGHT_M) < 1.5) continue; // wide openings have no leaf
      // Swing into the first listed room (or inward for the entry door).
      const room = L.rooms.find((r) => r.room_id === o.room_ids[0])!;
      const rr = rectOf(room);
      const dir = l.axis === "x" ? ((rr.x0 + rr.x1) / 2 > l.c ? 1 : -1) : ((rr.z0 + rr.z1) / 2 > l.c ? 1 : -1);
      const hinge: [number, number] = l.axis === "x" ? [l.c, l.t0] : [l.t0, l.c];
      const leafEnd: [number, number] = l.axis === "x" ? [l.c + dir * wdt, l.t0] : [l.t0, l.c + dir * wdt];
      const closed: [number, number] = l.axis === "x" ? [l.c, l.t1] : [l.t1, l.c];
      wallsG.append(s("line", { class: "door-leaf", x1: hinge[0], y1: hinge[1], x2: leafEnd[0], y2: leafEnd[1] }));
      const sweep = l.axis === "x" ? (dir > 0 ? 0 : 1) : (dir > 0 ? 1 : 0);
      wallsG.append(s("path", { class: "door-swing", d: `M ${leafEnd[0]} ${leafEnd[1]} A ${wdt} ${wdt} 0 0 ${sweep} ${closed[0]} ${closed[1]}` }));
    }
    svg.append(wallsG);

    // Labels: name, size and area.
    const labels = s("g", { class: "labels" });
    for (const room of L.rooms) {
      const r = rectOf(room);
      const cx = (r.x0 + r.x1) / 2, cz = (r.z0 + r.z1) / 2;
      const w = r.x1 - r.x0, d = r.z1 - r.z0 - 0.15;
      const dims = `${length(r.x1 - r.x0, this.opts.unit)} × ${length(r.z1 - r.z0, this.opts.unit)}`;
      // Shrink the text to the room's width (about 0.55 em per character), then drop
      // lines that don't fit its depth: area first, then size; a thin room gets one line.
      let f = Math.min(Math.min(w, d + 0.15) < 1.8 ? fs * 0.8 : fs, (w - 0.2) / (0.55 * Math.max(room.name.length, dims.length * 0.78)));
      f = Math.max(f, fs * 0.5);
      if (d >= f * 3.1) {
        labels.append(s("text", { class: "room-name", x: cx, y: cz - f * 0.35, "font-size": f }, room.name));
        labels.append(s("text", { class: "room-dims", x: cx, y: cz + f * 0.95, "font-size": f * 0.78 }, dims));
        labels.append(s("text", { class: "room-area", x: cx, y: cz + f * 2.0, "font-size": f * 0.7 }, areaText(area(r), this.opts.unit)));
      } else if (d >= f * 2.1) {
        labels.append(s("text", { class: "room-name", x: cx, y: cz - f * 0.1, "font-size": f }, room.name));
        labels.append(s("text", { class: "room-dims", x: cx, y: cz + f * 0.95, "font-size": f * 0.78 }, dims));
      } else {
        const one = 0.55 * f * (room.name.length + 2 + dims.length * 0.78) < w - 0.2;
        const t = s("text", { class: "room-name", x: cx, y: cz + f * 0.35, "font-size": f }, room.name);
        if (one) t.append(s("tspan", { class: "room-dims", "font-size": f * 0.78 }, `  ${dims}`));
        labels.append(t);
      }
    }
    svg.append(labels);

    // Overall dimensions outside the plan; the selected room's along its own walls.
    const dims = s("g", { class: "dims" });
    this.dimension(dims, [b.x0, b.z0 - 0.55], [b.x1, b.z0 - 0.55], length(w, this.opts.unit), fs * 0.85, "up");
    this.dimension(dims, [b.x0 - 0.55, b.z0], [b.x0 - 0.55, b.z1], length(d, this.opts.unit), fs * 0.85, "left");
    const sel = L.rooms.find((r) => r.room_id === this.opts.selected);
    if (sel) this.selectedDims(dims, sel, fs);
    svg.append(dims);

    if (this.opts.editable && sel) this.handles(sel);
  }

  private selectedDims(g: SVGGElement, room: Room, fs: number) {
    const r = rectOf(room);
    const inset = 0.38;
    this.dimension(g, [r.x0, r.z0 + inset], [r.x1, r.z0 + inset], length(r.x1 - r.x0, this.opts.unit), fs * 0.8, "up", true);
    this.dimension(g, [r.x0 + inset, r.z0], [r.x0 + inset, r.z1], length(r.z1 - r.z0, this.opts.unit), fs * 0.8, "right", true);
  }

  /** A dimension line with architect's tick marks and its length written on it. */
  private dimension(g: SVGGElement, a: [number, number], b: [number, number], text: string, f: number, side: "up" | "left" | "right", accent = false) {
    const cls = accent ? "dim accent" : "dim";
    const t = 0.09;
    g.append(s("line", { class: cls, x1: a[0], y1: a[1], x2: b[0], y2: b[1] }));
    for (const p of [a, b]) g.append(s("line", { class: `${cls} tick`, x1: p[0] - t, y1: p[1] + t, x2: p[0] + t, y2: p[1] - t }));
    const horizontal = Math.abs(a[1] - b[1]) < 1e-6;
    const mx = (a[0] + b[0]) / 2, mz = (a[1] + b[1]) / 2;
    const label = horizontal
      ? s("text", { class: `dim-text${accent ? " accent" : ""}`, x: mx, y: mz - f * 0.35, "font-size": f }, text)
      : s("text", { class: `dim-text${accent ? " accent" : ""}`, x: mx, y: mz, "font-size": f,
        transform: `rotate(-90 ${mx} ${mz}) translate(0 ${side === "left" ? -f * 0.35 : -f * 0.35})` }, text);
    g.append(label);
  }

  /** Invisible wide hit lines over the selected room's walls. */
  private handles(room: Room) {
    const r = rectOf(room);
    const g = s("g", { class: "handles" });
    const edges: Array<{ axis: "x" | "z"; c: number; t: number; a: [number, number]; b: [number, number]; label: string }> = [
      { axis: "x", c: r.x0, t: (r.z0 + r.z1) / 2, a: [r.x0, r.z0], b: [r.x0, r.z1], label: "left" },
      { axis: "x", c: r.x1, t: (r.z0 + r.z1) / 2, a: [r.x1, r.z0], b: [r.x1, r.z1], label: "right" },
      { axis: "z", c: r.z0, t: (r.x0 + r.x1) / 2, a: [r.x0, r.z0], b: [r.x1, r.z0], label: "top" },
      { axis: "z", c: r.z1, t: (r.x0 + r.x1) / 2, a: [r.x0, r.z1], b: [r.x1, r.z1], label: "bottom" },
    ];
    for (const e of edges) {
      const vis = s("line", { class: "handle-line", x1: e.a[0], y1: e.a[1], x2: e.b[0], y2: e.b[1] });
      const mid: [number, number] = [(e.a[0] + e.b[0]) / 2, (e.a[1] + e.b[1]) / 2];
      const knob = s("rect", {
        class: "handle-knob", x: mid[0] - (e.axis === "x" ? 0.09 : 0.32), y: mid[1] - (e.axis === "x" ? 0.32 : 0.09),
        width: e.axis === "x" ? 0.18 : 0.64, height: e.axis === "x" ? 0.64 : 0.18, rx: 0.06,
      });
      const hit = s("line", {
        class: `handle-hit ${e.axis === "x" ? "ew" : "ns"}`, x1: e.a[0], y1: e.a[1], x2: e.b[0], y2: e.b[1],
        "data-wall": e.label, tabindex: 0, role: "slider", "aria-label": `Move the ${e.label} wall of ${room.name}`,
      });
      hit.addEventListener("pointerdown", (ev) => this.startDrag(ev as PointerEvent, e.axis, e.c, e.t));
      hit.addEventListener("keydown", (ev) => {
        const k = (ev as KeyboardEvent).key;
        const step = (ev as KeyboardEvent).shiftKey ? 0.3048 : 0.0254 * 2;
        const dir = e.axis === "x" ? (k === "ArrowLeft" ? -1 : k === "ArrowRight" ? 1 : 0) : (k === "ArrowUp" ? -1 : k === "ArrowDown" ? 1 : 0);
        if (!dir) return;
        ev.preventDefault();
        const next = moveWall(this.layout, e.axis, e.c, e.t, e.c + dir * step);
        this.layout = next;
        this.opts.onChange?.(next, true);
        this.render();
        (this.svg.querySelector(`[data-wall="${e.label}"]`) as SVGElement | null)?.focus();
      });
      g.append(vis, knob, hit);
    }
    this.svg.append(g);
  }

  private toPlan(ev: PointerEvent): [number, number] {
    const m = this.svg.getScreenCTM();
    if (!m) return [0, 0];
    const p = new DOMPoint(ev.clientX, ev.clientY).matrixTransform(m.inverse());
    return [p.x, p.y];
  }

  private startDrag(ev: PointerEvent, axis: "x" | "z", c: number, t: number) {
    ev.preventDefault();
    ev.stopPropagation();
    (ev.target as Element).setPointerCapture(ev.pointerId);
    const p = this.toPlan(ev);
    this.drag = { axis, c, t, start: this.layout, pointer: axis === "x" ? p[0] : p[1] };
    const move = (e: PointerEvent) => {
      if (!this.drag) return;
      const q = this.toPlan(e);
      const delta = (axis === "x" ? q[0] : q[1]) - this.drag.pointer;
      // Snap to whole inches (or centimetres) so typed and dragged sizes agree.
      const unitStep = this.opts.unit === "m" ? 0.01 : 0.0254;
      const target = Math.round((c + delta) / unitStep) * unitStep;
      this.layout = moveWall(this.drag.start, axis, c, t, target);
      this.opts.onChange?.(this.layout, false);
      const d = this.drag;
      this.drag = null;
      this.render();
      this.drag = d;
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
      this.drag = null;
      this.opts.onChange?.(this.layout, true);
      this.render();
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
  }
}

export function planRect(layout: Layout): Rect {
  return bounds(layout);
}
