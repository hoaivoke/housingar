// The listing's rooms in 3D (three.js): walls with door and window gaps, floors, and the user's
// scanned furniture at real size. Two views: an overview you orbit around, and walking through at
// eye height (drag to look, keys or on-screen buttons to move, or move the phone to look around).
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import { Layout, Vec2, bounds, rectOf, roomAt, wallPieces, WallPiece } from "../layout";
import type { LibraryItem, Placement } from "../api";
import { blockingWalls, canStand, footprint, footprintsOverlap, hitsWalls } from "./collide";

export type ViewMode = "overview" | "walk";

export interface SceneEvents {
  onSelect: (p: Placement | null, overlapping: string | null) => void;
  /** Placements changed; `done` once a drag, rotation, add or removal is finished. */
  onChange: (placements: Placement[], done: boolean) => void;
  onMessage: (text: string) => void;
}

interface Placed {
  placement: Placement;
  object: THREE.Group;
  min: Vec2;
  max: Vec2;
  height: number;
  outline: THREE.LineSegments;
}

const EYE = 1.6;
const COLORS = {
  background: 0xf6f7f5,
  ground: 0xeceeea,
  wall: 0xf3f3ef,
  wallTop: 0xc9ced0,
  wood: 0xd8c6a6,
  tile: 0xe2e5e3,
  kitchen: 0xd3cfc5,
  selected: 0xf3d34a,
  overlap: 0xe8833a,
};

export class RoomScene {
  readonly canvas: HTMLCanvasElement;
  private renderer: THREE.WebGLRenderer;
  private scene = new THREE.Scene();
  private camera = new THREE.PerspectiveCamera(55, 1, 0.05, 200);
  private controls: OrbitControls;
  private roomGroup = new THREE.Group();
  private furnitureGroup = new THREE.Group();
  private exteriorWalls: Array<{ mesh: THREE.Mesh; mid: THREE.Vector3; outward: THREE.Vector3 }> = [];
  private ceiling: THREE.Group = new THREE.Group();
  private layout: Layout | null = null;
  private pieces: WallPiece[] = [];
  private placed: Placed[] = [];
  private items = new Map<string, LibraryItem>();
  private loader = new GLTFLoader();
  private models = new Map<string, Promise<THREE.Group>>();
  private selected: Placed | null = null;
  private mode: ViewMode = "overview";
  private yaw = 0;
  private pitch = 0;
  private keys = new Set<string>();
  private moveInput = { forward: 0, turn: 0 };
  private walkTarget: THREE.Vector3 | null = null;
  private gyro: { offset: number; q: THREE.Quaternion } | null = null;
  private gyroHandler: ((e: DeviceOrientationEvent) => void) | null = null;
  private resizeObserver: ResizeObserver;
  private clock = new THREE.Clock();
  private disposed = false;
  private raycaster = new THREE.Raycaster();
  private floorPlane = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);

  constructor(private container: HTMLElement, private events: SceneEvents) {
    this.renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: false });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.canvas = this.renderer.domElement;
    this.canvas.className = "room-canvas";
    this.canvas.tabIndex = 0;
    this.canvas.setAttribute("aria-label", "3D view of the place. Drag to look around; select furniture to move it.");
    container.append(this.canvas);

    this.scene.background = new THREE.Color(COLORS.background);
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0xd9d2c4, 2.1));
    const sun = new THREE.DirectionalLight(0xffffff, 1.25);
    sun.position.set(6, 12, 8);
    sun.castShadow = true;
    sun.shadow.mapSize.set(2048, 2048);
    sun.shadow.bias = -0.0005;
    this.scene.add(sun, sun.target);
    this.sun = sun;
    this.scene.add(this.roomGroup, this.furnitureGroup);

    this.controls = new OrbitControls(this.camera, this.canvas);
    this.controls.enableDamping = true;
    this.controls.maxPolarAngle = Math.PI * 0.47;
    this.controls.minDistance = 1.5;
    this.controls.maxDistance = 80;
    // Until the person moves the view, keep the whole place fitted as the stage resizes.
    this.controls.addEventListener("start", () => { this.viewTouched = true; });

    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(container);
    this.resize();
    this.bindPointer();
    this.bindKeys();
    this.renderer.setAnimationLoop(() => this.frame());
  }

  private viewTouched = false;
  private sun: THREE.DirectionalLight;

  // ------------------------------------------------------------------ room

  setLayout(layout: Layout) {
    const first = !this.layout;
    this.layout = layout;
    this.pieces = wallPieces(layout);
    this.roomGroup.clear();
    this.ceiling = new THREE.Group();
    this.exteriorWalls = [];
    const b = bounds(layout);
    const cx = (b.x0 + b.x1) / 2, cz = (b.z0 + b.z1) / 2;

    const ground = new THREE.Mesh(new THREE.PlaneGeometry(200, 200), new THREE.MeshStandardMaterial({ color: COLORS.ground, roughness: 1 }));
    ground.rotation.x = -Math.PI / 2;
    ground.position.y = -0.03;
    ground.receiveShadow = true;
    this.roomGroup.add(ground);

    for (const room of layout.rooms) {
      const r = rectOf(room);
      const color = room.kind === "bathroom" ? COLORS.tile : room.kind === "kitchen" ? COLORS.kitchen : COLORS.wood;
      const floor = new THREE.Mesh(new THREE.BoxGeometry(r.x1 - r.x0, 0.02, r.z1 - r.z0), new THREE.MeshStandardMaterial({ color, roughness: 0.85 }));
      floor.position.set((r.x0 + r.x1) / 2, -0.01, (r.z0 + r.z1) / 2);
      floor.receiveShadow = true;
      floor.userData.floor = true;
      this.roomGroup.add(floor);
      const ceil = new THREE.Mesh(new THREE.PlaneGeometry(r.x1 - r.x0, r.z1 - r.z0), new THREE.MeshBasicMaterial({ color: 0xebebe6, side: THREE.DoubleSide }));
      ceil.rotation.x = Math.PI / 2;
      ceil.position.set((r.x0 + r.x1) / 2, layout.ceiling_height_m, (r.z0 + r.z1) / 2);
      this.ceiling.add(ceil);
    }
    this.ceiling.visible = this.mode === "walk";
    this.roomGroup.add(this.ceiling);

    const top = new THREE.MeshStandardMaterial({ color: COLORS.wallTop, roughness: 1 });
    for (const p of this.pieces) {
      const len = Math.hypot(p.b[0] - p.a[0], p.b[1] - p.a[1]);
      const h = p.y1 - p.y0;
      const side = new THREE.MeshStandardMaterial({ color: COLORS.wall, roughness: 0.92, transparent: true, opacity: 1 });
      const geo = new THREE.BoxGeometry(len + 0.12, h, 0.12);
      // Darker top faces read as a section cut in the overview.
      const mesh = new THREE.Mesh(geo, [side, side, top, side, side, side]);
      mesh.position.set((p.a[0] + p.b[0]) / 2, (p.y0 + p.y1) / 2, (p.a[1] + p.b[1]) / 2);
      mesh.rotation.y = -Math.atan2(p.b[1] - p.a[1], p.b[0] - p.a[0]);
      mesh.castShadow = true;
      mesh.receiveShadow = true;
      this.roomGroup.add(mesh);
      if (p.exterior && p.outward) {
        this.exteriorWalls.push({ mesh, mid: mesh.position.clone(), outward: new THREE.Vector3(p.outward[0], 0, p.outward[1]) });
      }
    }

    const size = Math.max(b.x1 - b.x0, b.z1 - b.z0);
    const cam = this.sun.shadow.camera as THREE.OrthographicCamera;
    cam.left = -size; cam.right = size; cam.top = size; cam.bottom = -size; cam.near = 1; cam.far = 40;
    cam.updateProjectionMatrix();
    this.sun.position.set(cx + 6, 12, cz + 8);
    this.sun.target.position.set(cx, 0, cz);

    if (first) this.resetView();
    // Keep furniture valid in the edited plan.
    for (const p of this.placed) this.keepInside(p);
  }

  resetView() {
    if (!this.layout) return;
    const b = bounds(this.layout);
    const cx = (b.x0 + b.x1) / 2, cz = (b.z0 + b.z1) / 2;
    if (this.mode === "overview") {
      // Fit the whole place, walls included, seen from about 55° above the horizon:
      // the camera backs off until every corner of the box is inside the frame. On a tall
      // phone screen a long plan fits better seen from its end, so take the closer view.
      const hgt = this.layout.ceiling_height_m;
      const elev = (55 * Math.PI) / 180;
      const target = new THREE.Vector3(cx, hgt / 2, cz);
      const tanV = Math.tan((this.camera.fov * Math.PI) / 360) * 0.92;
      const tanH = tanV * this.camera.aspect;
      const fit = (azimDeg: number) => {
        const azim = (azimDeg * Math.PI) / 180;
        const back = new THREE.Vector3(Math.cos(elev) * Math.sin(azim), Math.sin(elev), Math.cos(elev) * Math.cos(azim));
        const right = new THREE.Vector3(0, 1, 0).cross(back).normalize();
        const up = back.clone().cross(right);
        let dist = 4;
        for (const x of [b.x0, b.x1]) for (const y of [0, hgt]) for (const z of [b.z0, b.z1]) {
          const v = new THREE.Vector3(x, y, z).sub(target);
          const along = v.dot(back);
          dist = Math.max(dist, along + Math.abs(v.dot(right)) / tanH, along + Math.abs(v.dot(up)) / tanV);
        }
        return { back, dist };
      };
      const front = fit(20), side = fit(110);
      const view = side.dist < front.dist * 0.85 ? side : front;
      this.controls.target.copy(target);
      this.camera.position.copy(target).addScaledVector(view.back, Math.min(this.controls.maxDistance, view.dist));
      this.controls.update();
      this.viewTouched = false;
    } else {
      const living = this.layout.rooms.find((r) => r.kind === "living") ?? this.layout.rooms[0];
      const r = rectOf(living);
      this.camera.position.set((r.x0 + r.x1) / 2, EYE, r.z1 - 0.6);
      this.yaw = 0;
      this.pitch = -0.08;
      this.applyLook();
    }
  }

  setMode(mode: ViewMode) {
    if (mode === this.mode) return;
    this.mode = mode;
    this.controls.enabled = mode === "overview";
    this.ceiling.visible = mode === "walk";
    this.walkTarget = null;
    if (mode === "overview") this.stopGyro();
    for (const w of this.exteriorWalls) this.setWallFade(w.mesh, false);
    this.resetView();
  }

  get viewMode() {
    return this.mode;
  }

  // ------------------------------------------------------------------ furniture

  setLibrary(items: LibraryItem[]) {
    this.items = new Map(items.map((i) => [i.item_id, i]));
  }

  private model(item: LibraryItem): Promise<THREE.Group> {
    if (!item.mesh_url) return Promise.reject(new Error(`${item.name} has no 3D model.`));
    if (!this.models.has(item.item_id)) {
      this.models.set(item.item_id, this.loader.loadAsync(item.mesh_url).then((g) => {
        g.scene.traverse((o) => {
          const m = o as THREE.Mesh;
          if (m.isMesh) { m.castShadow = true; m.receiveShadow = true; }
        });
        return g.scene;
      }));
    }
    return this.models.get(item.item_id)!;
  }

  async setPlacements(placements: Placement[]) {
    for (const p of this.placed) this.furnitureGroup.remove(p.object);
    this.placed = [];
    this.selected = null;
    for (const pl of placements) {
      const item = this.items.get(pl.item_id);
      if (!item) continue;
      try {
        await this.addPlaced(item, { ...pl });
      } catch (e) {
        this.events.onMessage(`Couldn't load ${pl.name}: ${(e as Error).message}`);
      }
    }
    this.events.onSelect(null, null);
  }

  private async addPlaced(item: LibraryItem, placement: Placement): Promise<Placed> {
    const src = await this.model(item);
    const object = new THREE.Group();
    const model = src.clone(true);
    object.add(model);
    const box = new THREE.Box3().setFromObject(model);
    const min: Vec2 = [box.min.x, box.min.z], max: Vec2 = [box.max.x, box.max.z];
    const height = box.max.y;
    const edges = new THREE.EdgesGeometry(new THREE.BoxGeometry(max[0] - min[0] + 0.03, height + 0.02, max[1] - min[1] + 0.03));
    const outline = new THREE.LineSegments(edges, new THREE.LineBasicMaterial({ color: COLORS.selected, depthTest: false, transparent: true }));
    outline.position.set((min[0] + max[0]) / 2, height / 2, (min[1] + max[1]) / 2);
    outline.renderOrder = 10;
    outline.visible = false;
    object.add(outline);
    object.userData.placementId = placement.placement_id;
    const placed: Placed = { placement, object, min, max, height, outline };
    this.apply(placed);
    this.furnitureGroup.add(object);
    this.placed.push(placed);
    return placed;
  }

  /** Adds a My stuff item in front of the camera, at the nearest free spot. */
  async addItem(item: LibraryItem): Promise<void> {
    if (!this.layout) return;
    const id = `pl_${Date.now().toString(36)}_${Math.floor(Math.random() * 1e4)}`;
    const placement: Placement = { placement_id: id, item_id: item.item_id, name: item.name, position: [0, 0, 0], yaw_deg: 0 };
    const placed = await this.addPlaced(item, placement);
    const start = this.lookPoint();
    const spot = this.freeSpot(placed, start);
    if (!spot) {
      this.furnitureGroup.remove(placed.object);
      this.placed = this.placed.filter((p) => p !== placed);
      this.events.onMessage(`There's no free spot big enough for ${item.name} here. Make a room bigger or move something.`);
      return;
    }
    placed.placement.position = [spot[0], 0, spot[1]];
    this.apply(placed);
    this.select(placed);
    this.emit(true);
  }

  /** Where the camera is looking on the floor (overview: orbit target; walk: 1.5 m ahead). */
  private lookPoint(): Vec2 {
    if (this.mode === "overview") return [this.controls.target.x, this.controls.target.z];
    const dir = new THREE.Vector3();
    this.camera.getWorldDirection(dir);
    return [this.camera.position.x + dir.x * 1.5, this.camera.position.z + dir.z * 1.5];
  }

  /** Nearest spot to `from` inside the place where the item fits without touching walls. */
  private freeSpot(p: Placed, from: Vec2): Vec2 | null {
    if (!this.layout) return null;
    const walls = blockingWalls(this.pieces, p.height);
    const others = this.placed.filter((o) => o !== p).map((o) => this.corners(o));
    const ok = (pos: Vec2, strict: boolean) => {
      if (!roomAt(this.layout!, pos[0], pos[1])) return false;
      const c = footprint(pos, p.placement.yaw_deg, p.min, p.max);
      if (hitsWalls(c, walls)) return false;
      return !strict || !others.some((o) => footprintsOverlap(c, o));
    };
    for (const strict of [true, false]) {
      for (let r = 0; r <= 8; r += 0.15) {
        const steps = r === 0 ? 1 : Math.ceil((2 * Math.PI * r) / 0.15);
        for (let i = 0; i < steps; i++) {
          const a = (i / steps) * Math.PI * 2;
          const pos: Vec2 = [from[0] + r * Math.cos(a), from[1] + r * Math.sin(a)];
          if (ok(pos, strict)) return pos;
        }
      }
    }
    return null;
  }

  private corners(p: Placed): Vec2[] {
    return footprint([p.placement.position[0], p.placement.position[2]], p.placement.yaw_deg, p.min, p.max);
  }

  /** Footprints for the plan drawing. */
  footprints(): Array<{ corners: Vec2[]; selected: boolean }> {
    return this.placed.map((p) => ({ corners: this.corners(p), selected: p === this.selected }));
  }

  private apply(p: Placed) {
    p.object.position.set(p.placement.position[0], 0, p.placement.position[2]);
    p.object.rotation.y = (p.placement.yaw_deg * Math.PI) / 180;
  }

  private overlapping(p: Placed): string | null {
    const c = this.corners(p);
    return this.placed.find((o) => o !== p && footprintsOverlap(c, this.corners(o)))?.placement.name ?? null;
  }

  private select(p: Placed | null) {
    if (this.selected) this.selected.outline.visible = false;
    this.selected = p;
    if (p) {
      p.outline.visible = true;
      const over = this.overlapping(p);
      (p.outline.material as THREE.LineBasicMaterial).color.set(over ? COLORS.overlap : COLORS.selected);
      this.events.onSelect(p.placement, over);
    } else {
      this.events.onSelect(null, null);
    }
  }

  rotateSelected(deg: number) {
    const p = this.selected;
    if (!p) return;
    const before = p.placement.yaw_deg;
    p.placement.yaw_deg = (((before + deg) % 360) + 360) % 360;
    if (hitsWalls(this.corners(p), blockingWalls(this.pieces, p.height))) {
      // Turning would push it into a wall: nudge it out, or refuse.
      const spot = this.freeSpot(p, [p.placement.position[0], p.placement.position[2]]);
      if (spot && Math.hypot(spot[0] - p.placement.position[0], spot[1] - p.placement.position[2]) < 0.6) {
        p.placement.position = [spot[0], 0, spot[1]];
      } else {
        p.placement.yaw_deg = before;
        this.events.onMessage("It doesn't fit turned that way here. Move it away from the wall first.");
        return;
      }
    }
    this.apply(p);
    this.select(p);
    this.emit(true);
  }

  removeSelected() {
    const p = this.selected;
    if (!p) return;
    this.furnitureGroup.remove(p.object);
    this.placed = this.placed.filter((o) => o !== p);
    this.select(null);
    this.emit(true);
  }

  deselect() {
    this.select(null);
  }

  /** After the plan changes, move anything now stuck in a wall to the nearest free spot. */
  private keepInside(p: Placed) {
    if (!this.layout) return;
    const pos: Vec2 = [p.placement.position[0], p.placement.position[2]];
    if (roomAt(this.layout, pos[0], pos[1]) && !hitsWalls(this.corners(p), blockingWalls(this.pieces, p.height))) return;
    const spot = this.freeSpot(p, pos);
    if (spot) {
      p.placement.position = [spot[0], 0, spot[1]];
      this.apply(p);
      this.emit(true);
    }
  }

  private emit(done: boolean) {
    this.events.onChange(this.placed.map((p) => ({ ...p.placement, position: [...p.placement.position] as [number, number, number] })), done);
  }

  // ------------------------------------------------------------------ input

  private ndc(e: PointerEvent): THREE.Vector2 {
    const r = this.canvas.getBoundingClientRect();
    return new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
  }

  private pickFurniture(e: PointerEvent): Placed | null {
    this.raycaster.setFromCamera(this.ndc(e), this.camera);
    const hits = this.raycaster.intersectObjects(this.placed.map((p) => p.object), true).filter((h) => (h.object as THREE.Mesh).isMesh);
    if (!hits.length) return null;
    let o: THREE.Object3D | null = hits[0].object;
    while (o && !o.userData.placementId) o = o.parent;
    return this.placed.find((p) => p.object === o) ?? null;
  }

  private floorPoint(e: PointerEvent): THREE.Vector3 | null {
    this.raycaster.setFromCamera(this.ndc(e), this.camera);
    const out = new THREE.Vector3();
    return this.raycaster.ray.intersectPlane(this.floorPlane, out);
  }

  private bindPointer() {
    let drag: { p: Placed; offset: Vec2; moved: boolean } | null = null;
    let look: { x: number; y: number; yaw: number; pitch: number; moved: boolean; id: number } | null = null;
    const pointers = new Set<number>();

    this.canvas.addEventListener("pointerdown", (e) => {
      pointers.add(e.pointerId);
      this.canvas.focus({ preventScroll: true });
      const hit = this.pickFurniture(e);
      if (hit) {
        this.select(hit);
        const fp = this.floorPoint(e);
        drag = { p: hit, offset: fp ? [hit.placement.position[0] - fp.x, hit.placement.position[2] - fp.z] : [0, 0], moved: false };
        this.controls.enabled = false;
        this.canvas.setPointerCapture(e.pointerId);
        return;
      }
      if (this.mode === "walk" && pointers.size === 1) {
        look = { x: e.clientX, y: e.clientY, yaw: this.yaw, pitch: this.pitch, moved: false, id: e.pointerId };
        this.canvas.setPointerCapture(e.pointerId);
      } else if (this.mode === "overview") {
        look = { x: e.clientX, y: e.clientY, yaw: 0, pitch: 0, moved: false, id: e.pointerId };
      }
    });

    this.canvas.addEventListener("pointermove", (e) => {
      if (drag) {
        const fp = this.floorPoint(e);
        if (!fp) return;
        const p = drag.p;
        const target: Vec2 = [fp.x + drag.offset[0], fp.z + drag.offset[1]];
        const walls = blockingWalls(this.pieces, p.height);
        const valid = (pos: Vec2) => !!roomAt(this.layout!, pos[0], pos[1]) && !hitsWalls(footprint(pos, p.placement.yaw_deg, p.min, p.max), walls);
        const cur: Vec2 = [p.placement.position[0], p.placement.position[2]];
        // Slide along walls: try the full move, then each axis.
        const next = [target, [target[0], cur[1]] as Vec2, [cur[0], target[1]] as Vec2].find(valid);
        if (next) {
          p.placement.position = [next[0], 0, next[1]];
          this.apply(p);
          drag.moved = true;
          const over = this.overlapping(p);
          (p.outline.material as THREE.LineBasicMaterial).color.set(over ? COLORS.overlap : COLORS.selected);
          this.emit(false);
        }
        return;
      }
      if (look && e.pointerId === look.id) {
        const dx = e.clientX - look.x, dy = e.clientY - look.y;
        if (Math.hypot(dx, dy) > 6) look.moved = true;
        if (this.mode === "walk" && !this.gyro) {
          this.yaw = look.yaw + dx * 0.005;
          this.pitch = Math.max(-1.2, Math.min(1.2, look.pitch + dy * 0.004));
          this.applyLook();
        }
      }
    });

    const end = (e: PointerEvent) => {
      pointers.delete(e.pointerId);
      if (drag) {
        const d = drag;
        drag = null;
        this.controls.enabled = this.mode === "overview";
        if (d.moved) {
          this.select(d.p);
          this.emit(true);
        }
        return;
      }
      if (look && e.pointerId === look.id) {
        const tapped = !look.moved;
        look = null;
        if (tapped) {
          if (this.mode === "walk") {
            // Tap the floor to walk there.
            const fp = this.floorPoint(e);
            if (fp && this.layout && roomAt(this.layout, fp.x, fp.z)) this.walkTarget = new THREE.Vector3(fp.x, EYE, fp.z);
          }
          this.select(null);
        }
      }
    };
    this.canvas.addEventListener("pointerup", end);
    this.canvas.addEventListener("pointercancel", end);
  }

  private bindKeys() {
    this.canvas.addEventListener("keydown", (e) => {
      const k = e.key.toLowerCase();
      if (this.selected && (k === "q" || k === "e")) { this.rotateSelected(k === "q" ? 15 : -15); e.preventDefault(); return; }
      if (this.selected && (k === "delete" || k === "backspace")) { this.removeSelected(); e.preventDefault(); return; }
      if (k === "escape") { this.select(null); return; }
      if (["w", "a", "s", "d", "arrowup", "arrowdown", "arrowleft", "arrowright"].includes(k)) {
        this.keys.add(k);
        if (this.mode === "walk") e.preventDefault();
      }
    });
    this.canvas.addEventListener("keyup", (e) => this.keys.delete(e.key.toLowerCase()));
    this.canvas.addEventListener("blur", () => this.keys.clear());
  }

  /** On-screen walk buttons: forward −1..1, turn −1..1 (held). */
  setMoveInput(forward: number, turn: number) {
    this.moveInput = { forward, turn };
  }

  // ------------------------------------------------------------------ look around with the phone

  get gyroAvailable(): boolean {
    return typeof window.DeviceOrientationEvent !== "undefined";
  }

  get gyroActive(): boolean {
    return !!this.gyro;
  }

  /** Start using the phone's orientation to look around (walk view). Call from a tap. */
  async startGyro(): Promise<string | null> {
    if (!this.gyroAvailable) return "This device can't report its orientation.";
    if (!window.isSecureContext) return "Looking around by moving the phone needs the site opened at http://127.0.0.1:8000 (USB) or over https.";
    // iPhones (and newer Chrome) ask first; some browsers say no here yet still send readings,
    // so what counts is whether readings arrive.
    const D = DeviceOrientationEvent as any;
    let permission = "granted";
    if (typeof D.requestPermission === "function") {
      try { permission = await D.requestPermission(); } catch { permission = "denied"; }
    }
    if (this.mode !== "walk") this.setMode("walk");
    let first = true;
    let received = false;
    this.gyro = { offset: 0, q: new THREE.Quaternion() };
    const zee = new THREE.Vector3(0, 0, 1);
    const q1 = new THREE.Quaternion(-Math.sqrt(0.5), 0, 0, Math.sqrt(0.5));
    const euler = new THREE.Euler();
    const q0 = new THREE.Quaternion();
    this.gyroHandler = (e: DeviceOrientationEvent) => {
      if (e.alpha == null || e.beta == null || e.gamma == null || !this.gyro) return;
      received = true;
      const orient = ((screen.orientation?.angle ?? (window as any).orientation ?? 0) * Math.PI) / 180;
      euler.set((e.beta * Math.PI) / 180, (e.alpha * Math.PI) / 180, (-e.gamma * Math.PI) / 180, "YXZ");
      const q = new THREE.Quaternion().setFromEuler(euler).multiply(q1).multiply(q0.setFromAxisAngle(zee, -orient));
      if (first) {
        // Keep facing the way the view faced when looking around started.
        const fwd = new THREE.Vector3(0, 0, -1).applyQuaternion(q);
        this.gyro.offset = this.yaw - Math.atan2(-fwd.x, -fwd.z);
        first = false;
      }
      this.gyro.q.setFromAxisAngle(new THREE.Vector3(0, 1, 0), this.gyro.offset).multiply(q);
    };
    window.addEventListener("deviceorientation", this.gyroHandler);
    await new Promise((r) => setTimeout(r, 900));
    if (!received) {
      this.stopGyro();
      return permission === "granted"
        ? "No motion readings arrived. On a laptop, drag to look around instead."
        : "Motion access was declined. Allow motion and orientation for this site in the browser's settings.";
    }
    return null;
  }

  stopGyro() {
    if (this.gyroHandler) window.removeEventListener("deviceorientation", this.gyroHandler);
    this.gyroHandler = null;
    if (this.gyro) {
      const fwd = new THREE.Vector3(0, 0, -1).applyQuaternion(this.camera.quaternion);
      this.yaw = Math.atan2(-fwd.x, -fwd.z);
      this.pitch = Math.asin(Math.max(-1, Math.min(1, fwd.y)));
    }
    this.gyro = null;
    this.applyLook();
  }

  // ------------------------------------------------------------------ frame loop

  private applyLook() {
    if (this.mode !== "walk") return;
    this.camera.quaternion.setFromEuler(new THREE.Euler(this.pitch, this.yaw, 0, "YXZ"));
  }

  private walkStep(dt: number) {
    const k = this.keys;
    let forward = this.moveInput.forward + (k.has("w") || k.has("arrowup") ? 1 : 0) - (k.has("s") || k.has("arrowdown") ? 1 : 0);
    let strafe = (k.has("d") ? 1 : 0) - (k.has("a") ? 1 : 0);
    const turn = this.moveInput.turn + (k.has("arrowright") ? 1 : 0) - (k.has("arrowleft") ? 1 : 0);
    if (turn && !this.gyro) { this.yaw -= turn * dt * 1.6; this.applyLook(); }
    if (this.gyro) this.camera.quaternion.copy(this.gyro.q);
    const dir = new THREE.Vector3();
    this.camera.getWorldDirection(dir);
    dir.y = 0;
    dir.normalize();
    const right = new THREE.Vector3(-dir.z, 0, dir.x);
    let move = new THREE.Vector3().addScaledVector(dir, forward).addScaledVector(right, strafe);
    if (this.walkTarget && !forward && !strafe) {
      const to = this.walkTarget.clone().sub(this.camera.position);
      to.y = 0;
      if (to.length() < 0.05) this.walkTarget = null;
      else move = to.normalize().multiplyScalar(Math.min(1, to.length() / 0.4));
    } else if (forward || strafe) {
      this.walkTarget = null;
    }
    if (move.lengthSq() === 0) return;
    const speed = 1.4;
    const step = move.multiplyScalar(speed * dt);
    const walls = blockingWalls(this.pieces, 1.7);
    const p = this.camera.position;
    const tryPos = (x: number, z: number) => canStand([x, z], walls) && (!this.layout || !!roomAt(this.layout, x, z));
    if (tryPos(p.x + step.x, p.z + step.z)) { p.x += step.x; p.z += step.z; } else if (tryPos(p.x + step.x, p.z)) p.x += step.x;
    else if (tryPos(p.x, p.z + step.z)) p.z += step.z;
    else this.walkTarget = null;
    forward = strafe = 0;
  }

  private setWallFade(mesh: THREE.Mesh, fade: boolean) {
    for (const m of mesh.material as THREE.MeshStandardMaterial[]) {
      m.opacity = fade ? 0.07 : 1;
      m.depthWrite = !fade;
    }
    mesh.castShadow = !fade;
  }

  private frame() {
    if (this.disposed) return;
    const dt = Math.min(0.05, this.clock.getDelta());
    if (this.mode === "overview") {
      this.controls.update();
      // Cut away the outer walls between the camera and the rooms.
      const cam = this.camera.position;
      for (const w of this.exteriorWalls) {
        const toCam = cam.clone().sub(w.mid);
        toCam.y = 0;
        this.setWallFade(w.mesh, toCam.dot(w.outward) > 0.3);
      }
    } else {
      this.walkStep(dt);
    }
    this.renderer.render(this.scene, this.camera);
  }

  private resize() {
    const w = this.container.clientWidth || 1, h = this.container.clientHeight || 1;
    this.renderer.setSize(w, h, false);
    const before = this.camera.aspect;
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    if (this.mode === "overview" && !this.viewTouched && w > 1 && h > 1 && Math.abs(before - this.camera.aspect) > 0.01) this.resetView();
  }

  /** For tests: the current view as a data URL. */
  snapshot(): string {
    this.renderer.render(this.scene, this.camera);
    return this.canvas.toDataURL("image/png");
  }

  dispose() {
    this.disposed = true;
    this.stopGyro();
    this.renderer.setAnimationLoop(null);
    this.resizeObserver.disconnect();
    this.controls.dispose();
    this.renderer.dispose();
    this.canvas.remove();
  }
}
